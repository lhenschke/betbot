"""arbhunterpro feed: parsing and polling.

Every league feed (json?nfl-pro, json?nhl-pro, ...) has the same shape: a list
of row objects followed by {"runtime": epoch}. Missing values arrive as the
string "None". Nothing here is league-specific.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

import httpx

from .models import ArbRow

log = logging.getLogger(__name__)


def _none(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, str) and v.strip() in ("", "None", "null", "NaN"):
        return None
    return v


def _float(v: Any) -> float | None:
    v = _none(v)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_date(v: Any, tz: ZoneInfo) -> float | None:
    v = _none(v)
    if v is None:
        return None
    try:
        return datetime.strptime(str(v), "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz).timestamp()
    except ValueError:
        return None


def _abbrs(row_id: str, book: str, league: str) -> tuple[str | None, str | None]:
    """Pull AWAY/HOME abbreviations out of ids like 251026-0630_LV_NFL_NOR_PIT_ATS_-3.

    Abbreviations may contain '-' (M-OH) but not '_', so split on '_'.
    """
    parts = row_id.split("_")
    try:
        i = parts.index(league, 1)
    except ValueError:
        return None, None
    if i + 2 >= len(parts):
        return None, None
    return parts[i + 1], parts[i + 2]


def parse_row(d: dict[str, Any], feed_url: str = "", tz: ZoneInfo | None = None) -> ArbRow | None:
    tz = tz or ZoneInfo("UTC")
    try:
        row_id = str(d["id"])
        book = str(d["book"])
        league = str(d["league"])
        sqline = float(d["sqline"])
        pinnline = float(d["pinnline"])
        arbsize = float(d["arbsize"])
    except (KeyError, TypeError, ValueError):
        return None
    away, home = _abbrs(row_id, book, league)
    start = _float(d.get("start_date"))
    return ArbRow(
        id=row_id,
        book=book,
        league=league,
        bettype=str(_none(d.get("bettype")) or ""),
        arbsize=arbsize,
        sqline=sqline,
        pinnline=pinnline,
        sq_team=str(_none(d.get("sq_team")) or ""),
        pinn_team=_none(d.get("pinn_team")),
        spread=_float(d.get("spread")),
        total=_float(d.get("total")),
        start_ms=int(start) if start is not None else None,
        updated_at=_parse_date(d.get("date"), tz),
        livetime=_none(d.get("livetime")),
        line3=_none(d.get("line3")),
        market_note=_none(d.get("market")),
        book_limit=_float(d.get("book_limit")),
        book_depth=_none(d.get("book_depth")),
        status=_none(d.get("status")),
        url=_none(d.get("url")),
        away_abbr=away,
        home_abbr=home,
        feed_url=feed_url,
        raw=d,
    )


def parse_feed(data: Any, feed_url: str = "", tz: ZoneInfo | None = None) -> tuple[list[ArbRow], float | None]:
    rows: list[ArbRow] = []
    runtime = None
    if not isinstance(data, list):
        return rows, runtime
    for item in data:
        if not isinstance(item, dict):
            continue
        if "runtime" in item and "id" not in item:
            runtime = _float(item["runtime"])
            continue
        row = parse_row(item, feed_url, tz)
        if row is None:
            log.warning("skipping unparseable feed row: %s", item)
        else:
            rows.append(row)
    return rows, runtime


@dataclass
class FeedUpdate:
    feed_url: str
    added: list[ArbRow] = field(default_factory=list)
    changed: list[ArbRow] = field(default_factory=list)
    removed: list[ArbRow] = field(default_factory=list)
    runtime: float | None = None


class FeedPoller:
    """Polls every configured feed URL concurrently and reports what changed."""

    def __init__(self, urls: list[str], interval_s: float = 1.0, date_tz: str = "UTC",
                 client: httpx.AsyncClient | None = None):
        self.urls = list(urls)
        self.interval_s = interval_s
        self.tz = ZoneInfo(date_tz)
        self.client = client or httpx.AsyncClient(timeout=10.0, http2=False,
                                                  headers={"User-Agent": "betbot/0.1"})
        self.rows: dict[str, dict[str, ArbRow]] = {}
        self.last_ok: dict[str, float] = {}
        self.errors: dict[str, str] = {}

    def current(self) -> dict[str, ArbRow]:
        out: dict[str, ArbRow] = {}
        for rows in self.rows.values():
            out.update(rows)
        return out

    def get(self, row_id: str) -> ArbRow | None:
        for rows in self.rows.values():
            if row_id in rows:
                return rows[row_id]
        return None

    def set_urls(self, urls: list[str]) -> None:
        self.urls = list(urls)
        for gone in set(self.rows) - set(urls):
            self.rows.pop(gone, None)

    def diff(self, url: str, new_rows: list[ArbRow], runtime: float | None) -> FeedUpdate:
        old = self.rows.get(url, {})
        new = {r.id: r for r in new_rows}
        upd = FeedUpdate(feed_url=url, runtime=runtime)
        for rid, r in new.items():
            prev = old.get(rid)
            if prev is None:
                upd.added.append(r)
            else:
                r.seen_at = prev.seen_at
                if not r.same_offer(prev):
                    upd.changed.append(r)
        upd.removed = [r for rid, r in old.items() if rid not in new]
        self.rows[url] = new
        return upd

    async def poll_once(self, url: str) -> FeedUpdate | None:
        try:
            resp = await self.client.get(url)
            resp.raise_for_status()
            rows, runtime = parse_feed(resp.json(), url, self.tz)
        except (httpx.HTTPError, ValueError) as e:
            self.errors[url] = str(e)
            log.warning("feed %s failed: %s", url, e)
            return None
        self.errors.pop(url, None)
        self.last_ok[url] = time.time()
        return self.diff(url, rows, runtime)

    async def run(self, on_update: Callable[[FeedUpdate], Awaitable[None]]) -> None:
        while True:
            started = time.monotonic()
            results = await asyncio.gather(*(self.poll_once(u) for u in self.urls))
            for upd in results:
                if upd and (upd.added or upd.changed or upd.removed):
                    await on_update(upd)
            await asyncio.sleep(max(0.05, self.interval_s - (time.monotonic() - started)))
