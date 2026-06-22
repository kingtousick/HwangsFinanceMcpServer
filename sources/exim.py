"""한국수출입은행 환율 API (선택적 폴백, EXIM_API_KEY 필요).

사내망에서는 연결이 차단되지만(가정용 PC에서는 통과), Yahoo 환율 실패 시 강등 대상.
  GET https://www.koreaexim.go.kr/site/program/financial/exchangeJSON?authkey=KEY&data=AP01
  → [{"cur_unit":"USD","deal_bas_r":"1,536.00","cur_nm":"미국 달러", ...}]
deal_bas_r(매매기준율)을 value로 사용. 등락 정보는 제공하지 않음(null).
"""
from __future__ import annotations

import os

from core import http
from core.schema import ok, now_kst_iso

_URL = "https://www.koreaexim.go.kr/site/program/financial/exchangeJSON"


async def get_rate(cur_unit: str = "USD") -> dict:
    """매매기준율. cur_unit 예: 'USD'. 반환 currency는 KRW(1 cur_unit당 원)."""
    key = os.environ.get("EXIM_API_KEY")
    if not key:
        raise RuntimeError("EXIM_API_KEY not set")
    data = await http.get_json(_URL, params={"authkey": key, "data": "AP01"})
    row = next((r for r in data if r.get("cur_unit", "").startswith(cur_unit)), None)
    if not row:
        raise ValueError(f"cur_unit not found: {cur_unit}")
    return ok(
        f"{cur_unit}/KRW",
        row["deal_bas_r"],
        timestamp=now_kst_iso(),
        currency="KRW",
        source="exim",
    )
