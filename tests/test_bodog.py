import json

import pytest

from betbot.books.bodog import (BodogAdapter, build_bet_payload, find_selection, interpret_result, parse_bettype,
                                parse_events, suggestion_price)
from betbot.models import BookEvent, Selection
from betbot.feed import parse_row
from betbot.matching.matcher import match_row
from tests.conftest import feed_row, load


def nav_leagues():
    out = []
    for s in ("football", "baseball", "hockey"):
        out += [c for c in load(f"oz_nav_{s}.json")["children"] if c["type"] == "LEAGUE"]
    return out


@pytest.mark.parametrize("league,names,path", [
    ("NFL", [], "/football/nfl"),
    ("NHL", [], "/hockey/nhl"),
    ("MLB", [], "/baseball/mlb"),
    ("NCAAF", ["College Football", "NCAA Football"], "/football/college-football"),
])
def test_league_discovery(league, names, path):
    assert BodogAdapter.pick_league(league, nav_leagues(), names) == path


def test_unknown_league_not_guessed():
    assert BodogAdapter.pick_league("XFL", nav_leagues(), []) is None


@pytest.mark.parametrize("bettype,key,period", [
    ("ATS", "2W-HCAP", "G"), ("Over", "2W-OU", "G"), ("Under", "2W-OU", "G"), ("ML", "2W-12", "G"),
    ("1H ATS", "2W-HCAP", "1H"), ("ATS_1Q", "2W-HCAP", "Q1"),
])
def test_parse_bettype(bettype, key, period):
    rule, p = parse_bettype(bettype)
    assert rule["market_key"] == key and p == period


def test_unknown_bettype():
    assert parse_bettype("Corners") is None


def event_and_row(**over):
    events = parse_events(load("oz_event_nwe_chi.json"), "OZ", "/football/nfl")
    row = parse_row(feed_row(**over))
    m = match_row(row, events)
    assert m.ok
    return m.event, load("oz_event_nwe_chi.json")[0]["events"][0], row, m.team


def test_alternate_spread():
    ev, js, row, team = event_and_row()
    sel, why = find_selection(ev, js, row, team)
    assert sel, why
    assert sel.outcome_description == "Chicago Bears" and sel.handicap == -5
    assert sel.market_description.startswith("Alternate Lines")
    assert sel.label == "Chicago Bears -5"


def test_main_spread_and_moneyline():
    ev, js, row, team = event_and_row(spread=-3)
    sel, _ = find_selection(ev, js, row, team)
    assert sel.market_description == "Game Lines / Point Spread" and sel.decimal == pytest.approx(1.909091)
    ev, js, row, team = event_and_row(bettype="ML", spread="None", sq_team="New England Patriots", pinn_team="CHI")
    sel, _ = find_selection(ev, js, row, team)
    assert sel.outcome_description == "New England Patriots" and sel.decimal == pytest.approx(2.4)


def test_game_total_not_team_total_or_prop():
    ev, js, row, team = event_and_row(bettype="Under", spread="None", total=50.5)
    sel, _ = find_selection(ev, js, row, team)
    assert sel.outcome_description == "Under" and sel.handicap == 50.5
    assert sel.market_description == "Game Lines / Total"
    # 23.5 only exists as a Patriots team total: must not be picked
    ev, js, row, team = event_and_row(bettype="Over", spread="None", total=23.5)
    sel, why = find_selection(ev, js, row, team)
    assert sel is None and "not offered" in why


def test_missing_line_reports_what_is_offered():
    ev, js, row, team = event_and_row(spread=-17)
    sel, why = find_selection(ev, js, row, team)
    assert sel is None and "-17" in why


def test_puck_line_in_nhl_coupon():
    events = parse_events(load("oz_coupon_nhl.json"), "OZ", "/hockey/nhl")
    ev = next(e for e in events if e.description == "Anaheim Ducks @ New York Rangers")
    js = ev.raw
    hcap = next(m for m in js["displayGroups"][0]["markets"] if m["key"] == "2W-HCAP")
    away = next(o for o in hcap["outcomes"] if o["type"] == "A")
    row = parse_row(feed_row(league="NHL", id="x_OZ_NHL_ANA_NYR_ATS", sq_team="Anaheim Ducks", pinn_team="NYR",
                             start_date=ev.start_ms, spread=float(away["price"]["handicap"])))
    m = match_row(row, events)
    sel, why = find_selection(m.event, js, row, m.team)
    assert sel and sel.outcome_id == away["id"], why


def test_bet_payload_shape():
    ev, js, row, team = event_and_row()
    sel, _ = find_selection(ev, js, row, team)
    p = build_bet_payload(sel, 25, suggestion={"selections": {"selection": [
        {"outcomeId": sel.outcome_id, "priceId": "999", "version": 7}]}})
    s = p["selections"]["selection"][0]
    assert s == {"outcomeId": sel.outcome_id, "id": 0, "system": "A", "priceId": "999", "oddsFormat": "AMERICAN",
                 "version": 7}
    b = p["bets"]["bet"][0]
    assert b["stakePerLine"] == 2500 and b["betType"] == "SINGLE" and b["specifyingRisk"] is True
    assert p["groups"]["group"][0]["type"] == "STRAIGHT" and p["channel"] == "WEB_BS"


def captured_selection():
    ev = BookEvent("OZ", "29514566", "Washington Commanders @ San Francisco 49ers", 0, [], False, "/x", "/nfl")
    return Selection("OZ", ev, "518431451", "Game Lines / Point Spread", "G", "2376622109",
                     "Washington Commanders", "40264870027", 1.909091, "-110", 7.0, True)


def test_payloads_match_a_real_captured_bet():
    cap = load("oz_capture_bet.json")
    sel = captured_selection()
    assert build_bet_payload(sel, 2.0, cap["suggestion"]) == cap["place_request"]
    assert build_bet_payload(sel, 0, cap["suggestion"], for_max_stake=True) == cap["max_stake_request"]
    assert suggestion_price(cap["suggestion"], sel.outcome_id) == pytest.approx(1.909091)


def test_captured_bet_responses():
    cap = load("oz_capture_bet.json")
    assert interpret_result(cap["place_response"])[0] == "PENDING"
    assert interpret_result(cap["status_response"])[:2] == ("SUCCESS", "26107603167872")


class FakeBrowser:
    async def session_id(self):
        return "SID"

    async def show_event(self, *a, **kw):
        pass


class CaptureTransport:
    """Replays the captured bet: records every call the adapter makes."""

    def __init__(self, cap):
        self.cap, self.calls = cap, []

    async def request(self, method, path, *, body=None, headers=None):
        self.calls.append((method, path, body, headers))
        if path.endswith("/betslip/maxStake"):
            return 200, self.cap["max_stake_response"]
        if "betslip?outcomeId=" in path:
            return 200, self.cap["suggestion"]
        if method == "POST" and path.endswith("/bet/betslip"):
            return 200, self.cap["place_response"]
        return 200, self.cap["status_response"]


async def test_place_api_replays_the_sites_call_sequence():
    from betbot.config import BookConfig
    cap = load("oz_capture_bet.json")
    t = CaptureTransport(cap)
    adapter = BodogAdapter("OZ", BookConfig(base_url="https://www.ozoon.eu"), t, None, FakeBrowser())
    res = await adapter.place_api(captured_selection(), 2.0, dry_run=False)
    assert res.status.value == "accepted" and res.reference == "26107603167872" and res.stake == 2.0
    methods = [(m, p.split("?")[0].rsplit("/", 2)[-2:]) for m, p, _, _ in t.calls]
    assert [m for m, _ in methods] == ["GET", "POST", "POST", "GET"]
    assert t.calls[0][1].endswith("/bet/betslip?outcomeId=A:2376622109:1")
    assert t.calls[1][2] == cap["max_stake_request"]
    assert t.calls[2][2] == cap["place_request"]
    assert t.calls[3][1].endswith("/bet/betslip/bae235ca-822a-3221-a857-2dd5ece6ab8d")
    for _, _, _, h in t.calls:
        assert h["Authorization"] == h["X-Session-Id"] == "Bearer SID"
    assert "X-Idempotency-Key" in t.calls[2][3]


async def test_max_stake_caps_in_dollars():
    from betbot.config import BookConfig
    cap = load("oz_capture_bet.json")
    t = CaptureTransport({**cap, "max_stake_response": {"maxStake": 37.5}})
    adapter = BodogAdapter("OZ", BookConfig(base_url="https://www.ozoon.eu"), t, None, FakeBrowser())
    res = await adapter.place_api(captured_selection(), 100.0, dry_run=True)
    assert res.stake == 37.0
    assert len(t.calls) == 2  # dry run stops before placing


async def test_lower_bet_slip_price_is_not_placed():
    from betbot.config import BookConfig
    cap = load("oz_capture_bet.json")
    sug = json.loads(json.dumps(cap["suggestion"]))
    sug["selections"]["selection"][0]["price"] = "1.80"
    t = CaptureTransport({**cap, "suggestion": sug})
    adapter = BodogAdapter("OZ", BookConfig(base_url="https://www.ozoon.eu"), t, None, FakeBrowser())
    res = await adapter.place_api(captured_selection(), 2.0, dry_run=False)
    assert res.status.value == "rejected" and res.retryable and len(t.calls) == 1


def test_interpret_result():
    assert interpret_result({"status": "PLACING", "key": "k"})[0] == "PENDING"
    ok = {"status": "SUCCESS", "bets": {"bet": [{"betRef": "R1", "response": {"status": "SUCCESS"}}]}}
    assert interpret_result(ok)[:2] == ("SUCCESS", "R1")
    rej = {"bets": {"bet": [{"response": {"status": "FAIL", "error": [{"code": "betRejectedMessage_PriceChange"}]}}]}}
    state, _, reason, retry = interpret_result(rej)
    assert state == "FAIL" and retry and "PriceChange" in reason
    hard = {"status": "FAIL", "error": [{"code": "betRejectedMessage_MaxRisk"}]}
    assert interpret_result(hard)[3] is False


def test_same_line_twice_takes_the_price_closest_to_the_feed():
    # e.g. a live "rest of game" market offering the same team and line at a very different price
    import copy
    ev, js, row, team = event_and_row(spread=-3)
    js = copy.deepcopy(js)
    main = next(m for dg in js["displayGroups"] for m in dg["markets"] if m["description"] == "Point Spread"
                and m["period"]["abbreviation"] == "G")
    other = copy.deepcopy(main)
    other["id"], other["description"] = "999", "Point Spread - Rest of Game"
    for o in other["outcomes"]:
        o["id"], o["price"] = o["id"] + "9", {**o["price"], "id": "p9", "american": "+500", "decimal": "6.0"}
    js["displayGroups"].insert(0, {"description": "Live Lines", "markets": [other]})
    sel, _ = find_selection(ev, js, row, team)  # the feed saw +150 (2.5): the main line, not the +500
    assert sel.market_description == "Game Lines / Point Spread"
    row.sqline = 5.8
    sel, _ = find_selection(ev, js, row, team)
    assert sel.market_description == "Live Lines / Point Spread - Rest of Game"


def test_live_rows_use_live_markets_only():
    # Ozoon keeps a game's pre-game markets after it goes live; a live row must not be priced from them
    import copy
    ev, js, row, team = event_and_row(spread=-3)
    js = copy.deepcopy(js)
    pre = next(m for dg in js["displayGroups"] for m in dg["markets"] if m["description"] == "Point Spread"
               and m["period"]["abbreviation"] == "G")
    live_row = parse_row(feed_row(spread=-3, league="NFL LIVE", livetime="Q2 05:00"))
    sel, why = find_selection(ev, js, live_row, team)
    assert sel is None and why == "no live 2W-HCAP G market"
    live = copy.deepcopy(pre)
    live["id"], live["period"] = "777", {**pre["period"], "description": "Live Game", "live": True}
    for o in live["outcomes"]:
        o["id"], o["price"] = o["id"] + "7", {**o["price"], "id": "p7", "american": "+120", "decimal": "2.2"}
    js["displayGroups"].insert(0, {"description": "Game Lines", "markets": [live]})
    assert find_selection(ev, js, live_row, team)[0].market_id == "777"
    assert find_selection(ev, js, row, team)[0].market_id == pre["id"]  # pre-game row: pre-game market
