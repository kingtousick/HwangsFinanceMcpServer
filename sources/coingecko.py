"""CoinGecko 크립토 소스 (KRW·USD 직접 지원).

실측(2026-06-22):
  GET /api/v3/simple/price?ids=bitcoin&vs_currencies=krw,usd&include_24hr_change=true
  → {"bitcoin":{"krw":99168890,"krw_24h_change":1.09,"usd":64495,"usd_24h_change":...}}

업비트/빗썸이 사내망에서 차단되므로 크립토 기본 소스로 사용한다.
심볼→CoinGecko id 매핑은 자주 쓰는 것을 정적 테이블로, 미스 시 /coins/list로 보강.
"""
from __future__ import annotations

from core import http
from core.schema import ok, now_kst_iso, to_float

_SIMPLE_URL = "https://api.coingecko.com/api/v3/simple/price"
_LIST_URL = "https://api.coingecko.com/api/v3/coins/list"

# 자주 쓰는 심볼 → id 정적 매핑
_STATIC_IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "XRP": "ripple",
    "SOL": "solana",
    "ADA": "cardano",
    "DOGE": "dogecoin",
    "USDT": "tether",
    "BNB": "binancecoin",
}

_dynamic_ids: dict[str, str] | None = None


async def _resolve_id(symbol: str) -> str:
    sym = symbol.upper()
    if sym in _STATIC_IDS:
        return _STATIC_IDS[sym]
    global _dynamic_ids
    if _dynamic_ids is None:
        coins = await http.get_json(_LIST_URL)
        # 동일 심볼이 여럿일 수 있으나 첫 매칭 사용(주요 코인은 정적 테이블로 커버)
        _dynamic_ids = {}
        for c in coins:
            s = str(c.get("symbol", "")).upper()
            _dynamic_ids.setdefault(s, c.get("id"))
    cid = _dynamic_ids.get(sym)
    if not cid:
        raise ValueError(f"unknown crypto symbol: {symbol}")
    return cid


async def get_price(symbol: str = "BTC", quote: str = "KRW") -> dict:
    """크립토 시세. quote는 'KRW' 또는 'USD'."""
    cur = quote.lower()
    cid = await _resolve_id(symbol)
    data = await http.get_json(
        _SIMPLE_URL,
        params={"ids": cid, "vs_currencies": cur, "include_24hr_change": "true"},
    )
    d = data[cid]
    return ok(
        symbol.upper(),
        d[cur],
        change_pct=to_float(d.get(f"{cur}_24h_change")),
        timestamp=now_kst_iso(),
        currency=quote.upper(),
        source="coingecko",
    )
