"""한국은행 경제통계시스템 ECOS OpenAPI 소스 — 100대 통계지표.

KeyStatisticList 엔드포인트 하나로 사전 큐레이션된 ~100개 핵심 거시지표
(기준금리, 국고채/CD 금리, CPI, M2, 가계신용, 환율, 성장률 등)를 받는다.
개별 통계표(StatisticSearch)는 통계표 코드 확정이 필요해 백로그로 미룸.

요청(키가 **URL 경로**에 들어감 — 주의):
  https://ecos.bok.or.kr/api/KeyStatisticList/{ECOS_API_KEY}/json/kr/1/100
응답(실호출로 필드명 확정 필요, 문서 기준 추정):
  {"KeyStatisticList": {"list_total_count": 100,
     "row": [{"CLASS_NAME":"시장금리","KEYSTAT_NAME":"한국은행 기준금리",
              "DATA_VALUE":"3.0","CYCLE":"202606","UNIT_NAME":"%"}, ...]}}
에러 봉투: {"RESULT":{"CODE":"INFO-100","MESSAGE":"인증키가 유효하지 않습니다..."}}
  → 빈 결과로 위장하지 않고 예외로 올린다(fiscal.py와 동일 원칙).

키가 경로에 있어 httpx 예외 메시지의 URL에 노출된다 — core/schema.py의
_PATH_KEY_RE가 fail() 시점에 마스킹한다(이 소스 추가와 함께 반영됨).

인증키: ECOS_API_KEY (https://ecos.bok.or.kr > Open API 인증키 신청, 무료).
"""
from __future__ import annotations

import os

from core import http
from core.schema import to_float

_URL_TMPL = "https://ecos.bok.or.kr/api/KeyStatisticList/{key}/json/kr/{start}/{end}"

# keywords 미지정 시 포트폴리오 점검에 필요한 기본 관심지표 필터
DEFAULT_KEYWORDS = [
    "기준금리", "국고채", "CD", "콜금리",
    "소비자물가", "M2", "가계신용", "원/달러", "경제성장",
]


def _key() -> str:
    k = os.environ.get("ECOS_API_KEY")
    if not k:
        raise RuntimeError("ECOS_API_KEY not set (ecos.bok.or.kr에서 발급)")
    return k


def _extract_rows(payload) -> list[dict]:
    """ECOS 응답에서 row 리스트 추출. 에러 봉투는 예외로."""
    if not isinstance(payload, dict):
        raise RuntimeError(f"unexpected ecos payload: {type(payload).__name__}")
    res = payload.get("RESULT")
    if isinstance(res, dict) and "KeyStatisticList" not in payload:
        raise RuntimeError(f"ecos error {res.get('CODE')}: {res.get('MESSAGE')}")
    body = payload.get("KeyStatisticList")
    if not isinstance(body, dict):
        raise RuntimeError(f"unexpected ecos payload: {str(payload)[:120]}")
    rows = body.get("row")
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict)]


async def key_statistics(keywords: list[str] | None = None) -> dict:
    """100대 통계지표 조회 후 키워드 필터(지표명·분류명 부분일치, 대소문자 무시).

    keywords가 빈 리스트면 필터 없이 전체 반환. None이면 DEFAULT_KEYWORDS.
    """
    url = _URL_TMPL.format(key=_key(), start=1, end=100)
    payload = await http.get_json(url, retries=1, timeout=10.0)
    rows = _extract_rows(payload)

    kws = DEFAULT_KEYWORDS if keywords is None else keywords

    def _hit(r: dict) -> bool:
        hay = f"{r.get('KEYSTAT_NAME') or ''} {r.get('CLASS_NAME') or ''}".lower()
        return any(kw.lower() in hay for kw in kws)

    picked = [r for r in rows if _hit(r)] if kws else rows
    indicators = [{
        "분류": r.get("CLASS_NAME"),
        "지표명": r.get("KEYSTAT_NAME"),
        "값": to_float(r.get("DATA_VALUE")),
        "단위": r.get("UNIT_NAME"),
        "기준시점": r.get("CYCLE"),
    } for r in picked]
    return {
        "name": "한국은행 100대 통계지표",
        "keywords": kws or None,
        "total_available": len(rows),
        "count": len(indicators),
        "indicators": indicators,
        "source": "ecos",
    }
