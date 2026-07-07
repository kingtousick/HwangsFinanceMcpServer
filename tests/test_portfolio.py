"""포트폴리오 소스/Tool 테스트 (외부 HTTP 없음 — 더미 fetcher 주입).

검증:
  - 평가·손익·자산배분 계산(주식/apt/현금)
  - USD 자산 환율 환산(환율 1회 조회)
  - 개별 시세 실패 → 부분 성공(errors에만 기록, 총계 제외)
  - 파일 로드: 없음/스키마 오류 안내, JSON 정상
  - Tool 종단: 파일 없음 → fail(안내 메시지)
"""
import json

import pytest

import finance_server as srv
from core import cache, http
from sources import portfolio


@pytest.fixture(autouse=True)
async def _reset():
    cache.clear()
    await http.aclose()
    yield
    await http.aclose()


def _fetchers(etf_error=False):
    calls = {"fx": 0}

    async def stock(t):
        if t.isdigit():
            return {"name": "삼성전자", "value": 72000.0, "currency": "KRW",
                    "source": "naver"}
        return {"name": t, "value": 200.0, "currency": "USD", "source": "yahoo"}

    async def etf(t):
        if etf_error:
            return {"name": t, "error": "boom", "source": "fallback"}
        return {"name": "TIGER", "value": 16000.0, "currency": "KRW", "source": "naver"}

    async def crypto(s, q):
        return {"name": s, "value": 100000000.0, "currency": q, "source": "coingecko"}

    async def apt(region, ym, months):
        return {"period": f"{ym}~", "items": [
            {"apt": "래미안팰리스", "avg_price_per_pyeong": 10000.0,
             "avg_deal_amount": 240000.0, "count": 3}]}

    async def fx():
        calls["fx"] += 1
        return {"value": 1400.0}

    return {"stock": stock, "etf": etf, "crypto": crypto, "apt": apt,
            "fx": fx, "_calls": calls}


async def test_snapshot_totals_and_allocation():
    data = {
        "holdings": [
            {"type": "stock", "ticker": "005930", "quantity": 10, "avg_price": 68000},
            {"type": "apt", "region": "강남구", "complex": "래미안", "quantity": 1,
             "avg_price": 250000, "pyeong": 25},  # 평가 10000만원/평×25평=25억
        ],
        "cash": {"KRW": 1000000},
    }
    snap = await portfolio.snapshot(data, _fetchers())
    st, apt = snap["holdings"]
    assert st["current_value_krw"] == 720000
    assert st["unrealized_pnl_krw"] == 40000
    assert st["return_pct"] == pytest.approx(5.88, abs=0.01)
    assert apt["current_price"] == 2_500_000_000          # 만원→원
    assert apt["avg_price"] == 2_500_000_000              # 만원 매입가→원 통일
    assert apt["unrealized_pnl_krw"] == 0
    assert apt["matched_complex"] == "래미안팰리스"
    assert snap["cash_value_krw"] == 1000000
    t = snap["totals"]
    assert t["total_value_krw"] == 720000 + 2_500_000_000
    assert t["total_with_cash_krw"] == t["total_value_krw"] + 1000000
    assert set(snap["allocation_pct"]) == {"주식", "부동산", "현금"}
    assert sum(snap["allocation_pct"].values()) == pytest.approx(100.0, abs=0.3)
    assert snap["errors"] == []
    assert snap["usd_krw"] is None  # USD 없으면 환율 조회 안 함


async def test_usd_conversion_single_fx_call():
    f = _fetchers()
    data = {"holdings": [
        {"type": "stock", "ticker": "AAPL", "quantity": 3, "avg_price": 180},
        {"type": "stock", "ticker": "MSFT", "quantity": 1, "avg_price": 100},
    ], "cash": {"USD": 100}}
    snap = await portfolio.snapshot(data, f)
    assert f["_calls"]["fx"] == 1  # 환율은 1회만
    aapl = snap["holdings"][0]
    assert aapl["current_value_krw"] == 3 * 200 * 1400
    assert aapl["cost_krw"] == 3 * 180 * 1400
    assert snap["cash_value_krw"] == 100 * 1400
    assert snap["usd_krw"] == pytest.approx(1400.0)


async def test_partial_failure_excluded_from_totals():
    data = {"holdings": [
        {"type": "stock", "ticker": "005930", "quantity": 1, "avg_price": 68000},
        {"type": "etf", "ticker": "381180", "quantity": 5, "avg_price": 15200},
    ]}
    snap = await portfolio.snapshot(data, _fetchers(etf_error=True))
    assert len(snap["errors"]) == 1
    assert snap["errors"][0]["ticker"] == "381180"
    assert snap["holdings"][1]["price_error"] == "boom"
    assert snap["totals"]["total_value_krw"] == 72000  # 실패분 제외


async def test_apt_match_failure_is_partial():
    data = {"holdings": [
        {"type": "apt", "region": "강남구", "complex": "없는단지", "quantity": 1,
         "avg_price": 100000},
    ]}
    snap = await portfolio.snapshot(data, _fetchers())
    assert "단지 매칭 실패" in snap["holdings"][0]["price_error"]
    assert snap["totals"]["total_value_krw"] == 0


def test_load_file_missing_gives_guidance(tmp_path):
    with pytest.raises(RuntimeError, match="PORTFOLIO_FILE_PATH"):
        portfolio.load_file(str(tmp_path / "nope.json"))


def test_load_file_bad_schema(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps([1, 2]), encoding="utf-8")
    with pytest.raises(RuntimeError, match="holdings"):
        portfolio.load_file(str(p))


def test_load_file_json_ok(tmp_path):
    p = tmp_path / "pf.json"
    p.write_text(json.dumps({"holdings": [], "cash": {"KRW": 1}}), encoding="utf-8")
    data, ap = portfolio.load_file(str(p))
    assert data["cash"]["KRW"] == 1
    assert ap == str(p)


async def test_tool_missing_file_returns_fail(tmp_path):
    res = await srv.get_portfolio_snapshot(str(tmp_path / "none.json"))
    assert res["source"] == "fallback"
    assert "포트폴리오 파일이 없습니다" in res["error"]
