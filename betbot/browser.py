"""The real, logged-in Chrome window the bot works through.

Two tabs share one Chrome profile (cookies, login):
  page      the visible tab: shows the event being bet, receives clicks in 'ui' mode,
            and does the idle browsing.
  api_page  a background tab on the same site that runs the site's own API calls with
            fetch(), so they carry the real cookies, origin and TLS fingerprint and
            never get cut off by the visible tab navigating.

With stealth on (the default) Chrome is driven by patchright, a patched Playwright that hides
the automation leaks (Runtime.enable, injected globals, command-line flags) sites look for.
patchright runs page scripts in an isolated world unless told otherwise, so everything here
that touches the site's own JavaScript goes through BrowserSession._eval, which picks the
page's main world.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

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
    "submit": 'button[type="submit"], input[type="submit"], button:has-text("Log In"), button:has-text("Login"), '
              'button:has-text("Sign In")',
    "remember": 'input[type="checkbox"][name*="remember" i], input[type="checkbox"][id*="remember" i]',
    "cookies": 'button:has-text("Accept All"), button:has-text("ACCEPT ALL")',
    "login_path": "/login",
    "challenge": 'iframe[src*="captcha"], iframe[src*="recaptcha"], iframe[src*="hcaptcha"], '
                 'iframe[src*="turnstile"]',
    "challenge_text": r"verification code|verify your|one-time code|security code|captcha",
}

LOGIN_HINTS_JS = """
() => [...document.querySelectorAll('a, button, [role=button], input[type=submit]')]
  .filter(e => e.offsetParent !== null && /log ?in|sign ?in|account/i.test(
    (e.innerText || e.value || '') + ' ' + e.id + ' ' + e.className))
  .slice(0, 15)
  .map(e => `${e.tagName} "${(e.innerText || e.value || '').trim().slice(0, 30)}" id=${e.id} class=${String(e.className).slice(0, 60)}`)
"""

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
      text: (n.innerText || '').slice(0, 60),
      attrs: Object.fromEntries([...n.attributes].filter(a => /^(id|name|type|placeholder|autocomplete|href|data-.*)$/.test(a.name))
                                .map(a => [a.name, a.value.slice(0, 80)]))}));
    window.betbotClick && window.betbotClick(JSON.stringify(path));
  }, true);
}
"""


class BrowserSession:
    def __init__(self, base_url: str, profile_dir: str | Path, channel: str = "chrome",
                 headless: bool = False, login_selectors: dict[str, str] | None = None,
                 executable_path: str | None = None, start_path: str = "/sports", stealth: bool = True):
        self.base_url = base_url.rstrip("/")
        self.profile_dir = Path(profile_dir)
        self.channel = channel
        self.executable_path = executable_path or None
        self.start_path = start_path
        self.headless = headless
        self.stealth = stealth
        self.login_selectors = {**DEFAULT_LOGIN_SELECTORS, **(login_selectors or {})}
        self._pw: Any = None
        self.context: Any = None
        self.page: Any = None
        self.api_page: Any = None
        self._nav_task: asyncio.Task | None = None
        # A book whose site has no window.OL sets its own login check here (see is_logged_in).
        self.logged_in_check: Callable[[], Awaitable[bool]] | None = None
        self.lock = asyncio.Lock()  # one bet / one login at a time per browser

    async def start(self) -> None:
        if self.stealth:
            from patchright.async_api import async_playwright
        else:
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
        await self.page.goto(self.base_url + self.start_path, wait_until="domcontentloaded")
        self.api_page = await self.context.new_page()
        await self.api_page.goto(self.base_url + self.start_path, wait_until="domcontentloaded")
        await self.page.bring_to_front()
        await human.pause(1.0, 2.0)
        await self.accept_cookies()

    async def stop(self) -> None:
        if self.context:
            await self.context.close()
        if self._pw:
            await self._pw.stop()

    async def _eval(self, page: Any, js: str, arg: Any = None) -> Any:
        """Run js in the page's main world, where the site's globals (window.OL, its fetch) live."""
        if self.stealth:
            return await page.evaluate(js, arg, isolated_context=False)
        return await page.evaluate(js, arg)

    async def evaluate(self, js: str, arg: Any = None) -> Any:
        """Run js in the background tab's main world, e.g. to read the site's CSRF token."""
        return await self._eval(self.api_page, js, arg)

    async def reload_api(self) -> None:
        await self.api_page.goto(self.base_url + self.start_path, wait_until="domcontentloaded")

    # ----- the site's API, from inside the page -----------------------------
    async def request(self, method: str, path: str, *, body: Any = None,
                      headers: dict[str, str] | None = None) -> tuple[int, Any]:
        # Relative paths go to whichever host the page is on: the site serves its betting API
        # from the same origin as the page (e.g. services.ozoon.eu), not necessarily base_url.
        url = path
        try:
            status, data = await self._eval(self.api_page, FETCH_JS, [method, url, body, headers or {}])
        except Exception as e:  # tab reloaded mid-call, etc.
            log.warning("in-page fetch failed (%s); reloading api tab", e)
            await self.reload_api()
            status, data = await self._eval(self.api_page, FETCH_JS, [method, url, body, headers or {}])
        return int(status), data

    async def auth(self) -> dict[str, Any]:
        for _ in range(20):  # the site's Auth API loads shortly after the page
            st = await self._eval(self.api_page, AUTH_JS)
            if st.get("ready"):
                return st
            await asyncio.sleep(0.25)
        return {"ready": False, "auth": False, "sid": None}

    async def is_logged_in(self) -> bool:
        if self.logged_in_check:
            return await self.logged_in_check()
        return bool((await self.auth()).get("auth"))

    async def session_id(self) -> str | None:
        st = await self.auth()
        return st.get("sid") if st.get("auth") else None

    async def balance(self) -> float | None:
        data = await self._eval(self.api_page, BALANCE_JS)
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
            await self.visit(self.base_url + self.start_path)
        await self.accept_cookies()
        # Sites often keep hidden copies of their login links and forms (mobile menus, closed
        # modals), so every lookup takes the first *visible* match.
        pw_box = _visible(page, s["password"])
        if not await pw_box.count():
            opener = _visible(page, s["open_login"])
            if await opener.count():
                await human.click(page, opener)
            else:
                await self.visit(self.base_url + s["login_path"])
                await self.accept_cookies()
            try:
                await pw_box.wait_for(state="visible", timeout=10000)
            except Exception:
                log.warning("login form not found on %s; login-looking elements: %s",
                            page.url, await self._login_hints())
                return f"failed:login form did not appear on {page.url.split('?')[0]}"
        email_box = _visible(page, s["email"])
        if not await email_box.count():  # no recognisable username box: the text box before the password
            email_box = pw_box.locator("xpath=preceding::input[not(@type) or @type='text' or @type='email' "
                                       "or @type='tel'][1]")
        await human.pause(0.4, 1.0)
        await human.type_into(page, email_box, email)
        await human.pause(0.3, 0.8)
        await human.type_into(page, pw_box, password)
        await human.pause(0.3, 0.9)
        remember = _visible(page, s["remember"])
        if await remember.count() and not await remember.is_checked():
            await human.click(page, remember, optional=True)
            await human.pause(0.2, 0.6)
        # Prefer the submit button of the password's own form: a header "Login" button that
        # opened the form would just close it again.
        form = pw_box.locator("xpath=ancestor::form[1]")
        submit = _visible(form, s["submit"]) if await form.count() else _visible(page, s["submit"])
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

    async def _login_hints(self) -> list[str]:
        try:
            return await self._eval(self.page, LOGIN_HINTS_JS)
        except Exception:  # noqa: BLE001 - only for the log
            return []

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
        if self.logged_in_check:
            if reload_api:
                await self.reload_api()
            return await self.logged_in_check()
        try:
            st = await self._eval(self.page, AUTH_JS)
        except Exception:  # the page is navigating after submit
            st = {}
        if st.get("auth") or reload_api:
            # the api tab picks up the new session cookies on reload
            await self.reload_api()
            return await self.is_logged_in()
        return False

    # ----- capture mode ------------------------------------------------------
    async def capture(self, out_path: Path, seconds: float = 600.0) -> None:
        """Record what the site does while a person browses and bets by hand.

        Saves every data request (XHR/fetch/page loads) with its response, websocket traffic and
        clicked elements to out_path, plus a short, shareable summary next to it. Cookies, auth
        headers and anything that looks like a password or token are blanked out.
        """
        records: list[dict[str, Any]] = []
        summary_path = out_path.with_name(out_path.stem + "-summary.txt")
        seen: dict[str, int] = {}

        def save() -> None:
            out_path.write_text(json.dumps(records, indent=2, default=str), encoding="utf-8")
            summary_path.write_text(_capture_summary(records), encoding="utf-8")

        async def on_response(resp: Any) -> None:
            req = resp.request
            if req.resource_type not in ("xhr", "fetch", "document", "eventsource"):
                return
            if _STATIC.search(req.url.split("?")[0]):
                return
            try:
                text = await resp.text()
            except Exception:
                text = ""
            try:
                body: Any = json.loads(text)
            except ValueError:
                body = text[:5000]
            key = f"{req.method} {req.url.split('?')[0]}"
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > 20:  # polling endpoints: the first few calls are enough
                return
            records.append({"ts": time.time(), "type": req.resource_type, "method": req.method, "url": req.url,
                            "headers": _redact_headers(await req.all_headers()),
                            "request": _redact_body(req.post_data), "status": resp.status, "response": body})
            save()
            print(f"captured {req.method} {req.url[:110]} -> {resp.status}")

        def on_websocket(ws: Any) -> None:
            frames = {"n": 0}
            records.append({"ts": time.time(), "websocket": ws.url})

            def frame(direction: str, payload: Any) -> None:
                frames["n"] += 1
                if frames["n"] <= 60:
                    records.append({"ts": time.time(), "ws": ws.url, "dir": direction,
                                    "data": _redact_body(payload if isinstance(payload, str) else repr(payload))[:3000]})
                    save()

            ws.on("framesent", lambda p: frame("sent", p))
            ws.on("framereceived", lambda p: frame("received", p))
            print(f"websocket {ws.url[:110]}")

        async def on_click(_source: Any, path_json: str) -> None:
            records.append({"ts": time.time(), "click": json.loads(path_json)})
            save()

        await self.context.expose_binding("betbotClick", on_click)
        await self.context.add_init_script(f"({CAPTURE_CLICK_JS})()")
        await self._eval(self.page, CAPTURE_CLICK_JS)
        self.context.on("response", lambda r: asyncio.ensure_future(on_response(r)))
        for pg in self.context.pages:
            pg.on("websocket", on_websocket)
        self.context.on("page", lambda pg: pg.on("websocket", on_websocket))
        print(f"Capture mode: log in, browse a league and a game, then place one small bet by hand.\n"
              f"Recording to {out_path} (summary: {summary_path}). Close this window when done.")
        await asyncio.sleep(seconds)


def _visible(scope: Any, selector: str) -> Any:
    """The first element matching selector that is actually shown."""
    return scope.locator(f"{selector} >> visible=true").first


_STATIC = re.compile(r"\.(svg|png|jpe?g|gif|webp|ico|css|js|woff2?|ttf|map)$", re.I)
_SECRET_KEYS = re.compile(r"pass|pwd|token|secret|pin$|otp|auth|session|cookie|card|cvv", re.I)


def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: ("<redacted>" if _SECRET_KEYS.search(k) else v) for k, v in headers.items()}


def _redact_body(body: Any) -> Any:
    if not body:
        return body
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        # form-encoded or plain text: blank out key=value pairs with secret-looking keys
        return re.sub(r"((?:^|[&\s])[^=&\s]*(?:pass|pwd|token|secret|otp|session)[^=&\s]*=)[^&\s]*",
                      r"\1<redacted>", str(body), flags=re.I)

    def walk(x: Any) -> Any:
        if isinstance(x, dict):
            return {k: ("<redacted>" if _SECRET_KEYS.search(str(k)) else walk(v)) for k, v in x.items()}
        if isinstance(x, list):
            return [walk(v) for v in x]
        return x

    return json.dumps(walk(data))


def _capture_summary(records: list[dict[str, Any]]) -> str:
    """A compact text version of a capture that's small enough to paste into a chat."""
    lines: list[str] = []
    shown: dict[str, int] = {}
    for r in records:
        if "click" in r:
            path = " < ".join(filter(None, (f"{c.get('tag')}[{c.get('testid') or ''}]{(c.get('text') or '')[:30]!r}"
                                            for c in r["click"][:4])))
            lines.append(f"CLICK {path}")
        elif "websocket" in r:
            lines.append(f"WEBSOCKET {r['websocket']}")
        elif "ws" in r:
            k = f"ws:{r['ws']}:{r['dir']}"
            shown[k] = shown.get(k, 0) + 1
            if shown[k] <= 5:
                lines.append(f"WS {r['dir']}: {str(r['data'])[:600]}")
        else:
            k = f"{r['method']} {r['url'].split('?')[0]}"
            shown[k] = shown.get(k, 0) + 1
            if shown[k] > 2 or r["type"] == "document" and r["method"] == "GET":
                if r["type"] == "document":
                    lines.append(f"PAGE {r['url'][:200]} -> {r['status']}")
                continue
            resp = r["response"] if isinstance(r["response"], str) else json.dumps(r["response"])
            lines.append(f"{r['method']} {r['url'][:300]} -> {r['status']}")
            if r.get("request"):
                lines.append(f"  sent: {str(r['request'])[:1500]}")
            lines.append(f"  got:  {resp[:1500]}")
    return "\n".join(lines) + "\n"


class BrowserTransport:
    """Adapter transport that sends every request from inside the logged-in page."""

    def __init__(self, session: BrowserSession):
        self.session = session

    async def request(self, method: str, path: str, *, body: Any = None,
                      headers: dict[str, str] | None = None) -> tuple[int, Any]:
        return await self.session.request(method, path, body=body, headers=headers)
