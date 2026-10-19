"""Plain HTTP transport, used for read-only work (tests, `betbot check`)."""

from __future__ import annotations

from typing import Any

import httpx


class HttpTransport:
    def __init__(self, base_url: str, client: httpx.AsyncClient | None = None):
        self.client = client or httpx.AsyncClient(
            base_url=base_url, timeout=15.0,
            headers={"Accept": "application/json",
                     "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                   "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"})

    async def request(self, method: str, path: str, *, body: Any = None,
                      headers: dict[str, str] | None = None) -> tuple[int, Any]:
        r = await self.client.request(method, path, json=body, headers=headers)
        try:
            data = r.json()
        except ValueError:
            data = None
        return r.status_code, data

    async def aclose(self) -> None:
        await self.client.aclose()
