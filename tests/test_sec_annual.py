"""get_sec_annual_metrics 테스트 (respx 모킹).

이 툴의 존재 이유가 곧 시험 대상이다 — 회사마다 다른 us-gaap 태그를 서버가
흡수하고, 연간값만 골라내고, 계산이 무의미해지는 자리에서는 값을 만들지 않는 것.
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
CIK = "0001652044"
YEARS = (2023, 2024, 2025)


@pytest.fixture(autouse=True)
def _ua(monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", "Test User test@example.com")
    monkeypatch.setattr(sec, "_LIMITER", RateLimiter(10_000.0))


def _dur(start, end, val, form="10-K", filed="2026-02-01"):
    return {"start": start, "end": end, "val": val, "form": form,
            "filed": filed, "accn": f"a-{end}", "fy": 2026, "fp": "FY"}


def _inst(end, val, form="10-K", filed="2026-02-01"):
    return {"end": end, "val": val, "form": form, "filed": filed,
            "accn": f"i-{end}", "fy": 2026, "fp": "FY"}


def _annual(vals: dict[int, float]):
    return [_dur(f"{y}-01-01", f"{y}-12-31", v) for y, v in vals.items()]


def _instants(vals: dict[int, float]):
    return [_inst(f"{y}-12-31", v) for y, v in vals.items()]


def _cc(facts, unit="USD"):
    return {"cik": 1652044, "units": {unit: facts}}


def _mock_universe(**bodies):
    """티커 목록과 모든 후보 태그를 모킹한다. 지정하지 않은 태그는 전부 404."""
    respx.get(TICKERS_URL).mock(return_value=httpx.Response(200, json={
        "0": {"cik_str": 1652044, "ticker": "GOOGL", "title": "Alphabet Inc."}}))
    every = [t for tags in {**sec_metrics._DUR_ITEMS,
                            **sec_metrics._INST_ITEMS}.values() for t in tags]
    for tag in every:
        body = bodies.get(tag)
        respx.get(CC.format(cik=CIK, tag=tag)).mock(
            return_value=httpx.Response(200 if body else 404,
                                        json=_cc(body) if body else {}))


def _base(**over):
    """3개 회계연도짜리 정상 기업 한 벌."""
    b = {
        "Revenues": _annual({y: 1000.0 + i * 100 for i, y in enumerate(YEARS)}),
        "OperatingIncomeLoss": _annual({y: 200.0 for y in YEARS}),
        "NetIncomeLoss": _annual({y: 100.0 for y in YEARS}),
        "DepreciationDepletionAndAmortization": _annual({y: 50.0 for y in YEARS}),
        "NetCashProvidedByUsedInOperatingActivities": _annual({y: 300.0 for y in YEARS}),
        "PaymentsToAcquirePropertyPlantAndEquipment": _annual({y: 80.0 for y in YEARS}),
        "PaymentsForRepurchaseOfCommonStock": _annual({y: 30.0 for y in YEARS}),
        "Assets": _instants({y: 5000.0 for y in YEARS}),
        "Liabilities": _instants({y: 3000.0 for y in YEARS}),
        "StockholdersEquity": _instants({y: 2000.0 for y in YEARS}),
        "CashAndCashEquivalentsAtCarryingValue": _instants({y: 400.0 for y in YEARS}),
        "LongTermDebtNoncurrent": _instants({y: 900.0 for y in YEARS}),
        "LongTermDebtCurrent": _instants({y: 100.0 for y in YEARS}),
    }
    b.update(over)
    return b


# ------------------------------------------------------- 태그 커버리지 선택 ★

@respx.mock
async def test_picks_dividend_tag_with_widest_coverage():
    """404가 아니어도 옛 기간만 남은 태그가 있다(실측: AAPL의
    PaymentsOfDividendsCommonStock은 FY2016~2017뿐). '첫 성공'을 채택하면
    배당이 통째로 비어 누적유보가 계산되지 않는다."""
    _mock_universe(**_base(
        PaymentsOfDividendsCommonStock=_annual({2016: 9.0, 2017: 9.0}),
        PaymentsOfDividends=_annual({y: 20.0 for y in YEARS}),
    ))
    r = await srv.get_sec_annual_metrics("GOOGL", years=3)
    assert r["tags"]["dividends"] == "PaymentsOfDividends"
    assert r["dividend_status"] == "disclosed"
    last = r["series"][-1]
    assert last["dividends"] == 20.0
    assert last["shareholder_returns"] == 50.0        # 배당 20 + 자사주 30
    assert last["retained"] == 50.0                   # 순이익 100 − 50
    assert last["retained_assumes_no_dividend"] is False


@respx.mock
async def test_fills_gap_years_from_lower_priority_tag():
    """회사가 중간에 태그를 갈아탄 경우 빠진 연도를 다음 후보로 메우고
    어떤 태그를 썼는지 남긴다."""
    _mock_universe(**_base(
        PaymentsOfDividendsCommonStock=_annual({2023: 15.0, 2024: 15.0}),
        PaymentsOfDividends=_annual({2025: 20.0}),
    ))
    r = await srv.get_sec_annual_metrics("GOOGL", years=3)
    assert r["tags"]["dividends"] == "PaymentsOfDividendsCommonStock"
    assert r["tag_fallbacks_used"]["dividends"] == ["PaymentsOfDividends"]
    assert [s["dividends"] for s in r["series"]] == [15.0, 15.0, 20.0]


@respx.mock
async def test_no_dividend_tag_marks_status_and_assumes_zero():
    """배당 태그 후보가 전부 비면 무배당 기업일 가능성이 높다. 누적유보는
    배당 0으로 계산하되 가정했다는 사실을 플래그로 남긴다."""
    _mock_universe(**_base())          # 배당 태그 전부 404
    r = await srv.get_sec_annual_metrics("GOOGL", years=3)
    assert r["tags"]["dividends"] is None
    assert r["dividend_status"] == "not_tagged"
    last = r["series"][-1]
    assert last["dividends"] is None
    assert last["retained_assumes_no_dividend"] is True
    assert last["retained"] == 70.0                   # 순이익 100 − 자사주 30


# ------------------------------------------------------------- 연간값 추출 ★

@respx.mock
async def test_ignores_half_year_and_quarter_facts():
    """같은 배열에 6개월 누적·3개월 단독이 섞여 와도 연간값만 채택한다.
    그대로 합산하면 이중계상이 된다."""
    facts = _annual({y: 1000.0 + i * 100 for i, y in enumerate(YEARS)})
    facts += [_dur("2025-01-01", "2025-06-30", 550.0, form="10-Q"),
              _dur("2025-07-01", "2025-09-30", 300.0, form="10-Q")]
    _mock_universe(**_base(Revenues=facts))
    r = await srv.get_sec_annual_metrics("GOOGL", years=3)
    assert [s["fy"] for s in r["series"]] == list(YEARS)
    assert [s["revenue"] for s in r["series"]] == [1000.0, 1100.0, 1200.0]


# --------------------------------------------------------- 계산 불가 자리 ★

@respx.mock
async def test_negative_equity_suppresses_roe():
    """자기자본이 음수면(자사주 누적 등) ROE는 부호가 뒤집혀 의미가 없다.
    실측 ORLY는 자기자본 −7.6억 달러라 그대로 두면 −3258%가 나온다."""
    _mock_universe(**_base(
        StockholdersEquity=_instants({y: -500.0 for y in YEARS})))
    r = await srv.get_sec_annual_metrics("GOOGL", years=3)
    assert all(s["equity_negative"] is True for s in r["series"])
    assert all(s["roe_pct"] is None for s in r["series"])
    assert r["roe_avg_pct"] is None
    assert any("ROIC" in e["reason"] for e in r["errors"])


@respx.mock
async def test_derives_liabilities_when_untagged():
    """Liabilities를 태깅하지 않는 기업(실측 ORLY)은 자산−자본으로 복원한다."""
    _mock_universe(**_base(Liabilities=None))
    r = await srv.get_sec_annual_metrics("GOOGL", years=3)
    last = r["series"][-1]
    assert last["liabilities"] == 3000.0              # 5000 − 2000
    assert last["liabilities_derived"] is True
    assert any(e["field"] == "liabilities" for e in r["errors"])


@respx.mock
async def test_derived_fields_and_roe_average():
    _mock_universe(**_base(
        PaymentsOfDividends=_annual({y: 20.0 for y in YEARS})))
    r = await srv.get_sec_annual_metrics("GOOGL", years=3)
    last = r["series"][-1]
    assert last["debt_total"] == 1000.0               # 장기 900 + 유동 100
    assert last["net_debt"] == 600.0                  # 1000 − 현금 400
    assert last["ebitda"] == 250.0                    # 영업이익 200 + D&A 50
    assert last["net_debt_to_ebitda"] == 2.4
    assert last["fcf"] == 220.0                       # OCF 300 − capex 80
    assert last["roe_pct"] == 5.0                     # 100 / 2000
    assert r["roe_avg_pct"] == 5.0
    assert r["unit"] == "USD" and r["data_kind"] == "filing"
    assert r["errors"] == []
