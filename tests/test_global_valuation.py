"""get_global_valuation / compare_valuation 테스트 (respx 모킹, 실네트워크 없음).

crumb 획득 시퀀스, 적자기업 null 처리, quoteSummary 실패 시 chart v8 강등,
국내·해외 혼용 라우팅, 개별 실패의 행 격리를 검증한다.
"""
from __future__ import annotations

import httpx
import pytest
import respx

import finance_server as srv
from sources import yahoo_valuation

COOKIE_URL = "https://fc.yahoo.com/"
CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"
QS = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/"
CHART1 = "https://query1.finance.yahoo.com/v8/finance/chart/"
CHART2 = "https://query2.finance.yahoo.com/v8/finance/chart/"

# 네이버 국내 밸류에이션(혼용 라우팅 테스트용)
NAVER_M = "https://m.stock.naver.com/api/stock/000660/integration"


@pytest.fixture(autouse=True)
def _reset_crumb():
    yahoo_valuation.reset()
    yield
    yahoo_valuation.reset()


def _num(v, fmt=None):
    return {"raw": v, "fmt": fmt if fmt is not None else str(v)}


def _qs_result(**over):
    """정상적인 흑자 기업 quoteSummary 응답."""
    base = {
        "price": {
            "symbol": "NVDA", "longName": "NVIDIA Corporation",
            "currency": "USD", "exchangeName": "NasdaqGS",
            "marketState": "REGULAR",
            "marketCap": _num(4.5e12), "regularMarketPrice": _num(180.0),
        },
        "summaryDetail": {
            "trailingPE": _num(52.5), "forwardPE": _num(38.1),
            "priceToSalesTrailing12Months": _num(28.4),
            "dividendYield": _num(0.0002, "0.02%"),
            "trailingAnnualDividendRate": _num(0.04),
            "fiftyTwoWeekHigh": _num(200.0), "fiftyTwoWeekLow": _num(90.0),
        },
        "defaultKeyStatistics": {
            "enterpriseValue": _num(4.4e12), "priceToBook": _num(48.2),
            "pegRatio": _num(1.35), "enterpriseToEbitda": _num(45.0),
            "trailingEps": _num(3.43),
        },
        "financialData": {
            "currentPrice": _num(180.0), "returnOnEquity": _num(0.9145),
            "grossMargins": _num(0.7501), "operatingMargins": _num(0.6210),
            "profitMargins": _num(0.5580),
            "totalRevenue": _num(1.65e11), "freeCashflow": _num(7.2e10),
        },
    }
    base.update(over)
    return base


def _mock_crumb(crumb="Abc123"):
    respx.get(COOKIE_URL).mock(return_value=httpx.Response(
        404, headers={"set-cookie": "A1=xyz; Domain=.yahoo.com; Path=/"}))
    respx.get(CRUMB_URL).mock(return_value=httpx.Response(200, text=crumb))


def _mock_qs(symbol, result, status=200):
    body = {"quoteSummary": {"result": [result] if result else None, "error": None}}
    return respx.get(f"{QS}{symbol}").mock(
        return_value=httpx.Response(status, json=body))


def _chart_body(**meta):
    m = {"symbol": "NVDA", "shortName": "NVIDIA", "currency": "USD",
         "exchangeName": "NasdaqGS", "regularMarketPrice": 180.0,
         "fiftyTwoWeekHigh": 200.0, "fiftyTwoWeekLow": 90.0, "gmtoffset": -14400}
    m.update(meta)
    return {"chart": {"result": [{"meta": m}], "error": None}}


# ---------------------------------------------------------------- 정상 경로

@respx.mock
async def test_global_valuation_full_fields():
    _mock_crumb()
    _mock_qs("NVDA", _qs_result())
    r = await srv.get_global_valuation("NVDA")

    assert r["symbol"] == "NVDA"
    assert r["name"] == "NVIDIA Corporation"
    assert r["currency"] == "USD" and r["exchange"] == "NasdaqGS"
    assert r["market_cap"] == 4.5e12
    assert r["trailing_pe"] == 52.5 and r["forward_pe"] == 38.1
    assert r["pbr"] == 48.2 and r["psr"] == 28.4 and r["peg"] == 1.35
    assert r["ev_ebitda"] == 45.0
    assert r["roe_pct"] == 91.45
    assert r["gross_margin_pct"] == 75.01
    assert r["operating_margin_pct"] == 62.10
    assert r["net_margin_pct"] == 55.80
    assert r["eps_ttm"] == 3.43
    assert r["week52_high"] == 200.0 and r["week52_low"] == 90.0
    assert r["pct_from_52wk_high"] == -10.0
    assert r["data_kind"] == "intraday"        # marketState=REGULAR
    assert r["source"] == "yahoo_quote_summary"
    assert r["errors"] == []                   # 수용기준 T2: errors 비어 있음


@respx.mock
async def test_crumb_sequence_is_used():
    """쿠키 프라이밍 → getcrumb → quoteSummary?crumb= 순으로 호출된다."""
    _mock_crumb("Zz9")
    route = _mock_qs("NVDA", _qs_result())
    await srv.get_global_valuation("NVDA")
    assert route.calls[0].request.url.params["crumb"] == "Zz9"
    assert route.calls[0].request.url.params["modules"].startswith("price,")


@respx.mock
async def test_market_closed_is_prev_close():
    _mock_crumb()
    res = _qs_result()
    res["price"] = dict(res["price"], marketState="CLOSED")
    _mock_qs("NVDA", res)
    r = await srv.get_global_valuation("NVDA")
    assert r["data_kind"] == "prev_close"


@respx.mock
async def test_loss_making_company_has_null_pe_without_error():
    """적자 기업은 trailingPE/pegRatio 키가 빈 dict로 온다 — null이 정상."""
    _mock_crumb()
    res = _qs_result()
    res["summaryDetail"] = dict(res["summaryDetail"], trailingPE={})
    res["defaultKeyStatistics"] = dict(res["defaultKeyStatistics"],
                                       pegRatio={}, trailingEps=_num(-1.2))
    _mock_qs("LOSS", res)
    r = await srv.get_global_valuation("LOSS")
    assert r["trailing_pe"] is None and r["peg"] is None
    assert r["eps_ttm"] == -1.2
    assert r["pbr"] == 48.2       # 나머지 지표는 정상
    assert r["errors"] == []      # 적자는 오류가 아니다


@respx.mock
async def test_missing_module_reported_in_errors():
    _mock_crumb()
    res = _qs_result()
    res["financialData"] = {}
    _mock_qs("NVDA", res)
    r = await srv.get_global_valuation("NVDA")
    assert r["roe_pct"] is None
    assert [e["field"] for e in r["errors"]] == ["financialData"]


@respx.mock
async def test_dividend_yield_accepts_percent_style_fmt():
    """야후가 raw를 퍼센트(0.44)로 주더라도 fmt를 우선해 정확히 읽는다."""
    _mock_crumb()
    res = _qs_result()
    res["summaryDetail"] = dict(res["summaryDetail"],
                                dividendYield=_num(0.44, "0.44%"))
    _mock_qs("NVDA", res)
    r = await srv.get_global_valuation("NVDA")
    assert r["dividend_yield_pct"] == 0.44


# ---------------------------------------------------------------- 강등 경로

@respx.mock
async def test_falls_back_to_chart_when_crumb_fails():
    respx.get(COOKIE_URL).mock(return_value=httpx.Response(404))
    respx.get(CRUMB_URL).mock(return_value=httpx.Response(200, text="<html>consent"))
    respx.get(f"{CHART1}NVDA").mock(return_value=httpx.Response(200, json=_chart_body()))

    r = await srv.get_global_valuation("NVDA")
    assert r["source"] == "yahoo_chart(partial)"
    assert r["name"] == "NVIDIA" and r["currency"] == "USD"
    assert r["week52_high"] == 200.0 and r["pct_from_52wk_high"] == -10.0
    assert r["trailing_pe"] is None and r["pbr"] is None
    assert [e["field"] for e in r["errors"]] == ["fundamentals"]


@respx.mock
async def test_crumb_refetched_on_401():
    """crumb 만료(401)면 한 번만 재획득 후 재시도한다."""
    _mock_crumb("first")
    body = {"quoteSummary": {"result": [_qs_result()], "error": None}}
    route = respx.get(f"{QS}NVDA").mock(side_effect=[
        httpx.Response(401), httpx.Response(200, json=body)])
    r = await srv.get_global_valuation("NVDA")
    assert r["source"] == "yahoo_quote_summary"
    assert len(route.calls) == 2


@respx.mock
async def test_invalid_symbol_returns_errors_not_exception():
    """수용기준 T5/T11: 예외를 던지지 않고 errors로 알린다."""
    _mock_crumb()
    respx.get(f"{QS}INVALID_XYZ").mock(return_value=httpx.Response(404))
    respx.get(f"{CHART1}INVALID_XYZ").mock(return_value=httpx.Response(404))
    respx.get(f"{CHART2}INVALID_XYZ").mock(return_value=httpx.Response(404))

    r = await srv.get_global_valuation("INVALID_XYZ")
    assert r["symbol"] == "INVALID_XYZ"
    assert r["market_cap"] is None
    assert {e["field"] for e in r["errors"]} == {"fundamentals", "price"}


async def test_empty_ticker():
    r = await srv.get_global_valuation("   ")
    assert r["errors"][0]["field"] == "symbol"


# ---------------------------------------------------------------- 비교표

@respx.mock
async def test_compare_valuation_three_us_tickers():
    _mock_crumb()
    for sym, pe in (("NVDA", 52.5), ("AVGO", 40.0), ("MU", 12.0)):
        res = _qs_result()
        res["price"] = dict(res["price"], symbol=sym, longName=sym)
        res["summaryDetail"] = dict(res["summaryDetail"], trailingPE=_num(pe))
        _mock_qs(sym, res)

    out = await srv.compare_valuation(["NVDA", "AVGO", "MU"])
    assert out["count"] == 3
    assert [r["ticker"] for r in out["rows"]] == ["NVDA", "AVGO", "MU"]
    assert [r["per"] for r in out["rows"]] == [52.5, 40.0, 12.0]
    # 모든 행이 동일한 지표 키 집합을 갖는다(정렬된 비교표)
    for r in out["rows"]:
        assert set(srv._COMPARE_METRICS) <= set(r)
        assert r["currency"] == "USD"
    assert out["missing_count"] == 0


@respx.mock
async def test_compare_valuation_mixed_domestic_and_us():
    """수용기준 T4: 6자리 코드는 네이버, 그 외는 야후로 라우팅되고 통화가 각각 유지된다."""
    _mock_crumb()
    _mock_qs("NVDA", _qs_result())
    respx.get(NAVER_M).mock(return_value=httpx.Response(200, json={
        "stockName": "SK하이닉스",
        "totalInfos": [
            {"code": "marketValue", "key": "시가총액", "value": "300조 1,000억"},
            {"code": "per", "key": "PER", "value": "12.35배"},
            {"code": "pbr", "key": "PBR", "value": "2.05배"},
            {"code": "eps", "key": "EPS", "value": "20,000원"},
            {"code": "dvr", "key": "배당수익률", "value": "1.20%"},
        ],
    }))

    out = await srv.compare_valuation(["NVDA", "000660"])
    us, kr = out["rows"]
    assert us["ticker"] == "NVDA" and us["currency"] == "USD"
    assert kr["ticker"] == "000660" and kr["currency"] == "KRW"
    assert kr["per"] == 12.35 and kr["pbr"] == 2.05
    assert kr["market_cap"] == 3001000 * 1e8          # 억원 → 원
    # 국내가 제공하지 않는 축은 null이고 사유가 남는다
    assert kr["psr"] is None and kr["roe_pct"] is None
    assert {e["field"] for e in kr["errors"]} >= {"psr", "peg", "roe_pct"}


@respx.mock
async def test_compare_valuation_isolates_row_failure():
    """수용기준 T5: 1행 성공 + 1행 errors, 예외 미발생."""
    _mock_crumb()
    _mock_qs("NVDA", _qs_result())
    respx.get(f"{QS}INVALID_XYZ").mock(return_value=httpx.Response(404))
    respx.get(f"{CHART1}INVALID_XYZ").mock(return_value=httpx.Response(404))
    respx.get(f"{CHART2}INVALID_XYZ").mock(return_value=httpx.Response(404))

    out = await srv.compare_valuation(["NVDA", "INVALID_XYZ"])
    good, bad = out["rows"]
    assert good["per"] == 52.5 and good["errors"] == []
    assert bad["per"] is None and bad["errors"]
    assert out["missing_count"] == len(srv._COMPARE_METRICS)


@respx.mock
async def test_compare_valuation_caps_at_ten():
    _mock_crumb()
    for i in range(12):
        _mock_qs(f"T{i}", _qs_result())
    out = await srv.compare_valuation([f"T{i}" for i in range(12)])
    assert out["count"] == 10
    assert out["dropped"] == ["T10", "T11"]


@respx.mock
async def test_compare_valuation_custom_metrics():
    _mock_crumb()
    _mock_qs("NVDA", _qs_result())
    out = await srv.compare_valuation(["NVDA"], metrics=["ev_ebitda", "eps"])
    row = out["rows"][0]
    assert row["ev_ebitda"] == 45.0 and row["eps"] == 3.43
    assert "per" not in row


@respx.mock
async def test_compare_valuation_normalize_krw_adds_field_only():
    """환산값은 별도 필드로만 붙고 원래 통화·비율 지표는 그대로다."""
    _mock_crumb()
    _mock_qs("NVDA", _qs_result())
    respx.get("https://query1.finance.yahoo.com/v8/finance/chart/KRW%3DX").mock(
        return_value=httpx.Response(200, json={"chart": {"result": [{"meta": {
            "regularMarketPrice": 1400.0, "chartPreviousClose": 1395.0,
            "currency": "KRW", "regularMarketTime": 1781815326}}]}}))

    out = await srv.compare_valuation(["NVDA"], normalize_krw=True)
    row = out["rows"][0]
    assert out["usd_krw"] == 1400.0
    assert row["currency"] == "USD"              # 원래 통화 유지
    assert row["market_cap"] == 4.5e12           # 원래 값 유지
    assert row["market_cap_krw"] == round(4.5e12 * 1400.0, 0)
    assert row["per"] == 52.5                    # 비율 지표는 환산하지 않는다


async def test_compare_valuation_empty_input():
    out = await srv.compare_valuation([])
    assert out["count"] == 0 and out["rows"] == []
