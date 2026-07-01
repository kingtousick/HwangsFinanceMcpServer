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

# "공정률 ('26.3월 기준) 68.67%" — 공정률과 퍼센트 사이에 기준월 숫자('26.3)가 끼므로
# [^%]로 건너뛰고(비탐욕) 퍼센트 바로 앞 숫자를 잡는다.
_PCT_RE = re.compile(r"공정률[^%]{0,40}?([0-9]{1,3}(?:\.[0-9]+)?)\s*%")
# "'26.3월 기준" / "2026.3월 기준" / "'26. 3. 기준"
_BASE_MONTH_RE = re.compile(r"['’]?(\d{2,4})[.\-]\s*(\d{1,2})\s*월?\s*기준")


def _norm(s: str) -> str:
    """공백/하이픈/물결 제거 + 소문자 — 표기 차이 흡수용 매칭 키."""
    return re.sub(r"[\s\-~]", "", s or "").lower()


async def _get_browser():
    global _browser
    if _browser is None:
        from playwright.async_api import async_playwright  # 지연 import
        pw = await async_playwright().start()
        # 사내망 TLS 가로채기로 chromium 다운로드가 막히므로(SELF_SIGNED_CERT_IN_CHAIN)
        # Windows 기본 브라우저(Edge)→Chrome→번들 chromium 순으로 시스템 브라우저 우선.
        last: Exception | None = None
        for kwargs in ({"channel": "msedge"}, {"channel": "chrome"}, {}):
            try:
                _browser = await pw.chromium.launch(headless=True, **kwargs)
                break
            except Exception as e:  # noqa: BLE001 - 다음 브라우저로 폴백
                last = e
        if _browser is None:
            raise last or RuntimeError("no chromium/edge/chrome available")
    return _browser


async def _page_items(url: str) -> list[dict]:
    """주요사업현황 아코디언에서 (사업명, 패널텍스트) 목록 추출.

    각 사업은 <li class="news">로, 제목(토글 링크)과 상세(사업내용·추진현황·공정률)를
    함께 담는다. inner_text는 펼쳐진 항목만 보여 숨은 패널을 놓치므로 li별 textContent를 쓴다.
    """
    async with _lock:  # 동시성 1
        browser = await _get_browser()
        page = await browser.new_page()
        try:
            await page.goto(url, wait_until="networkidle", timeout=20000)
            return await page.evaluate(
                """() => [...document.querySelectorAll('li.news')].map(li => {
                    const a = li.querySelector('a');
                    return {
                        title: ((a ? a.innerText : '') || '').replace(/\\s+/g,' ').trim(),
                        text: (li.textContent || '').replace(/\\s+/g,' ').trim(),
                    };
                }).filter(x => x.title)"""
            )
        finally:
            await page.close()


def _base_month(text: str) -> str | None:
    m = _BASE_MONTH_RE.search(text)
    if not m:
        return None
    yy, mm = m.group(1), int(m.group(2))
    year = int(yy) + 2000 if len(yy) == 2 else int(yy)
    return f"{year}-{mm:02d}"


def _extract(items: list[dict], keywords: list[str]) -> list[dict]:
    """li별 (사업명, 패널)에서 키워드 매칭 사업의 공정률·기준월 추출.

    매칭은 공백/표기 차이를 흡수하도록 정규화 후 **제목(사업명)** 부분일치. 패널 본문까지
    매칭하면 다른 노선을 언급한 설명文에 걸려 오염되므로(GTX-A가 C노선을 잡는 등) 제목만 본다.
    """
    kn = [_norm(k) for k in keywords if k]
    out: list[dict] = []
    seen: set = set()
    for it in items:
        title = it.get("title") or ""
        text = it.get("text") or ""
        if not any(k in _norm(title) for k in kn):
            continue
        m = _PCT_RE.search(text)
        if not m:
            continue
        pct = float(m.group(1))
        if title in seen:
            continue
        seen.add(title)
        out.append({
            "사업명": title,
            "공정률_pct": pct,
            "기준월": _base_month(text),
        })
    return out


async def get_progress(keywords: list[str], kric_m: str | None = None) -> dict:
    """주요사업현황 페이지에서 키워드 매칭 사업의 공정률 추출.

    kric_m이 주어지면 해당 구분 페이지만, 없으면 광역+일반 둘 다 렌더링.
    """
    targets = [_PAGES[kric_m]] if kric_m in _PAGES else list(_PAGES.values())
    results = await asyncio.gather(*(_page_items(u) for u in targets),
                                   return_exceptions=True)
    progress: list[dict] = []
    errors: list[Exception] = []
    for res in results:
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
        "note": "국가철도공단 주요사업현황 HTML 스크래핑(월 단위 공정률)",
    }


async def aclose() -> None:
    global _browser
    if _browser is not None:
        await _browser.close()
        _browser = None
