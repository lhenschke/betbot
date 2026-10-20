import json

import pytest

from betbot.books.base import LeagueNotMapped
from betbot.books.mybookie import (MyBookieAdapter, build_options_payload, build_place_payload, find_selection,
                                   interpret_place, max_risk, parse_events, parse_money, to_decimal)
from betbot.config import BookConfig
from betbot.feed import parse_row
from betbot.matching.matcher import match_row
from tests.conftest import feed_row, load


def nfl_events():
    return parse_events(load("myb_lines_nfl.json")["response"], "MyB", "79")


def game(events, event_id):
    return next(e for e in events if e.id == event_id).raw


def test_events_and_start_times():
    evs = nfl_events()
    was_sf = next(e for e in evs if e.id == "29912810")
    assert was_sf.description == "Washington Commanders @ San Francisco 49ers"
    assert [(c.name, c.home) for c in was_sf.competitors] == [("Washington Commanders", False),
                                                              ("San Francisco 49ers", True)]
    # 20:15 in the player's time zone (GMT-4) is 00:15 UTC the next day
    assert was_sf.start_ms == 1792455300000 and not was_sf.live
    live = parse_events(load("myb_lines_nhl_live.json")["response"], "MyB", "4345")
    assert len(live) == 3 and all(e.live for e in live)


def myb_row(**over):
    # a real MyB row from feed_nfl-pro.json
    base = dict(book="MyB", id="251026-1000_MyB_NFL_HOU_NYG_ATS_10", sq_team="NY Giants", pinn_team="HOU",
                spread=10, sqline=1.90909090909, start_date=1792947600000, url="https://engine.mybookie.ag/sports/home")
    return parse_row(feed_row(**{**base, **over}))


def test_feed_row_matches_and_quotes():
    evs, row = nfl_events(), myb_row()
    m = match_row(row, evs)
    assert m.ok, m.reason
    assert m.event.description == "New York Giants @ Houston Texans" and m.team.name == "New York Giants"
    sel, why = find_selection(m.event, m.event.raw, row, m.team)
    assert sel, why
    assert sel.label == "New York Giants +10" and sel.decimal == pytest.approx(1.909091) and sel.is_open
    assert sel.american == "-110" and sel.outcome_id == "528698938"


def test_shaded_price_comes_from_odd_not_odd_american():
    evs = nfl_events()
    ev = next(e for e in evs if e.id == "29912810")
    row = myb_row(sq_team="Washington Commanders", pinn_team="SFO", spread=7.5, start_date=ev.start_ms)
    sel, _ = find_selection(ev, ev.raw, row, ev.competitors[0])
    assert sel.american == "-120" and sel.decimal == pytest.approx(1.833333)  # oddAmerican says -110
    home, _ = find_selection(ev, ev.raw, myb_row(spread=-7.5), ev.competitors[1])
    assert home.american == "+100" and home.decimal == 2.0


def test_moneyline_total_and_missing_line():
    evs = nfl_events()
    ev = next(e for e in evs if e.id == "29912810")
    ml, _ = find_selection(ev, ev.raw, myb_row(bettype="ML", spread="None"), ev.competitors[0])
    assert ml.label == "Washington Commanders" and ml.american == "+330" and ml.handicap is None
    under, _ = find_selection(ev, ev.raw, myb_row(bettype="Under", spread="None", total=51.5), None)
    assert under.label == "Under 51.5" and under.decimal == pytest.approx(1.909091)
    sel, why = find_selection(ev, ev.raw, myb_row(spread=6.5), ev.competitors[0])
    assert sel is None and why == "line 6.5 not offered (book has 7.5)"
    sel, why = find_selection(ev, ev.raw, myb_row(bettype="1H ATS"), ev.competitors[0])
    assert sel is None and "unsupported" in why


def test_unpriced_market_is_not_quoted():
    ev = next(e for e in nfl_events() if e.id == "29929521")
    sel, why = find_selection(ev, ev.raw, myb_row(spread=1), ev.competitors[0])
    assert sel is None and why == "no price posted yet"


def captured_selection():
    ev = next(e for e in nfl_events() if e.id == "29912810")
    row = myb_row(sq_team="Washington Commanders", pinn_team="SFO", spread=7.5, start_date=ev.start_ms)
    sel, _ = find_selection(ev, ev.raw, row, ev.competitors[0])
    return sel


def test_payloads_match_a_real_captured_bet():
    cap, sel = load("myb_capture_bet.json"), captured_selection()
    assert build_options_payload(sel) == cap["bet_options_request"]
    assert build_place_payload(cap["bet_options_response"], sel, 2.0) == cap["place_request"]


def test_limits_and_replies():
    cap = load("myb_capture_bet.json")
    s = cap["bet_options_response"]["content"][0]["selections"][0]
    assert max_risk(s) == pytest.approx(120.0)  # the site shows "Max Bet: $120.00" at -120
    assert max_risk({**s, "odd": 150}) == pytest.approx(100.0)
    assert interpret_place(cap["place_response"]) == ("SUCCESS", "302000001", None, False)
    bad = {"content": [{"isSuccess": False, "errorMessage": "The odds have changed"}]}
    assert interpret_place(bad) == ("FAIL", None, "The odds have changed", True)
    assert parse_money("$1,018.50") == 1018.5 and to_decimal(None) is None


class FakeBrowser:
    logged_in_check = None

    async def evaluate(self, js, arg=None):
        return {"csrf": "C", "xsrf": "X"}


class CaptureTransport:
    """Replays the captured bet: records every call the adapter makes."""

    def __init__(self, cap, statuses=None):
        self.cap, self.calls, self.statuses = cap, [], list(statuses or [])

    async def request(self, method, path, *, body=None, headers=None):
        self.calls.append((method, path, body, headers))
        status = self.statuses.pop(0) if self.statuses else 200
        if path.endswith("/bet-options"):
            return status, self.cap["bet_options_response"]
        if path.endswith("/place-bet"):
            return status, self.cap["place_response"]
        return status, self.cap["balance_response"]


def adapter(t, browser=None):
    cfg = BookConfig(base_url="https://engine.mybookie.ag", adapter="mybookie", start_path="/sports/home")
    return MyBookieAdapter("MyB", cfg, t, None, browser or FakeBrowser())


async def test_place_api_replays_the_sites_call_sequence():
    cap = load("myb_capture_bet.json")
    t = CaptureTransport(cap)
    res = await adapter(t).place_api(captured_selection(), 2.0, dry_run=False)
    assert res.status.value == "accepted" and res.reference == "302000001" and res.stake == 2.0
    assert [p for _, p, _, _ in t.calls] == ["/sports_api/bet-options", "/sports_api/place-bet"]
    assert t.calls[0][2] == cap["bet_options_request"] and t.calls[1][2] == cap["place_request"]
    for _, _, _, h in t.calls:
        assert h["X-CSRF-TOKEN"] == "C" and h["X-XSRF-TOKEN"] == "X"


async def test_dry_run_caps_stake_and_stops_before_placing():
    t = CaptureTransport(load("myb_capture_bet.json"))
    res = await adapter(t).place_api(captured_selection(), 500.0, dry_run=True)
    assert res.status.value == "dry_run" and res.stake == 120.0 and len(t.calls) == 1


async def test_lower_bet_slip_price_is_not_placed():
    cap = load("myb_capture_bet.json")
    opts = json.loads(json.dumps(cap["bet_options_response"]))
    opts["content"][0]["selections"][0]["odd"] = -130
    t = CaptureTransport({**cap, "bet_options_response": opts})
    res = await adapter(t).place_api(captured_selection(), 2.0, dry_run=False)
    assert res.status.value == "rejected" and res.retryable and len(t.calls) == 1


async def test_stale_csrf_token_reloads_once():
    class Browser(FakeBrowser):
        reloads = 0

        async def reload_api(self):
            Browser.reloads += 1

    t = CaptureTransport(load("myb_capture_bet.json"), statuses=[419])
    a = adapter(t, Browser())
    assert await a.balance() == 18.0
    assert Browser.reloads == 1 and len(t.calls) == 2


async def test_login_check_is_installed_on_the_browser():
    b = FakeBrowser()
    a = adapter(CaptureTransport(load("myb_capture_bet.json")), b)
    assert b.logged_in_check == a._logged_in and await b.logged_in_check()


async def test_league_ids():
    a = adapter(CaptureTransport({}))
    assert [await a.resolve_league(lg) for lg in ("NFL", "NCAAF", "NHL", "MLB")] == ["79", "78", "4345", "710"]
    a.cfg.leagues["NFL"] = "999"
    assert await a.resolve_league("NFL") == "999"  # config overrides the built-in ids


def menu():
    from betbot.books.mybookie import menu_leagues
    return menu_leagues(load("myb_menu.json"))


@pytest.mark.parametrize("league,names,lid", [
    ("NFL", [], "79"), ("NHL", [], "4345"), ("MLB", [], "710"), ("NBA", [], "1167"), ("CFL", [], "84"),
    ("NCAAF", ["NCAA Football", "College Football"], "78"), ("XFL", [], None),
])
def test_league_discovery_from_the_menu(league, names, lid):
    from betbot.books.mybookie import pick_league
    assert pick_league(league, menu(), names) == lid


async def test_unlisted_league_is_looked_up_in_the_menu():
    class MenuTransport:
        calls = []

        async def request(self, method, path, *, body=None, headers=None):
            self.calls.append((method, path))
            return 200, load("myb_menu.json")

    t = MenuTransport()
    a = adapter(t)
    assert await a.resolve_league("NCAAF") == "78" and t.calls == []  # built in
    assert await a.resolve_league("NBA") == "1167" and await a.resolve_league("CFL") == "84"
    assert t.calls == [("GET", "/sports_api/get-menu")]  # fetched once
    with pytest.raises(LeagueNotMapped):
        await a.resolve_league("XFL")


def test_ncaaf_and_mlb_feed_rows_match():
    evs = parse_events(load("myb_lines_ncaaf.json")["response"], "MyB", "78")
    for d in load("feed_ncaaf-pro.json"):
        if d.get("id") == "221026-1600_MyB_NCAAF_LIB_KENN_ATS_11":
            row = parse_row(d)
    m = match_row(row, evs)
    assert m.ok and m.event.description == "Liberty Flames @ Kennesaw State Owls", m.reason
    sel, why = find_selection(m.event, m.event.raw, row, m.team)
    assert (sel and sel.label == "Kennesaw State Owls +11") or "line 11 not offered" in why
    evs = parse_events(load("myb_lines_mlb.json")["response"], "MyB", "710")
    row = parse_row(next(d for d in load("feed_mlb-pro.json") if d.get("book") == "MyB"))
    m = match_row(row, evs)
    assert m.ok and m.team.name == "Tampa Bay Rays"
    sel, why = find_selection(m.event, m.event.raw, row, m.team)
    assert sel and sel.label == "Tampa Bay Rays -1.5" and sel.american == "+140"


def test_live_row_is_not_priced_from_a_pregame_listing():
    ev = next(e for e in nfl_events() if e.id == "29912810")  # not live in the capture
    sel, why = find_selection(ev, ev.raw, myb_row(league="NFL LIVE", livetime="Q1 08:00", spread=7.5),
                              ev.competitors[0])
    assert sel is None and why == "the book lists this game as not live yet"
    live = parse_events(load("myb_lines_nhl_live.json")["response"], "MyB", "4345")[0]
    sel, why = find_selection(live, live.raw, myb_row(league="NHL", bettype="ML", spread="None"), live.competitors[0])
    assert sel is None and why == "the book lists this game as live"


def test_accepted_price_reads_what_mybookie_took():
    from betbot.books.mybookie import accepted_price
    assert accepted_price(load("myb_capture_bet.json")["place_response"]) == pytest.approx(1.8333, abs=1e-3)
    assert accepted_price({"content": [{"details": []}]}) is None and accepted_price(None) is None
