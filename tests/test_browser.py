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
