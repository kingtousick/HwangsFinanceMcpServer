"""get_credit_spreads / get_rpo_backlog 테스트 (respx 모킹).

FRED CSV의 '.' 결측 처리와 백분위 계산이 핵심이다.
"""
from __future__ import annotations

from datetime import date, timedelta

import httpx
import pytest
import respx

import finance_server as srv
from core.ratelimit import RateLimiter
from sources import fred, sec, sec_metrics

FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
CC = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/us-gaap/{tag}.json"
CIK = "0000789019"   # Microsoft

RPO = "RevenueRemainingPerformanceObligation"
RPO_PCT = "RevenueRemainingPerformanceObligationPercentage"


def _csv(rows, header="observation_date,DGS10"):
    return "\n".join([header] + [f"{d},{v}" for d, v in rows])


def _daily(n, start_value=1.0, step=0.01, end=date(2026, 8, 7)):
    """영업일 근사 시계열(하루 간격)."""
    return [((end - timedelta(days=n - 1 - i)).isoformat(),
             f"{start_value + step * i:.2f}") for i in range(n)]


# ---------------------------------------------------------------- CSV 파싱

def test_parse_csv_drops_missing_markers():
    text = _csv([("2026-08-03", "4.10"), ("2026-08-04", "."),
                 ("2026-08-05", ""), ("2026-08-06", "NA"),
                 ("2026-08-07", "4.25")])
    pts = fred.parse_csv(text)
    assert [p["date"] for p in pts] == ["2026-08-03", "2026-08-07"]
    assert [p["value"] for p in pts] == [4.10, 4.25]


def test_parse_csv_accepts_legacy_header():
    """헤더 첫 열 이름은 DATE / observation_date로 갈리므로 위치로 파싱한다."""
    pts = fred.parse_csv(_csv([("2026-08-07", "4.25")], header="DATE,DGS10"))
    assert pts == [{"date": "2026-08-07", "value": 4.25}]


def test_parse_csv_empty():
    assert fred.parse_csv("") == []
    assert fred.parse_csv("observation_date,DGS10\n") == []


# ---------------------------------------------------------------- 시리즈 조회

@respx.mock
async def test_credit_spreads_default_five_series():
    """수용기준 T9: 기본 5개 시리즈의 최신값 + percentile_1y."""
    respx.get(FRED_URL).mock(return_value=httpx.Response(
        200, text=_csv(_daily(400, 1.0, 0.005))))

    r = await srv.get_credit_spreads()
    assert [s["id"] for s in r["series"]] == fred.DEFAULT_IDS
    assert r["count"] == 5
    assert r["source"] == "FRED" and r["data_kind"] == "prev_close"
    assert r["errors"] == []
    for s in r["series"]:
        assert s["latest"] is not None
        assert s["percentile_1y"] is not None
        assert s["percentile_5y"] is not None
        assert s["name"] and s["unit"]           # 한글 이름·단위가 붙는다
    assert r["as_of"] == "2026-08-07"


@respx.mock
async def test_percentile_and_changes_are_date_based():
    """단조 증가 계열이면 최신값이 최고치라 백분위 100%."""
    respx.get(FRED_URL).mock(return_value=httpx.Response(
        200, text=_csv(_daily(400, 1.0, 0.01))))

    r = await srv.get_credit_spreads(["DGS10"], period="3mo")
    s = r["series"][0]
    assert s["percentile_1y"] == 100.0
    # 하루 0.01씩 오르므로 30일 전 대비 +0.30, 91일 전 대비 +0.91
    assert s["change_1m"] == 0.30
    assert s["change_3m"] == 0.91


@respx.mock
async def test_points_downsampled_when_too_many():
    respx.get(FRED_URL).mock(return_value=httpx.Response(
        200, text=_csv(_daily(800, 1.0, 0.001))))

    r = await srv.get_credit_spreads(["DGS10"], period="5y")
    s = r["series"][0]
    assert s["interval"] in ("weekly", "monthly")
    assert s["count"] <= 120 and s["count"] == len(s["points"])


@respx.mock
async def test_period_slices_points_but_not_percentile():
    respx.get(FRED_URL).mock(return_value=httpx.Response(
        200, text=_csv(_daily(400, 1.0, 0.01))))

    short = await srv.get_credit_spreads(["DGS10"], period="1mo")
    s = short["series"][0]
    assert s["interval"] == "daily" and s["count"] <= 40
    # points는 1개월치지만 백분위는 여전히 1년/5년 기준
    assert s["percentile_1y"] == 100.0 and s["percentile_5y"] == 100.0


@respx.mock
async def test_partial_failure_keeps_other_series():
    def _handler(request):
        if request.url.params.get("id") == "DGS10":
            return httpx.Response(500)
        return httpx.Response(200, text=_csv(_daily(400, 1.0, 0.005)))

    respx.get(FRED_URL).mock(side_effect=_handler)
    r = await srv.get_credit_spreads(["DGS10", "T10Y2Y"])
    bad, good = r["series"]
    assert bad["id"] == "DGS10" and bad["latest"] is None and bad["error"]
    assert good["id"] == "T10Y2Y" and good["latest"] is not None
    assert [e["field"] for e in r["errors"]] == ["DGS10"]


@respx.mock
async def test_html_response_is_rejected():
    """잘못된 시리즈 ID면 FRED가 HTML을 준다 — CSV로 오인하지 않는다."""
    respx.get(FRED_URL).mock(return_value=httpx.Response(
        200, text="<!DOCTYPE html><html>Not found</html>"))
    r = await srv.get_credit_spreads(["NOPE"])
    assert r["series"][0]["latest"] is None
    assert "CSV가 아님" in r["errors"][0]["reason"]


@respx.mock
async def test_offline_returns_errors_not_exception():
    """수용기준 T11."""
    respx.get(FRED_URL).mock(side_effect=httpx.ConnectError("offline"))
    r = await srv.get_credit_spreads(["DGS10"])
    assert r["errors"] and r["series"][0]["latest"] is None


@respx.mock
async def test_timeout_reason_is_not_empty():
    """httpx.ReadTimeout은 str()이 빈 문자열이라 클래스명이라도 남겨야 한다."""
    respx.get(FRED_URL).mock(side_effect=httpx.ReadTimeout(""))
    r = await srv.get_credit_spreads(["DGS10"])
    assert r["errors"][0]["reason"] == "ReadTimeout"
    assert r["series"][0]["error"] == "ReadTimeout"


# ---------------------------------------------------------------- RPO

@pytest.fixture
def _ua(monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", "Test User test@example.com")
    monkeypatch.setattr(sec, "_LIMITER", RateLimiter(10_000.0))


def _mock_tickers():
    respx.get(TICKERS_URL).mock(return_value=httpx.Response(200, json={
        "0": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"}}))


def _mock_concept(tag, body, status=200):
    return respx.get(CC.format(cik=CIK, tag=tag)).mock(
        return_value=httpx.Response(status, json=body))


def _inst(end, val, filed="2026-05-01", accn=None):
    return {"end": end, "val": val, "form": "10-Q", "accn": accn or f"i-{end}",
            "filed": filed}


@respx.mock
async def test_rpo_series_and_growth(_ua):
    _mock_tickers()
    _mock_concept(RPO, {"units": {"USD": [
        _inst("2025-06-30", 100.0), _inst("2025-09-30", 110.0),
        _inst("2025-12-31", 120.0), _inst("2026-03-31", 130.0),
        _inst("2026-06-30", 150.0),
    ]}})
    _mock_concept(RPO_PCT, {}, status=404)

    r = await srv.get_rpo_backlog("MSFT")
    assert r["disclosed"] is True and r["unit"] == "USD"
    assert [s["val"] for s in r["series"]] == [100, 110, 120, 130, 150]
    assert r["qoq_pct"] == round((150 / 130 - 1) * 100, 2)
    assert r["yoy_pct"] == round((150 / 100 - 1) * 100, 2)
    assert all(s["ambiguous"] is False for s in r["series"])


@respx.mock
async def test_rpo_multiple_facts_flagged_ambiguous(_ua):
    """세그먼트별 다중 사실은 합산하지 않고 ambiguous로 표시한다."""
    _mock_tickers()
    _mock_concept(RPO, {"units": {"USD": [
        _inst("2026-06-30", 90.0, accn="seg-a"),
        _inst("2026-06-30", 60.0, accn="seg-b"),
    ]}})
    _mock_concept(RPO_PCT, {}, status=404)

    r = await srv.get_rpo_backlog("MSFT")
    assert len(r["series"]) == 1
    assert r["series"][0]["ambiguous"] is True
    assert r["series"][0]["val"] in (90.0, 60.0)   # 합산(150)하지 않는다


@respx.mock
async def test_rpo_not_disclosed(_ua):
    _mock_tickers()
    _mock_concept(RPO, {}, status=404)
    _mock_concept(RPO_PCT, {}, status=404)

    r = await srv.get_rpo_backlog("MSFT")
    assert r["disclosed"] is False and r["series"] == []
    assert r["errors"][0]["field"] == "rpo"


async def test_rpo_missing_user_agent(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    r = await srv.get_rpo_backlog("MSFT")
    assert r["errors"][0]["field"] == "sec_user_agent"
