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
    # sqline 2.5 vs pinnline 1.649 is -4 in the feed; OZ now offers 2.7 (+170) vs -154 -> +16
    adapter.decimal = 2.7
    await feed(eng, poller, arbsize=2)
    assert len(adapter.placed) == 1
    sel, stake, dry = adapter.placed[0]
    assert dry is True and stake == 47  # your stake as set: rounding is only for limits that cut it
    v = next(iter(eng.views.values()))
    assert v.state == "placed" and v.live_arbsize == pytest.approx(16, abs=0.1)
    # the same row changing again must not bet the same selection twice
    await feed(eng, poller, arbsize=3, sqline=2.55)
    assert len(adapter.placed) == 1


async def test_value_gone_is_not_bet(setup):
    cfg, ledger, poller, adapter, eng = setup
    adapter.decimal = 2.3  # +130 vs -154 -> -24
    await feed(eng, poller, arbsize=2)
    assert adapter.placed == []
    v = next(iter(eng.views.values()))
    assert v.state == "skipped" and "value gone" in v.reason


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


async def test_live_row_too_old_is_skipped(setup):
    cfg, ledger, poller, adapter, eng = setup
    adapter.decimal = 2.7
    await feed(eng, poller, arbsize=2, livetime="Q2 10:00", date="2020-01-01 00:00:00")
    assert adapter.placed == []
    assert "old" in next(iter(eng.views.values())).reason


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
    res = await eng.one_click(v.row.id)  # the price now (2.5 vs 1.649 -> -4) is still >= -6 - tolerance
    assert res.status.value == "dry_run" and len(adapter.placed) == 1
    adapter.decimal = 2.2  # +120 vs -154 -> -34: worse than what was shown, so not placed
    await feed(eng, poller, arbsize=-7, sqline=2.4, id="221026-1715_OZ_NFL_NWE_CHI_ATS_-6.5", spread=-6.5)
    res = await eng.one_click("221026-1715_OZ_NFL_NWE_CHI_ATS_-6.5")
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
    cfg.stake.default, adapter.decimal = 2, 2.7  # below round_to and min_stake: still placed as set
    await feed(eng, poller, arbsize=2)
    assert [stake for _, stake, _ in adapter.placed] == [2]
    cfg.stake.default = 47
    eng.views.clear()
    await feed(eng, poller, arbsize=2, id="221026-1715_OZ_NFL_NWE_CHI_ATS_-4", spread=-4)
    assert [stake for _, stake, _ in adapter.placed] == [2, 47]


async def test_a_limit_that_cuts_the_stake_is_rounded_but_not_to_nothing(setup):
    cfg, ledger, poller, adapter, eng = setup
    adapter.decimal = 2.7
    await feed(eng, poller, arbsize=2, book_limit=38)  # the feed's book limit: 38 -> 35
    await feed(eng, poller, arbsize=2, book_limit=7, id="221026-1715_OZ_NFL_NWE_CHI_ATS_-4", spread=-4)  # 7 -> 5
    await feed(eng, poller, arbsize=2, book_limit=4.5, id="221026-1715_OZ_NFL_NWE_CHI_ATS_-6", spread=-6)
    assert [stake for _, stake, _ in adapter.placed] == [35, 5]
    v = eng.views["221026-1715_OZ_NFL_NWE_CHI_ATS_-6"]  # 4.5 stays 4.5, which is under min_stake 5
    assert v.reason == "book limit leaves only 4.5, below min_stake 5"
