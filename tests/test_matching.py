from betbot.books.bodog import parse_events
from betbot.feed import parse_row
from betbot.matching.matcher import match_row
from betbot.matching.teams import Aliases, abbr_score, normalize, team_score
from tests.conftest import feed_row, load


def test_normalize():
    assert normalize("Indiana (#6)") == "indiana"
    assert normalize("NY Giants") == "new york giants"
    assert normalize("Arizona St") == "arizona state"
    assert normalize("St. Louis Blues") == "saint louis blues"
    assert normalize("Texas A&M") == "texas am"


def test_team_scores():
    none = Aliases()
    assert team_score("NY Giants", "New York Giants", none) == 100
    assert team_score("Pittsburgh", "Pittsburgh Steelers", none) >= 90
    assert team_score("Michigan", "Michigan State", none) < 85
    assert team_score("Arizona St", "Arizona State", none) == 100
    ncaaf = Aliases.for_league("NCAAF")
    assert team_score("Ole Miss", "Mississippi", ncaaf) == 100
    assert team_score("Mississippi", "Mississippi State", ncaaf) == 0


def test_abbreviations():
    nfl = Aliases.for_league("NFL")
    assert abbr_score("NWE", "New England Patriots", nfl) == 100
    assert abbr_score("NWE", "Chicago Bears", nfl) == 0
    assert abbr_score("XYZ", "Chicago Bears", Aliases()) < 50


def nfl_events():
    return parse_events(load("oz_coupon_nfl.json"), "OZ", "/football/nfl")


def test_matches_nfl_row_to_event():
    m = match_row(parse_row(feed_row()), nfl_events())
    assert m.ok, m.reason
    assert m.event.id == "29513980"
    assert m.team.name == "Chicago Bears"


def test_other_book_spelling_and_time_drift():
    # feed start time 30 min off, team written differently
    row = parse_row(feed_row(sq_team="Chicago", start_date=1792714500000 + 30 * 60_000))
    m = match_row(row, nfl_events())
    assert m.ok and m.event.id == "29513980"


def test_wrong_time_is_unmatched():
    row = parse_row(feed_row(start_date=1792714500000 + 6 * 3600_000))
    m = match_row(row, nfl_events())
    assert not m.ok


def test_unknown_team_is_unmatched():
    m = match_row(parse_row(feed_row(sq_team="Green Bay Packers", pinn_team="DET")), nfl_events())
    assert not m.ok and "no team" in m.reason


def test_ncaaf_ranked_team_names():
    events = parse_events(load("oz_coupon_ncaaf.json"), "OZ", "/football/college-football")
    rows = [r for r in (parse_row(d) for d in load("feed_ncaaf-pro.json") if "id" in d) if r]
    matched = {r.id: match_row(r, events) for r in rows}
    ok = [m for m in matched.values() if m.ok]
    assert len(ok) >= len(rows) * 0.8, {k: v.reason for k, v in matched.items() if not v.ok}
    for rid, m in matched.items():
        if m.ok:
            assert team_score(next(r for r in rows if r.id == rid).sq_team, m.team.name,
                              Aliases.for_league("NCAAF")) >= 85


def test_nhl_special_event_is_not_matched():
    events = parse_events(load("oz_coupon_nhl.json"), "OZ", "/hockey/nhl")
    special = next(e for e in events if e.description == "Away Goals @ Home Goals")
    row = parse_row(feed_row(league="NHL", id="x_OZ_NHL_ANA_NYR_ATS_1.5", sq_team="New York Rangers",
                             pinn_team="ANA", start_date=special.start_ms, spread=1.5))
    m = match_row(row, events)
    assert m.ok and m.event.description == "Anaheim Ducks @ New York Rangers"
