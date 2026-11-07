import asyncio

import pytest

from betbot.config import BookConfig, Config
from betbot.engine import AUTO, BORDERLINE, IGNORED, Engine
from betbot.feed import FeedPoller, parse_row
from betbot.ledger import Ledger
from betbot.models import BetResult, BetStatus, BookEvent, Competitor, Selection
from tests.conftest import feed_row

CHI = Competitor("c1", "Chicago Bears", True)
NE = Competitor("c2", "New England Patriots", False)
EVENT = BookEvent("OZ", "e1", "New England Patriots @ Chicago Bears", 1792714500000, [CHI, NE], False, "/x", "/nfl")


class FakeAdapter:
    def __init__(self, decimal=2.5, is_open=True):
        self.decimal, self.is_open = decimal, is_open
        self.placed = []
        self.browser = None
        self.reject_first = None

    async def events(self, league, live_hint=False):
        return [EVENT]

    async def quote(self, event, row, team, max_age_s=0.0):
        return Selection("OZ", event, "m1", "Alt / Spread", "G", "o1", team.name, "p1", self.decimal,
                         "+150", row.spread, self.is_open), "ok"

    async def place_api(self, sel, stake, dry_run):
        self.placed.append((sel, stake, dry_run))
        if self.reject_first:
            r, self.reject_first = self.reject_first, None
            return r
        return BetResult(BetStatus.DRY_RUN if dry_run else BetStatus.ACCEPTED, stake, sel.decimal, reference="R")

    place_ui = place_api


@pytest.fixture
def setup(tmp_path):
    cfg = Config(books={"OZ": BookConfig(base_url="https://x", account="me")})
    cfg.pacing.prematch_delay_s = (0, 0)
    cfg.pacing.min_gap_s = 0
    cfg.stake.default = 47
    ledger = Ledger(tmp_path / "t.sqlite3")
    poller = FeedPoller([])
    adapter = FakeAdapter()
    eng = Engine(cfg, ledger, poller, {"OZ": adapter})
    return cfg, ledger, poller, adapter, eng


async def feed(eng, poller, **row):
    upd = poller.diff("u", [parse_row(feed_row(**row))], None)
    await eng.on_feed(upd)
    await asyncio.gather(*list(eng._tasks))


def test_classify(setup):
    eng = setup[4]
    assert eng.classify(3) == AUTO and eng.classify(1) == AUTO
    assert eng.classify(0) == BORDERLINE and eng.classify(-3) == BORDERLINE
    assert eng.classify(-4) == IGNORED


async def test_auto_bet_dry_run_and_dedupes(setup):
    cfg, ledger, poller, adapter, eng = setup
    adapter.decimal = 2.7
    await feed(eng, poller, arbsize=2)
    assert len(adapter.placed) == 1
    sel, stake, dry = adapter.placed[0]
    assert dry is True and stake == 47  # your stake as set: rounding is only for limits that cut it
    v = next(iter(eng.views.values()))
    assert v.state == "placed" and v.live_arbsize == 2  # the feed's arbsize, trusted as-is
    # the same row changing again must not bet the same selection twice
    await feed(eng, poller, arbsize=3, sqline=2.55)
    assert len(adapter.placed) == 1


async def test_value_gone_is_not_bet(setup):
    cfg, ledger, poller, adapter, eng = setup
    await feed(eng, poller, arbsize=0)  # borderline, queued, shown at arbsize 0
    v = next(iter(eng.views.values()))
    poller.diff("u", [parse_row(feed_row(arbsize=-10), "u")], None)  # the feed's arb collapses
    res = await eng.one_click(v.row.id)
    assert res.status.value == "rejected" and "value gone" in res.reason and adapter.placed == []


async def test_closed_market_is_not_bet(setup):
    cfg, ledger, poller, adapter, eng = setup
    adapter.decimal, adapter.is_open = 2.7, False
    await feed(eng, poller, arbsize=2)
    assert adapter.placed == []


async def test_borderline_goes_to_queue_then_one_click(setup):
    cfg, ledger, poller, adapter, eng = setup
    adapter.decimal = 2.5
    await feed(eng, poller, arbsize=-2)
    v = next(iter(eng.views.values()))
    assert v.state == "queued" and adapter.placed == []
    res = await eng.one_click(v.row.id)
    assert res.status == BetStatus.DRY_RUN and len(adapter.placed) == 1


async def test_auto_off_and_caps(setup):
    cfg, ledger, poller, adapter, eng = setup
    adapter.decimal = 2.7
    cfg.auto_enabled = False
    await feed(eng, poller, arbsize=2)
    assert adapter.placed == []
    cfg.auto_enabled = True
    cfg.stake.daily_cap = 3  # below min stake
    cfg.dry_run = False
    await feed(eng, poller, arbsize=3, sqline=2.6)
    assert adapter.placed == []
    assert next(iter(eng.views.values())).reason == "daily cap leaves only 3, below min_stake 5"


async def feed_from(eng, poller, runtime, **row):
    """Like feed(), from feed URL "u" with the feed's runtime stamp."""
    await eng.on_feed(poller.diff("u", [parse_row(feed_row(**row), "u")], runtime))
    await asyncio.gather(*list(eng._tasks))


async def test_steady_live_line_is_bet_but_not_while_the_feed_is_stuck(setup):
    cfg, ledger, poller, adapter, eng = setup
    adapter.decimal = 2.7
    # the row's date is when its prices last changed: a line steady for minutes is still current
    await feed_from(eng, poller, 1000.0, arbsize=2, livetime="Q2 10:00", date="2020-01-01 00:00:00")
    assert len(adapter.placed) == 1
    poller.runtime_moved["u"] -= 60  # the feed's runtime hasn't moved for a minute
    await feed_from(eng, poller, 1000.0, arbsize=2, livetime="Q2 09:00", id="221026-1715_OZ_NFL_NWE_CHI_ATS_-4",
                    spread=-4)
    assert len(adapter.placed) == 1
    assert eng.views["221026-1715_OZ_NFL_NWE_CHI_ATS_-4"].reason == "the feed hasn't updated for 60s"


async def test_price_change_rejection_requotes_once(setup):
    cfg, ledger, poller, adapter, eng = setup
    cfg.dry_run = False
    adapter.decimal = 2.7
    adapter.reject_first = BetResult(BetStatus.REJECTED, 45, reason="PriceChange", retryable=True)
    await feed(eng, poller, arbsize=2)
    assert len(adapter.placed) == 2
    assert next(iter(eng.views.values())).state == "placed"


async def test_rejections_pause_auto(setup):
    cfg, ledger, poller, adapter, eng = setup
    cfg.dry_run = False
    cfg.pacing.pause_after_rejections = 1
    adapter.decimal = 2.7
    adapter.reject_first = BetResult(BetStatus.REJECTED, 45, reason="MaxRisk")
    await feed(eng, poller, arbsize=2)
    assert eng.books["OZ"].paused


async def test_mouse_betting_is_the_default_and_never_falls_back_to_the_api(setup):
    from betbot.books.base import UiNotReady
    cfg, ledger, poller, adapter, eng = setup
    assert cfg.books["OZ"].auto_mode == "ui"
    calls = []

    async def ui_fails(sel, stake, dry_run):
        calls.append("ui")
        raise UiNotReady("odds button not found")

    async def api(sel, stake, dry_run):
        calls.append("api")

    adapter.place_ui, adapter.place_api = ui_fails, api
    adapter.decimal = 2.7
    await feed(eng, poller, arbsize=2)
    v = next(iter(eng.views.values()))
    assert calls == ["ui"] and v.state == "skipped" and v.reason == "mouse betting: odds button not found"


async def test_ignored_row_can_be_placed_by_hand(setup):
    cfg, ledger, poller, adapter, eng = setup
    await feed(eng, poller, arbsize=-6)  # below borderline_min: matched, but neither bet nor queued
    v = next(iter(eng.views.values()))
    assert v.category == "ignored" and v.state == "matched" and adapter.placed == []
    res = await eng.one_click(v.row.id)  # feed arb -6 is still >= shown -6, minus tolerance
    assert res.status.value == "dry_run" and len(adapter.placed) == 1
    other = "221026-1715_OZ_NFL_NWE_CHI_ATS_-6.5"
    await feed(eng, poller, arbsize=-6, id=other, spread=-6.5)
    poller.diff("u", [parse_row(feed_row(arbsize=-20, id=other, spread=-6.5), "u")], None)  # arb collapses
    res = await eng.one_click(other)
    assert res.status.value == "rejected" and "value gone" in res.reason and len(adapter.placed) == 1


async def test_row_that_leaves_the_feed_mid_processing_stays_gone(setup):
    # Quotes wait behind the book's request spacing; a row removed meanwhile must not come back as
    # "queued" (or "matched"), or clicking it only says "row no longer in the feed".
    cfg, ledger, poller, adapter, eng = setup
    gate = asyncio.Event()
    real_quote, real_events = adapter.quote, adapter.events

    async def slow_quote(*a, **kw):
        await gate.wait()
        return await real_quote(*a, **kw)

    async def slow_events(*a, **kw):
        await gate.wait()
        return await real_events(*a, **kw)

    for slow in ("quote", "events"):
        gate.clear()
        setattr(adapter, slow, slow_quote if slow == "quote" else slow_events)
        eng.views.clear()
        arb = -1 if slow == "quote" else -6  # borderline is quoted; ignored is only matched
        await eng.on_feed(poller.diff("u", [parse_row(feed_row(arbsize=arb))], None))
        await asyncio.sleep(0)
        await eng.on_feed(poller.diff("u", [], None))  # the row leaves the feed mid-processing
        gate.set()
        await asyncio.gather(*list(eng._tasks))
        v = next(iter(eng.views.values()))
        assert v.state == "gone", (slow, v.state)
        setattr(adapter, slow, real_quote if slow == "quote" else real_events)


async def test_your_stake_is_placed_exactly_as_set(setup):
    cfg, ledger, poller, adapter, eng = setup
    cfg.one_bet_per_market = False  # two lines of the same spread here
    cfg.stake.default, adapter.decimal = 2, 2.7  # below round_to and min_stake: still placed as set
    await feed(eng, poller, arbsize=2)
    assert [stake for _, stake, _ in adapter.placed] == [2]
    cfg.stake.default = 47
    eng.views.clear()
    await feed(eng, poller, arbsize=2, id="221026-1715_OZ_NFL_NWE_CHI_ATS_-4", spread=-4)
    assert [stake for _, stake, _ in adapter.placed] == [2, 47]


async def test_a_limit_that_cuts_the_stake_is_rounded_but_not_to_nothing(setup):
    cfg, ledger, poller, adapter, eng = setup
    cfg.one_bet_per_market = False  # three lines of the same spread here
    adapter.decimal = 2.7
    await feed(eng, poller, arbsize=2, book_limit=38)  # the feed's book limit: 38 -> 35
    await feed(eng, poller, arbsize=2, book_limit=7, id="221026-1715_OZ_NFL_NWE_CHI_ATS_-4", spread=-4)  # 7 -> 5
    await feed(eng, poller, arbsize=2, book_limit=4.5, id="221026-1715_OZ_NFL_NWE_CHI_ATS_-6", spread=-6)
    assert [stake for _, stake, _ in adapter.placed] == [35, 5]
    v = eng.views["221026-1715_OZ_NFL_NWE_CHI_ATS_-6"]  # 4.5 stays 4.5, which is under min_stake 5
    assert v.reason == "book limit leaves only 4.5, below min_stake 5"


async def test_feed_age_only_holds_back_automatic_bets(setup):
    cfg, ledger, poller, adapter, eng = setup
    adapter.decimal = 2.7
    poller.diff("u", [], 999.0)
    poller.runtime_moved["u"] -= 60  # stuck feed
    await feed_from(eng, poller, 999.0, arbsize=2, livetime="Q2 05:00")
    v = next(iter(eng.views.values()))
    assert adapter.placed == [] and "hasn't updated" in v.reason
    res = await eng.one_click(v.row.id)
    assert res.status.value == "dry_run" and len(adapter.placed) == 1


async def test_skipped_automatic_row_can_be_placed_by_hand(setup):
    cfg, ledger, poller, adapter, eng = setup
    cfg.auto_enabled = False  # an auto row the bot won't place by itself
    await feed(eng, poller, arbsize=2)
    v = next(iter(eng.views.values()))
    assert v.category == "auto" and v.state == "skipped" and adapter.placed == []
    cfg.auto_enabled = True
    res = await eng.one_click(v.row.id)  # but can still be placed by hand
    assert res.status.value == "dry_run" and len(adapter.placed) == 1


# ----- narrowing: leagues, live only, one bet per market, max stake ---------------------------------

LIVE_ATP = dict(id="021126-0740_OZ_ATP LIVE_machac t_jacquet k_ATS_1.5", league="ATP LIVE", livetime="LIVE",
                sq_team="Chicago Bears", spread=1.5)


async def test_only_live_tennis(setup):
    cfg, ledger, poller, adapter, eng = setup
    cfg.only.leagues, cfg.only.live = ["ATP", "WTA"], True
    await feed(eng, poller, arbsize=2)  # pre-game NFL: left out
    assert eng.views == {} and adapter.placed == []
    await feed(eng, poller, arbsize=2, **{**LIVE_ATP, "league": "NFL LIVE",
                                          "id": "191026-2015_OZ_NFL LIVE_NWE_CHI_ATS_1.5"})  # live, not tennis
    assert eng.views == {} and adapter.placed == []
    await feed(eng, poller, arbsize=2, **LIVE_ATP)
    assert len(adapter.placed) == 1


async def test_changing_the_filter_refreshes_what_is_shown(setup):
    cfg, ledger, poller, adapter, eng = setup
    cfg.auto_enabled = False
    await feed(eng, poller, arbsize=2)
    assert len(eng.views) == 1
    cfg.only.live = True
    await eng.refilter()
    assert eng.views == {}
    cfg.only.live = False
    await eng.refilter()
    await asyncio.gather(*list(eng._tasks))
    assert len(eng.views) == 1


async def test_one_bet_per_market(setup):
    cfg, ledger, poller, adapter, eng = setup
    await feed(eng, poller, arbsize=3)  # Bears -5
    await feed(eng, poller, arbsize=2, id="221026-1715_OZ_NFL_NWE_CHI_ATS_-4", spread=-4)  # same game's spread
    assert len(adapter.placed) == 1
    v = eng.views["221026-1715_OZ_NFL_NWE_CHI_ATS_-4"]
    assert v.state == "skipped" and v.reason == "already bet this game's market"
    # a total on the same game is a different market
    real_quote = adapter.quote

    async def quote_total(event, row, team, max_age_s=0.0):
        sel, why = await real_quote(event, row, team, max_age_s)
        sel.market_type, sel.market_description, sel.outcome_description = "total", "Total", "Over"
        return sel, why

    adapter.quote = quote_total
    await feed(eng, poller, arbsize=2, id="221026-1715_OZ_NFL_NWE_CHI_Over_44.5", bettype="Over", spread="None",
               total=44.5)
    assert len(adapter.placed) == 2


async def test_best_line_of_a_game_goes_first(setup):
    cfg, ledger, poller, adapter, eng = setup
    rows = [parse_row(feed_row(arbsize=a, id=f"221026-1715_OZ_NFL_NWE_CHI_ATS_{s}", spread=s))
            for a, s in ((2, -4), (5, -5))]
    await eng.on_feed(poller.diff("u", rows, None))
    await asyncio.gather(*list(eng._tasks))
    assert [sel.handicap for sel, _, _ in adapter.placed] == [-5]


async def test_max_stake_takes_what_the_book_allows(setup):
    cfg, ledger, poller, adapter, eng = setup
    cfg.stake.mode, cfg.stake.max_per_event, cfg.stake.daily_cap = "max", 100000, 100000

    async def book_max(sel):
        return 1056.0

    adapter.book_max_stake = book_max
    await feed(eng, poller, arbsize=2, book_limit=5000)
    assert [stake for _, stake, _ in adapter.placed] == [1055]  # rounded down to round_to


async def test_max_stake_within_caps_and_balance(setup):
    cfg, ledger, poller, adapter, eng = setup
    cfg.stake.mode = "max"

    async def book_max(sel):
        return 1056.0

    adapter.book_max_stake = book_max
    eng.books["OZ"].balance = 312.4
    await feed(eng, poller, arbsize=2, book_limit=5000)
    assert [stake for _, stake, _ in adapter.placed] == [310]  # balance below the 500 event cap


async def test_max_stake_falls_back_to_the_feed_limit(setup):
    cfg, ledger, poller, adapter, eng = setup
    cfg.stake.mode = "max"

    async def book_max(sel):
        return None

    adapter.book_max_stake = book_max
    await feed(eng, poller, arbsize=2, book_limit="None")  # nothing to go on: not bet
    assert adapter.placed == [] and next(iter(eng.views.values())).reason.startswith("book max unknown")
    await feed(eng, poller, arbsize=2, book_limit=250, id="221026-1715_OZ_NFL_NWE_CHI_ATS_-4", spread=-4)
    assert [stake for _, stake, _ in adapter.placed] == [250]


def test_old_ledger_gets_the_market_column(tmp_path):
    import sqlite3
    p = tmp_path / "old.sqlite3"
    db = sqlite3.connect(p)
    db.execute("CREATE TABLE bets (ts REAL, account TEXT, book TEXT, row_id TEXT, selection_key TEXT, event_id TEXT, "
               "description TEXT, stake REAL, decimal REAL, arbsize REAL, mode TEXT, trigger TEXT, status TEXT, "
               "reference TEXT, reason TEXT, timings TEXT, dry_run INTEGER)")
    db.commit()
    db.close()
    ledger = Ledger(p)
    ledger.record_bet(account="me", book="OZ", row_id="r", selection_key="k", event_id="e", description="d",
                      stake=5, decimal=2.0, arbsize=1, mode="ui", trigger="auto", status="accepted", reference="R",
                      reason=None, timings={}, dry_run=False, market_key="OZ:e:spread:G")
    assert ledger.already_bet_market("me", "OZ:e:spread:G")
    assert not ledger.already_bet_market("me", "OZ:e:total:G")


def test_dashboard_settings(setup):
    from fastapi.testclient import TestClient

    from betbot.dashboard.app import create_app
    cfg, ledger, poller, adapter, eng = setup
    client = TestClient(create_app(eng))
    s = client.post("/api/settings", json={"stake_mode": "max", "only_live": True, "only_leagues": "atp, wta",
                                           "one_bet_per_market": False}).json()
    assert (s["stake_mode"], s["only_live"], s["only_leagues"], s["one_bet_per_market"]) == \
        ("max", True, ["ATP", "WTA"], False)
    assert cfg.stake.mode == "max" and cfg.only.leagues == ["ATP", "WTA"]


async def test_max_mode_presses_the_slips_max_button(setup):
    cfg, ledger, poller, adapter, eng = setup
    cfg.stake.mode = "max"
    adapter.has_max_button = True
    calls = []

    async def book_max(sel):
        raise AssertionError("the API max isn't used when the slip has a Max button")

    async def place_ui(sel, stake, dry_run, use_max_button=False, min_stake=0.0):
        calls.append((stake, use_max_button, min_stake))
        return BetResult(BetStatus.DRY_RUN, 312.0, sel.decimal)

    adapter.book_max_stake, adapter.place_ui = book_max, place_ui
    await feed(eng, poller, arbsize=2, book_limit="None")
    assert calls == [(500, True, 5)]  # the most your caps allow: the 500 event cap
    assert ledger.recent_bets(1)[0]["stake"] == 312.0  # what the slip actually took


async def test_a_book_that_doesnt_answer_shows_why(setup):
    cfg, ledger, poller, adapter, eng = setup

    async def events(league, live_hint=False):
        raise TimeoutError("the https://x tab didn't answer within 20s")

    adapter.events = events
    await feed(eng, poller, arbsize=2)
    v = next(iter(eng.views.values()))
    assert v.state == "unmatched" and "couldn't get NFL games from OZ" in v.reason and "didn't answer" in v.reason
    assert adapter.placed == []
