"""SEC EDGAR XBRL 어댑터 — 미국 상장사 재무 원자료. 인증키 불필요.

엔드포인트(실측):
  티커→CIK  https://www.sec.gov/files/company_tickers.json
            {"0":{"cik_str":320193,"ticker":"AAPL","title":"Apple Inc."}, ...}
  개념 조회  https://data.sec.gov/api/xbrl/companyconcept/CIK{cik10}/us-gaap/{tag}.json

companyfacts(전체 일괄)는 대형 filer가 5~30MB라 JSON 파싱만으로 이벤트루프를
0.3~1초 막는다(stdio MCP에 치명적). 필요한 태그가 최대 5개뿐이므로 태그별
companyconcept를 병렬 호출한다 — 태그 단위로 실패·캐시가 독립적이라는 이점도 크다.

SEC는 '이름 이메일' 형식의 User-Agent를 요구하며 없으면 403을 준다. 그리고
10 req/s를 넘기면 IP를 차단하므로 RateLimiter로 간격을 벌린다.

이 모듈의 함수는 실패 시 **raise** 한다(sources 계층 규약). 부분성공 dict로
바꾸는 일은 sources/sec_metrics.py가 맡는다.
"""
from __future__ import annotations

import logging
import os

import httpx

from core import http
from core.cache import cached
from core.ratelimit import RateLimiter

logger = logging.getLogger("finance-mcp")

_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_CONCEPT_URL = ("https://data.sec.gov/api/xbrl/companyconcept/"
                "CIK{cik}/{taxonomy}/{tag}.json")

TTL_SEC = 86400.0          # 공시 자료는 하루 단위로만 바뀐다
# SEC 하드리밋 10 req/s에서 20% 마진.
_LIMITER = RateLimiter(8.0)

_UA_ENV = "SEC_USER_AGENT"
_UA_HELP = (
    'SEC_USER_AGENT 미설정. SEC EDGAR는 "이름 이메일" 형식의 User-Agent를 '
    "요구하며 없으면 403을 반환합니다. .env에 "
    'SEC_USER_AGENT="Hong Gildong hong@example.com" 를 추가하세요.'
)


class SecConfigError(RuntimeError):
    """User-Agent 미설정 등 사용자가 조치해야 하는 설정 오류(네트워크 실패와 구분)."""


class ConceptNotFound(LookupError):
    """해당 회사가 그 us-gaap 태그를 공시하지 않는다(영구 사실)."""


def _headers() -> dict:
    ua = os.environ.get(_UA_ENV, "").strip()
    if not ua or "@" not in ua:
        raise SecConfigError(_UA_HELP)
    return {"User-Agent": ua,
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate"}


async def _get(url: str) -> dict:
    return await http.get_json(url, headers=_headers(), retries=2,
                               backoff=0.5, timeout=10.0, limiter=_LIMITER)


async def cik_map() -> dict[str, dict]:
    """{티커: {cik, title}}. 약 1MB이므로 24시간 디스크 캐시."""
    async def fetch():
        data = await _get(_TICKERS_URL)
        rows = data.values() if isinstance(data, dict) else (data or [])
        out = {}
        for r in rows:
            t = str((r or {}).get("ticker") or "").strip().upper()
            cik = (r or {}).get("cik_str")
            if not t or cik is None:
                continue
            out[t] = {"cik": str(cik).zfill(10), "title": r.get("title")}
        if not out:
            raise RuntimeError("SEC 티커 목록이 비어 있습니다")
        return {"map": out}

    return (await cached("sec:tickers", fetch, TTL_SEC, disk=True))["map"]


async def resolve_cik(ticker: str) -> tuple[str, str]:
    """티커 → (10자리 CIK, 회사명). 미등재 시 raise."""
    # 야후는 BRK-B, SEC는 BRK-B로 통일돼 있으나 사용자가 BRK.B를 넣는 경우가 잦다.
    t = (ticker or "").strip().upper().replace(".", "-")
    m = await cik_map()
    hit = m.get(t)
    if not hit:
        raise LookupError(f"SEC에 등재되지 않은 티커입니다: {ticker}"
                          " (미국 상장사만 조회 가능)")
    return hit["cik"], hit.get("title") or t


async def concept(cik10: str, tag: str, taxonomy: str = "us-gaap") -> dict:
    """companyconcept 원본 JSON. 미공시 태그는 ConceptNotFound.

    404(=그 회사가 그 태그를 안 씀)는 영구 사실이므로 '없음' 마커를 24시간
    캐시해 반복 요청을 막는다.
    """
    url = _CONCEPT_URL.format(cik=cik10, taxonomy=taxonomy, tag=tag)

    async def fetch():
        try:
            return await _get(url)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return {"__missing__": True}
            raise

    data = await cached(f"sec:cc:{cik10}:{taxonomy}:{tag}", fetch,
                        TTL_SEC, disk=True)
    if data.get("__missing__"):
        raise ConceptNotFound(f"us-gaap:{tag} 미공시 (CIK {cik10})")
    return data


async def concept_any(cik10: str, tags: list[str],
                      taxonomy: str = "us-gaap") -> tuple[str, dict]:
    """우선순위대로 시도해 첫 성공 태그를 (태그명, JSON)으로 반환.

    같은 항목이라도 회사마다 쓰는 us-gaap 태그가 달라서 필요하다.
    전부 실패하면 마지막 예외를 raise 한다.
    """
    last_exc: Exception | None = None
    for tag in tags:
        try:
            return tag, await concept(cik10, tag, taxonomy)
        except (ConceptNotFound, httpx.HTTPStatusError) as e:
            last_exc = e
            continue
    raise last_exc or ConceptNotFound(f"태그 후보 전부 미공시: {tags}")
