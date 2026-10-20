"""Decision pipeline: classify -> match -> re-check -> stake -> place -> record."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from .books.base import BookAdapter, LeagueNotMapped, UiNotReady
from .config import Config
from .credentials import get_credentials
from .feed import FeedPoller, FeedUpdate
from .ledger import Ledger
from .matching.matcher import match_row
from .models import ArbRow, BetResult, BetStatus, BookEvent, Competitor, Selection
from .odds import arbsize_cents, cents, decimal_to_american

log = logging.getLogger(__name__)

AUTO, BORDERLINE, IGNORED = "auto", "borderline", "ignored"


def _am(dec: float) -> str:
    return f"{decimal_to_american(dec):+.0f}"


@dataclass
class RowView:
    row: ArbRow
    category: str
    state: str = "new"            # new, matched, unmatched, league_not_mapped, queued, placing, placed, skipped, gone
    reason: str = ""
    event: BookEvent | None = None
    team: Competitor | None = None
    selection: Selection | None = None
    live_arbsize: float | None = None
    quoted_at: float | None = None
    updated: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        r, s = self.row, self.selection
        return {
            "id": r.id, "book": r.book, "league": r.league, "bettype": r.bettype, "line": r.line,
            "sq_team": r.sq_team, "sqline": r.sqline, "pinnline": r.pinnline, "arbsize": r.arbsize,
            "live": r.is_live, "livetime": r.livetime, "start_ms": r.start_ms, "note": r.market_note,
            "category": self.category, "state": self.state, "reason": self.reason,
            "event": self.event.description if self.event else None,
            "selection": f"{s.label} @ {s.american} ({s.market_description})" if s else None,
            "book_decimal": s.decimal if s else None,
            "live_arbsize": None if self.live_arbsize is None else round(self.live_arbsize, 1),
            "quoted_at": self.quoted_at, "updated": self.updated,
        }


@dataclass
class BookState:
    paused: str | None = None       # reason auto betting is paused for this book
    logged_in: bool | None = None
    balance: float | None = None
    rejections_in_row: int = 0
    last_bet_ts: float = 0.0
    bet_times: deque = field(default_factory=lambda: deque(maxlen=500))


class Engine:
    def __init__(self, cfg: Config, ledger: Ledger, poller: FeedPoller, adapters: dict[str, BookAdapter],
                 save_config: Callable[[], None] | None = None):
        self.cfg = cfg
        self.ledger = ledger
        self.poller = poller
        self.adapters = adapters
        self.views: dict[str, RowView] = {}
        self.books: dict[str, BookState] = {code: BookState() for code in adapters}
        self.listeners: list[Callable[[dict[str, Any]], Awaitable[None]]] = []
        self.save_config = save_config or (lambda: None)
        self._inflight: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        self._idle: dict[str, asyncio.Future] = {}

    # ----- plumbing -----------------------------------------------------------
    async def emit(self, msg: dict[str, Any]) -> None:
        for fn in list(self.listeners):
            try:
                await fn(msg)
            except Exception:  # noqa: BLE001 - a dead websocket must not stop betting
                self.listeners.remove(fn)

    def _spawn(self, coro: Awaitable[Any]) -> None:
        t = asyncio.ensure_future(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def account(self, book: str) -> str:
        bc = self.cfg.books.get(book)
        return (bc.account if bc and bc.account else "default")

    def classify(self, arbsize: float) -> str:
        th = self.cfg.thresholds
        if arbsize >= th.auto_min:
            return AUTO
        if arbsize >= th.borderline_min:
            return BORDERLINE
        return IGNORED

    def book_enabled(self, book: str) -> bool:
        bc = self.cfg.books.get(book)
        return bool(bc and bc.enabled and book in self.adapters)

    # ----- feed updates -------------------------------------------------------
    async def on_feed(self, upd: FeedUpdate) -> None:
        for row in upd.removed:
            v = self.views.get(row.id)
            if v and v.state not in ("placed",):
                v.state, v.reason, v.updated = "gone", "no longer in feed", time.time()
        for row in upd.added + upd.changed:
            if not self.book_enabled(row.book):
                continue
            cat = self.classify(row.arbsize)
            v = self.views.get(row.id)
            if v is None:
                v = self.views[row.id] = RowView(row=row, category=cat)
            else:
                v.row, v.category, v.updated = row, cat, time.time()
                if v.state == "gone":
                    v.state = "new"
            self._spawn(self.process(row.id))
        await self.emit({"type": "rows"})

    async def process(self, row_id: str) -> None:
        if row_id in self._inflight:
            return
        self._inflight.add(row_id)
        try:
            v = self.views[row_id]
            if not await self.resolve(v):
                return
            if v.category == AUTO:
                if v.state != "placed":
                    await self.attempt(row_id, trigger="auto")
            elif v.category == BORDERLINE:
                await self.refresh_quote(v, max_age_s=5.0)
                if self._left_feed(v):
                    return
                if v.state not in ("placed", "placing"):
                    v.state = "queued"
                    await self.emit({"type": "queue", "row": v.as_dict()})
        except Exception as e:  # noqa: BLE001 - one bad row must not stop the engine
            log.exception("processing %s failed", row_id)
            v = self.views.get(row_id)
            if v:
                v.state, v.reason = "skipped", f"error: {e}"
        finally:
            self._inflight.discard(row_id)
            await self.emit({"type": "rows"})

    def _left_feed(self, v: RowView) -> bool:
        """Processing waits on the books (catalog fetches, spaced-out quotes); a row that left the
        feed meanwhile stays "gone" rather than coming back as matched or queued."""
        if self.poller.get(v.row.id) is not None:
            return False
        v.state, v.reason = "gone", "no longer in feed"
        return True

    async def resolve(self, v: RowView) -> bool:
        if v.event is not None:
            return True
        adapter = self.adapters[v.row.book]
        try:
            events = await adapter.events(v.row.league, live_hint=v.row.is_live)
            if self._left_feed(v):
                return False
        except LeagueNotMapped as e:
            v.state, v.reason = "league_not_mapped", str(e)
            self.ledger.record_decision(v.row.id, v.row.book, v.row.league, v.row.arbsize, "unmatched", str(e))
            return False
        m = match_row(v.row, events)
        if not m.ok:
            v.state, v.reason = "unmatched", m.reason
            self.ledger.record_decision(v.row.id, v.row.book, v.row.league, v.row.arbsize, "unmatched", m.reason)
            return False
        v.event, v.team, v.state, v.reason = m.event, m.team, "matched", ""
        return True

    async def refresh_quote(self, v: RowView, max_age_s: float = 0.0) -> Selection | None:
        """Re-price a row. Display-only refreshes may reuse a few-seconds-old event; bets never do."""
        sel, why = await self.adapters[v.row.book].quote(v.event, v.row, v.team, max_age_s)
        v.selection, v.quoted_at = sel, time.time()
        if sel is None:
            v.live_arbsize, v.reason = None, why
            return None
        latest = self.poller.get(v.row.id) or v.row
        v.live_arbsize = arbsize_cents(sel.decimal, latest.pinnline)
        v.reason = (self.price_gap(latest, sel) or "") if sel.is_open else "market closed or suspended"
        return sel

    def price_gap(self, row: ArbRow, sel: Selection) -> str | None:
        """Why not to trust a quote whose price is far from the one the feed saw for the same bet: the
        bot found a different market (e.g. a live "rest of game" line), or one side is stale."""
        limit = self.cfg.thresholds.max_price_gap
        if not limit or row.sqline <= 1.0 or sel.decimal <= 1.0:
            return None
        gap = abs(cents(sel.decimal) - cents(row.sqline))
        if gap <= limit:
            return None
        return (f"book price {sel.american} is {gap:.0f} cents from the feed's {_am(row.sqline)}: "
                f"probably a different market or a stale price; not betting")

    # ----- the bet ------------------------------------------------------------
    def in_active_hours(self) -> bool:
        ah = self.cfg.human.active_hours
        if not ah.enabled:
            return True
        now = datetime.now(ZoneInfo(ah.timezone)).time()
        start, end = dtime.fromisoformat(ah.start), dtime.fromisoformat(ah.end)
        return start <= now <= end if start <= end else (now >= start or now <= end)

    def pacing_block(self, book: str) -> str | None:
        p, st, now = self.cfg.pacing, self.books[book], time.time()
        if now - st.last_bet_ts < p.min_gap_s:
            return f"pacing: less than {p.min_gap_s:g}s since last bet"
        if sum(1 for t in st.bet_times if now - t < 60) >= p.max_per_minute:
            return "pacing: per-minute limit"
        if sum(1 for t in st.bet_times if now - t < 3600) >= p.max_per_hour:
            return "pacing: per-hour limit"
        return None

    def stake_for(self, row: ArbRow, sel: Selection) -> tuple[float, str]:
        """Your stake, exactly as set. Only when a limit cuts it down is the result rounded down to a
        multiple of round_to (never to nothing) and held to min_stake (in attempt)."""
        sc = self.cfg.stake
        acct = self.account(row.book)
        caps = {"stake": self.cfg.stake_for(row.book)}
        if row.book_limit:
            caps["book limit"] = row.book_limit
        caps["event cap"] = sc.max_per_event - self.ledger.event_exposure(acct, row.book, sel.event.id)
        day_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        caps["daily cap"] = sc.daily_cap - self.ledger.staked_since(acct, day_start)
        limit_name, amount = min(caps.items(), key=lambda kv: kv[1])
        if limit_name != "stake" and sc.round_to > 0 and amount >= sc.round_to:
            amount = int(amount / sc.round_to) * sc.round_to
        return max(0.0, int(amount * 100) / 100), limit_name

    def _skip(self, v: RowView, reason: str) -> BetResult:
        v.state, v.reason = ("queued" if v.category == BORDERLINE else "skipped"), reason
        self.ledger.record_decision(v.row.id, v.row.book, v.row.league, v.row.arbsize, "skip", reason)
        return BetResult(BetStatus.REJECTED, 0, reason=reason)

    async def attempt(self, row_id: str, trigger: str, min_arbsize: float | None = None,
                      mode: str | None = None) -> BetResult:
        v = self.views[row_id]
        book = v.row.book
        bc, st, adapter = self.cfg.books[book], self.books[book], self.adapters[book]
        th = self.cfg.thresholds
        auto = trigger == "auto"
        if min_arbsize is None:
            min_arbsize = th.auto_min
        mode = mode or (bc.auto_mode if auto else bc.one_click_mode)

        if auto and not self.cfg.auto_enabled:
            return self._skip(v, "auto betting is switched off")
        if auto and st.paused:
            return self._skip(v, f"paused: {st.paused}")
        if auto and not self.in_active_hours():
            return self._skip(v, "outside active hours")
        latest = self.poller.get(row_id)
        if latest is None:
            return self._skip(v, "row no longer in the feed")
        age_limit = th.max_live_age_s if latest.is_live else th.max_prematch_age_s
        feed_age = self.poller.feed_age(latest.feed_url)
        # Only automatic bets wait for a live feed; a bet you click is re-quoted at the book and placed.
        if auto and age_limit and feed_age is not None and feed_age > age_limit:
            return self._skip(v, f"the feed hasn't updated for {feed_age:.0f}s")
        if block := self.pacing_block(book):
            if auto:
                return self._skip(v, block)
            await asyncio.sleep(max(0.0, self.cfg.pacing.min_gap_s - (time.time() - st.last_bet_ts)))

        browser = adapter.browser
        lock = browser.lock if browser else asyncio.Lock()
        self._stop_idle(book)
        async with lock:
            v.state = "placing"
            await self.emit({"type": "rows"})
            if browser and not await browser.is_logged_in():
                st.logged_in = False
                self._spawn(self.ensure_login(book))
                return self._skip(v, "not logged in")
            result: BetResult | None = None
            for attempt_no in range(2):
                sel = await self.refresh_quote(v)
                if sel is None:
                    return self._skip(v, v.reason or "selection not found")
                if not sel.is_open:
                    return self._skip(v, "market closed or suspended")
                if gap := self.price_gap(latest, sel):
                    return self._skip(v, gap)
                if v.live_arbsize is None or v.live_arbsize < min_arbsize - 1e-9:
                    return self._skip(v, f"value gone: arbsize now {v.live_arbsize:.1f} < {min_arbsize:g}")
                acct = self.account(book)
                if self.ledger.already_bet(acct, sel.key, include_dry_run=self.cfg.dry_run):
                    v.state, v.reason = "placed", "already bet this selection"
                    return BetResult(BetStatus.REJECTED, 0, reason=v.reason)
                if (auto and self.cfg.pacing.skip_marginal_pct and round(v.live_arbsize) <= th.auto_min
                        and random.random() < self.cfg.pacing.skip_marginal_pct):
                    return self._skip(v, "randomly skipped marginal bet (pacing)")
                stake, limit_name = self.stake_for(latest, sel)
                if stake <= 0 or (limit_name != "stake" and stake < self.cfg.stake.min_stake):
                    return self._skip(v, f"{limit_name} leaves only {stake:g}, below min_stake "
                                         f"{self.cfg.stake.min_stake:g}")
                if auto and attempt_no == 0:
                    lo, hi = self.cfg.pacing.live_delay_s if latest.is_live else self.cfg.pacing.prematch_delay_s
                    await asyncio.sleep(random.uniform(lo, hi))
                try:
                    if mode == "ui":
                        try:
                            result = await adapter.place_ui(sel, stake, self.cfg.dry_run)
                        except UiNotReady as e:  # nothing was submitted; never quietly switch to the API
                            log.warning("%s: mouse betting couldn't get to the bet: %s", row_id, e)
                            return self._skip(v, f"mouse betting: {e}")
                    else:
                        result = await adapter.place_api(sel, stake, self.cfg.dry_run)
                except PermissionError as e:
                    st.logged_in = False
                    self._spawn(self.ensure_login(book))
                    return self._skip(v, f"login needed: {e}")
                if result.status == BetStatus.REJECTED and result.retryable and attempt_no == 0:
                    log.info("%s: %s, re-quoting once", row_id, result.reason)
                    continue
                break
            assert result is not None
            self._record(v, sel, result, mode, trigger)
            return result

    def _record(self, v: RowView, sel: Selection, result: BetResult, mode: str, trigger: str) -> None:
        book, st = v.row.book, self.books[v.row.book]
        status = {BetStatus.ACCEPTED: "accepted", BetStatus.DRY_RUN: "accepted",
                  BetStatus.REJECTED: "rejected", BetStatus.ERROR: "pending"}[result.status]
        self.ledger.record_bet(
            account=self.account(book), book=book, row_id=v.row.id, selection_key=sel.key,
            event_id=sel.event.id, description=f"{sel.event.description}: {sel.label} ({sel.market_description})",
            stake=result.stake, decimal=result.decimal, arbsize=v.live_arbsize, mode=mode, trigger=trigger,
            status=status, reference=result.reference, reason=result.reason, timings=result.timings,
            dry_run=result.status == BetStatus.DRY_RUN)
        now = time.time()
        if result.status in (BetStatus.ACCEPTED, BetStatus.DRY_RUN):
            v.state = "placed"
            v.reason = "dry run: would place" if result.status == BetStatus.DRY_RUN else f"ref {result.reference}"
            st.rejections_in_row = 0
            st.last_bet_ts = now
            st.bet_times.append(now)
        elif result.status == BetStatus.ERROR:
            v.state, v.reason = "placed", result.reason or "status unknown"
            st.last_bet_ts = now
        else:
            v.state, v.reason = "skipped", f"rejected: {result.reason}"
            st.rejections_in_row += 1
            if st.rejections_in_row >= self.cfg.pacing.pause_after_rejections:
                st.paused = f"{st.rejections_in_row} rejections in a row"
        self._spawn(self.emit({"type": "bet", "row": v.as_dict(), "status": result.status.value}))

    async def one_click(self, row_id: str, mode: str | None = None) -> BetResult:
        v = self.views.get(row_id)
        if v is None:
            return BetResult(BetStatus.REJECTED, 0, reason="unknown row")
        shown = v.live_arbsize if v.live_arbsize is not None else v.row.arbsize
        floor = shown - self.cfg.thresholds.one_click_tolerance
        if row_id in self._inflight:
            return BetResult(BetStatus.REJECTED, 0, reason="already being processed")
        self._inflight.add(row_id)
        try:
            if not await self.resolve(v):
                return BetResult(BetStatus.REJECTED, 0, reason=v.reason)
            return await self.attempt(row_id, trigger="one_click", min_arbsize=floor, mode=mode)
        finally:
            self._inflight.discard(row_id)
            await self.emit({"type": "rows"})

    # ----- login and background behaviour ---------------------------------------
    async def ensure_login(self, book: str) -> None:
        adapter, st = self.adapters[book], self.books[book]
        browser = adapter.browser
        if browser is None:
            return
        if await browser.is_logged_in():
            st.logged_in = True
            if st.paused and st.paused.startswith("login"):
                st.paused = None
            return
        acct = self.account(book)
        if self.ledger.login_attempts_since(book, acct, time.time() - 3600) >= 3:
            st.paused = "login: 3 automatic attempts this hour; log in by hand"
            await self.emit({"type": "alert", "message": f"{book}: please log in by hand"})
            return
        creds = get_credentials(book, acct)
        if not creds:
            st.paused = "login: no saved credentials (run betbot set-credentials)"
            await self.emit({"type": "alert", "message": f"{book}: logged out and no saved credentials"})
            return
        async with browser.lock:
            outcome = await browser.login(*creds)
        self.ledger.record_login(book, acct, outcome)
        if outcome == "ok":
            st.logged_in, st.paused = True, None
            await self.emit({"type": "info", "message": f"{book}: logged back in"})
        else:
            st.logged_in = False
            st.paused = ("login: captcha/verification needs you" if outcome == "challenge"
                         else f"login: {outcome}")
            await self.emit({"type": "alert", "message": f"{book}: {st.paused}"})

    async def session_watch(self, book: str) -> None:
        adapter = self.adapters[book]
        while True:
            await asyncio.sleep(random.uniform(30, 60))
            if adapter.browser is None or adapter.browser.lock.locked():
                continue
            try:
                ok = await adapter.browser.is_logged_in()
                self.books[book].logged_in = ok
                if not ok:
                    await self.ensure_login(book)
                else:
                    self.books[book].balance = await adapter.balance()
                    if self.books[book].paused and self.books[book].paused.startswith("login"):
                        self.books[book].paused = None
            except Exception:  # noqa: BLE001
                log.exception("session check failed for %s", book)

    async def idle_loop(self, book: str) -> None:
        adapter, h = self.adapters[book], self.cfg.human
        while True:
            await asyncio.sleep(random.uniform(*h.idle_every_s))
            if not h.idle_browsing or adapter.browser is None or adapter.browser.lock.locked():
                continue
            if time.time() - self.books[book].last_bet_ts < 20 or self._inflight:
                continue
            # Idle browsing doesn't take the bet lock; a bet cancels it instead (see attempt()).
            self._idle[book] = asyncio.ensure_future(adapter.idle_action())
            try:
                await self._idle[book]
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                log.debug("idle action ended early", exc_info=True)

    def _stop_idle(self, book: str) -> None:
        t = self._idle.get(book)
        if t and not t.done():
            t.cancel()

    def state(self) -> dict[str, Any]:
        views = sorted(self.views.values(), key=lambda v: (-v.row.arbsize, v.row.id))
        return {
            "rows": [v.as_dict() for v in views if v.state != "gone" or time.time() - v.updated < 120],
            "books": {code: {"enabled": self.book_enabled(code), "paused": st.paused, "logged_in": st.logged_in,
                             "balance": st.balance, "mode": self.cfg.books[code].auto_mode,
                             "one_click_mode": self.cfg.books[code].one_click_mode}
                      for code, st in self.books.items()},
            "feeds": [{"url": u, "ok": u not in self.poller.errors, "error": self.poller.errors.get(u),
                       "last_ok": self.poller.last_ok.get(u)} for u in self.poller.urls],
            "settings": {"dry_run": self.cfg.dry_run, "auto_enabled": self.cfg.auto_enabled,
                         "stake": self.cfg.stake.default, "auto_min": self.cfg.thresholds.auto_min,
                         "borderline_min": self.cfg.thresholds.borderline_min},
            "bets": self.ledger.recent_bets(50),
        }
