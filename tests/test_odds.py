import glob

import pytest

from betbot.feed import parse_feed
from betbot.odds import (american_to_decimal, arbsize_cents, decimal_to_american, min_decimal_for_arbsize,
                         parse_american)
from tests.conftest import FIX, load


def test_conversions_round_trip():
    for am in (-250, -110, -101, 100, 105, 150, 400):
        assert decimal_to_american(american_to_decimal(am)) == pytest.approx(am)
    assert parse_american("EVEN") == 100
    assert parse_american("-120") == -120


@pytest.mark.parametrize("sq,pinn,expected", [
    (2.06, 1.925, -2),            # +106 / -108
    (1.95238095238, 2.09, 4),     # -105 / +109
    (1.9900990099, 1.98, -3),     # -101 / -102
    (2.5, 1.649, -4),             # +150 / -154
])
def test_arbsize_examples(sq, pinn, expected):
    assert round(arbsize_cents(sq, pinn)) == expected


def test_arbsize_matches_every_snapshot_row():
    n = 0
    for path in glob.glob(str(FIX / "feed_*.json")):
        rows, _ = parse_feed(load(path.split("/")[-1]))
        for r in rows:
            assert round(arbsize_cents(r.sqline, r.pinnline)) == r.arbsize, r.id
            n += 1
    assert n > 30


@pytest.mark.parametrize("pinn", [1.649, 1.925, 2.09, 2.31])
@pytest.mark.parametrize("target", [-3, 0, 1, 4])
def test_min_decimal_is_the_threshold(pinn, target):
    d = min_decimal_for_arbsize(pinn, target)
    assert arbsize_cents(d, pinn) == pytest.approx(target, abs=1e-6)
    assert arbsize_cents(d * 0.995, pinn) < target
