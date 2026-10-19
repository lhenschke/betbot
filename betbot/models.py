"""Plain data types shared by the feed, matcher, book adapters and engine."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


@dataclass
class ArbRow:
    id: str
    book: str
    league: str
    bettype: str
    arbsize: float
    sqline: float
    pinnline: float
    sq_team: str
    pinn_team: str | None
    spread: float | None
    total: float | None
    start_ms: int | None
    updated_at: float | None  # epoch seconds parsed from the feed's "date"
    livetime: str | None
    line3: str | None
    market_note: str | None
    book_limit: float | None
    book_depth: str | None
    status: str | None
    url: str | None
    away_abbr: str | None
    home_abbr: str | None
    feed_url: str = ""
    seen_at: float = field(default_factory=time.time)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_live(self) -> bool:
        return self.livetime is not None

    @property
    def line(self) -> float | None:
        """The handicap or total this row is about, if any."""
        if self.spread is not None:
            return self.spread
        return self.total

    def same_offer(self, other: "ArbRow") -> bool:
        return (self.sqline, self.pinnline, self.line, self.arbsize) == (
            other.sqline, other.pinnline, other.line, other.arbsize)


@dataclass
class Competitor:
    id: str
    name: str
    home: bool


@dataclass
class BookEvent:
    book: str
    id: str
    description: str
    start_ms: int
    competitors: list[Competitor]
    live: bool
    link: str
    league_link: str
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def competitor(self, competitor_id: str) -> Competitor | None:
        return next((c for c in self.competitors if c.id == competitor_id), None)


@dataclass
class Selection:
    """A single bettable outcome at a book, with the price seen at quote time."""

    book: str
    event: BookEvent
    market_id: str
    market_description: str
    period: str
    outcome_id: str
    outcome_description: str
    price_id: str
    decimal: float
    american: str
    handicap: float | None
    is_open: bool
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def label(self) -> str:
        """'Chicago Bears -5' or 'Under 45.5'."""
        if self.handicap is None:
            return self.outcome_description
        if self.outcome_description.lower() in ("over", "under"):
            return f"{self.outcome_description} {self.handicap:g}"
        return f"{self.outcome_description} {self.handicap:+g}"

    @property
    def key(self) -> str:
        """Identifies 'the same bet' for dedupe, regardless of price."""
        line = "" if self.handicap is None else f"{self.handicap:g}"
        return f"{self.book}:{self.event.id}:{self.market_description}:{self.period}:{self.outcome_description}:{line}"


class BetStatus(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    ERROR = "error"
    DRY_RUN = "dry_run"


@dataclass
class BetResult:
    status: BetStatus
    stake: float
    decimal: float | None = None
    reference: str | None = None
    reason: str | None = None
    retryable: bool = False
    timings: dict[str, float] = field(default_factory=dict)
    raw: Any = None
