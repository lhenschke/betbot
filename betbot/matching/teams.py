"""Team-name normalising, alias lookup and scoring.

Alias files live in matching/aliases/<LEAGUE>.yaml and map a canonical team
name to the other spellings and abbreviations books and the feed use. A league
without an alias file still works through normalised and fuzzy comparison.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml
from rapidfuzz import fuzz

ALIAS_DIR = Path(__file__).parent / "aliases"
# Aliases added from the dashboard live outside the code folder so updates never clash with them.
LOCAL_ALIAS_DIR = Path("data") / "aliases"

# Extra words that make two names different teams ("Michigan" vs "Michigan State").
DISTINGUISHING = {
    "state", "tech", "am", "southern", "northern", "eastern", "western", "central",
    "international", "poly", "christian", "baptist", "atlantic", "north", "south",
    "east", "west", "methodist", "carolina", "florida", "kentucky", "illinois",
    "texas", "georgia", "tennessee", "arizona", "dakota", "mexico", "virginia",
    "connecticut", "louisiana", "alabama", "mississippi", "michigan", "ohio",
    "university", "college", "city", "valley", "upstate", "pacific",
}

_EXPAND_ANYWHERE = {"ny": "new york", "la": "los angeles", "univ": "university", "u": "university",
                    "intl": "international"}


def normalize(name: str) -> str:
    s = name.lower().replace("&", " and ").replace("a and m", "am")
    s = re.sub(r"\(\s*#?\s*\d+\s*\)|#\s*\d+", " ", s)  # rankings: "Indiana (#6)"
    s = re.sub(r"[().,'’`]", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    tokens = [t for t in s.split() if t and t != "the"]
    out: list[str] = []
    for i, t in enumerate(tokens):
        if t == "st":
            out.append("saint" if i == 0 else "state")
        elif t in _EXPAND_ANYWHERE and len(tokens) > 1:
            out.append(_EXPAND_ANYWHERE[t])
        else:
            out.append(t)
    return " ".join(out)


class Aliases:
    def __init__(self, mapping: dict[str, list[str]] | None = None):
        self.canonical: dict[str, str] = {}
        for canon, variants in (mapping or {}).items():
            c = normalize(canon)
            self.canonical[c] = c
            for v in variants or []:
                self.canonical[normalize(str(v))] = c
                self.canonical[str(v).upper()] = c  # raw abbreviation, e.g. "M-OH"

    def resolve(self, name: str) -> str | None:
        return self.canonical.get(str(name).upper()) or self.canonical.get(normalize(name))

    @classmethod
    def for_league(cls, league: str) -> "Aliases":
        return _load(league.upper())


@lru_cache(maxsize=64)
def _load(league: str) -> Aliases:
    merged: dict[str, list[str]] = {}
    for d in (ALIAS_DIR, LOCAL_ALIAS_DIR):
        p = d / f"{league}.yaml"
        if p.exists():
            for canon, variants in (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).items():
                merged.setdefault(canon, []).extend(variants or [])
    return Aliases(merged)


def reload_aliases() -> None:
    _load.cache_clear()


def team_score(feed_name: str, book_name: str, aliases: Aliases) -> float:
    """0-100 confidence that two names are the same team."""
    if not feed_name or not book_name:
        return 0.0
    ca, cb = aliases.resolve(feed_name), aliases.resolve(book_name)
    if ca and cb:
        return 100.0 if ca == cb else 0.0
    a, b = normalize(feed_name), normalize(book_name)
    if ca and not cb:
        a = ca
    if cb and not ca:
        b = cb
    if a == b:
        return 100.0
    ta, tb = set(a.split()), set(b.split())
    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if small and small < big:
        extra = big - small
        if not any(t in DISTINGUISHING or len(t) <= 3 for t in extra):
            return 92.0  # "Pittsburgh" vs "Pittsburgh Steelers"
        return 40.0      # "Michigan" vs "Michigan State"
    return float(fuzz.token_sort_ratio(a, b))


def _initials_fit(abbr: str, name: str) -> bool:
    letters = re.sub(r"[^A-Z]", "", abbr.upper())
    words = normalize(name).upper().split()
    if not letters or not words or letters[0] != words[0][0]:
        return False
    hay = "".join(words)
    i = 0
    for ch in hay:
        if i < len(letters) and ch == letters[i]:
            i += 1
    return i == len(letters)


def abbr_score(abbr: str | None, book_name: str, aliases: Aliases) -> float:
    """How well a feed abbreviation (NOR, M-OH, ...) fits a book team name."""
    if not abbr:
        return 50.0
    ca, cb = aliases.resolve(abbr), aliases.resolve(book_name)
    if ca:
        if cb:
            return 100.0 if ca == cb else 0.0
        return 100.0 if team_score(ca, book_name, Aliases()) >= 90 else 20.0
    return 70.0 if _initials_fit(abbr, book_name) else 30.0
