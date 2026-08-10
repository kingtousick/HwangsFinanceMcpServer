"""Yahoo Finance chart v8 소스 (httpx 직접 호출, yfinance 미사용).

실측(2026-06-22) 스키마:
  GET https://query1.finance.yahoo.com/v8/finance/chart/^GSPC?range=1d&interval=1d
  → {"chart":{"result":[{"meta":{"regularMarketPrice":7500.58,
        "chartPreviousClose":7420.1,"currency":"USD",
        "regularMarketTime":1781815326, ...}}]}}

미국 주식/지수(^GSPC, ^IXIC, ^SOX, 티커)와 환율(KRW=X)을 동일 함수로 처리.
query1 실패 시 query2 호스트로 강등.
"""
from __future__ import annotations

import urllib.parse
from datetime import datetime, timedelta, timezone

from core import http
from core.schema import ok, epoch_to_kst_iso, to_float
from core.series import PERIODS_PER_YEAR, summarize

_HOSTS = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]
_PATH = "/v8/finance/chart/{symbol}"

# Yahoo가 허용하는 range/interval 값(그 외는 422). 관측 수가 과하지 않도록
# period별 기본 interval을 정해 둔다(1y·일봉=250점 → 주봉 52점).
VALID_PERIODS = ("5d", "1mo", "3mo", "6mo", "ytd", "1y", "2y", "5y", "10y", "max")
VALID_INTERVALS = ("1d", "1wk", "1mo")
_AUTO_INTERVAL = {
    "5d": "1d", "1mo": "1d", "3mo": "1d", "ytd": "1wk", "6mo": "1wk",
    "1y": "1wk", "2y": "1wk", "5y": "1mo", "10y": "1mo", "max": "1mo",
}


def auto_interval(period: str) -> str:
    """period에 맞는 기본 interval(관측 50~120점 수준)."""
    return _AUTO_INTERVAL.get(period, "1d")


async def get_meta(symbol: str) -> dict:
    """chart v8 응답의 meta 블록만 반환(query1 실패 시 query2로 강등).

    meta에는 시세뿐 아니라 currency/exchangeName/fiftyTwoWeekHigh/Low가 들어 있어
    quoteSummary가 막혔을 때의 최소 폴백 정보원으로도 쓴다.
    """
    enc = urllib.parse.quote(symbol, safe="")
    params = {"range": "1d", "interval": "1d"}
    last_exc: Exception | None = None
    for host in _HOSTS:
        url = f"https://{host}{_PATH.format(symbol=enc)}"
        try:
            data = await http.get_json(url, params=params, retries=0)
            return data["chart"]["result"][0]["meta"]
        except Exception as e:  # noqa: BLE001 - query2로 강등
            last_exc = e
    raise last_exc  # type: ignore[misc]


async def get_quote(symbol: str, name: str | None = None) -> dict:
    """Yahoo 심볼 시세. symbol 예: '^GSPC', '^SOX', 'AAPL', 'KRW=X'."""
    m = await get_meta(symbol)
    value = to_float(m.get("regularMarketPrice"))
    prev = to_float(m.get("chartPreviousClose"))
    change = None
    pct = None
    if value is not None and prev not in (None, 0):
        change = value - prev
        pct = change / prev * 100
    return ok(
        name or m.get("shortName") or symbol,
        value,
        change=change,
        change_pct=pct,
        timestamp=epoch_to_kst_iso(m.get("regularMarketTime")),
        currency=m.get("currency"),
        source="yahoo",
    )


def _local_date(epoch, gmtoffset: int) -> str | None:
    """거래소 현지 날짜(YYYY-MM-DD).

    KST로 변환하면 미국장 마감(16:00 ET)이 다음 날로 밀려 날짜가 하루씩
    어긋난다. meta.gmtoffset으로 거래소 로컬 시각을 만들어 날짜만 취한다.
    """
    if epoch is None:
        return None
    try:
        tz = timezone(timedelta(seconds=int(gmtoffset or 0)))
        return datetime.fromtimestamp(float(epoch), tz=tz).strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return None


async def get_history(symbol: str, period: str = "1y",
                      interval: str | None = None,
                      name: str | None = None) -> dict:
    """Yahoo 심볼 시계열(OHLCV). get_quote와 동일 엔드포인트의 배열부를 파싱한다.

    interval 미지정 시 period에 맞춰 자동 선택(auto_interval).
    """
    enc = urllib.parse.quote(symbol, safe="")
    iv = interval or auto_interval(period)
    params = {"range": period, "interval": iv}
    last_exc: Exception | None = None
    for host in _HOSTS:
        url = f"https://{host}{_PATH.format(symbol=enc)}"
        try:
            data = await http.get_json(url, params=params, retries=0, timeout=10.0)
            r = data["chart"]["result"][0]
            m = r.get("meta") or {}
            stamps = r.get("timestamp") or []
            quote = ((r.get("indicators") or {}).get("quote") or [{}])[0]
            gmt = m.get("gmtoffset")

            points = []
            for i, ts in enumerate(stamps):
                close = to_float((quote.get("close") or [None] * len(stamps))[i])
                if close is None:
                    continue  # 휴장·결측 봉 제외
                points.append({
                    "date": _local_date(ts, gmt),
                    "open": to_float((quote.get("open") or [None] * len(stamps))[i]),
                    "high": to_float((quote.get("high") or [None] * len(stamps))[i]),
                    "low": to_float((quote.get("low") or [None] * len(stamps))[i]),
                    "close": close,
                    "volume": to_float((quote.get("volume") or [None] * len(stamps))[i]),
                })
            if not points:
                raise RuntimeError(f"no data points for {symbol} (period={period})")

            granularity = m.get("dataGranularity") or iv
            return {
                "name": name or m.get("shortName") or symbol,
                "symbol": m.get("symbol") or symbol,
                "period": period,
                "interval": granularity,
                "currency": m.get("currency"),
                "count": len(points),
                "points": points,
                "stats": summarize(
                    points, periods_per_year=PERIODS_PER_YEAR.get(granularity, 252)),
                "week52_high": to_float(m.get("fiftyTwoWeekHigh")),
                "week52_low": to_float(m.get("fiftyTwoWeekLow")),
                "source": "yahoo",
            }
        except Exception as e:  # noqa: BLE001 - query2로 강등
            last_exc = e
    raise last_exc  # type: ignore[misc]
