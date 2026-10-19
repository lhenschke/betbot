"""betbot command line.

  betbot run                 start the bot (browser per enabled book + dashboard)
  betbot run --no-browser    read-only: match and quote rows, no betting
  betbot check               one pass over the feeds, print how each row matches at each book
  betbot set-credentials OZ  save a book's login in the system credential store
  betbot capture OZ          record one manual bet's requests and clicks, to confirm the payload
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import shutil
import sys
from pathlib import Path

from . import human
from .books.base import BookAdapter, adapter_class
from .config import Config
from .credentials import prompt_and_store
from .engine import Engine, RowView
from .feed import FeedPoller
from .ledger import Ledger
from .transport import HttpTransport

log = logging.getLogger("betbot")


def setup_logging(data_dir: Path, verbose: bool) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(data_dir / "betbot.log", encoding="utf-8")])
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def build_adapters(cfg: Config, ledger: Ledger, with_browser: bool) -> dict[str, BookAdapter]:
    from .browser import BrowserSession, BrowserTransport

    adapters: dict[str, BookAdapter] = {}
    for code, bc in cfg.books.items():
        if not bc.enabled:
            continue
        cls = adapter_class(bc.adapter)
        if with_browser:
            session = BrowserSession(bc.base_url, Path(cfg.profile_dir) / f"{code}-{bc.account or 'default'}",
                                     channel=cfg.browser_channel, headless=cfg.headless,
                                     login_selectors=bc.login_selectors, executable_path=cfg.browser_path,
                                     start_path=bc.start_path, stealth=cfg.stealth)
            await session.start()
            adapters[code] = cls(code, bc, BrowserTransport(session), ledger, session)
        else:
            adapters[code] = cls(code, bc, HttpTransport(bc.base_url), ledger, None)
    return adapters


def _quiet_windows_resets(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    """Windows logs a traceback whenever a browser drops a connection; that's harmless."""
    if isinstance(context.get("exception"), ConnectionResetError):
        return
    loop.default_exception_handler(context)


async def cmd_run(cfg: Config, config_path: Path, with_browser: bool) -> int:
    import uvicorn

    from .dashboard.app import create_app
    from .matching import teams
    from .updater import RESTART_CODE, Updater

    asyncio.get_running_loop().set_exception_handler(_quiet_windows_resets)
    human.configure(tuple(cfg.human.typing_delay_ms))
    teams.LOCAL_ALIAS_DIR = Path(cfg.data_dir) / "aliases"
    ledger = Ledger(Path(cfg.data_dir) / "betbot.sqlite3")
    adapters = await build_adapters(cfg, ledger, with_browser)
    poller = FeedPoller(cfg.feeds, cfg.feed_interval_s, cfg.feed_date_tz)
    engine = Engine(cfg, ledger, poller, adapters, save_config=lambda: cfg.save(config_path))
    exit_code = 0

    def request_restart() -> None:
        nonlocal exit_code
        exit_code = RESTART_CODE
        server.should_exit = True

    updater = Updater(engine, request_restart)
    tasks = [asyncio.create_task(poller.run(engine.on_feed)), asyncio.create_task(updater.run())]
    for code in adapters:
        if with_browser:
            await engine.ensure_login(code)
            tasks.append(asyncio.create_task(engine.session_watch(code)))
            tasks.append(asyncio.create_task(engine.idle_loop(code)))
    server = uvicorn.Server(uvicorn.Config(create_app(engine, updater), host=cfg.dashboard_host,
                                           port=cfg.dashboard_port, log_level="warning"))
    log.info("dashboard: http://%s:%s  (dry_run=%s)", cfg.dashboard_host, cfg.dashboard_port, cfg.dry_run)
    try:
        await server.serve()
    finally:
        for t in tasks:
            t.cancel()
        for a in adapters.values():
            if a.browser:
                await a.browser.stop()
    return exit_code


async def cmd_check(cfg: Config) -> None:
    """Read-only: one feed pass, then match and quote each row of each enabled book."""
    ledger = Ledger(Path(cfg.data_dir) / "check.sqlite3")
    adapters = await build_adapters(cfg, ledger, with_browser=False)
    poller = FeedPoller(cfg.feeds, cfg.feed_interval_s, cfg.feed_date_tz)
    engine = Engine(cfg, ledger, poller, adapters)
    for url in cfg.feeds:
        upd = await poller.poll_once(url)
        n = len(poller.rows.get(url, {}))
        print(f"{url}: {n} rows" + ("" if upd else f"  (FAILED: {poller.errors.get(url)})"))
    rows = [r for r in poller.current().values()]
    books = {r.book for r in rows}
    print(f"books in feed: {', '.join(sorted(books))}; enabled: {', '.join(adapters) or 'none'}")
    if not adapters:
        return
    # Every row is checked against every enabled book, so matching can be tested
    # even when no feed row is for that book yet.
    for code in adapters:
        print(f"\n=== {code} ===")
        for r in sorted(rows, key=lambda r: -r.arbsize):
            v = RowView(row=dataclasses.replace(r, book=code), category=engine.classify(r.arbsize))
            try:
                if await engine.resolve(v):
                    await engine.refresh_quote(v)
            except Exception as e:  # noqa: BLE001
                v.reason = f"error: {e}"
            d = v.as_dict()
            print(f"{r.arbsize:+4.0f} {r.league:6} {r.id[:44]:44} -> "
                  f"{d['selection'] or d['event'] or '-'} | now={d['live_arbsize']} {v.reason or v.state}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="betbot")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    run = sub.add_parser("run")
    run.add_argument("--no-browser", action="store_true", help="read-only, no login and no betting")
    sub.add_parser("check")
    sc = sub.add_parser("set-credentials")
    sc.add_argument("book")
    cp = sub.add_parser("capture")
    cp.add_argument("book")
    cp.add_argument("--minutes", type=float, default=10)
    sub.add_parser("init")
    args = ap.parse_args()

    config_path = Path(args.config)
    if args.cmd == "init":
        example = Path(__file__).resolve().parent.parent / "config.example.yaml"
        if config_path.exists():
            print(f"{config_path} already exists")
        else:
            shutil.copy(example, config_path)
            print(f"wrote {config_path}; edit it, then run: betbot set-credentials OZ")
        return
    cfg = Config.load(config_path)
    setup_logging(Path(cfg.data_dir), args.verbose)

    if args.cmd == "set-credentials":
        book = cfg.book_code(args.book)
        bc = cfg.books.get(book)
        prompt_and_store(book, (bc.account if bc else "") or "default")
    elif args.cmd == "check":
        asyncio.run(cmd_check(cfg))
    elif args.cmd == "capture":
        asyncio.run(cmd_capture(cfg, args.book, args.minutes))
    else:
        sys.exit(asyncio.run(cmd_run(cfg, config_path, with_browser=not getattr(args, "no_browser", False))))


async def cmd_capture(cfg: Config, book: str, minutes: float) -> None:
    from .browser import BrowserSession

    from .config import KNOWN_BOOKS

    book = cfg.book_code(book)
    bc = cfg.books.get(book)
    if bc:
        base_url, start_path, account = bc.base_url, bc.start_path, bc.account or "default"
    elif book in KNOWN_BOOKS:
        (base_url, start_path), account = KNOWN_BOOKS[book], "default"
    else:
        raise SystemExit(f"unknown book {book}; known: {', '.join(KNOWN_BOOKS)}")
    session = BrowserSession(base_url, Path(cfg.profile_dir) / f"{book}-{account}",
                             channel=cfg.browser_channel, headless=False, executable_path=cfg.browser_path,
                             start_path=start_path, stealth=cfg.stealth)
    await session.start()
    out = Path(cfg.data_dir) / f"capture-{book}.json"
    try:
        await session.capture(out, minutes * 60)
    finally:
        await session.stop()


if __name__ == "__main__":
    main()
