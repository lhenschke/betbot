import pytest

from betbot.books.bodog import (BodogAdapter, build_bet_payload, find_selection, interpret_result, parse_bettype,
                                parse_events)
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


def test_interpret_result():
    assert interpret_result({"status": "PLACING", "key": "k"})[0] == "PENDING"
    ok = {"status": "SUCCESS", "bets": {"bet": [{"betRef": "R1", "response": {"status": "SUCCESS"}}]}}
    assert interpret_result(ok)[:2] == ("SUCCESS", "R1")
    rej = {"bets": {"bet": [{"response": {"status": "FAIL", "error": [{"code": "betRejectedMessage_PriceChange"}]}}]}}
    state, _, reason, retry = interpret_result(rej)
    assert state == "FAIL" and retry and "PriceChange" in reason
    hard = {"status": "FAIL", "error": [{"code": "betRejectedMessage_MaxRisk"}]}
    assert interpret_result(hard)[3] is False
