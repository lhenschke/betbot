"""Local dashboard: live rows, one-click queue, settings, history."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from ..engine import Engine
from ..matching import teams
from ..updater import Updater

STATIC = Path(__file__).parent / "static"


def create_app(engine: Engine, updater: "Updater | None" = None) -> FastAPI:
    app = FastAPI(title="betbot")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/state")
    async def state() -> JSONResponse:
        st = engine.state()
        if updater:
            u = updater.status
            st["update"] = {"enabled": u.enabled, "reason": u.reason, "behind": u.behind, "latest": u.latest,
                            "restarting": u.restart_requested, "auto": engine.cfg.auto_update}
        return JSONResponse(st)

    @app.post("/api/update")
    async def update() -> JSONResponse:
        """Check GitHub now; if there's a new version, restart into it once no bet is in progress."""
        if not updater or not updater.status.enabled:
            return JSONResponse({"ok": False, "reason": updater.status.reason if updater else "no updater"})
        await asyncio.to_thread(updater.check)
        if updater.status.behind and not updater.status.restart_requested:
            asyncio.ensure_future(updater.restart_when_safe())
            return JSONResponse({"ok": True, "reason": f"updating to: {updater.status.latest}"})
        return JSONResponse({"ok": True, "reason": "already up to date"})

    @app.post("/api/place")
    async def place(body: dict[str, Any]) -> JSONResponse:
        res = await engine.one_click(str(body["row_id"]), mode=body.get("mode"))
        return JSONResponse({"status": res.status.value, "reason": res.reason, "stake": res.stake,
                             "reference": res.reference})

    @app.post("/api/dismiss")
    async def dismiss(body: dict[str, Any]) -> JSONResponse:
        v = engine.views.get(str(body["row_id"]))
        if v:
            v.state, v.reason = "skipped", "dismissed"
        await engine.emit({"type": "rows"})
        return JSONResponse({"ok": True})

    @app.post("/api/settings")
    async def settings(body: dict[str, Any]) -> JSONResponse:
        cfg = engine.cfg
        if "dry_run" in body:
            cfg.dry_run = bool(body["dry_run"])
        if "auto_enabled" in body:
            cfg.auto_enabled = bool(body["auto_enabled"])
        if "stake" in body:
            cfg.stake.default = float(body["stake"])
        if "auto_min" in body:
            cfg.thresholds.auto_min = float(body["auto_min"])
        if "borderline_min" in body:
            cfg.thresholds.borderline_min = float(body["borderline_min"])
        if "feeds" in body:
            cfg.feeds = [u.strip() for u in body["feeds"] if u.strip()]
            engine.poller.set_urls(cfg.feeds)
        for code, b in (body.get("books") or {}).items():
            bc = cfg.books.get(code)
            if not bc:
                continue
            for k in ("enabled",):
                if k in b:
                    setattr(bc, k, bool(b[k]))
            for k in ("auto_mode", "one_click_mode"):
                if b.get(k) in ("api", "ui"):
                    setattr(bc, k, b[k])
            if b.get("unpause"):
                engine.books[code].paused = None
                engine.books[code].rejections_in_row = 0
        for v in engine.views.values():
            v.category = engine.classify(v.row.arbsize)
        engine.save_config()
        await engine.emit({"type": "rows"})
        return JSONResponse(engine.state()["settings"])

    @app.post("/api/kill")
    async def kill() -> JSONResponse:
        engine.cfg.auto_enabled = False
        engine.save_config()
        await engine.emit({"type": "alert", "message": "Auto betting switched OFF"})
        return JSONResponse({"auto_enabled": False})

    @app.post("/api/alias")
    async def alias(body: dict[str, Any]) -> JSONResponse:
        """Teach the matcher that `variant` is the same team as `canonical` in `league`."""
        league, canonical, variant = body["league"].upper(), body["canonical"].strip(), body["variant"].strip()
        teams.LOCAL_ALIAS_DIR.mkdir(parents=True, exist_ok=True)
        p = teams.LOCAL_ALIAS_DIR / f"{league}.yaml"
        data = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
        data = data or {}
        data.setdefault(canonical, [])
        if variant not in data[canonical]:
            data[canonical].append(variant)
        p.write_text(yaml.safe_dump(data, sort_keys=True, allow_unicode=True), encoding="utf-8")
        teams.reload_aliases()
        for v in engine.views.values():
            if v.row.league.upper() == league and v.state == "unmatched":
                v.state = "new"
                asyncio.ensure_future(engine.process(v.row.id))
        return JSONResponse({"ok": True})

    @app.post("/api/league")
    async def league(body: dict[str, Any]) -> JSONResponse:
        """Map a feed league to a book league path by hand, e.g. NCAAF -> /football/college-football."""
        code, feed_league, path = body["book"], body["feed_league"].upper(), body["path"].strip()
        engine.cfg.books[code].leagues[feed_league] = path
        engine.save_config()
        for v in engine.views.values():
            if v.row.book == code and v.row.league.upper() == feed_league and v.state == "league_not_mapped":
                v.state = "new"
                asyncio.ensure_future(engine.process(v.row.id))
        return JSONResponse({"ok": True})

    @app.websocket("/ws")
    async def ws(sock: WebSocket) -> None:
        await sock.accept()

        async def push(msg: dict[str, Any]) -> None:
            await sock.send_text(json.dumps(msg, default=str))

        engine.listeners.append(push)
        try:
            while True:
                await sock.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            if push in engine.listeners:
                engine.listeners.remove(push)

    return app
