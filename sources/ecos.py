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
from datetime import datetime

from core import http
from core.schema import KST, to_float
from core.series import recent_changes, summarize

_URL_TMPL = "https://ecos.bok.or.kr/api/KeyStatisticList/{key}/json/kr/{start}/{end}"
_SEARCH_TMPL = ("https://ecos.bok.or.kr/api/StatisticSearch/{key}/json/kr/"
                "{start}/{end}/{stat}/{cycle}/{s}/{e}/{items}")

# keywords 미지정 시 포트폴리오 점검에 필요한 기본 관심지표 필터
DEFAULT_KEYWORDS = [
    "기준금리", "국고채", "CD", "콜금리",
    "소비자물가", "M2", "가계신용", "원/달러", "경제성장",
]

# 시계열 프리셋 — 통계표/항목 코드는 StatisticItemList 실측으로 확정(2026-08-07).
# 사용자가 코드를 몰라도 이름만으로 조회할 수 있게 한다.
SERIES_PRESETS: dict[str, dict] = {
    "기준금리":   {"stat": "722Y001", "item": "0101000", "cycle": "M",
                   "label": "한국은행 기준금리"},
    "콜금리":     {"stat": "721Y001", "item": "1010000", "cycle": "M",
                   "label": "무담보콜금리(1일)"},
    "CD금리":     {"stat": "721Y001", "item": "2010000", "cycle": "M",
                   "label": "CD(91일)"},
    "국고채3년":  {"stat": "721Y001", "item": "5020000", "cycle": "M",
                   "label": "국고채(3년)"},
    "국고채10년": {"stat": "721Y001", "item": "5050000", "cycle": "M",
                   "label": "국고채(10년)"},
    "소비자물가": {"stat": "901Y009", "item": "0", "cycle": "M",
                   "label": "소비자물가지수(총지수)"},
    "M2":         {"stat": "161Y005", "item": "BBHS00", "cycle": "M",
                   "label": "M2(평잔, 계절조정)"},
    "가계신용":   {"stat": "151Y001", "item": "1000000", "cycle": "Q",
                   "label": "가계신용(분기)"},
}

# 주기별 '직전 N관측 대비' 비교 구간
_LOOKBACKS = {
    "M": {"3개월": 3, "6개월": 6, "12개월": 12},
    "Q": {"1분기": 1, "4분기": 4},
    "D": {"1개월": 22, "3개월": 66},
    "A": {"1년": 1, "3년": 3},
}


def _key() -> str:
    k = os.environ.get("ECOS_API_KEY")
    if not k:
        raise RuntimeError("ECOS_API_KEY not set (ecos.bok.or.kr에서 발급)")
    return k


def _extract_rows(payload, body_key: str = "KeyStatisticList") -> list[dict]:
    """ECOS 응답에서 row 리스트 추출. 에러 봉투는 예외로.

    ECOS는 조회 결과가 없을 때도 RESULT 봉투(INFO-200)로 응답하므로,
    빈 결과로 위장하지 않고 예외로 올려 상위에서 fail()로 만든다.
    """
    if not isinstance(payload, dict):
        raise RuntimeError(f"unexpected ecos payload: {type(payload).__name__}")
    res = payload.get("RESULT")
    if isinstance(res, dict) and body_key not in payload:
        raise RuntimeError(f"ecos error {res.get('CODE')}: {res.get('MESSAGE')}")
    body = payload.get(body_key)
    if not isinstance(body, dict):
        raise RuntimeError(f"unexpected ecos payload: {str(payload)[:120]}")
    rows = body.get("row")
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict)]


def _period_bounds(cycle: str, periods: int) -> tuple[str, str]:
    """주기별 (시작, 종료) 시점 문자열. 종료는 넉넉히 미래로 둬 최신치까지 받는다."""
    now = datetime.now(tz=KST)
    cyc = cycle.upper()
    if cyc == "A":
        return str(now.year - periods), str(now.year + 1)
    if cyc == "Q":
        q = (now.month - 1) // 3 + 1
        total = now.year * 4 + (q - 1) - periods
        return f"{total // 4}Q{total % 4 + 1}", f"{now.year + 1}Q4"
    if cyc == "D":
        total = now.year * 12 + (now.month - 1) - max(1, periods // 21)
        return f"{total // 12:04d}{total % 12 + 1:02d}01", f"{now.year:04d}{now.month:02d}31"
    total = now.year * 12 + (now.month - 1) - periods
    return f"{total // 12:04d}{total % 12 + 1:02d}", f"{now.year + 1:04d}12"


async def search_series(stat: str, item: str, cycle: str, periods: int,
                        rows_max: int = 500) -> list[dict]:
    """StatisticSearch 원시 row 조회. item은 '코드' 또는 '코드/코드'(2차원 통계표)."""
    s, e = _period_bounds(cycle, periods)
    url = _SEARCH_TMPL.format(key=_key(), start=1, end=rows_max, stat=stat,
                              cycle=cycle.upper(), s=s, e=e, items=item)
    payload = await http.get_json(url, retries=1, timeout=15.0)
    return _extract_rows(payload, "StatisticSearch")


def build_series(rows: list[dict], label: str, stat: str, item: str,
                 cycle: str, *, extra: dict | None = None) -> dict:
    """StatisticSearch row → 시계열 + 요약 통계 정규화."""
    points = [{"time": r.get("TIME"), "value": to_float(r.get("DATA_VALUE"))}
              for r in rows if r.get("TIME") is not None]
    points.sort(key=lambda p: str(p["time"]))
    if not points:
        raise RuntimeError(f"'{label}' 시계열 데이터가 없습니다(코드/주기 확인)")
    unit = next((r.get("UNIT_NAME") for r in rows if r.get("UNIT_NAME")), None)
    lookbacks = _LOOKBACKS.get(cycle.upper(), {})
    out = {
        "name": label,
        "stat_code": stat,
        "item_code": item,
        "cycle": cycle.upper(),
        "unit": unit,
        "count": len(points),
        "points": points,
        "stats": summarize(points, value_key="value", label_key="time"),
        # 금리(연%)는 절대 변화(%p)가, 지수·잔액은 변화율이 의미 있어 둘 다 준다.
        "changes": recent_changes(points, lookbacks, pct=False),
        "changes_pct": recent_changes(points, lookbacks),
        "source": "ecos",
    }
    if extra:
        out.update(extra)
    return out


async def series(indicator: str, periods: int = 36) -> dict:
    """거시지표 시계열. indicator는 프리셋 이름 또는 '통계표코드/항목코드/주기'.

    예: '기준금리', '국고채3년', '722Y001/0101000/M'.
    periods는 조회할 관측 수(월 계열이면 개월 수).
    """
    spec = SERIES_PRESETS.get(indicator)
    if spec is None:
        parts = [p for p in indicator.replace(":", "/").split("/") if p]
        if len(parts) < 2:
            raise ValueError(
                f"알 수 없는 지표 '{indicator}'. 프리셋: {', '.join(SERIES_PRESETS)} "
                "또는 '통계표코드/항목코드/주기' 형식으로 지정하세요")
        spec = {"stat": parts[0], "item": parts[1],
                "cycle": parts[2] if len(parts) > 2 else "M", "label": indicator}

    rows = await search_series(spec["stat"], spec["item"], spec["cycle"], periods)
    return build_series(rows, spec["label"], spec["stat"], spec["item"],
                        spec["cycle"])


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
