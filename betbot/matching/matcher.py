"""Generic feed-row -> book-event matcher, shared by every book and league.

It narrows by start time, scores team names, and accepts only a clear winner.
Anything doubtful comes back unmatched with a reason; it never guesses.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import ArbRow, BookEvent, Competitor
from .teams import Aliases, abbr_score, team_score

TEAM_MIN = 85.0
MARGIN = 8.0


@dataclass
class MatchResult:
    ok: bool
    reason: str
    event: BookEvent | None = None
    team: Competitor | None = None      # the competitor named by sq_team
    score: float = 0.0


def match_row(row: ArbRow, events: list[BookEvent], aliases: Aliases | None = None,
              window_min: float = 90.0) -> MatchResult:
    aliases = aliases or Aliases.for_league(row.league)
    window_ms = window_min * 60_000
    scored: list[tuple[float, float, BookEvent, Competitor]] = []
    in_window = 0
    for ev in events:
        if len(ev.competitors) != 2:
            continue  # specials like "Away Goals @ Home Goals" have no real teams
        dt = abs(ev.start_ms - row.start_ms) if row.start_ms else 0
        if dt > window_ms:
            continue
        in_window += 1
        best = max(ev.competitors, key=lambda c: team_score(row.sq_team, c.name, aliases))
        ts = team_score(row.sq_team, best.name, aliases)
        if ts < TEAM_MIN:
            continue
        opp = next(c for c in ev.competitors if c is not best)
        os = abbr_score(row.pinn_team, opp.name, aliases)
        if os == 0.0:
            continue  # the alias table says the opponent is a different team
        time_bonus = 5.0 * (1 - dt / window_ms) if window_ms else 0.0
        scored.append((ts + 0.2 * os + time_bonus, ts, ev, best))

    if not scored:
        if in_window == 0:
            return MatchResult(False, "no book event near this start time")
        return MatchResult(False, f"no team matching '{row.sq_team}' among {in_window} events near this time")
    scored.sort(key=lambda s: s[0], reverse=True)
    top = scored[0]
    if len(scored) > 1 and top[0] - scored[1][0] < MARGIN:
        names = ", ".join(s[2].description for s in scored[:3])
        return MatchResult(False, f"ambiguous: {names}")
    return MatchResult(True, "matched", top[2], top[3], top[1])
