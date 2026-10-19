"""SQLite record of decisions, bets, logins and learned league mappings."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    ts REAL, row_id TEXT, book TEXT, league TEXT, arbsize REAL,
    decision TEXT, reason TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS bets (
    ts REAL, account TEXT, book TEXT, row_id TEXT, selection_key TEXT, event_id TEXT,
    description TEXT, stake REAL, decimal REAL, arbsize REAL, mode TEXT, trigger TEXT,
    status TEXT, reference TEXT, reason TEXT, timings TEXT, dry_run INTEGER
);
CREATE INDEX IF NOT EXISTS bets_key ON bets(account, selection_key);
CREATE INDEX IF NOT EXISTS bets_event ON bets(account, book, event_id);
CREATE TABLE IF NOT EXISTS logins (ts REAL, book TEXT, account TEXT, outcome TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS league_map (
    book TEXT, feed_league TEXT, book_path TEXT, source TEXT, ts REAL,
    PRIMARY KEY (book, feed_league)
);
"""

LIVE_STATUSES = ("accepted", "pending")


class Ledger:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(SCHEMA)

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def _all(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    # decisions
    def record_decision(self, row_id: str, book: str, league: str, arbsize: float,
                        decision: str, reason: str = "", detail: Any = None) -> None:
        self._exec("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?)",
                   (time.time(), row_id, book, league, arbsize, decision, reason,
                    json.dumps(detail) if detail is not None else None))

    # bets
    def record_bet(self, *, account: str, book: str, row_id: str, selection_key: str,
                   event_id: str, description: str, stake: float, decimal: float | None,
                   arbsize: float | None, mode: str, trigger: str, status: str,
                   reference: str | None, reason: str | None, timings: dict[str, float],
                   dry_run: bool) -> None:
        self._exec("INSERT INTO bets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (time.time(), account, book, row_id, selection_key, event_id, description,
                    stake, decimal, arbsize, mode, trigger, status, reference, reason,
                    json.dumps(timings), int(dry_run)))

    def already_bet(self, account: str, selection_key: str, include_dry_run: bool = False) -> bool:
        sql = "SELECT 1 FROM bets WHERE account=? AND selection_key=? AND status IN (?,?)"
        if not include_dry_run:
            sql += " AND dry_run=0"
        return bool(self._all(sql + " LIMIT 1", (account, selection_key, *LIVE_STATUSES)))

    def event_exposure(self, account: str, book: str, event_id: str) -> float:
        r = self._all("SELECT COALESCE(SUM(stake),0) s FROM bets WHERE account=? AND book=? "
                      "AND event_id=? AND status IN (?,?) AND dry_run=0",
                      (account, book, event_id, *LIVE_STATUSES))
        return float(r[0]["s"])

    def staked_since(self, account: str, since_ts: float) -> float:
        r = self._all("SELECT COALESCE(SUM(stake),0) s FROM bets WHERE account=? AND ts>=? "
                      "AND status IN (?,?) AND dry_run=0", (account, since_ts, *LIVE_STATUSES))
        return float(r[0]["s"])

    def recent_bets(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM bets ORDER BY ts DESC LIMIT ?", (limit,))

    # logins
    def record_login(self, book: str, account: str, outcome: str, detail: str = "") -> None:
        self._exec("INSERT INTO logins VALUES (?,?,?,?,?)", (time.time(), book, account, outcome, detail))

    def login_attempts_since(self, book: str, account: str, since_ts: float) -> int:
        r = self._all("SELECT COUNT(*) n FROM logins WHERE book=? AND account=? AND ts>=? "
                      "AND outcome != 'ok_existing'", (book, account, since_ts))
        return int(r[0]["n"])

    # league map
    def get_league_path(self, book: str, feed_league: str) -> str | None:
        r = self._all("SELECT book_path FROM league_map WHERE book=? AND feed_league=?",
                      (book, feed_league))
        return r[0]["book_path"] if r else None

    def set_league_path(self, book: str, feed_league: str, path: str, source: str) -> None:
        self._exec("INSERT OR REPLACE INTO league_map VALUES (?,?,?,?,?)",
                   (book, feed_league, path, source, time.time()))
