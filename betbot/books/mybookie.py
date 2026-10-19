"""Adapter for MyBookie (engine.mybookie.ag; the feed calls it MyB).

Endpoints (POST with a JSON body, relative to the book's base URL):
  lines    /sports_api/leagues-lines  {"leagueId": "79", ...}: a league's games and main markets
  options  /sports_api/bet-options    the bet slip's check: current price, limits, securityCode
  place    /sports_api/place-bet      the bet itself, carrying that securityCode
  balance  /update-balance            {"available": "$18.00", ...}; also the login check

Like the site's own calls, every request carries the page's CSRF tokens (X-CSRF-TOKEN from the
page, X-XSRF-TOKEN from its cookie). Prices are American odds in each outcome's "odd";
"oddAmerican" is a different, unshaded number. Game times are in the player's time zone, whose
offset comes with every lines reply. All of this comes from a captured manual bet
(tests/fixtures/myb_capture_bet.json).
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from .. import human, odds
from ..models import ArbRow, BetResult, BetStatus, BookEvent, Competitor, Selection
from .base import BookAdapter, LeagueNotMapped, UiNotReady, register
from .bodog import _f, parse_bettype

log = logging.getLogger(__name__)

LINES = "/sports_api/leagues-lines"
OPTIONS = "/sports_api/bet-options"
PLACE = "/sports_api/place-bet"
BALANCE = "/update-balance"
RULES_PATH = Path(__file__).with_name("mybookie_markets.yaml")
SITE_TZ = ZoneInfo("America/New_York")  # only if a reply has no playerTimeZone
RETRYABLE = re.compile(r"odd|line|point|price|chang", re.I)

CSRF_JS = """
() => {
  const common = (((window.axios || {}).defaults || {}).headers || {}).common || {};
  const meta = document.querySelector('meta[name="csrf-token"]');
  const m = document.cookie.match(/(?:^|;\\s*)XSRF-TOKEN=([^;]*)/);
  return {csrf: common['X-CSRF-TOKEN'] || (meta && meta.content) || null,
          xsrf: m ? decodeURIComponent(m[1]) : (common['X-XSRF-TOKEN'] || null)};
}
"""


@lru_cache(maxsize=1)
def load_rules() -> dict[str, Any]:
    return yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))


def to_decimal(odd: Any) -> float | None:
    """'-120' -> 1.8333; None (no price posted yet) or nonsense -> None."""
    a = _f(odd)
    try:
        return odds.american_to_decimal(a) if a is not None else None
    except ValueError:
        return None


def parse_money(text: Any) -> float | None:
    """'$1,018.50' -> 1018.5"""
    return _f(re.sub(r"[^\d.\-]", "", str(text or ""))) if text is not None else None


def parse_events(data: Any, book: str, league_id: str) -> list[BookEvent]:
    data = data if isinstance(data, dict) else {}
    gmt = _f((data.get("playerTimeZone") or {}).get("GMT"))
    tz = timezone(timedelta(hours=gmt)) if gmt is not None else SITE_TZ
    out: list[BookEvent] = []
    for ln in data.get("lines") or []:
        for day in ln.get("dates") or []:
            for g in day.get("games") or []:
                away, home = g.get("visitorTeam"), g.get("homeTeam")
                if not away or not home or not g.get("gameDatetime"):
                    continue
                start = datetime.fromisoformat(g["gameDatetime"]).replace(tzinfo=tz)
                out.append(BookEvent(
                    book=book, id=str(g["id"]), description=f"{away} @ {home}",
                    start_ms=int(start.timestamp() * 1000),
                    competitors=[Competitor(f"{g['id']}:visitor", away, False), Competitor(f"{g['id']}:home", home, True)],
                    live=bool(g.get("isLive")), link=league_id, league_link=league_id, raw=g))
    return out


def find_selection(event: BookEvent, game: dict[str, Any], row: ArbRow, team: Competitor | None,
                   rules: dict[str, Any] | None = None) -> tuple[Selection | None, str]:
    parsed = parse_bettype(row.bettype, rules or load_rules())
    if not parsed:
        return None, f"unsupported bettype '{row.bettype}'"
    rule, period = parsed
    want_line = {"spread": row.spread, "total": row.total, "none": None}[rule["line"]]
    if rule["line"] != "none" and want_line is None:
        return None, f"row has no {rule['line']} value"
    if rule["pick"] == "team" and team is None:
        return None, "no matched team for a team bet"
    m = (game.get("gameMarketTypes") or {}).get(rule["market"])
    if not m:
        return None, f"no {rule['market']} market"
    want_kind = ("home" if team.home else "visitor") if rule["pick"] == "team" else rule["pick"]
    seen_lines: list[str] = []
    for o in m.get("gameMarketOutComes") or []:
        if o.get("outcomeType") != want_kind:
            continue
        point = _f(o.get("point"))
        if want_line is not None:
            if point is None:
                continue
            seen_lines.append(f"{point:g}")
            if abs(point - want_line) > 1e-9:
                continue
        dec = to_decimal(o.get("odd"))
        if dec is None:
            return None, "no price posted yet"
        return Selection(
            book=event.book, event=event, market_id=str(m["id"]),
            market_description=m.get("marketDescription", ""), period=period,
            outcome_id=str(o["id"]), outcome_description=team.name if team and rule["pick"] == "team"
            else rule["pick"].capitalize(),
            price_id=str(o["odd"]), decimal=dec, american=f"{int(_f(o['odd'])):+d}",
            handicap=point if rule["line"] != "none" else None,
            is_open=bool(m.get("isActive")) and m.get("status") == 1 and o.get("active") == 1,
            raw={"game": game, "market": m, "outcome": o}), "ok"
    if seen_lines:
        return None, f"line {want_line:g} not offered (book has {', '.join(sorted(set(seen_lines)))})"
    return None, f"no {want_kind} outcome in the {rule['market']} market"


def build_options_payload(sel: Selection) -> dict[str, Any]:
    """The bet slip's check for one straight bet, as the site sends it when an outcome is clicked."""
    return {
        "bets": [{"type": "straight", "wagerType": 0, "openSpots": 0, "wagerDetailCount": 1,
                  "OutComeList": [{"id": int(sel.outcome_id), "MarketType": sel.raw["market"]["marketType"],
                                   "IsAlreadyPlacedInOpenBet": "false", "BettingFrom": 0, "BetBoostId": None}]}],
        "FillIdWager": None, "IsOnOpenBets": "false", "IncludeBetTypes": "false",
        "BetTypesRequestModel": {"listSelection": [{"lineSportId": sel.raw["game"]["sportId"]}],
                                 "fillOpenSpots": "false", "betId": None},
    }


def build_place_payload(options: dict[str, Any], sel: Selection, stake: float) -> dict[str, Any]:
    """place-bet payload exactly as the site's bet slip sends it (checked against a captured bet).

    Everything comes from the bet-options reply except the outcome's side (home/visitor/over/under),
    which only the lines reply has. AcceptOddsChanges stays "false", so a price that moved after
    the check is rejected rather than taken.
    """
    c = options["content"][0]
    s = c["selections"][0]
    point = s.get("point")
    risk: float | int = int(stake) if stake == int(stake) else stake
    return {
        "UseFreePlayAvailable": "false", "IfBetRiskAmount": 0, "MainWagerType": 0, "AcceptOddsChanges": "false",
        "SecurityCode": c["securityCode"], "IsMoneyBagPlace": "false",
        "Bets": [{
            "betId": s["outComeId"], "type": "straight", "amount": f"{stake:g}", "wagerType": 0,
            "idWagerType": c["idWagerType"], "roundRobinCombinations": 1, "riskWin": risk, "openSpots": 0,
            "FillIdWager": None, "teaserCombinations": 0,
            "details": [{
                "outComeId": s["outComeId"], "sportId": s["sportId"], "lineMarketId": s["lineMarketId"],
                "marketId": s["marketId"], "gameId": s["gameId"], "lineTournamentId": s["gameTournamentId"],
                "marketType": s["marketType"], "odd": s["odd"],
                "points": f"{point:g}" if point is not None else "0", "originalPoints": 0, "pointsPurchased": 0,
                "isLive": "true" if s.get("isLive") else "false", "pitcher": 0, "pointsPurchasedJuice": 0,
                "isAlreadyPlacedInOpenBet": "false", "sportName": s["sportName"], "bettingForm": 0,
                "outcomeType": sel.raw["outcome"]["outcomeType"], "point": point, "bettingFrom": 0,
                "providerOutComeId": s["providerOutComeId"],
            }],
        }],
    }


def max_risk(selection: dict[str, Any]) -> float | None:
    """Largest stake the bet slip allows. max_bet caps the win on favourites ($100 at -120 is a
    $120 stake, as the site shows) and the stake on underdogs; max_payout_amount caps the win."""
    lim = selection.get("limits") or {}
    mb, odd = _f(lim.get("max_bet")), _f(selection.get("odd"))
    if not mb or odd is None:
        return None
    risk = mb * (-odd / 100.0) if odd < 0 else mb
    dec, payout = to_decimal(odd), _f(lim.get("max_payout_amount"))
    if dec and payout:
        risk = min(risk, payout / (dec - 1.0))
    return risk


def interpret_place(resp: Any) -> tuple[str, str | None, str | None, bool]:
    """-> (SUCCESS|FAIL|UNKNOWN, ticket number, reason, retryable) from a place-bet reply."""
    content = resp.get("content") if isinstance(resp, dict) else None
    if not content:
        return "FAIL", None, f"unexpected reply: {str(resp)[:200]}", False
    c = content[0]
    if c.get("isSuccess") and c.get("ticketNumber"):
        return "SUCCESS", str(c["ticketNumber"]), None, False
    if c.get("isSuccess"):
        return "UNKNOWN", None, "accepted without a ticket number (check My Bets)", False
    reason = str(c.get("errorMessage") or c.get("errorMessageKey") or "rejected")
    return "FAIL", None, reason, bool(RETRYABLE.search(reason))


@register("mybookie")
class MyBookieAdapter(BookAdapter):
    def __init__(self, *a: Any, **kw: Any):
        super().__init__(*a, **kw)
        self.rules = load_rules()
        self._catalog: dict[str, tuple[float, list[BookEvent]]] = {}
        self._games: dict[str, tuple[float, dict[str, Any]]] = {}
        self._gate = asyncio.Lock()
        self._last_read = 0.0
        self._balance: tuple[float, float | None] = (0.0, None)
        if self.browser:
            # the browser's default login check reads Ozoon's page API; MyBookie has its own
            self.browser.logged_in_check = self._logged_in

    # ----- talking to the site ---------------------------------------------
    async def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json, text/plain, */*", "Cache-Control": "no-cache", "Pragma": "no-cache"}
        if self.browser:
            tok = await self.browser.evaluate(CSRF_JS) or {}
            if tok.get("csrf"):
                h["X-CSRF-TOKEN"] = tok["csrf"]
            if tok.get("xsrf"):
                h["X-XSRF-TOKEN"] = tok["xsrf"]
        return h

    async def _post(self, path: str, body: Any = None, *, read: bool = False) -> tuple[int, Any]:
        """One POST. Reads are spaced out with one polite back-off on HTTP 429; a stale CSRF token
        (HTTP 419, e.g. after logging in) reloads the page for fresh tokens and retries once."""
        for attempt in range(2):
            if read:
                async with self._gate:
                    wait = self.cfg.min_request_gap_s - (time.monotonic() - self._last_read)
                    if wait > 0:
                        await asyncio.sleep(wait)
                    self._last_read = time.monotonic()
            status, data = await self.transport.request("POST", path, body=body, headers=await self._headers())
            if attempt == 0 and status == 419 and self.browser:
                log.info("%s: CSRF token expired on %s, reloading the page", self.code, path)
                await self.browser.reload_api()
                continue
            if attempt == 0 and status == 429 and read:
                log.warning("%s: HTTP 429 on %s, backing off", self.code, path)
                await asyncio.sleep(random.uniform(2.0, 4.0))
                continue
            break
        return status, data

    async def _logged_in(self) -> bool:
        status, data = await self._post(BALANCE)
        if status == 200 and isinstance(data, dict) and "available" in data:
            self._balance = (time.monotonic(), parse_money(data["available"]))
            return True
        return False

    async def balance(self) -> float | None:
        if not self.browser:
            return None
        if time.monotonic() - self._balance[0] > 5 and not await self._logged_in():
            return None
        return self._balance[1]

    # ----- reading -------------------------------------------------------
    async def resolve_league(self, feed_league: str) -> str:
        if feed_league in self.cfg.leagues:
            return str(self.cfg.leagues[feed_league])
        if self.ledger and (lid := self.ledger.get_league_path(self.code, feed_league)):
            return lid
        lid = (self.rules.get("league_ids") or {}).get(feed_league.upper())
        if not lid:
            raise LeagueNotMapped(
                f"{self.code}: no leagueId known for {feed_league} (set books.{self.code}.leagues.{feed_league} "
                f"in config; capture mode shows it as \"leagueId\" when you open the league)")
        return str(lid)

    async def _refresh(self, league_id: str, live: bool) -> list[BookEvent]:
        """Fetch a league's games (plus its live view when live) and remember each game's markets."""
        bodies = [{"leagueId": league_id, "redirect": False, "isHome": True, "breadcrumbId": "", "doLazyLoad": False}]
        if live:
            bodies.append({"leagueId": league_id, "redirect": False, "isHome": True, "breadcrumbId": "",
                           "isLive": True})
        merged: dict[str, BookEvent] = {}
        for body in bodies:
            status, data = await self._post(LINES, body, read=True)
            if status >= 400 or not isinstance(data, dict):
                raise RuntimeError(f"POST {LINES} {league_id} -> HTTP {status}")
            for ev in parse_events(data, self.code, league_id):
                merged[ev.id] = ev  # the live view, fetched second, wins
        now = time.monotonic()
        for ev in merged.values():
            self._games[ev.id] = (now, ev.raw)
        return list(merged.values())

    async def events(self, feed_league: str, live_hint: bool = False) -> list[BookEvent]:
        league_id = await self.resolve_league(feed_league)
        ts, events = self._catalog.get(league_id, (0.0, []))
        has_live = live_hint or any(e.live for e in events)
        ttl = self.cfg.live_catalog_refresh_s if has_live else self.cfg.catalog_refresh_s
        if time.time() - ts > ttl:
            events = await self._refresh(league_id, has_live)
            self._catalog[league_id] = (time.time(), events)
        return events

    async def quote(self, event: BookEvent, row: ArbRow, team: Competitor | None,
                    max_age_s: float = 0.0) -> tuple[Selection | None, str]:
        ts, game = self._games.get(event.id, (0.0, None))
        if game is None or not max_age_s or time.monotonic() - ts > max_age_s:
            asked = time.monotonic()
            await self._refresh(event.league_link, event.live or row.is_live)
            ts, game = self._games.get(event.id, (0.0, None))
            if game is None or ts < asked:
                return None, "event no longer listed"
        return find_selection(event, game, row, team, self.rules)

    # ----- betting through the site's API, inside the page ----------------
    async def place_api(self, sel: Selection, stake: float, dry_run: bool) -> BetResult:
        t0 = time.monotonic()
        timings: dict[str, float] = {}
        if self.browser is None and dry_run:  # read-only run: show what would be sent
            return BetResult(BetStatus.DRY_RUN, stake, sel.decimal, reason="no browser (read-only)",
                             timings=timings, raw=build_options_payload(sel))
        status, opts = await self._post(OPTIONS, build_options_payload(sel))
        timings["options_ms"] = (time.monotonic() - t0) * 1000
        if status in (401, 419):
            raise PermissionError("session expired")
        content = opts.get("content") if isinstance(opts, dict) else None
        if status >= 400 or not content or not content[0].get("selections"):
            return BetResult(BetStatus.REJECTED, stake, sel.decimal, timings=timings, raw=opts,
                             reason=f"bet slip check failed (HTTP {status})")
        c, s = content[0], content[0]["selections"][0]
        if not c.get("isSuccess", True) or not (s.get("error") or {}).get("isSuccess", True):
            reason = str(c.get("errorMessage") or (s.get("error") or {}).get("errorMessage")
                         or c.get("errorMessageKey") or "bet slip refused the selection")
            return BetResult(BetStatus.REJECTED, stake, sel.decimal, timings=timings, raw=opts,
                             reason=f"bet slip says: {reason}", retryable=bool(RETRYABLE.search(reason)))
        price = to_decimal(s.get("odd"))
        if price is None or price < sel.decimal - 1e-6:
            return BetResult(BetStatus.REJECTED, stake, sel.decimal, retryable=True, timings=timings, raw=opts,
                             reason=f"PriceChange: bet slip offers {s.get('odd')}, quoted {sel.american}")
        point = _f(s.get("point"))
        if sel.handicap is not None and (point is None or abs(point - sel.handicap) > 1e-9):
            return BetResult(BetStatus.REJECTED, stake, sel.decimal, retryable=True, timings=timings, raw=opts,
                             reason=f"LineChange: bet slip has {s.get('point')}, quoted {sel.handicap:g}")
        mx = max_risk(s)
        if mx is not None and stake > mx:
            stake = float(int(mx))
        payload = build_place_payload(opts, sel, stake)
        if dry_run:
            return BetResult(BetStatus.DRY_RUN, stake, price, timings=timings, raw=payload,
                             reason=f"max stake {mx:g}" if mx is not None else "max stake unknown")
        status, resp = await self._post(PLACE, payload)
        timings["post_ms"] = timings["final_ms"] = (time.monotonic() - t0) * 1000
        if status in (401, 419):
            raise PermissionError("session expired")
        state, ref, reason, retry = interpret_place(resp)
        if state == "SUCCESS":
            return BetResult(BetStatus.ACCEPTED, stake, price, reference=ref, timings=timings, raw=resp)
        if state == "UNKNOWN":
            return BetResult(BetStatus.ERROR, stake, price, reason=reason, timings=timings, raw=resp)
        if status >= 400:
            reason = f"HTTP {status}: {reason}"
        return BetResult(BetStatus.REJECTED, stake, price, reason=reason, retryable=retry, timings=timings, raw=resp)

    async def place_ui(self, sel: Selection, stake: float, dry_run: bool) -> BetResult:
        # The page keeps one URL for every league and the capture didn't record what identifies an
        # outcome button, so clicking isn't set up yet; the engine places through the API instead.
        raise UiNotReady("MyBookie bet slip clicking isn't set up yet")

    # ----- staying humanlike -----------------------------------------------
    async def idle_action(self) -> None:
        if not self.browser:
            return
        if random.random() < 0.3:
            await self.browser.visit(self.cfg.base_url + self.cfg.start_path)
        await human.idle_scroll(self.browser.page)
