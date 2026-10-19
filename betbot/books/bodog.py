"""Adapter for Bovada-platform books (Ozoon now; Bovada later via base_url).

Endpoints (all relative to the book's base URL):
  nav       GET  /services/sports/event/v2/nav/A/description{path}
  coupon    GET  /services/sports/event/coupon/events/A/description{path}?marketFilterId=def&lang=en
  event     GET  /services/sports/event/coupon/events/A/description{event.link}?lang=en
  suggest   GET  /services/sports/bet/betslip?outcomeId=A:{outcomeId}:1
  maxStake  POST /services/sports/bet/betslip/maxStake
  place     POST /services/sports/bet/betslip           (X-Session-Id, X-Idempotency-Key)
  status    GET  /services/sports/bet/betslip/{key}     (polled through the live delay)

The bet payload mirrors what the site's own bet slip sends. Capture mode
(betbot capture) records a real manual bet so this can be checked.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .. import human
from ..models import ArbRow, BetResult, BetStatus, BookEvent, Competitor, Selection
from ..matching.teams import normalize
from .base import BookAdapter, LeagueNotMapped, UiNotReady, register

log = logging.getLogger(__name__)

SVC = "/services/sports"
RULES_PATH = Path(__file__).with_name("bodog_markets.yaml")
RETRYABLE_CODES = ("PriceChange", "TotalChange", "SpreadChange")

# Page selectors for 'ui' mode. Defaults come from the site's bet-slip code;
# override per book under books.<CODE>.ui_selectors if the site changes.
DEFAULT_SELECTORS = {
    "outcome": '[data-testid="outcome-container-{outcome_id}"]',
    "betslip": '[data-testid="betslip-container"]',
    "single_card": '[data-testid="betslip-collapsible-{outcome_id}"]',
    "stake_input": '[data-testid="betslip-container"] .ol-text__externalContainer:has-text("Stake") input',
    "place_button": '[data-testid="betslip-container"] button:has-text("Place Bet")',
    "confirmation": '[data-testid="bet-slip-single-tab-confirmation-bet"]',
    "clear_all": '[data-testid="clear-all-button"]',
}


@lru_cache(maxsize=1)
def load_rules() -> dict[str, Any]:
    return yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))


def parse_bettype(bettype: str, rules: dict[str, Any] | None = None) -> tuple[dict[str, Any], str] | None:
    """'ATS' -> (rule, 'G'); '1H ATS' -> (rule, '1H'); unknown -> None."""
    rules = rules or load_rules()
    tokens = [t for t in re.split(r"[\s_]+", bettype.strip().upper()) if t]
    periods = {str(k).upper(): v for k, v in rules["periods"].items()}
    period = periods[""]
    rest = []
    for t in tokens:
        if t in periods and t not in rules["bettypes"]:
            period = periods[t]
        else:
            rest.append(t)
    key = "".join(rest)
    rule = rules["bettypes"].get(key)
    return (rule, period) if rule else None


def parse_events(data: Any, book: str, league_link: str) -> list[BookEvent]:
    out: list[BookEvent] = []
    for grp in data if isinstance(data, list) else []:
        for ev in grp.get("events", []):
            comps = [Competitor(id=str(c["id"]), name=c["name"], home=bool(c.get("home")))
                     for c in ev.get("competitors", []) if c.get("name")]
            out.append(BookEvent(book=book, id=str(ev["id"]), description=ev.get("description", ""),
                                 start_ms=int(ev.get("startTime") or 0), competitors=comps,
                                 live=bool(ev.get("live")), link=ev.get("link", ""),
                                 league_link=league_link, raw=ev))
    return out


def _f(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def find_selection(event: BookEvent, ev_json: dict[str, Any], row: ArbRow,
                   team: Competitor | None, rules: dict[str, Any] | None = None) -> tuple[Selection | None, str]:
    parsed = parse_bettype(row.bettype, rules)
    if not parsed:
        return None, f"unsupported bettype '{row.bettype}'"
    rule, period = parsed
    want_line = {"spread": row.spread, "total": row.total, "none": None}[rule["line"]]
    if rule["line"] != "none" and want_line is None:
        return None, f"row has no {rule['line']} value"
    if rule["pick"] == "team" and team is None:
        return None, "no matched team for a team bet"
    comp_names = [normalize(c.name) for c in event.competitors]
    seen_lines: list[str] = []

    for dg in ev_json.get("displayGroups", []):
        for m in dg.get("markets", []):
            if m.get("key") != rule["market_key"]:
                continue
            if (m.get("period") or {}).get("abbreviation") != period:
                continue
            desc = m.get("description", "")
            if rule["pick"] in ("over", "under"):
                nd = normalize(desc)
                if " - " in desc or any(n and n in nd for n in comp_names):
                    continue  # team totals
            for o in m.get("outcomes", []):
                price = o.get("price") or {}
                if rule["pick"] == "team":
                    if o.get("competitorId") != team.id and normalize(o.get("description", "")) != normalize(team.name):
                        continue
                elif o.get("type") != {"over": "O", "under": "U"}[rule["pick"]]:
                    continue
                hcap = _f(price.get("handicap"))
                if want_line is not None:
                    if hcap is None:
                        continue
                    seen_lines.append(price.get("handicap"))
                    if abs(hcap - want_line) > 1e-9:
                        continue
                dec = _f(price.get("decimal"))
                if not dec or not price.get("id"):
                    continue
                is_open = m.get("status") == "O" and o.get("status") == "O"
                return Selection(
                    book=event.book, event=event, market_id=str(m["id"]),
                    market_description=f"{dg.get('description', '')} / {desc}", period=period,
                    outcome_id=str(o["id"]), outcome_description=o.get("description", ""),
                    price_id=str(price["id"]), decimal=dec, american=str(price.get("american", "")),
                    handicap=hcap, is_open=is_open, raw={"market": m, "outcome": o}), "ok"
    if seen_lines:
        return None, f"line {want_line:g} not offered (book has {', '.join(sorted(set(seen_lines)))})"
    return None, f"no {rule['market_key']} {period} market"


def suggestion_price(suggestion: dict[str, Any] | None, outcome_id: str) -> float | None:
    """The price the bet slip would actually take, from the site's own bet-slip check."""
    for sel in ((suggestion or {}).get("selections") or {}).get("selection", []):
        if str(sel.get("outcomeId")) == outcome_id:
            return _f(sel.get("price"))
    return None


def build_bet_payload(sel: Selection, stake: float, suggestion: dict[str, Any] | None = None,
                      odds_format: str = "AMERICAN", for_max_stake: bool = False) -> dict[str, Any]:
    """Single-bet payload exactly as the site's bet slip sends it (checked against a captured bet).

    priceId and lineGroup come from the bet-slip check (GET .../betslip?outcomeId=...) when there is
    one. The max-stake request is the same payload with no stake and specifyingRisk false.
    """
    price_id, version = sel.price_id, None
    for s in ((suggestion or {}).get("selections") or {}).get("selection", []):
        if str(s.get("outcomeId")) == sel.outcome_id:
            price_id = str(s.get("priceId") or price_id)
            version = s.get("version")
    cents = 0 if for_max_stake else int(round(stake * 100))
    payload: dict[str, Any] = {
        "channel": "WEB_BS",
        "device": "DESKTOP",
        "bets": {"bet": [{
            "betGroups": {"groupId": [0]}, "betType": "SINGLE", "isBox": False,
            "oddsFormat": odds_format, "specifyingRisk": not for_max_stake, "stakePerLine": cents,
            "potentialWin": int(round(cents * (sel.decimal - 1.0))),
        }]},
        "groups": {"group": [{
            "id": 0, "type": "STRAIGHT",
            "groupSelections": [{"groupSelection": [{"selectionId": 0, "order": 0, "isBanker": False}]}],
        }]},
        "selections": {"selection": [{
            "outcomeId": sel.outcome_id, "id": 0, "system": "A", "priceId": price_id,
            "oddsFormat": odds_format, **({"version": version} if version is not None else {}),
        }]},
    }
    line_group = (suggestion or {}).get("lineGroup")
    if line_group is not None:
        payload["lineGroup"] = line_group
    return payload


def _errors(resp: Any) -> list[dict[str, Any]]:
    if not isinstance(resp, dict):
        return []
    errs = list(resp.get("error") or [])
    for b in (resp.get("bets") or {}).get("bet", []):
        errs += list(((b.get("response") or {}).get("error")) or [])
    return errs


def interpret_result(resp: Any) -> tuple[str, str | None, str | None, bool]:
    """-> (SUCCESS|FAIL|PENDING, reference, reason, retryable) from a placement/status response."""
    if not isinstance(resp, dict):
        return "FAIL", None, "empty response", False
    bets = (resp.get("bets") or {}).get("bet", [])
    statuses = [((b.get("response") or {}).get("status")) for b in bets]
    top = resp.get("status")
    errs = _errors(resp)
    reason = "; ".join(str(e.get("code") or e.get("message")) for e in errs) or None
    retryable = any(any(c in str(e.get("code", "")) for c in RETRYABLE_CODES) for e in errs)
    if statuses and all(s == "SUCCESS" for s in statuses):
        return "SUCCESS", bets[0].get("betRef"), None, False
    if any(s in ("FAIL", "REJECTED") for s in statuses) or top in ("FAIL", "REJECTED", "ERROR"):
        return "FAIL", None, reason or top, retryable
    if top in ("PLACING", "PROCESSING", "REFERRING") or any(s in ("PLACING", "REFERRING") for s in statuses):
        return "PENDING", None, None, False
    if errs:
        return "FAIL", None, reason, retryable
    return "PENDING", None, None, False


@register("bodog")
class BodogAdapter(BookAdapter):
    def __init__(self, *a: Any, **kw: Any):
        super().__init__(*a, **kw)
        self.rules = load_rules()
        self._catalog: dict[str, tuple[float, list[BookEvent]]] = {}
        self._nav: list[dict[str, Any]] | None = None
        self.selectors = {**DEFAULT_SELECTORS, **(getattr(self.cfg, "ui_selectors", None) or {})}
        self._gate = asyncio.Lock()
        self._last_read = 0.0
        self._event_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}

    # ----- reading -------------------------------------------------------
    async def _get(self, path: str) -> Any:
        """Read-only odds/nav requests: spaced out, with one polite back-off on HTTP 429."""
        for attempt in range(2):
            async with self._gate:
                wait = self.cfg.min_request_gap_s - (time.monotonic() - self._last_read)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_read = time.monotonic()
            status, data = await self.transport.request("GET", path)
            if status == 429 and attempt == 0:
                log.warning("%s: HTTP 429 on %s, backing off", self.code, path.split("?")[0])
                async with self._gate:
                    await asyncio.sleep(random.uniform(2.0, 4.0))
                    self._last_read = time.monotonic()
                continue
            break
        if status >= 400:
            raise RuntimeError(f"GET {path} -> HTTP {status}")
        return data

    async def _nav_leagues(self) -> list[dict[str, Any]]:
        if self._nav is not None:
            return self._nav
        leagues: list[dict[str, Any]] = []
        root = await self._get(f"{SVC}/event/v2/nav/A/description?lang=en")
        for sport in root.get("children") or []:
            try:
                sub = await self._get(f"{SVC}/event/v2/nav/A/description{sport['link']}?lang=en")
            except Exception as e:  # noqa: BLE001 - one bad sport shouldn't stop discovery
                log.warning("nav %s failed: %s", sport.get("link"), e)
                continue
            leagues += [c for c in sub.get("children") or [] if c.get("type") == "LEAGUE"]
        self._nav = leagues
        return leagues

    @staticmethod
    def pick_league(feed_league: str, nav_leagues: list[dict[str, Any]], names: list[str]) -> str | None:
        wanted = {normalize(n) for n in [feed_league, *names]}
        hits = [c for c in nav_leagues if normalize(c.get("description", "")) in wanted]
        if not hits:
            return None
        return max(hits, key=lambda c: c.get("numEvents") or 0)["link"]

    async def resolve_league(self, feed_league: str) -> str:
        if feed_league in self.cfg.leagues:
            return self.cfg.leagues[feed_league]
        if self.ledger and (p := self.ledger.get_league_path(self.code, feed_league)):
            return p
        names = (self.rules.get("league_names") or {}).get(feed_league.upper(), [])
        path = self.pick_league(feed_league, await self._nav_leagues(), names)
        if not path:
            raise LeagueNotMapped(f"{self.code} has no league named {feed_league} "
                                  f"(set books.{self.code}.leagues.{feed_league} in config)")
        if self.ledger:
            self.ledger.set_league_path(self.code, feed_league, path, "discovered")
        log.info("%s: league %s -> %s", self.code, feed_league, path)
        return path

    async def events(self, feed_league: str, live_hint: bool = False) -> list[BookEvent]:
        path = await self.resolve_league(feed_league)
        ts, events = self._catalog.get(path, (0.0, []))
        has_live = live_hint or any(e.live for e in events)
        ttl = self.cfg.live_catalog_refresh_s if has_live else self.cfg.catalog_refresh_s
        if time.time() - ts > ttl:
            data = await self._get(f"{SVC}/event/coupon/events/A/description{path}?marketFilterId=def&lang=en")
            events = parse_events(data, self.code, path)
            self._catalog[path] = (time.time(), events)
        return events

    async def event_json(self, event: BookEvent, max_age_s: float = 0.0) -> dict[str, Any] | None:
        ts, cached = self._event_cache.get(event.id, (0.0, None))
        if max_age_s and time.monotonic() - ts <= max_age_s:
            return cached
        data = await self._get(f"{SVC}/event/coupon/events/A/description{event.link}?lang=en")
        found = None
        for grp in data if isinstance(data, list) else []:
            for ev in grp.get("events", []):
                if str(ev.get("id")) == event.id:
                    found = ev
        self._event_cache[event.id] = (time.monotonic(), found)
        return found

    async def quote(self, event: BookEvent, row: ArbRow, team: Competitor | None,
                    max_age_s: float = 0.0) -> tuple[Selection | None, str]:
        ev = await self.event_json(event, max_age_s)
        if ev is None:
            return None, "event no longer listed"
        return find_selection(event, ev, row, team, self.rules)

    # ----- betting through the site's API, inside the page ----------------
    async def _session_headers(self) -> dict[str, str]:
        sid = await self.browser.session_id() if self.browser else None
        if not sid:
            raise PermissionError("not logged in")
        # the site sends the session in both headers on every bet-slip call
        return {"Authorization": f"Bearer {sid}", "X-Session-Id": f"Bearer {sid}",
                "Content-Type": "application/json", "Accept": "application/json, text/plain, */*"}

    async def suggestion(self, sel: Selection, headers: dict[str, str]) -> dict[str, Any] | None:
        status, data = await self.transport.request(
            "GET", f"{SVC}/bet/betslip?outcomeId=A:{sel.outcome_id}:1", headers=headers)
        if status >= 400 or not isinstance(data, dict):
            return None
        return data

    async def max_stake(self, sel: Selection, suggestion: dict[str, Any] | None,
                        headers: dict[str, str]) -> float | None:
        """Largest stake the book will take, in account currency (the reply is {"maxStake": 1056.0})."""
        status, data = await self.transport.request(
            "POST", f"{SVC}/bet/betslip/maxStake", body=build_bet_payload(sel, 0, suggestion, for_max_stake=True),
            headers=headers)
        if status >= 400 or not isinstance(data, dict):
            return None
        v = _f(data.get("maxStake"))
        return v if v and v > 0 else None

    async def place_api(self, sel: Selection, stake: float, dry_run: bool) -> BetResult:
        t0 = time.monotonic()
        timings: dict[str, float] = {}
        if self.browser is None and dry_run:  # read-only run: show what would be sent
            return BetResult(BetStatus.DRY_RUN, stake, sel.decimal, reason="no browser (read-only)",
                             timings=timings, raw=build_bet_payload(sel, stake))
        headers = await self._session_headers()
        if self.browser:
            await self.browser.show_event(self.cfg.base_url, sel.event.link)
        sug = await self.suggestion(sel, headers)
        timings["suggest_ms"] = (time.monotonic() - t0) * 1000
        if sug is not None and sug.get("status") not in (None, "AVAILABLE"):
            return BetResult(BetStatus.REJECTED, stake, sel.decimal, reason=f"bet slip says {sug.get('status')}",
                             timings=timings, raw=sug)
        sug_price = suggestion_price(sug, sel.outcome_id)
        if sug_price is not None and sug_price < sel.decimal - 1e-6:
            return BetResult(BetStatus.REJECTED, stake, sel.decimal, retryable=True, timings=timings, raw=sug,
                             reason=f"PriceChange: bet slip offers {sug_price:g}, quoted {sel.decimal:g}")
        mx = await self.max_stake(sel, sug, headers)
        if mx is not None and stake > mx:
            stake = float(int(mx))
        payload = build_bet_payload(sel, stake, sug)
        timings["maxstake_ms"] = (time.monotonic() - t0) * 1000
        if dry_run:
            return BetResult(BetStatus.DRY_RUN, stake, sel.decimal, reason=f"max stake {mx}",
                             timings=timings, raw=payload)
        headers = {**headers, "X-Idempotency-Key": str(uuid.uuid4())}
        status, resp = await self.transport.request("POST", f"{SVC}/bet/betslip", body=payload, headers=headers)
        timings["post_ms"] = (time.monotonic() - t0) * 1000
        if status == 401:
            raise PermissionError("session expired")
        result = await self._await_result(resp, headers, timings, t0)
        result.stake = stake
        result.decimal = sel.decimal
        return result

    async def _await_result(self, resp: Any, headers: dict[str, str], timings: dict[str, float],
                            t0: float, timeout_s: float = 45.0) -> BetResult:
        key = resp.get("key") if isinstance(resp, dict) else None
        state, ref, reason, retry = interpret_result(resp)
        deadline = time.monotonic() + timeout_s
        while state == "PENDING" and key and time.monotonic() < deadline:
            await asyncio.sleep(0.8)
            _, resp = await self.transport.request(
                "GET", f"{SVC}/bet/betslip/{key}",
                headers={k: v for k, v in headers.items() if k != "X-Idempotency-Key"})
            state, ref, reason, retry = interpret_result(resp)
        timings["final_ms"] = (time.monotonic() - t0) * 1000
        if state == "SUCCESS":
            return BetResult(BetStatus.ACCEPTED, 0, reference=ref, timings=timings, raw=resp)
        if state == "PENDING":
            return BetResult(BetStatus.ERROR, 0, reason="no final status (check My Bets)", timings=timings, raw=resp)
        return BetResult(BetStatus.REJECTED, 0, reason=reason, retryable=retry, timings=timings, raw=resp)

    # ----- betting by clicking the page like a person ----------------------
    async def place_ui(self, sel: Selection, stake: float, dry_run: bool) -> BetResult:
        if not self.browser:
            raise UiNotReady("ui mode needs a browser")
        page = self.browser.page
        t0 = time.monotonic()
        timings: dict[str, float] = {}
        s = {k: v.format(outcome_id=sel.outcome_id) for k, v in self.selectors.items()}
        try:
            await self.browser.show_event(self.cfg.base_url, sel.event.link, force=True)
            await self.browser.accept_cookies()
            await human.click(page, page.locator(s["clear_all"]).first, optional=True)
            outcome = page.locator(s["outcome"]).first
            await outcome.wait_for(state="visible", timeout=8000)
            await human.click(page, outcome)
            stake_box = page.locator(s["stake_input"]).first
            await stake_box.wait_for(state="visible", timeout=8000)
            await human.type_into(page, stake_box, f"{stake:g}")
        except Exception as e:  # nothing has been submitted yet
            await human.click(page, page.locator(s["clear_all"]).first, optional=True)
            raise UiNotReady(f"bet slip page element not found: {e}") from e
        timings["filled_ms"] = (time.monotonic() - t0) * 1000
        if dry_run:
            await asyncio.sleep(random.uniform(1.0, 2.0))
            await human.click(page, page.locator(s["clear_all"]).first, optional=True)
            return BetResult(BetStatus.DRY_RUN, stake, sel.decimal, reason="stopped before Place",
                             timings=timings)
        async with page.expect_response(
                lambda r: r.request.method == "POST" and r.url.rstrip("/").endswith("/bet/betslip"),
                timeout=15000) as resp_info:
            await human.click(page, page.locator(s["place_button"]).first)
        resp = await (await resp_info.value).json()
        timings["post_ms"] = (time.monotonic() - t0) * 1000
        headers = await self._session_headers()
        result = await self._await_result(resp, headers, timings, t0)
        result.stake, result.decimal = stake, sel.decimal
        return result

    # ----- staying humanlike -----------------------------------------------
    async def idle_action(self) -> None:
        if not self.browser:
            return
        paths = list(self._catalog) or ["/football/nfl"]
        choice = random.random()
        if choice < 0.55:
            await self.browser.visit(self.cfg.base_url + "/sports" + random.choice(paths))
        elif choice < 0.75:
            await self.browser.visit(self.cfg.base_url + "/sports/live")
        elif choice < 0.85:
            await self.browser.visit(self.cfg.base_url + "/sports")
        await human.idle_scroll(self.browser.page)

    async def balance(self) -> float | None:
        if not self.browser:
            return None
        return await self.browser.balance()
