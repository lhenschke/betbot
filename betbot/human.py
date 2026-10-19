"""Humanlike input: curved mouse paths, uneven typing, idle scrolling, jitter."""

from __future__ import annotations

import asyncio
import math
import random
from typing import Any

TYPING_DELAY_MS: tuple[int, int] = (60, 190)
_mouse: dict[int, tuple[float, float]] = {}


def configure(typing_delay_ms: tuple[int, int]) -> None:
    global TYPING_DELAY_MS
    TYPING_DELAY_MS = typing_delay_ms


async def pause(lo: float, hi: float) -> None:
    await asyncio.sleep(random.uniform(lo, hi))


def bezier_path(start: tuple[float, float], end: tuple[float, float], steps: int) -> list[tuple[float, float]]:
    """Points along a cubic Bezier with random control points, eased in and out."""
    (x0, y0), (x3, y3) = start, end
    dx, dy = x3 - x0, y3 - y0
    dist = math.hypot(dx, dy) or 1.0
    nx, ny = -dy / dist, dx / dist  # normal, to bow the path sideways
    bow1 = random.uniform(-0.25, 0.25) * dist
    bow2 = random.uniform(-0.25, 0.25) * dist
    x1, y1 = x0 + dx * random.uniform(0.2, 0.4) + nx * bow1, y0 + dy * random.uniform(0.2, 0.4) + ny * bow1
    x2, y2 = x0 + dx * random.uniform(0.6, 0.8) + nx * bow2, y0 + dy * random.uniform(0.6, 0.8) + ny * bow2
    pts = []
    for i in range(1, steps + 1):
        t = i / steps
        t = t * t * (3 - 2 * t)  # ease in/out
        u = 1 - t
        pts.append((u ** 3 * x0 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t ** 3 * x3,
                    u ** 3 * y0 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t ** 3 * y3))
    return pts


async def move_to(page: Any, x: float, y: float) -> None:
    start = _mouse.get(id(page))
    if start is None:
        vp = page.viewport_size or {"width": 1280, "height": 800}
        start = (random.uniform(0.3, 0.7) * vp["width"], random.uniform(0.3, 0.7) * vp["height"])
    dist = math.hypot(x - start[0], y - start[1])
    steps = max(8, min(45, int(dist / random.uniform(12, 25))))
    if dist > 200 and random.random() < 0.3:  # overshoot a little, then correct
        ox, oy = x + random.uniform(-12, 12), y + random.uniform(-8, 8)
        for px, py in bezier_path(start, (ox, oy), steps):
            await page.mouse.move(px, py)
            await asyncio.sleep(random.uniform(0.004, 0.014))
        start = (ox, oy)
        steps = random.randint(4, 8)
    for px, py in bezier_path(start, (x, y), steps):
        await page.mouse.move(px, py)
        await asyncio.sleep(random.uniform(0.004, 0.014))
    _mouse[id(page)] = (x, y)


async def click(page: Any, locator: Any, optional: bool = False) -> bool:
    try:
        if optional and not await locator.count():
            return False
        await locator.scroll_into_view_if_needed(timeout=5000)
        box = await locator.bounding_box()
    except Exception:
        if optional:
            return False
        raise
    if not box:
        if optional:
            return False
        raise RuntimeError("element has no box (hidden?)")
    x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
    y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
    await move_to(page, x, y)
    await pause(0.05, 0.18)
    await page.mouse.down()
    await pause(0.04, 0.12)
    await page.mouse.up()
    return True


async def type_text(page: Any, text: str) -> None:
    lo, hi = TYPING_DELAY_MS
    for ch in text:
        await page.keyboard.type(ch)
        delay = random.uniform(lo, hi) / 1000
        if random.random() < 0.06:
            delay += random.uniform(0.2, 0.6)  # the occasional hesitation
        await asyncio.sleep(delay)


async def type_into(page: Any, locator: Any, text: str) -> None:
    await click(page, locator)
    await pause(0.1, 0.3)
    await page.keyboard.press("Control+A")
    await page.keyboard.press("Backspace")
    await type_text(page, text)


async def idle_scroll(page: Any) -> None:
    for _ in range(random.randint(1, 4)):
        await page.mouse.wheel(0, random.choice([-1, 1, 1, 1]) * random.randint(150, 650))
        await pause(0.6, 2.5)
