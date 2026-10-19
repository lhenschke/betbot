"""The real, logged-in Chrome window the bot works through.

Two tabs share one Chrome profile (cookies, login):
  page      the visible tab: shows the event being bet, receives clicks in 'ui' mode,
            and does the idle browsing.
  api_page  a background tab on the same site that runs the site's own API calls with
            fetch(), so they carry the real cookies, origin and TLS fingerprint and
            never get cut off by the visible tab navigating.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from . import human

log = logging.getLogger(__name__)

STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    "--start-maximized",
]

DEFAULT_LOGIN_SELECTORS = {
    "open_login": 'button:has-text("Log In"), a:has-text("Log In"), button:has-text("Login"), '
                  'a:has-text("Login"), button:has-text("Sign In"), a:has-text("Sign In")',
    "email": 'input[type="email"], input[autocomplete="username"], input[name*="email" i], '
             'input[id*="email" i], input[name*="user" i]',
    "password": 'input[type="password"]',
    "submit": 'form button[type="submit"], button:has-text("Log In"), button:has-text("Login")',
    "remember": 'input[type="checkbox"][name*="remember" i], input[type="checkbox"][id*="remember" i]',
    "cookies": 'button:has-text("Accept All"), button:has-text("ACCEPT ALL")',
    "login_path": "/login",
    "challenge": 'iframe[src*="captcha"], iframe[src*="recaptcha"], iframe[src*="hcaptcha"], '
                 'iframe[src*="turnstile"]',
    "challenge_text": r"verification code|verify your|one-time code|security code|captcha",
}

FETCH_JS = """
async ([method, url, body, headers]) => {
  const opts = {method, credentials: 'include', headers: Object.assign({'Accept': 'application/json'}, headers || {})};
  if (body !== null && body !== undefined) {
    opts.body = JSON.stringify(body);
    opts.headers['Content-Type'] = 'application/json';
  }
  const r = await fetch(url, opts);
  const text = await r.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (e) { data = {_text: text.slice(0, 500)}; }
  return [r.status, data];
}
"""

AUTH_JS = """
async () => {
  try {
    if (!window.OL || !window.OL.getAPI) return {ready: false, auth: false, sid: null};
    const a = await window.OL.getAPI('Auth');
    const sid = a && a.getSessionId ? await a.getSessionId() : null;
    const auth = a && a.isAuthenticated ? await a.isAuthenticated() : !!sid;
    return {ready: true, auth: !!auth, sid: sid || null};
  } catch (e) { return {ready: false, auth: false, sid: null, err: String(e)}; }
}
"""

BALANCE_JS = """
async () => {
  try { const b = await window.OL.getAPI('Balances'); return await b.getBalances(); }
  catch (e) { return null; }
}
"""

CAPTURE_CLICK_JS = """
() => {
  if (window.__betbotCapture) return;
  window.__betbotCapture = true;
  document.addEventListener('click', (ev) => {
    const path = ev.composedPath().filter(n => n instanceof Element).slice(0, 6).map(n => ({
      tag: n.tagName, testid: n.getAttribute('data-testid'), cls: (n.className || '').toString().slice(0, 80),
      text: (n.innerText || '').slice(0, 60)}));
    window.betbotClick && window.betbotClick(JSON.stringify(path));
  }, true);
}
"""


class BrowserSession:
    def __init__(self, base_url: str, profile_dir: str | Path, channel: str = "chrome",
                 headless: bool = False, login_selectors: dict[str, str] | None = None,
                 executable_path: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.profile_dir = Path(profile_dir)
        self.channel = channel
        self.executable_path = executable_path or None
        self.headless = headless
        self.login_selectors = {**DEFAULT_LOGIN_SELECTORS, **(login_selectors or {})}
        self._pw: Any = None
        self.context: Any = None
        self.page: Any = None
        self.api_page: Any = None
        self._nav_task: asyncio.Task | None = None
        self.lock = asyncio.Lock()  # one bet / one login at a time per browser

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        self.context = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            channel=None if self.executable_path else (self.channel or None),
            executable_path=self.executable_path,
            headless=self.headless,
            no_viewport=True,
            args=STEALTH_ARGS,
            ignore_default_args=["--enable-automation"],
        )
        pages = self.context.pages
        self.page = pages[0] if pages else await self.context.new_page()
        await self.page.goto(self.base_url + "/sports", wait_until="domcontentloaded")
        self.api_page = await self.context.new_page()
        await self.api_page.goto(self.base_url + "/sports", wait_until="domcontentloaded")
        await self.page.bring_to_front()
        await human.pause(1.0, 2.0)
        await self.accept_cookies()

    async def stop(self) -> None:
        if self.context:
            await self.context.close()
        if self._pw:
            await self._pw.stop()

    # ----- the site's API, from inside the page -----------------------------
    async def request(self, method: str, path: str, *, body: Any = None,
                      headers: dict[str, str] | None = None) -> tuple[int, Any]:
        # Relative paths go to whichever host the page is on: the site serves its betting API
        # from the same origin as the page (e.g. services.ozoon.eu), not necessarily base_url.
        url = path
        try:
            status, data = await self.api_page.evaluate(FETCH_JS, [method, url, body, headers or {}])
        except Exception as e:  # tab reloaded mid-call, etc.
            log.warning("in-page fetch failed (%s); reloading api tab", e)
            await self.api_page.goto(self.base_url + "/sports", wait_until="domcontentloaded")
            status, data = await self.api_page.evaluate(FETCH_JS, [method, url, body, headers or {}])
        return int(status), data

    async def auth(self) -> dict[str, Any]:
        for _ in range(20):  # the site's Auth API loads shortly after the page
            st = await self.api_page.evaluate(AUTH_JS)
            if st.get("ready"):
                return st
            await asyncio.sleep(0.25)
        return {"ready": False, "auth": False, "sid": None}

    async def is_logged_in(self) -> bool:
        return bool((await self.auth()).get("auth"))

    async def session_id(self) -> str | None:
        st = await self.auth()
        return st.get("sid") if st.get("auth") else None

    async def balance(self) -> float | None:
        data = await self.api_page.evaluate(BALANCE_JS)
        if not isinstance(data, list):
            return None
        for kind in ("Available", "AvailableBalance", "Cash", "Real"):
            for b in data:
                if str(b.get("type", "")).lower() == kind.lower() and b.get("amount") is not None:
                    return float(b["amount"])
        amounts = [b.get("amount") for b in data if isinstance(b.get("amount"), (int, float))]
        return float(amounts[0]) if amounts else None

    # ----- the visible tab ---------------------------------------------------
    async def visit(self, url: str) -> None:
        await self.page.goto(url, wait_until="domcontentloaded")
        await human.pause(0.8, 2.0)

    async def show_event(self, base_url: str, link: str, force: bool = False) -> None:
        """Put the event on screen. Waits in 'ui' mode (force); otherwise runs alongside the bet."""
        url = f"{base_url.rstrip('/')}/sports{link}"
        if self.page.url.split("?")[0].rstrip("/") == url:
            return
        if force:
            if self._nav_task and not self._nav_task.done():
                await asyncio.gather(self._nav_task, return_exceptions=True)
            await self.page.goto(url, wait_until="domcontentloaded")
            await human.pause(0.6, 1.4)
            return
        if not self._nav_task or self._nav_task.done():
            self._nav_task = asyncio.create_task(self.page.goto(url, wait_until="domcontentloaded"))

    # ----- logging in like a person -----------------------------------------
    async def login(self, email: str, password: str, timeout_s: float = 45.0) -> str:
        """Returns 'ok', 'challenge' (captcha/2FA: a person must finish) or 'failed:<why>'."""
        s = self.login_selectors
        page = self.page
        await page.bring_to_front()
        if self.base_url not in page.url:
            await self.visit(self.base_url + "/sports")
        await self.accept_cookies()
        pw_box = page.locator(s["password"]).first
        if not await pw_box.is_visible():
            opener = page.locator(s["open_login"]).first
            if await opener.count() and await opener.is_visible():
                await human.click(page, opener)
            else:
                await self.visit(self.base_url + s["login_path"])
                await self.accept_cookies()
            try:
                await pw_box.wait_for(state="visible", timeout=10000)
            except Exception:
                return "failed:login form did not appear"
        await human.pause(0.4, 1.0)
        await human.type_into(page, page.locator(s["email"]).first, email)
        await human.pause(0.3, 0.8)
        await human.type_into(page, pw_box, password)
        await human.pause(0.3, 0.9)
        remember = page.locator(s["remember"]).first
        if await remember.count() and not await remember.is_checked():
            await human.click(page, remember, optional=True)
            await human.pause(0.2, 0.6)
        submit = page.locator(s["submit"]).first
        if await submit.count():
            await human.click(page, submit)
        else:
            await page.keyboard.press("Enter")
        deadline = time.monotonic() + timeout_s
        tick = 0
        while time.monotonic() < deadline:
            await asyncio.sleep(1.0)
            tick += 1
            if await self._logged_in_after_submit(reload_api=tick % 5 == 0):
                return "ok"
            if await self.challenge_visible():
                return "challenge"
        return "failed:not logged in after submitting"

    async def accept_cookies(self) -> None:
        """Click the cookie banner away, as a person would before doing anything else."""
        btn = self.page.locator(self.login_selectors["cookies"]).first
        try:
            if await btn.count() and await btn.is_visible():
                await human.click(self.page, btn, optional=True)
                await human.pause(0.3, 0.8)
        except Exception:  # noqa: BLE001 - a banner we can't click is not fatal
            pass

    async def challenge_visible(self) -> bool:
        s = self.login_selectors
        if await self.page.locator(s["challenge"]).count():
            return True
        return await self.page.get_by_text(re.compile(s["challenge_text"], re.I)).count() > 0

    async def _logged_in_after_submit(self, reload_api: bool) -> bool:
        try:
            st = await self.page.evaluate(AUTH_JS)
        except Exception:  # the page is navigating after submit
            st = {}
        if st.get("auth") or reload_api:
            # the api tab picks up the new session cookies on reload
            await self.api_page.goto(self.base_url + "/sports", wait_until="domcontentloaded")
            return await self.is_logged_in()
        return False

    # ----- capture mode ------------------------------------------------------
    async def capture(self, out_path: Path, seconds: float = 600.0) -> None:
        """Record bet-slip requests/responses and clicked elements while a person bets by hand."""
        records: list[dict[str, Any]] = []

        async def on_response(resp: Any) -> None:
            req = resp.request
            if "/services/sports/bet/" not in req.url:
                return
            try:
                body = await resp.json()
            except Exception:
                body = None
            hdrs = {k: ("<redacted>" if k.lower() in ("cookie", "authorization", "x-session-id") else v)
                    for k, v in (await req.all_headers()).items()}
            records.append({"ts": time.time(), "method": req.method, "url": req.url, "headers": hdrs,
                            "request": req.post_data, "status": resp.status, "response": body})
            out_path.write_text(json.dumps(records, indent=2))
            print(f"captured {req.method} {req.url} -> {resp.status}")

        async def on_click(_source: Any, path_json: str) -> None:
            records.append({"ts": time.time(), "click": json.loads(path_json)})
            out_path.write_text(json.dumps(records, indent=2))

        await self.context.expose_binding("betbotClick", on_click)
        await self.context.add_init_script(f"({CAPTURE_CLICK_JS})()")
        await self.page.evaluate(CAPTURE_CLICK_JS)
        self.context.on("response", lambda r: asyncio.ensure_future(on_response(r)))
        print(f"Capture mode: place one small bet by hand in the Chrome window. Recording to {out_path}")
        await asyncio.sleep(seconds)


class BrowserTransport:
    """Adapter transport that sends every request from inside the logged-in page."""

    def __init__(self, session: BrowserSession):
        self.session = session

    async def request(self, method: str, path: str, *, body: Any = None,
                      headers: dict[str, str] | None = None) -> tuple[int, Any]:
        return await self.session.request(method, path, body=body, headers=headers)
