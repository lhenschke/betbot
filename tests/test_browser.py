from betbot.browser import BrowserSession


class FakePage:
    def __init__(self):
        self.calls = []

    async def evaluate(self, js, arg=None, **kw):
        self.calls.append(kw)
        return {"ready": True, "auth": True, "sid": "S1"}


async def test_stealth_reads_the_sites_globals_from_the_main_world():
    # patchright evaluates in an isolated world by default, where window.OL doesn't exist
    s = BrowserSession("https://x", "unused", stealth=True)
    s.api_page = FakePage()
    assert await s.session_id() == "S1"
    assert s.api_page.calls == [{"isolated_context": False}]


async def test_plain_playwright_gets_no_patchright_arguments():
    s = BrowserSession("https://x", "unused", stealth=False)
    s.api_page = FakePage()
    assert await s.is_logged_in()
    assert s.api_page.calls == [{}]


class StuckPage:
    """A background tab that never answers (frozen by Chrome, or a request that stalls) a set number
    of times, then answers normally."""

    def __init__(self, stuck_times):
        self.stuck_times, self.calls, self.reloads = stuck_times, [], 0

    async def evaluate(self, js, arg=None, **kw):
        import asyncio
        self.calls.append(arg)
        if len(self.calls) <= self.stuck_times:
            await asyncio.sleep(3600)
        return [200, {"ok": True}]

    async def goto(self, *a, **kw):
        self.reloads += 1


def stuck_session(monkeypatch, stuck_times):
    from betbot import browser
    monkeypatch.setattr(browser, "FETCH_TIMEOUT_S", {"GET": 0.01})
    monkeypatch.setattr(browser, "POST_TIMEOUT_S", 0.01)
    monkeypatch.setattr(browser, "EVAL_GRACE_S", 0.05)
    s = BrowserSession("https://x", "unused")
    s.api_page = StuckPage(stuck_times)
    return s


async def test_a_stuck_read_is_given_up_on_and_tried_once_more(monkeypatch):
    s = stuck_session(monkeypatch, 1)
    assert await s.request("GET", "/coupon") == (200, {"ok": True})
    assert s.api_page.reloads == 1 and len(s.api_page.calls) == 2


async def test_a_read_stuck_twice_fails_instead_of_waiting_forever(monkeypatch):
    import pytest
    s = stuck_session(monkeypatch, 2)
    with pytest.raises(TimeoutError, match="didn't answer"):
        await s.request("GET", "/coupon")


async def test_a_stuck_post_is_never_sent_twice(monkeypatch):
    import pytest
    s = stuck_session(monkeypatch, 1)
    with pytest.raises(TimeoutError):
        await s.request("POST", "/bet/betslip", body={"bet": 1})
    assert len(s.api_page.calls) == 1 and s.api_page.reloads == 1
