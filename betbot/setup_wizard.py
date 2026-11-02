"""First-time setup: a few plain questions instead of editing config.yaml by hand.

`betbot setup` (run.bat and install.ps1 start it when there is no config.yaml yet) asks which books
this PC bets at, what to bet, how much, and the book logins, then writes config.yaml. Run it again
any time to start over; everything it sets can also be changed on the dashboard or in config.yaml.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .credentials import get_credentials, prompt_and_store

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.yaml"

BOOK_CHOICES = {"1": ["OZ"], "2": ["MyB"], "3": ["OZ", "MyB"]}
SCOPE_CHOICES = {
    "1": ([], False),             # everything in the feeds, pre-game and live
    "2": (["ATP", "WTA"], True),  # live tennis
    "3": (["ATP", "WTA"], False),  # all tennis
}


@dataclass
class Answers:
    account: str
    books: list[str] = field(default_factory=lambda: ["OZ"])
    leagues: list[str] = field(default_factory=list)
    live_only: bool = False
    stake: float | None = None       # None = max (the most the book takes)
    max_per_event: float = 500.0
    daily_cap: float = 5000.0
    one_bet_per_market: bool = True


def build_config(a: Answers, example: Path = EXAMPLE) -> Config:
    """config.yaml's settings from the answers, on top of config.example.yaml. Dry run stays on."""
    cfg = Config.load(example)
    for code, bc in cfg.books.items():
        bc.enabled = code in a.books
        bc.account = a.account
    cfg.only.leagues, cfg.only.live = list(a.leagues), a.live_only
    if a.stake is None:
        cfg.stake.mode = "max"
    else:
        cfg.stake.mode, cfg.stake.default = "fixed", a.stake
    cfg.stake.max_per_event, cfg.stake.daily_cap = a.max_per_event, a.daily_cap
    cfg.one_bet_per_market = a.one_bet_per_market
    cfg.dry_run = True
    cfg.add_new_feeds()
    return cfg


# ----- asking -------------------------------------------------------------------
def _ask(question: str, default: str) -> str:
    got = input(f"{question} [{default}]: ").strip()
    return got or default


def _choose(question: str, options: dict[str, str], default: str) -> str:
    print(f"\n{question}")
    for k, label in options.items():
        print(f"  {k}) {label}")
    while True:
        got = _ask("Type a number", default)
        if got in options:
            return got
        print(f"  Please type one of: {', '.join(options)}")


def _money(question: str, default: str, allow_max: bool = False) -> float | None:
    while True:
        got = _ask(question, default).lower().replace("$", "").replace(",", "")
        if allow_max and got == "max":
            return None
        try:
            if float(got) > 0:
                return float(got)
        except ValueError:
            pass
        print("  Please type an amount" + (", or max" if allow_max else ""))


def _yes(question: str, default: bool) -> bool:
    return _ask(question + " (y/n)", "y" if default else "n").lower().startswith("y")


def ask() -> Answers:
    default_account = (os.environ.get("COMPUTERNAME") or socket.gethostname() or "me").lower()
    print("\nBetBot setup. Press Enter to keep the answer in [brackets].\n")
    account = _ask("A name for this PC's sportsbook account (any label, e.g. lee-office)", default_account)
    books = BOOK_CHOICES[_choose("Which sportsbooks does this PC bet at?",
                                 {"1": "Ozoon only", "2": "MyBookie only", "3": "Ozoon and MyBookie"}, "1")]
    leagues, live = SCOPE_CHOICES[_choose(
        "What should it bet?",
        {"1": "Everything in the feeds (football, hockey, basketball, tennis...)",
         "2": "Live tennis only (ATP and WTA)", "3": "Tennis only, pre-game and live"}, "2")]
    stake = _money("\nStake per bet: an amount, or max for the most the book will take", "max", allow_max=True)
    per_event = _money("Most to stake on one game in total (across its spread, moneyline and total)",
                       "5000" if stake is None else "500")
    daily = _money("Most to stake in one day", "20000" if stake is None else "5000")
    one = _yes("At most one spread, one moneyline and one total per game?", True)
    return Answers(account=account, books=books, leagues=leagues, live_only=live, stake=stake,
                   max_per_event=per_event or 500, daily_cap=daily or 5000, one_bet_per_market=one)


def run(config_path: Path) -> None:
    if config_path.exists() and not _yes(f"{config_path} already exists. Replace it with new answers?", False):
        print("Kept the existing settings.")
        return
    a = ask()
    cfg = build_config(a)
    cfg.save(config_path)
    print(f"\nSaved your settings in {config_path.resolve()}.")
    for book in a.books:
        name = {"OZ": "Ozoon", "MyB": "MyBookie"}.get(book, book)
        if get_credentials(book, a.account) and not _yes(f"A {name} login is already saved. Change it?", False):
            continue
        print(f"\nYour {name} login (saved in Windows Credential Manager, not in a file; "
              "the password doesn't show as you type):")
        prompt_and_store(book, a.account)
    print("\nAll set. It starts in DRY RUN: it does everything except press Place, so you can watch first.\n"
          "When you're happy, untick 'Dry run' on the dashboard (http://127.0.0.1:8765).\n")
