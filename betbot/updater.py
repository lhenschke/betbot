"""Self-update from GitHub.

When BetBot runs from a git clone, it checks the remote branch now and then.
If there are new commits it waits until no bet is in progress, then exits with
RESTART_CODE; run.bat pulls the new version, installs any new packages and
starts it again. A ZIP install (no .git folder) simply reports updates as off.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .engine import Engine

log = logging.getLogger(__name__)

RESTART_CODE = 3
REPO = Path(__file__).resolve().parent.parent


@dataclass
class UpdateStatus:
    enabled: bool = False
    reason: str = ""
    behind: int = 0
    latest: str = ""
    checked_at: float | None = None
    restart_requested: bool = False
    extra: dict = field(default_factory=dict)


def _git(*args: str) -> str:
    out = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, timeout=60,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout).strip() or f"git {args[0]} failed")
    return out.stdout.strip()


class Updater:
    def __init__(self, engine: "Engine", request_exit: Callable[[], None]):
        self.engine = engine
        self.request_exit = request_exit
        self.status = UpdateStatus()
        if not (REPO / ".git").exists():
            self.status.reason = "installed from a ZIP; see README to switch to auto-updates"
        elif not shutil.which("git"):
            self.status.reason = "Git is not installed"
        else:
            self.status.enabled = True

    def check(self) -> None:
        _git("fetch", "--quiet")
        self.status.behind = int(_git("rev-list", "--count", "HEAD..@{u}") or 0)
        self.status.latest = _git("log", "-1", "--format=%s", "@{u}") if self.status.behind else ""
        self.status.checked_at = time.time()

    def safe_to_restart(self) -> bool:
        e = self.engine
        if e._inflight:
            return False
        if any(a.browser is not None and a.browser.lock.locked() for a in e.adapters.values()):
            return False
        return all(time.time() - st.last_bet_ts > 60 for st in e.books.values())

    async def restart_when_safe(self) -> None:
        self.status.restart_requested = True
        await self.engine.emit({"type": "info", "message": "Updating: restarting once no bet is in progress"})
        while not self.safe_to_restart():
            await asyncio.sleep(2)
        log.info("restarting to update (%s)", self.status.latest)
        self.request_exit()

    async def run(self) -> None:
        if not self.status.enabled:
            log.info("auto-update off: %s", self.status.reason)
            return
        while True:
            try:
                await asyncio.to_thread(self.check)
                self.status.reason = ""
            except Exception as e:  # noqa: BLE001 - no network or auth problem: try again later
                self.status.reason = f"update check failed: {e}"
                log.warning(self.status.reason)
            if self.status.behind and not self.status.restart_requested:
                await self.engine.emit({"type": "info", "message": f"Update available: {self.status.latest}"})
                if self.engine.cfg.auto_update:
                    await self.restart_when_safe()
                    return
            await asyncio.sleep(max(1.0, self.engine.cfg.update_check_minutes) * 60)
