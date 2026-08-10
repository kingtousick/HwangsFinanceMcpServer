"""get_capex_series / get_sec_fundamentals 테스트 (respx 모킹).

핵심은 현금흐름표 YTD 누적 → 분기 단독값 차분이 실제 SEC 응답 모양에서
정확히 동작하는지다.
"""
from __future__ import annotations

import httpx
import pytest
import respx

import finance_server as srv
from core.ratelimit import RateLimiter
from sources import sec, sec_metrics

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
CC = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/us-gaap/{tag}.json"

CAPEX = "PaymentsToAcquirePropertyPlantAndEquipment"
CAPEX_ALT = "PaymentsToAcquireProductiveAssets"
OCF = "NetCashProvidedByUsedInOperatingActivities"
REV = "RevenueFromContractWithCustomerExcludingAssessedTax"
CIK = "0001652044"   # Alphabet


@pytest.fixture(autouse=True)
def _ua(monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", "Test User test@example.com")
    monkeypatch.setattr(sec, "_LIMITER", RateLimiter(10_000.0))


def _mock_tickers():
    respx.get(TICKERS_URL).mock(return_value=httpx.Response(200, json={
        "0": {"cik_str": 1652044, "ticker": "GOOGL", "title": "Alphabet Inc."}}))


def _cc(facts, unit="USD"):
    return {"cik": 1652044, "units": {unit: facts}}


def _mock_concept(tag, body, status=200):
    return respx.get(CC.format(cik=CIK, tag=tag)).mock(
        return_value=httpx.Response(status, json=body))


def _mock_missing(*tags):
    """대체 태그 후보를 전부 404로 막는다.

    모킹하지 않고 두면 respx가 '매칭 없음' 예외를 내는데, 그건 HTTP 오류가
    아니라 재시도 대상이라 테스트가 백오프만큼 느려진다.
    """
    for t in tags:
        _mock_concept(t, {}, status=404)


def _mock_no_ocf_rev():
    _mock_missing(*sec_metrics._OCF_TAGS, *sec_metrics._REV_TAGS)


def _f(start, end, val, form="10-Q", filed="2026-05-01"):
    return {"start": start, "end": end, "val": val, "form": form,
            "filed": filed, "accn": f"a-{end}", "fy": 2026, "fp": "Q1"}


def _ytd_year(year, q1, h1, m9, fy, form_fy="10-K"):
    """달력연도 결산 기업의 전형적인 누적(YTD) 공시 세트."""
    return [
        _f(f"{year}-01-01", f"{year}-03-31", q1),
        _f(f"{year}-01-01", f"{year}-06-30", h1),
        _f(f"{year}-01-01", f"{year}-09-30", m9),
        _f(f"{year}-01-01", f"{year}-12-31", fy, form=form_fy),
    ]


def _by_fp(series):
    return {(r["fy"], r["fp"]): r for r in series}


# ---------------------------------------------------------------- YTD 차분 ★

@respx.mock
async def test_capex_quarterly_values_from_ytd_cumulative():
    _mock_tickers()
    # 2025년 capex 누적: 3M 20, 6M 45, 9M 75, FY 110 → 분기 20/25/30/35
    _mock_concept(CAPEX, _cc(_ytd_year(2025, 20.0, 45.0, 75.0, 110.0)))
    _mock_concept(OCF, _cc(_ytd_year(2025, 40.0, 90.0, 150.0, 220.0)))
    _mock_concept(REV, _cc([
        # 손익계산서는 3개월 단독 공시가 흔하다 → 차분하지 않는다
        _f("2025-01-01", "2025-03-31", 100.0),
        _f("2025-04-01", "2025-06-30", 110.0),
        _f("2025-07-01", "2025-09-30", 120.0),
        _f("2025-10-01", "2025-12-31", 130.0),
        _f("2025-01-01", "2025-12-31", 460.0, form="10-K"),
    ]))

    r = await srv.get_capex_series("GOOGL", 8)
    rows = _by_fp(r["series"])
    assert [rows[(2025, f"Q{i}")]["capex"] for i in (1, 2, 3, 4)] == [20, 25, 30, 35]
    assert rows[(2025, "Q1")]["capex_derived"] is False   # Q1 누적 = Q1 단독
    assert rows[(2025, "Q4")]["capex_derived"] is True    # 110 − 75
    assert rows[(2025, "Q4")]["ocf_derived"] is True      # 220 − 150
    assert rows[(2025, "Q4")]["revenue_derived"] is False  # 직접 공시
    # FCF = OCF − CAPEX (분기 단독 기준)
    assert rows[(2025, "Q4")]["fcf"] == 70.0 - 35.0
    assert rows[(2025, "Q4")]["capex_to_revenue_pct"] == round(35 / 130 * 100, 2)
    assert r["tags"] == {"capex": CAPEX, "ocf": OCF, "revenue": REV}
    assert r["data_kind"] == "filing"


@respx.mock
async def test_capex_yoy_and_ttm():
    _mock_tickers()
    _mock_concept(CAPEX, _cc(
        _ytd_year(2024, 10.0, 22.0, 36.0, 52.0)
        + _ytd_year(2025, 20.0, 45.0, 75.0, 110.0)))
    _mock_no_ocf_rev()

    r = await srv.get_capex_series("GOOGL", 8)
    rows = _by_fp(r["series"])
    assert rows[(2024, "Q4")]["capex"] == 16.0     # 52 − 36
    assert rows[(2025, "Q4")]["capex"] == 35.0     # 110 − 75
    # 최신 분기(2025 Q4) vs 전년 동일 분기(2024 Q4)
    assert r["yoy_capex_pct"] == round((35 / 16 - 1) * 100, 2)
    assert r["ttm_capex"] == 110.0 and r["ttm_basis"] == "fy"
    # OCF/매출 미공시는 행 값 null + 최상위 errors
    assert rows[(2025, "Q4")]["ocf"] is None and rows[(2025, "Q4")]["fcf"] is None
    assert {e["field"] for e in r["errors"]} >= {"ocf", "revenue"}


@respx.mock
async def test_capex_negative_tagging_is_normalized():
    """일부 filer는 현금유출을 음수로 태깅한다 — 절대값으로 정규화."""
    _mock_tickers()
    _mock_concept(CAPEX, _cc(_ytd_year(2025, -20.0, -45.0, -75.0, -110.0)))
    _mock_no_ocf_rev()

    r = await srv.get_capex_series("GOOGL")
    assert all(row["capex"] > 0 for row in r["series"])
    assert r["ttm_capex"] == 110.0


@respx.mock
async def test_capex_skips_quarter_when_prior_cumulative_missing():
    """앞 분기 누적이 없으면 값을 만들지 않는다(추정 금지)."""
    _mock_tickers()
    _mock_concept(CAPEX, _cc([
        _f("2025-01-01", "2025-03-31", 20.0),
        # 6M 누적 없음
        _f("2025-01-01", "2025-09-30", 75.0),
        _f("2025-01-01", "2025-12-31", 110.0, form="10-K"),
    ]))
    _mock_no_ocf_rev()

    rows = _by_fp((await srv.get_capex_series("GOOGL"))["series"])
    assert (2025, "Q2") not in rows and (2025, "Q3") not in rows
    assert rows[(2025, "Q4")]["capex"] == 35.0


@respx.mock
async def test_capex_limits_to_requested_quarters():
    _mock_tickers()
    _mock_concept(CAPEX, _cc(
        _ytd_year(2024, 10.0, 22.0, 36.0, 52.0)
        + _ytd_year(2025, 20.0, 45.0, 75.0, 110.0)))
    _mock_no_ocf_rev()

    r = await srv.get_capex_series("GOOGL", quarters=3)
    assert len(r["series"]) == 3
    assert r["series"][-1]["fp"] == "Q4" and r["series"][-1]["fy"] == 2025


@respx.mock
async def test_capex_falls_back_to_alternate_tag():
    _mock_tickers()
    _mock_concept(CAPEX, {}, status=404)
    _mock_concept(CAPEX_ALT, _cc(_ytd_year(2025, 20.0, 45.0, 75.0, 110.0)))
    _mock_no_ocf_rev()

    r = await srv.get_capex_series("GOOGL")
    assert r["tags"]["capex"] == CAPEX_ALT
    assert len(r["series"]) == 4


@respx.mock
async def test_capex_missing_entirely_returns_errors():
    _mock_tickers()
    _mock_missing(*sec_metrics._CAPEX_TAGS)
    _mock_no_ocf_rev()
    r = await srv.get_capex_series("GOOGL")
    assert r["series"] == []
    assert any(e["field"] == "capex" for e in r["errors"])


async def test_capex_missing_user_agent(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    r = await srv.get_capex_series("GOOGL")
    assert r["errors"][0]["field"] == "sec_user_agent"


# ---------------------------------------------------------------- 원자료 툴

@respx.mock
async def test_sec_fundamentals_single_concept_mirrors_to_top_level():
    _mock_tickers()
    _mock_concept("Revenues", _cc([
        _f("2025-01-01", "2025-12-31", 460.0, form="10-K", filed="2026-02-01"),
        _f("2024-01-01", "2024-12-31", 400.0, form="10-K", filed="2025-02-01"),
    ]))
    r = await srv.get_sec_fundamentals("GOOGL", ["Revenues"], years=3)
    assert r["concept"] == "Revenues" and r["unit"] == "USD"
    assert [s["val"] for s in r["series"]] == [400.0, 460.0]
    assert r["results"][0]["concept"] == "Revenues"
    assert r["data_kind"] == "filing"


@respx.mock
async def test_sec_fundamentals_prefers_latest_filed_and_flags_restated():
    _mock_tickers()
    _mock_concept("NetIncomeLoss", _cc([
        _f("2025-01-01", "2025-12-31", 100.0, form="10-K", filed="2026-02-01"),
        _f("2025-01-01", "2025-12-31", 95.0, form="10-K/A", filed="2026-06-01"),
    ]))
    r = await srv.get_sec_fundamentals("GOOGL", ["NetIncomeLoss"])
    assert len(r["series"]) == 1
    assert r["series"][0]["val"] == 95.0          # filed 최신본
    assert r["series"][0]["form"] == "10-K/A"
    assert r["series"][0]["restated"] is True


@respx.mock
async def test_sec_fundamentals_partial_failure_per_concept():
    _mock_tickers()
    _mock_concept("Revenues", _cc([_f("2025-01-01", "2025-12-31", 460.0)]))
    _mock_concept("NoSuchTag", {}, status=404)
    r = await srv.get_sec_fundamentals("GOOGL", ["Revenues", "NoSuchTag"])
    assert len(r["results"]) == 2
    assert r["results"][0]["series"] and r["results"][1]["series"] == []
    assert r["errors"][0]["field"] == "NoSuchTag"
    assert "concept" not in r     # 태그가 2개면 최상위 미러링 없음


async def test_sec_fundamentals_requires_concepts():
    r = await srv.get_sec_fundamentals("GOOGL", [])
    assert r["errors"][0]["field"] == "concepts"
