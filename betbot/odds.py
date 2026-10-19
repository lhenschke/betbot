"""Odds conversions and the feed's arbsize metric.

arbsize is the gap in American-odds "cents" between the soft book's price on
one side and Pinnacle's price on the other side. Each price is mapped to its
distance from even money (+106 -> 6, -108 -> -8) and the two are added, so
+106 / -108 -> -2 and -105 / +109 -> +4. Positive means a true arb.
"""

from __future__ import annotations


def decimal_to_american(dec: float) -> float:
    if dec <= 1.0:
        raise ValueError(f"decimal odds must be > 1.0, got {dec}")
    if dec >= 2.0:
        return (dec - 1.0) * 100.0
    return -100.0 / (dec - 1.0)


def american_to_decimal(am: float) -> float:
    if -100.0 < am < 100.0:
        raise ValueError(f"American odds must be <= -100 or >= +100, got {am}")
    if am > 0:
        return 1.0 + am / 100.0
    return 1.0 + 100.0 / -am


def parse_american(text: str) -> float:
    """Parse an American price as books display it ("+120", "-110", "EVEN")."""
    t = text.strip().upper()
    if t in ("EVEN", "EV", "PK"):
        return 100.0
    return float(t)


def cents(dec: float) -> float:
    """Distance of a price from even money in American cents."""
    am = decimal_to_american(dec)
    return am - 100.0 if am > 0 else am + 100.0


def arbsize_cents(sq_dec: float, pinn_dec: float) -> float:
    return cents(sq_dec) + cents(pinn_dec)


def min_decimal_for_arbsize(pinn_dec: float, target: float) -> float:
    """Lowest soft-book decimal price that still gives arbsize >= target."""
    need = target - cents(pinn_dec)
    am = need + 100.0 if need >= 0 else need - 100.0
    return american_to_decimal(am)
