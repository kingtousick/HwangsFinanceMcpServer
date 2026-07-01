"""국가철도공단 주요사업현황 공정률 스크래핑 (Playwright, 최후 수단 성격).

철도/광역교통 사업의 '진행현황·공정률'을 잡는다. "'26.3월 기준 공정률 19.4%"처럼
월 단위 공정률이 노선별로 공개된다. 공식 API가 없어 HTML을 렌더링해 추출한다.

대상 페이지(kr.or.kr 사업소개>철도건설>주요사업현황):
  광역철도: https://www.kr.or.kr/sub/info.do?m=05010402
  일반철도: https://www.kr.or.kr/sub/info.do?m=05010302

**불안정성 경고**: 페이지 구조가 바뀌면 추출이 깨진다. 공정률/기준월/사업명 추출은
정규식 휴리스틱이며, Playwright 미설치·타임아웃 시 예외를 올려 상위에서 fallback(서버
크래시 없음). playwright_fb.py와 동일하게 브라우저 인스턴스를 재사용하고 동시성 1로 제한.
"""
from __future__ import annotations

import asyncio
import re

_lock = asyncio.Lock()
_browser = None  # 재사용 브라우저 인스턴스

_PAGES = {
    "05010402": "https://www.kr.or.kr/sub/info.do?m=05010402",  # 광역철도
    "05010302": "https://www.kr.or.kr/sub/info.do?m=05010302",  # 일반철도
}

# "공정률 19.4%" / "공정률 : 19.4 %"
_PCT_RE = re.compile(r"공정률[^0-9%]{0,8}([0-9]{1,3}(?:\.[0-9]+)?)\s*%")
# "'26.3월 기준" / "2026.3월 기준" / "'26. 3. 기준"
_BASE_MONTH_RE = re.compile(r"['’]?(\d{2,4})[.\-]\s*(\d{1,2})\s*월?\s*기준")


async def _get_browser():
    global _browser
    if _browser is None:
        from playwright.async_api import async_playwright  # 지연 import
        pw = await async_playwright().start()
        _browser = await pw.chromium.launch(headless=True)
    return _browser


async def _page_text(url: str) -> str:
    async with _lock:  # 동시성 1
        browser = await _get_browser()
        page = await browser.new_page()
        try:
            await page.goto(url, wait_until="networkidle", timeout=15000)
            return await page.inner_text("body")
        finally:
            await page.close()


def _nearest_base_month(text: str, pos: int) -> str | None:
    """pos 주변에서 가장 가까운 '기준월'을 찾는다(앞쪽 우선)."""
    best, best_d = None, 10 ** 9
    for m in _BASE_MONTH_RE.finditer(text):
        d = abs(m.start() - pos)
        if d < best_d:
            best_d, best = d, m
    if not best:
        return None
    yy, mm = best.group(1), int(best.group(2))
    year = int(yy) + 2000 if len(yy) == 2 else int(yy)
    return f"{year}-{mm:02d}"


def _extract(text: str, keywords: list[str]) -> list[dict]:
    """공정률 출현 지점마다 앞 컨텍스트를 사업명 후보로, 가까운 기준월을 묶어 추출.

    컨텍스트는 '직전 공정률 매칭 끝 ~ 이번 매칭 시작'으로 한정해 인접 사업으로 번지는 것을
    막는다(추가로 최대 120자로 컷). 키워드가 그 컨텍스트에 포함된 항목만 반환.
    """
    out: list[dict] = []
    seen: set = set()
    prev_end = 0
    for m in _PCT_RE.finditer(text):
        raw = text[max(prev_end, m.start() - 120):m.start()]
        prev_end = m.end()
        ctx = re.sub(r"\s+", " ", raw).strip()
        if not any(kw in ctx for kw in keywords):
            continue
        pct = float(m.group(1))
        base = _nearest_base_month(text, m.start())
        key = (ctx[-30:], pct)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "사업명": ctx[-40:] or None,   # 앞 컨텍스트(휴리스틱)
            "공정률_pct": pct,
            "기준월": base,
        })
    return out


async def get_progress(keywords: list[str], kric_m: str | None = None) -> dict:
    """주요사업현황 페이지에서 키워드 매칭 사업의 공정률 추출.

    kric_m이 주어지면 해당 구분 페이지만, 없으면 광역+일반 둘 다 렌더링.
    """
    targets = [_PAGES[kric_m]] if kric_m in _PAGES else list(_PAGES.values())
    texts = await asyncio.gather(*(_page_text(u) for u in targets),
                                 return_exceptions=True)
    progress: list[dict] = []
    errors: list[Exception] = []
    for res in texts:
        if isinstance(res, Exception):
            errors.append(res)
            continue
        progress.extend(_extract(res, keywords))
    if not progress and errors:
        raise errors[0]

    progress.sort(key=lambda p: p["공정률_pct"], reverse=True)
    return {
        "name": "국가철도공단 공정률",
        "keywords": keywords,
        "count": len(progress),
        "progress": progress,
        "source": "krna_progress",
        "note": "HTML 스크래핑 휴리스틱 — 사업명은 페이지 컨텍스트 추정값",
    }


async def aclose() -> None:
    global _browser
    if _browser is not None:
        await _browser.close()
        _browser = None
