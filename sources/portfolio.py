"""로컬 보유자산 파일 기반 포트폴리오 평가 (계좌 연동·DB 없음 — Non-Goal 준수).

파일 스키마(JSON 기본, .yml/.yaml은 PyYAML 설치 시):
{
  "base_currency": "KRW",
  "holdings": [
    {"type": "stock",  "ticker": "005930", "name": "삼성전자", "quantity": 10, "avg_price": 68000},
    {"type": "stock",  "ticker": "AAPL",   "quantity": 3, "avg_price": 180},      // USD 자동 인식
    {"type": "etf",    "ticker": "381180", "quantity": 5, "avg_price": 15200},
    {"type": "crypto", "ticker": "BTC", "quote": "KRW", "quantity": 0.05, "avg_price": 92000000},
    {"type": "apt", "region": "강남구", "complex": "래미안", "quantity": 1,
     "avg_price": 250000, "pyeong": 25, "deal_ym": "202605", "months": 6}
  ],
  "cash": {"KRW": 5000000, "USD": 1000}
}

단위 규약:
  - stock/etf/crypto: avg_price는 해당 자산의 거래 통화 기준 1단위 가격
    (국내 주식 원, 미국 주식 USD, 크립토는 quote 통화).
  - apt: avg_price는 **만원 단위 호당 총 매입가**(실거래가 API 단위와 통일).
    현재가는 get_apt_trade_summary의 단지 평균 평당가 × pyeong(미지정 시 단지 평균
    거래가) — 최근 N개월 평균이라 참고용 추정치다.
  - deal_ym 미지정 시 전월(실거래 신고 지연 고려), months 기본 6.

평가는 부분 성공 허용: 개별 종목 시세 실패는 holdings[i].price_error로만 남기고
총계·배분에서 제외한다(get_rail_project_status와 동일 철학). USD 자산이 있으면
환율(USD/KRW)을 1회 조회해 원화 환산한다.

시세 fetcher는 finance_server.py의 기존 tool 함수를 dict로 주입받는다
(순환 임포트 회피 + 테스트에서 더미 주입 용이).
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta

from core.schema import KST, now_kst_iso, to_float

_TYPE_LABEL = {"stock": "주식", "etf": "ETF", "crypto": "크립토", "apt": "부동산"}


# ---------------------------------------------------------------- 파일 로드


def load_file(path: str | None) -> tuple[dict, str]:
    """포트폴리오 파일 로드. 반환: (data, 절대경로). 실패 시 안내 포함 예외."""
    p = path or os.environ.get("PORTFOLIO_FILE_PATH") or "portfolio.json"
    ap = os.path.abspath(p)
    if not os.path.exists(ap):
        raise RuntimeError(
            f"포트폴리오 파일이 없습니다: '{ap}'. path 인자 또는 PORTFOLIO_FILE_PATH "
            "환경변수로 지정하세요(스키마는 README '포트폴리오 스냅샷' 참고)")
    with open(ap, encoding="utf-8") as f:
        text = f.read()
    if ap.lower().endswith((".yml", ".yaml")):
        try:
            import yaml
        except ImportError as e:
            raise RuntimeError(
                "YAML 포트폴리오는 PyYAML이 필요합니다: pip install pyyaml "
                "(또는 JSON으로 작성)") from e
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("holdings"), list):
        raise RuntimeError("포트폴리오 스키마 오류: 최상위에 holdings 리스트가 필요합니다")
    return data, ap


def _last_month_ym() -> str:
    now = datetime.now(tz=KST)
    return (now.replace(day=1) - timedelta(days=1)).strftime("%Y%m")


# ---------------------------------------------------------------- 개별 평가


async def _eval_one(h: dict, fetchers: dict) -> dict:
    t = str(h.get("type") or "").lower()
    out = {
        "type": t,
        "ticker": h.get("ticker"),
        "name": h.get("name"),
        "quantity": to_float(h.get("quantity")) or 0.0,
        "avg_price": to_float(h.get("avg_price")),
        "currency": "KRW",
        "current_price": None,
        "price_source": None,
        "price_error": None,
    }
    try:
        if t in ("stock", "etf"):
            ticker = str(h.get("ticker") or "").strip()
            if not ticker:
                raise ValueError("ticker가 없습니다")
            r = await fetchers[t](ticker)
            if "error" in r:
                raise RuntimeError(r["error"])
            out.update(current_price=r.get("value"),
                       currency=r.get("currency") or "KRW",
                       name=out["name"] or r.get("name"),
                       price_source=r.get("source"))
        elif t == "crypto":
            sym = str(h.get("ticker") or "BTC")
            quote = str(h.get("quote") or "KRW").upper()
            r = await fetchers["crypto"](sym, quote)
            if "error" in r:
                raise RuntimeError(r["error"])
            out.update(current_price=r.get("value"),
                       currency=r.get("currency") or quote,
                       name=out["name"] or r.get("name") or sym,
                       price_source=r.get("source"))
        elif t == "apt":
            region = h.get("region")
            comp = h.get("complex") or h.get("name")
            if not region or not comp:
                raise ValueError("apt 보유분은 region과 complex(단지명)가 필요합니다")
            ym = str(h.get("deal_ym") or _last_month_ym())
            months = int(h.get("months") or 6)
            r = await fetchers["apt"](str(region), ym, months)
            if "error" in r:
                raise RuntimeError(r["error"])
            match = next((it for it in r.get("items", [])
                          if str(comp) in str(it.get("apt") or "")), None)
            if match is None:
                raise RuntimeError(
                    f"단지 매칭 실패: '{comp}' (region={region}, "
                    f"period={r.get('period') or ym}) — 거래가 없거나 단지명 확인 필요")
            pyeong = to_float(h.get("pyeong"))
            ppp = to_float(match.get("avg_price_per_pyeong"))
            if pyeong and ppp:
                cur_manwon = ppp * pyeong
            else:
                cur_manwon = to_float(match.get("avg_deal_amount"))
            if not cur_manwon:
                raise RuntimeError(f"단지 '{comp}' 평가액 산출 불가(표본 부족)")
            out.update(current_price=round(cur_manwon * 10000.0),  # 만원 → 원(호당)
                       name=out["name"] or match.get("apt"),
                       price_source="molit_summary",
                       matched_complex=match.get("apt"),
                       matched_deal_count=match.get("count"),
                       valuation_note=f"최근 {months}개월 단지평균 기준 추정치")
            # apt avg_price는 만원 단위 총 매입가 → 원 단위로 통일
            if out["avg_price"] is not None:
                out["avg_price"] = round(out["avg_price"] * 10000.0)
        else:
            raise ValueError(f"알 수 없는 type: '{t}' (stock/etf/crypto/apt)")
    except Exception as e:  # noqa: BLE001 - 개별 실패는 부분 성공으로 흡수
        out["price_error"] = str(e)
    return out


# ---------------------------------------------------------------- 스냅샷 조립


async def snapshot(data: dict, fetchers: dict) -> dict:
    """보유자산 평가·손익·자산배분 스냅샷. 개별 시세 실패는 부분 성공 처리."""
    holdings = [h for h in data.get("holdings", []) if isinstance(h, dict)]
    cash = data.get("cash") or {}

    evaluated = await asyncio.gather(*[_eval_one(h, fetchers) for h in holdings])

    # USD 환산 필요 여부 → 환율 1회 조회
    needs_fx = any(h["currency"] == "USD" and h["price_error"] is None
                   for h in evaluated)
    needs_fx = needs_fx or any(k.upper() == "USD" for k in cash)
    fx_rate = None
    fx_error = None
    if needs_fx:
        try:
            fx = await fetchers["fx"]()
            if "error" in fx:
                raise RuntimeError(fx["error"])
            fx_rate = to_float(fx.get("value"))
        except Exception as e:  # noqa: BLE001
            fx_error = f"USD/KRW 환율 조회 실패: {e}"

    def _to_krw(amount: float | None, currency: str) -> float | None:
        if amount is None:
            return None
        if currency == "KRW":
            return amount
        if currency == "USD" and fx_rate:
            return amount * fx_rate
        return None  # 미지원 통화 또는 환율 실패

    errors: list[dict] = []
    alloc: dict[str, float] = {}
    total_cost = total_value = 0.0
    for i, h in enumerate(evaluated):
        if h["price_error"] is not None:
            errors.append({"index": i, "ticker": h.get("ticker") or h.get("name"),
                           "error": h["price_error"]})
            continue
        qty, cur = h["quantity"], h["current_price"]
        value_krw = _to_krw(cur * qty if cur is not None else None, h["currency"])
        cost_krw = _to_krw(h["avg_price"] * qty if h["avg_price"] is not None else None,
                           h["currency"])
        if value_krw is None:
            h["price_error"] = (fx_error or f"통화 환산 불가: {h['currency']}")
            errors.append({"index": i, "ticker": h.get("ticker") or h.get("name"),
                           "error": h["price_error"]})
            continue
        h["current_value_krw"] = round(value_krw)
        if cost_krw is not None:
            h["cost_krw"] = round(cost_krw)
            h["unrealized_pnl_krw"] = round(value_krw - cost_krw)
            h["return_pct"] = round((value_krw / cost_krw - 1) * 100, 2) if cost_krw else None
            total_cost += cost_krw
        total_value += value_krw
        label = _TYPE_LABEL.get(h["type"], h["type"])
        alloc[label] = alloc.get(label, 0.0) + value_krw

    # 현금(KRW 그대로, USD는 환산)
    cash_krw = 0.0
    for ccy, amt in cash.items():
        v = _to_krw(to_float(amt), str(ccy).upper())
        if v is None:
            errors.append({"index": None, "ticker": f"cash:{ccy}",
                           "error": fx_error or f"통화 환산 불가: {ccy}"})
            continue
        cash_krw += v
    if cash_krw:
        alloc["현금"] = alloc.get("현금", 0.0) + cash_krw
    grand_total = total_value + cash_krw

    allocation_pct = {k: round(v / grand_total * 100, 1) for k, v in alloc.items()} \
        if grand_total else {}

    return {
        "name": "포트폴리오 스냅샷",
        "as_of": now_kst_iso(),
        "base_currency": "KRW",
        "usd_krw": fx_rate,
        "holdings": evaluated,
        "cash": cash,
        "cash_value_krw": round(cash_krw),
        "allocation_pct": allocation_pct,
        "totals": {
            "total_cost_krw": round(total_cost),
            "total_value_krw": round(total_value),
            "total_with_cash_krw": round(grand_total),
            "total_pnl_krw": round(total_value - total_cost),
            "total_return_pct": round((total_value / total_cost - 1) * 100, 2)
            if total_cost else None,
        },
        "errors": errors,
        "source": "portfolio_local",
    }
