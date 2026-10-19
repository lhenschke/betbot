"""Book adapter interface and registry.

A book adapter knows one sportsbook platform: how to list its leagues and
events, find the outcome a feed row is about, quote it fresh, and place a bet.
Matching rows to events is shared (betbot.matching); adapters only supply
events. New books go in this package and register with @register("name").
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Callable, Protocol

from ..models import ArbRow, BetResult, BookEvent, Competitor, Selection

if TYPE_CHECKING:
    from ..browser import BrowserSession
    from ..config import BookConfig
    from ..ledger import Ledger


class Transport(Protocol):
    """How an adapter talks HTTP: plain httpx, or fetch() inside the logged-in page."""

    async def request(self, method: str, path: str, *, body: Any = None,
                      headers: dict[str, str] | None = None) -> tuple[int, Any]: ...


class LeagueNotMapped(Exception):
    pass


class UiNotReady(Exception):
    """'ui' placement couldn't find the page elements; nothing was submitted."""


class BookAdapter(ABC):
    code: str

    def __init__(self, code: str, cfg: "BookConfig", transport: Transport, ledger: "Ledger | None" = None,
                 browser: "BrowserSession | None" = None):
        self.code = code
        self.cfg = cfg
        self.transport = transport
        self.ledger = ledger
        self.browser = browser

    @abstractmethod
    async def resolve_league(self, feed_league: str) -> str:
        """Book league path for a feed league; raises LeagueNotMapped."""

    @abstractmethod
    async def events(self, feed_league: str, live_hint: bool = False) -> list[BookEvent]:
        """Cached event catalog for a league, refreshed on the adapter's schedule."""

    @abstractmethod
    async def quote(self, event: BookEvent, row: ArbRow, team: Competitor | None,
                    max_age_s: float = 0.0) -> tuple[Selection | None, str]:
        """Selection for the row at this event, or (None, reason). max_age_s=0 forces a fresh fetch."""

    @abstractmethod
    async def place_api(self, sel: Selection, stake: float, dry_run: bool) -> BetResult:
        ...

    @abstractmethod
    async def place_ui(self, sel: Selection, stake: float, dry_run: bool) -> BetResult:
        ...

    async def is_logged_in(self) -> bool:
        return self.browser is not None and await self.browser.is_logged_in()

    async def balance(self) -> float | None:
        return None

    async def idle_action(self) -> None:
        """Something a person might do while waiting; optional."""


_REGISTRY: dict[str, type[BookAdapter]] = {}


def register(name: str) -> Callable[[type[BookAdapter]], type[BookAdapter]]:
    def deco(cls: type[BookAdapter]) -> type[BookAdapter]:
        _REGISTRY[name] = cls
        return cls
    return deco


def adapter_class(name: str) -> type[BookAdapter]:
    from . import bodog, mybookie  # noqa: F401  (registers built-in adapters)
    if name not in _REGISTRY:
        raise KeyError(f"unknown book adapter '{name}'; known: {sorted(_REGISTRY)}")
    return _REGISTRY[name]
