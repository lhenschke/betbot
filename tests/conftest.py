import json
from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text())


def feed_row(**over):
    """A feed row dict in the arbhunterpro shape, with "None" strings like the real feed."""
    row = {"arbsize": 2, "bettype": "ATS", "book": "OZ", "book_depth": "None", "book_limit": 3000,
           "date": "2026-10-19 17:25:45", "id": "221026-1715_OZ_NFL_NWE_CHI_ATS_-5", "league": "NFL",
           "line3": "None", "livetime": "None", "market": "None", "pinn_team": "NWE", "pinnline": 1.649,
           "spread": -5, "sq_team": "Chicago Bears", "sqline": 2.5, "start_date": 1792714500000,
           "status": "up", "total": "None", "url": "https://www.ozoon.eu"}
    row.update(over)
    return row


@pytest.fixture
def fixture():
    return load
