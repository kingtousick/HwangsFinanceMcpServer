"""업비트 크립토 KRW 소스 (선택적 폴백).

사내망에서는 차단되지만(가정용 PC에서는 통과), CoinGecko 실패 시 강등 대상.
  GET https://api.upbit.com/v1/ticker?markets=KRW-BTC
  → [{"trade_price":..., "signed_change_rate":...}]
"""
from __future__ import annotations

from core import http
from core.schema import ok, now_kst_iso, to_float

_URL = "https://api.upbit.com/v1/ticker"


async def get_price(symbol: str = "BTC") -> dict:
    """업비트 KRW 마켓 시세."""
    data = await http.get_json(_URL, params={"markets": f"KRW-{symbol.upper()}"})
    d = data[0]
    return ok(
        symbol.upper(),
        d["trade_price"],
        change_pct=(to_float(d.get("signed_change_rate")) or 0) * 100,
        timestamp=now_kst_iso(),
        currency="KRW",
        source="upbit",
    )
