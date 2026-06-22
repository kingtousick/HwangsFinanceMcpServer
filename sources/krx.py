"""KRX MDC JSON 소스 (국내 2순위, best-effort).

주의: KRX data.krx.co.kr MDC는 bld 파라미터와 Referer/OTP 흐름에 민감하며,
단순 POST는 'LOGOUT'/400을 반환할 수 있다(2026-06-22 실측 확인). 본 모듈은
응답이 기대 구조(OutBlock_1)가 아니면 예외를 던져 상위 cascade가 다음 소스로
강등하도록 한다. 실서비스 활성화 전 bld/파라미터를 1회 실호출로 검증할 것.

네이버 polling이 국내 지수/종목을 안정적으로 커버하므로 이 소스는 보조 수단이다.
"""
from __future__ import annotations

from core import http
from core.schema import ok, now_kst_iso

_URL = "http://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
_HEADERS = {
    "Referer": "http://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd",
    "X-Requested-With": "XMLHttpRequest",
}

# 전종목 일별 시세 (종목코드 -> 종가). bld는 KRX 개편 시 바뀔 수 있어 검증 필요.
_STOCK_BLD = "dbms/MDC/STAT/standard/MDCSTAT01501"


async def get_stock(stock_code: str, trd_dd: str, name: str | None = None) -> dict:
    """국내 종목 일별 종가. trd_dd 형식 'YYYYMMDD'. 검증 전이므로 실패 시 예외."""
    client = http.get_client()
    r = await client.post(
        _URL,
        headers={**http.get_client().headers, **_HEADERS},
        data={"bld": _STOCK_BLD, "trdDd": trd_dd, "share": "1", "money": "1"},
    )
    r.raise_for_status()
    data = r.json()
    rows = data.get("OutBlock_1") or data.get("output")
    if not rows:
        raise ValueError("KRX MDC unexpected response (bld 검증 필요)")
    row = next((x for x in rows if x.get("ISU_SRT_CD") == stock_code), None)
    if not row:
        raise ValueError(f"stock not found in KRX response: {stock_code}")
    return ok(
        name or row.get("ISU_ABBRV") or stock_code,
        row.get("TDD_CLSPRC"),
        change=row.get("CMPPREVDD_PRC"),
        change_pct=row.get("FLUC_RT"),
        timestamp=now_kst_iso(),
        currency="KRW",
        source="krx",
    )
