"""Adapter for MyBookie (engine.mybookie.ag; the feed calls it MyB).

Endpoints (POST with a JSON body, relative to the book's base URL):
  lines    /sports_api/leagues-lines  {"leagueId": "79", ...}: a league's games and main markets
  options  /sports_api/bet-options    the bet slip's check: current price, limits, securityCode
  place    /sports_api/place-bet      the bet itself, carrying that securityCode
  balance  /update-balance            {"available": "$18.00", ...}; also the login check
  menu     GET /sports_api/get-menu   every league with its leagueId, for finding leagues by name

Like the site's own calls, every request carries the page's CSRF tokens (X-CSRF-TOKEN from the
page, X-XSRF-TOKEN from its cookie). Prices are American odds in each outcome's "odd";
"oddAmerican" is a different, unshaded number. Game times are in the player's time zone, whose
offset comes with every lines reply. All of this comes from a captured manual bet
(tests/fixtures/myb_capture_bet.json).
"""

from __future__ import annotations

import asyncio
import json
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
from ..browser import _visible
from ..matching.teams import normalize
from ..models import ArbRow, BetResult, BetStatus, BookEvent, Competitor, Selection
from .base import BookAdapter, LeagueNotMapped, UiNotReady, register
from .bodog import _f, parse_bettype

log = logging.getLogger(__name__)

LINES = "/sports_api/leagues-lines"
OPTIONS = "/sports_api/bet-options"
PLACE = "/sports_api/place-bet"
BALANCE = "/update-balance"
MENU = "/sports_api/get-menu"
RULES_PATH = Path(__file__).with_name("mybookie_markets.yaml")
SITE_TZ = ZoneInfo("America/New_York")  # only if a reply has no playerTimeZone
RETRYABLE = re.compile(r"odd|line|point|price|chang", re.I)

# Page selectors for 'ui' mode, from captures of the site; override per book under
# books.MyB.ui_selectors if the site changes. The page keeps one URL for every league, so the
# bot gets to a game by clicking the league in the left menu, opening its groups as needed.
DEFAULT_SELECTORS = {
    "menu_league": 'a[data-league_id="{league_id}"]',
    "menu_group": '.parent-league-menu[data-parent_id="{group_id}"]',
    "outcome": 'button.line-odds[id="{outcome_id}"]',  # its class gets "active" while in the bet slip
    "live_link": '#live_item_link',
    "live_sport": 'a[data-name="{sport}"]',
    "betslip_tab": '#bet-slip-tab',                       # "Bet Slip 1": opens the slip, shows its count
    "straight_tab": '#straight-tab a, a.bet-type-tab[data-type="straight"]',
    "stake_input": '#betSlip input.risk_input.risk_straight',
    "place_button": '#betSlip button.place-bet-btn',
    "continue": '.bet-content__confirmation__new-bet',    # "CONTINUE BETTING" after a bet
}
LIVE_SPORT_NAMES = {"Ice Hockey": "Hockey", "American Football": "Football"}

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


class _Stopped(Exception):
    """The page's place-bet request wasn't the bet the bot set up; it was blocked."""


def _json(text: Any) -> Any:
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


def menu_leagues(menu: Any) -> list[dict[str, Any]]:
    """Every league link in the site's menu (it lists most leagues more than once)."""
    out: list[dict[str, Any]] = []

    def walk(nodes: Any) -> None:
        for n in nodes if isinstance(nodes, list) else []:
            if isinstance(n, dict):
                if n.get("type") == "league_link" and n.get("league_id"):
                    out.append(n)
                walk(n.get("children"))

    walk((menu or {}).get("menu_data") if isinstance(menu, dict) else None)
    return out


def pick_league(feed_league: str, leagues: list[dict[str, Any]], names: list[str]) -> str | None:
    """leagueId of the menu league named like the feed league: 'NFL Game Odds' for NFL, 'NCAA Football'
    for NCAAF. Short names aren't used: futures like 'NFL Offensive Rookie of the Year' have short
    name 'NFL' too. Pre-game links win over live-only ones."""
    wanted = {normalize(n) for n in [feed_league, *names]}
    hits = [n for n in leagues
            if re.sub(r" (game odds|game lines|lines)$", "", normalize(n.get("display_name") or "")) in wanted]
    if not hits:
        return None
    hits.sort(key=lambda n: bool(n.get("is_live")))
    ids = [str(n["league_id"]) for n in hits if not n.get("is_live")] or [str(hits[0]["league_id"])]
    return max(set(ids), key=ids.count)


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


def accepted_price(resp: Any) -> float | None:
    """The decimal price MyBookie took, from a place-bet reply (content[0].details[0].odd)."""
    content = resp.get("content") if isinstance(resp, dict) else None
    details = (content[0].get("details") or []) if content else []
    return to_decimal(details[0].get("odd")) if details else None


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
        self._menu: list[dict[str, Any]] | None = None
        self.selectors = {**DEFAULT_SELECTORS, **(getattr(self.cfg, "ui_selectors", None) or {})}
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

    async def _menu_leagues(self) -> list[dict[str, Any]]:
        if self._menu is None:
            status, data = await self.transport.request("GET", MENU, headers=await self._headers())
            if status >= 400:
                raise RuntimeError(f"GET {MENU} -> HTTP {status}")
            self._menu = menu_leagues(data)
        return self._menu

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
        if lid := (self.rules.get("league_ids") or {}).get(feed_league.upper()):
            return str(lid)
        names = (self.rules.get("league_names") or {}).get(feed_league.upper(), [])
        lid = pick_league(feed_league, await self._menu_leagues(), names)
        if not lid:
            raise LeagueNotMapped(
                f"{self.code} has no league named {feed_league} (set books.{self.code}.leagues.{feed_league} "
                f"in config; capture mode shows it as \"leagueId\" when you open the league)")
        if self.ledger:
            self.ledger.set_league_path(self.code, feed_league, lid, "discovered")
        log.info("%s: league %s -> leagueId %s", self.code, feed_league, lid)
        return lid

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
            return BetResult(BetStatus.ACCEPTED, stake, accepted_price(resp) or price, reference=ref,
                             timings=timings, raw=resp)
        if state == "UNKNOWN":
            return BetResult(BetStatus.ERROR, stake, price, reason=reason, timings=timings, raw=resp)
        if status >= 400:
            reason = f"HTTP {status}: {reason}"
        return BetResult(BetStatus.REJECTED, stake, price, reason=reason, retryable=retry, timings=timings, raw=resp)

    # ----- betting by clicking the page like a person ----------------------
    async def place_ui(self, sel: Selection, stake: float, dry_run: bool) -> BetResult:
        """Click the outcome, type the stake, press Place, as a person would.

        The bet slip's own check (bet-options, sent by the page when the outcome is clicked) gives
        the price and limits. Anything that goes wrong before Place deselects the outcome again and
        raises UiNotReady, so nothing is left in the slip.
        """
        if not self.browser:
            raise UiNotReady("ui mode needs a browser")
        page = self.browser.page
        t0 = time.monotonic()
        timings: dict[str, float] = {}
        s = {k: v.format(outcome_id=sel.outcome_id, league_id=sel.event.league_link, group_id="{group_id}",
                         sport="{sport}") for k, v in self.selectors.items()}
        outcome = _visible(page, s["outcome"])
        try:
            await page.bring_to_front()
            await self._show_outcome(page, sel, s, outcome)
            if await self._slip_count(page, s):
                raise UiNotReady("the bet slip already has selections in it; clear it in the Chrome window")
            async with page.expect_response(
                    lambda r: r.request.method == "POST" and r.url.split("?")[0].endswith(OPTIONS),
                    timeout=10000) as info:
                await human.click(page, outcome)
            resp = await info.value
            sent, opts = _json(resp.request.post_data), await resp.json()
            ids = {o.get("id") for b in (sent or {}).get("bets") or [] for o in b.get("OutComeList") or []}
            if ids != {int(sel.outcome_id)}:
                raise UiNotReady("the bet slip has other selections in it; clear it in the Chrome window")
            timings["options_ms"] = (time.monotonic() - t0) * 1000
            content = opts.get("content") if isinstance(opts, dict) else None
            if not content or not content[0].get("selections"):
                raise UiNotReady(f"no bet slip reply (HTTP {resp.status})")
            c, sl = content[0], content[0]["selections"][0]
            if not c.get("isSuccess", True) or not (sl.get("error") or {}).get("isSuccess", True):
                reason = str(c.get("errorMessage") or (sl.get("error") or {}).get("errorMessage")
                             or c.get("errorMessageKey") or "bet slip refused the selection")
                await self._deselect(page, outcome)
                return BetResult(BetStatus.REJECTED, stake, sel.decimal, timings=timings, raw=opts,
                                 reason=f"bet slip says: {reason}", retryable=bool(RETRYABLE.search(reason)))
            price, point = to_decimal(sl.get("odd")), _f(sl.get("point"))
            moved = None
            if price is None or price < sel.decimal - 1e-6:
                moved = f"PriceChange: bet slip offers {sl.get('odd')}, quoted {sel.american}"
            elif sel.handicap is not None and (point is None or abs(point - sel.handicap) > 1e-9):
                moved = f"LineChange: bet slip has {sl.get('point')}, quoted {sel.handicap:g}"
            if moved:
                await self._deselect(page, outcome)
                return BetResult(BetStatus.REJECTED, stake, sel.decimal, retryable=True, timings=timings,
                                 raw=opts, reason=moved)
            mx = max_risk(sl)
            if mx is not None and stake > mx:
                stake = float(int(mx))
            await human.click(page, _visible(page, s["straight_tab"]), optional=True)
            stake_box = _visible(page, s["stake_input"])
            if not await stake_box.count():
                await human.click(page, _visible(page, s["betslip_tab"]), optional=True)
            await stake_box.wait_for(state="visible", timeout=6000)
            await human.type_into(page, stake_box, f"{stake:g}")
            place_btn = _visible(page, s["place_button"])
            await place_btn.wait_for(state="visible", timeout=6000)
        except UiNotReady:
            await self._deselect(page, outcome)
            raise
        except Exception as e:  # nothing has been submitted yet
            await self._deselect(page, outcome)
            raise UiNotReady(f"bet slip page element not found: {e}") from e
        timings["filled_ms"] = (time.monotonic() - t0) * 1000
        if dry_run:
            await asyncio.sleep(random.uniform(1.0, 2.0))
            await self._deselect(page, outcome)
            return BetResult(BetStatus.DRY_RUN, stake, price, timings=timings,
                             reason="stopped before Place" + (f" (max stake {mx:g})" if mx is not None else ""))
        # Last check, on the request itself as the page sends it: exactly this one bet, for exactly this
        # stake. Anything else (another selection, the stake typed into the wrong box) is stopped.
        sent: asyncio.Future = asyncio.get_running_loop().create_future()

        async def guard(route: Any) -> None:
            body = _json(route.request.post_data) or {}
            bets = body.get("Bets") or []
            details = (bets[0].get("details") or [{}]) if bets else [{}]
            ok = (len(bets) == 1 and str(bets[0].get("betId")) == sel.outcome_id
                  and abs((_f(bets[0].get("amount")) or -1.0) - stake) < 0.005
                  and _f(details[0].get("odd")) == _f(sl.get("odd")))
            if not sent.done():
                sent.set_result((ok, bets))
            if not ok:
                await route.abort()
            elif str(body.get("AcceptOddsChanges", "false")).lower() != "false":
                # the slip's "accept odds changes" setting would take any new price: send it switched off
                await route.continue_(post_data=json.dumps({**body, "AcceptOddsChanges": "false"}))
            else:
                await route.continue_()

        await page.route(f"**{PLACE}", guard)
        try:
            async with page.expect_response(
                    lambda r: r.request.method == "POST" and r.url.split("?")[0].endswith(PLACE),
                    timeout=20000) as info:
                await human.pause(0.2, 0.6)
                await human.click(page, place_btn)
                ok, bets = await asyncio.wait_for(sent, 20)
                if not ok:
                    raise _Stopped(bets)
            resp = await info.value
        except _Stopped as e:
            await self._deselect(page, outcome)
            got = [(b.get("betId"), b.get("amount"), (b.get("details") or [{}])[0].get("odd")) for b in e.args[0]]
            return BetResult(BetStatus.REJECTED, stake, price, timings=timings, retryable=True,
                             reason=f"stopped: the bet slip was about to send {got}, not {sel.outcome_id} "
                                    f"for {stake:g} at {sl.get('odd')}")
        finally:
            await page.unroute(f"**{PLACE}", guard)
        timings["post_ms"] = timings["final_ms"] = (time.monotonic() - t0) * 1000
        if resp.status in (401, 419):
            raise PermissionError("session expired")
        data = await resp.json()
        state, ref, reason, retry = interpret_place(data)
        if state == "SUCCESS":
            await human.pause(1.0, 2.5)
            await human.click(page, _visible(page, s["continue"]), optional=True)
            return BetResult(BetStatus.ACCEPTED, stake, accepted_price(data) or price, reference=ref,
                             timings=timings, raw=data)
        await self._deselect(page, outcome)
        if state == "UNKNOWN":
            return BetResult(BetStatus.ERROR, stake, price, reason=reason, timings=timings, raw=data)
        return BetResult(BetStatus.REJECTED, stake, price, reason=reason, retryable=retry, timings=timings, raw=data)

    async def _show_outcome(self, page: Any, sel: Selection, s: dict[str, str], outcome: Any) -> None:
        """Get the outcome's button on screen: its league from the menu, or the live page."""
        if await outcome.count():
            return
        if not await page.locator(s["menu_league"]).count():
            await self.browser.visit(self.cfg.base_url + self.cfg.start_path)
        await self._open_league(page, s)
        try:
            await outcome.wait_for(state="visible", timeout=8000)
            return
        except Exception:  # noqa: BLE001 - live games may only be on the live page
            if not (sel.event.live or (sel.event.raw or {}).get("isLive")):
                raise
        sport = (sel.event.raw or {}).get("sport", "")
        await human.click(page, _visible(page, s["live_link"]))
        await human.pause(0.8, 1.6)
        await human.click(page, _visible(page, s["live_sport"].format(sport=LIVE_SPORT_NAMES.get(sport, sport))))
        await outcome.wait_for(state="visible", timeout=8000)

    async def _open_league(self, page: Any, s: dict[str, str]) -> None:
        """Click the league in the left menu, first opening the groups it sits in (BASEBALL > USA > MLB)."""
        links = page.locator(s["menu_league"])
        n = await links.count()
        if not n:
            raise UiNotReady("league not in the page's menu")
        chains = []
        for i in range(n):
            groups = await links.nth(i).evaluate(
                "a => { const g = []; for (let n = a.parentElement; n; n = n.parentElement)"
                " if (n.matches('.left-menu-group[data-id]')) g.unshift(n.getAttribute('data-id')); return g; }")
            visible = await links.nth(i).is_visible()
            chains.append((not visible, len(groups), i, groups))
        _, _, i, groups = min(chains)  # a link already showing, else the one fewest groups deep
        link = links.nth(i)
        for depth, gid in enumerate(groups):
            nxt = (page.locator(s["menu_group"].format(group_id=groups[depth + 1])).first
                   if depth + 1 < len(groups) else link)
            if await nxt.is_visible():
                continue
            await human.click(page, _visible(page, s["menu_group"].format(group_id=gid)))
            await nxt.wait_for(state="visible", timeout=4000)
        await human.click(page, link)
        await human.pause(0.6, 1.4)

    async def _slip_count(self, page: Any, s: dict[str, str]) -> int:
        tab = page.locator(s["betslip_tab"]).first
        if not await tab.count():
            return 0
        m = re.search(r"\d+", await tab.inner_text())
        return int(m.group()) if m else 0

    async def _deselect(self, page: Any, outcome: Any) -> None:
        """Take the outcome out of the bet slip: clicking a selected outcome again removes it."""
        try:
            if await outcome.count() and "active" in (await outcome.get_attribute("class") or "").split():
                await human.click(page, outcome)
        except Exception:  # noqa: BLE001 - best effort
            log.warning("%s: could not clear the outcome from the bet slip", self.code)

    # ----- staying humanlike -----------------------------------------------
    async def idle_action(self) -> None:
        if not self.browser:
            return
        if random.random() < 0.3:
            await self.browser.visit(self.cfg.base_url + self.cfg.start_path)
        await human.idle_scroll(self.browser.page)
