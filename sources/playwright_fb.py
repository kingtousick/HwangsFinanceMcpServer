"""Playwright 렌더링 fallback (최후 수단).

설계서 §7: 브라우저 인스턴스를 재사용(매 호출 기동 금지), 동시성 1로 제한.
playwright 미설치 시 ImportError를 던져 상위 cascade가 무시하도록 한다.

국내 지수/종목은 네이버 모바일 금융 페이지에서 현재가를 긁는다. JS 렌더링이
필요한 ETF CHECK 등도 같은 방식으로 확장 가능(여기서는 네이버 지수만 구현).
"""
from __future__ import annotations

import asyncio
import re

from core.schema import ok, now_kst_iso, to_float

_lock = asyncio.Lock()
_browser = None  # 재사용 브라우저 인스턴스


async def _get_browser():
    global _browser
    if _browser is None:
        from playwright.async_api import async_playwright  # 지연 import
        pw = await async_playwright().start()
        _browser = await pw.chromium.launch(headless=True)
    return _browser


async def get_index(code: str, name: str | None = None) -> dict:
    """네이버 모바일 금융에서 지수 현재가를 렌더링해 추출. code: 'KOSPI'/'KOSDAQ'."""
    url = f"https://m.stock.naver.com/domestic/index/{code}/total"
    async with _lock:  # 동시성 1
        browser = await _get_browser()
        page = await browser.new_page()
        try:
            await page.goto(url, wait_until="networkidle", timeout=15000)
            text = await page.inner_text("body")
        finally:
            await page.close()
    # 현재가 패턴 추출(렌더링 결과에서 첫 숫자 그룹). 구조 변동에 방어적.
    m = re.search(r"([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?)", text)
    if not m:
        raise ValueError("playwright: price not found")
    return ok(
        name or code,
        to_float(m.group(1)),
        timestamp=now_kst_iso(),
        currency="KRW",
        source="playwright",
    )


async def aclose() -> None:
    global _browser
    if _browser is not None:
        await _browser.close()
        _browser = None
