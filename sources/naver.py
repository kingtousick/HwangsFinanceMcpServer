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

import ast
from datetime import datetime, timedelta

from core import http
from core.schema import KST, ok, to_float
from core.series import PERIODS_PER_YEAR, summarize

_INDEX_URL = "https://polling.finance.naver.com/api/realtime/domestic/index/{code}"
_STOCK_URL = "https://polling.finance.naver.com/api/realtime/domestic/stock/{code}"

# 일별시세(비공식). 응답은 JSON이 아니라 JS 배열 리터럴이라 ast로 파싱한다.
#   [['날짜','시가','고가','저가','종가','거래량','외국인소진율'],
#    ["20260807", 235000, 239500, 229000, 231000, 20424708, 46.63], ...]
_SISE_URL = "https://api.finance.naver.com/siseJson.naver"

# period → 조회 시작일 계산용 일수(주말·휴장 감안해 넉넉히)
_PERIOD_DAYS = {
    "5d": 12, "1mo": 40, "3mo": 100, "6mo": 195,
    "1y": 380, "2y": 750, "5y": 1850, "10y": 3680, "max": 12000,
}
_TIMEFRAME = {"1d": "day", "1wk": "week", "1mo": "month"}


def _fmt(d: datetime) -> str:
    return d.strftime("%Y%m%d")


def _start_time(period: str, today: datetime) -> str:
    if period == "ytd":
        return _fmt(today.replace(month=1, day=1))
    return _fmt(today - timedelta(days=_PERIOD_DAYS.get(period, 380)))

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


def _parse_sise(text: str) -> list[list]:
    """siseJson 응답(JS 배열 리터럴)을 파싱. literal_eval은 코드 실행 위험이 없다."""
    body = text.strip()
    if not body.startswith("["):
        raise RuntimeError(f"unexpected siseJson body: {body[:80]}")
    rows = ast.literal_eval(body)
    if not isinstance(rows, list) or len(rows) < 2:
        raise RuntimeError("siseJson: 데이터 행이 없습니다(코드/기간 확인)")
    return rows


async def get_history(stock_code: str, period: str = "1y",
                      interval: str = "1d", name: str | None = None) -> dict:
    """국내 주식/ETF 일·주·월봉 시계열. stock_code는 6자리 코드.

    네이버 일별시세는 외국인소진율(foreign_ratio)을 함께 준다.
    """
    today = datetime.now(tz=KST)
    params = {
        "symbol": stock_code,
        "requestType": "1",
        "startTime": _start_time(period, today),
        "endTime": _fmt(today),
        "timeframe": _TIMEFRAME.get(interval, "day"),
    }
    text = await http.get_text(_SISE_URL, params=params, retries=1)
    rows = _parse_sise(text)

    header = [str(h).strip() for h in rows[0]]
    idx = {k: header.index(k) for k in header}

    def _col(row, key):
        i = idx.get(key)
        return to_float(row[i]) if i is not None and i < len(row) else None

    points = []
    for row in rows[1:]:
        if not isinstance(row, (list, tuple)) or not row:
            continue
        close = _col(row, "종가")
        if close is None:
            continue
        raw_date = str(row[idx.get("날짜", 0)])
        points.append({
            "date": f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
                    if len(raw_date) == 8 else raw_date,
            "open": _col(row, "시가"),
            "high": _col(row, "고가"),
            "low": _col(row, "저가"),
            "close": close,
            "volume": _col(row, "거래량"),
            "foreign_ratio": _col(row, "외국인소진율"),
        })
    if not points:
        raise RuntimeError(f"no data points for {stock_code} (period={period})")

    return {
        "name": name or stock_code,
        "symbol": stock_code,
        "period": period,
        "interval": interval,
        "currency": "KRW",
        "count": len(points),
        "points": points,
        "stats": summarize(points,
                           periods_per_year=PERIODS_PER_YEAR.get(interval, 252)),
        "source": "naver",
    }
