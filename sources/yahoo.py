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

from core import http
from core.schema import ok, epoch_to_kst_iso, to_float

_HOSTS = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]
_PATH = "/v8/finance/chart/{symbol}"


async def get_quote(symbol: str, name: str | None = None) -> dict:
    """Yahoo 심볼 시세. symbol 예: '^GSPC', '^SOX', 'AAPL', 'KRW=X'."""
    enc = urllib.parse.quote(symbol, safe="")
    params = {"range": "1d", "interval": "1d"}
    last_exc: Exception | None = None
    for host in _HOSTS:
        url = f"https://{host}{_PATH.format(symbol=enc)}"
        try:
            data = await http.get_json(url, params=params, retries=0)
            m = data["chart"]["result"][0]["meta"]
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
        except Exception as e:  # noqa: BLE001 - query2로 강등
            last_exc = e
    raise last_exc  # type: ignore[misc]
