"""Configuration, loaded from config.yaml (see config.example.yaml)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

PlaceMode = Literal["api", "ui"]


class Thresholds(BaseModel):
    auto_min: float = 1.0          # arbsize at or above this is placed automatically
    borderline_min: float = -3.0   # arbsize from here up to auto_min goes to the one-click queue
    one_click_tolerance: float = 1.0  # one-click bets may slip this many cents below what was shown
    max_live_age_s: float = 15.0   # live rows whose feed "date" is older than this are not bet
    max_prematch_age_s: float = 0.0  # 0 disables the age check for pre-match rows


class StakeConfig(BaseModel):
    default: float = 50.0
    round_to: float = 5.0          # stakes are rounded down to a "nice" multiple of this
    min_stake: float = 5.0
    max_per_event: float = 500.0
    daily_cap: float = 5000.0


class Pacing(BaseModel):
    min_gap_s: float = 4.0
    max_per_minute: int = 4
    max_per_hour: int = 40
    skip_marginal_pct: float = 0.0  # randomly skip this share of auto bets at exactly auto_min
    prematch_delay_s: tuple[float, float] = (0.3, 1.2)
    live_delay_s: tuple[float, float] = (0.1, 0.4)
    pause_after_rejections: int = 5  # consecutive rejections before auto mode pauses


class ActiveHours(BaseModel):
    enabled: bool = False
    start: str = "08:00"
    end: str = "23:59"
    timezone: str = "America/Vancouver"


class HumanConfig(BaseModel):
    idle_browsing: bool = True
    idle_every_s: tuple[float, float] = (45.0, 180.0)
    typing_delay_ms: tuple[int, int] = (60, 190)
    active_hours: ActiveHours = ActiveHours()


# Books the bot knows how to reach, for commands like `betbot capture MyB` before a book is configured.
# Keys are the feed's book codes.
KNOWN_BOOKS = {
    "OZ": ("https://www.ozoon.eu", "/sports"),
    "BV": ("https://www.bovada.lv", "/sports"),
    "MyB": ("https://engine.mybookie.ag", "/sports/home"),
}


class BookConfig(BaseModel):
    model_config = ConfigDict(coerce_numbers_to_str=True)  # leagues: {MLB: 1234} works unquoted

    enabled: bool = True
    adapter: str = "bodog"
    base_url: str
    start_path: str = "/sports"  # page the browser opens on
    account: str = ""             # label for this PC's account, used in the ledger and credentials
    auto_mode: PlaceMode = "api"
    one_click_mode: PlaceMode = "ui"
    leagues: dict[str, str] = Field(default_factory=dict)  # feed league -> book league path override
    stake: float | None = None    # per-book stake override
    min_request_gap_s: float = 0.75 # spacing between this book's odds lookups (avoids HTTP 429)
    catalog_refresh_s: float = 60.0
    live_catalog_refresh_s: float = 5.0
    login_url: str | None = None
    ui_selectors: dict[str, str] = Field(default_factory=dict)     # overrides for 'ui' mode
    login_selectors: dict[str, str] = Field(default_factory=dict)  # overrides for re-login


class Config(BaseModel):
    feeds: list[str] = Field(default_factory=lambda: [
        "https://arbhunterpro.com/json?nfl-pro",
        "https://arbhunterpro.com/json?ncaaf-pro",
        "https://arbhunterpro.com/json?mlb-pro",
        "https://arbhunterpro.com/json?nhl-pro",
    ])
    feed_interval_s: float = 1.0
    feed_date_tz: str = "UTC"
    dry_run: bool = True
    auto_enabled: bool = True
    thresholds: Thresholds = Thresholds()
    stake: StakeConfig = StakeConfig()
    pacing: Pacing = Pacing()
    human: HumanConfig = HumanConfig()
    books: dict[str, BookConfig] = Field(default_factory=lambda: {
        "OZ": BookConfig(base_url="https://www.ozoon.eu"),
    })
    auto_update: bool = True          # pull new versions from GitHub and restart when idle
    update_check_minutes: float = 10.0
    data_dir: str = "data"
    profile_dir: str = "profiles"
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8765
    browser_channel: str = "chrome"  # use the installed Chrome; "" for Playwright's Chromium
    browser_path: str = ""           # explicit browser executable; overrides browser_channel
    headless: bool = False
    stealth: bool = True             # drive Chrome with patchright (undetected Playwright); false = plain Playwright

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        p = Path(path)
        if not p.exists():
            return cls()
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return cls.model_validate(data)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            yaml.safe_dump(self.model_dump(mode="json"), sort_keys=False, allow_unicode=True),
            encoding="utf-8")

    def book_code(self, name: str) -> str:
        """The configured (or known) spelling of a book code typed in any case: 'MYB' -> 'MyB'."""
        for code in [*self.books, *KNOWN_BOOKS]:
            if code.upper() == name.upper():
                return code
        return name

    def stake_for(self, book: str) -> float:
        bc = self.books.get(book)
        return bc.stake if bc and bc.stake else self.stake.default
