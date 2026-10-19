from betbot.feed import FeedPoller, parse_feed, parse_row
from tests.conftest import feed_row, load


def test_none_strings_become_none():
    r = parse_row(feed_row())
    assert r.total is None and r.livetime is None and r.market_note is None and r.book_depth is None
    assert r.spread == -5 and r.line == -5
    assert not r.is_live
    assert (r.away_abbr, r.home_abbr) == ("NWE", "CHI")


def test_abbreviations_with_dashes_and_totals():
    r = parse_row(feed_row(id="241026-1000_OZ_NCAAF_M-OH_CMU_Under_42.5", league="NCAAF", bettype="Under",
                           spread="None", total=42.5))
    assert (r.away_abbr, r.home_abbr) == ("M-OH", "CMU")
    assert r.line == 42.5


def test_live_row():
    r = parse_row(feed_row(livetime="Q3 04:12"))
    assert r.is_live


def test_runtime_and_bad_rows():
    rows, runtime = parse_feed([feed_row(), {"junk": 1}, {"runtime": 1792442892.9}])
    assert len(rows) == 1 and runtime == 1792442892.9


def test_real_snapshots_parse():
    rows, runtime = parse_feed(load("feed_ncaaf-pro_snapshot1.json"))
    assert len(rows) == 20 and runtime


def test_diff_added_changed_removed():
    p = FeedPoller(["u"])
    a, b = parse_row(feed_row()), parse_row(feed_row(id="x_OZ_NFL_A_B_ATS_1"))
    upd = p.diff("u", [a, b], None)
    assert len(upd.added) == 2 and not upd.changed and not upd.removed
    a2 = parse_row(feed_row(sqline=2.6, arbsize=3))
    upd = p.diff("u", [a2], None)
    assert [r.id for r in upd.changed] == [a.id]
    assert [r.id for r in upd.removed] == [b.id]
    assert p.get(a.id).sqline == 2.6
