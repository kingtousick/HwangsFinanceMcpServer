"""네이버 금융 polling JSON 소스.

실측(2026-06-22)으로 확인한 스키마:
  GET https://polling.finance.naver.com/api/realtime/domestic/index/KOSPI
  → {"datas":[{"closePriceRaw":"9167.34","compareToPreviousClosePriceRaw":"114.92",
               "fluctuationsRatioRaw":"1.27","localTradedAt":"2026-06-22T10:50:47+09:00",
               "compareToPreviousPrice":{"code":"2",...}, ...}]}
  종목/ETF: .../domestic/stock/{6자리코드}  (closePrice 콤마 포함, *Raw 동일 제공)

*Raw 필드는 콤마가 없어 그대로 float 변환 가능하므로 우선 사용한다.
compareToPreviousPrice.code: 2=상승(+), 5=하락(-) → 하락 시 부호 보정.
"""
from __future__ import annotations

from core import http
from core.schema import ok, to_float

_INDEX_URL = "https://polling.finance.naver.com/api/realtime/domestic/index/{code}"
_STOCK_URL = "https://polling.finance.naver.com/api/realtime/domestic/stock/{code}"

# compareToPreviousPrice.code 가 하락 계열이면 음수 부호 부여
_DOWN_CODES = {"3", "4", "5"}  # 3=하한, 4=하락, 5=보합하락 계열 (방어적으로 포함)


def _parse(d: dict, name: str) -> dict:
    value = to_float(d.get("closePriceRaw") or d.get("closePrice"))
    change = to_float(d.get("compareToPreviousClosePriceRaw")
                      or d.get("compareToPreviousClosePrice"))
    pct = to_float(d.get("fluctuationsRatioRaw") or d.get("fluctuationsRatio"))

    code = (d.get("compareToPreviousPrice") or {}).get("code")
    if code in _DOWN_CODES:
        if change is not None and change > 0:
            change = -change
        if pct is not None and pct > 0:
            pct = -pct

    return ok(
        name,
        value,
        change=change,
        change_pct=pct,
        timestamp=d.get("localTradedAt"),
        currency="KRW",
        source="naver",
    )


async def get_index(code: str, name: str | None = None) -> dict:
    """KOSPI / KOSDAQ 지수. code는 'KOSPI' 또는 'KOSDAQ'."""
    data = await http.get_json(_INDEX_URL.format(code=code))
    d = data["datas"][0]
    return _parse(d, name or code)


async def get_stock(stock_code: str, name: str | None = None) -> dict:
    """국내 주식/ETF. stock_code는 6자리 코드(예: '005930', '381180')."""
    data = await http.get_json(_STOCK_URL.format(code=stock_code))
    d = data["datas"][0]
    return _parse(d, name or d.get("stockName") or stock_code)
