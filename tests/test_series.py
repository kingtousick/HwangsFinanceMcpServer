"""시계열 tool 3종 테스트 (respx HTTP 모킹).

검증:
  - core/series 파생지표 계산(기간수익률·MDD·변동성·N관측 전 대비)
  - get_price_history: Yahoo OHLC 파싱 + 거래소 현지 날짜(gmtoffset) 적용,
    네이버 일별시세(JS 배열 리터럴) 파싱, 네이버 실패 → Yahoo(.KS) 강등
  - get_macro_series: 프리셋/직접코드 해석, 절대변화·변화율 동시 산출,
    에러 봉투 비삼킴
  - get_realty_price_index: 2차원 항목 순서(유형→지역), 지역 별칭, KB 소스,
    시군구 입력 시 안내 메시지
"""
import httpx
import pytest
import respx

import finance_server as srv
from core import cache, http
from core.series import max_drawdown, recent_changes, summarize, volatility_pct

ECOS_KEY = "SECRETKEY123"
ECOS_SEARCH = r"https://ecos\.bok\.or\.kr/api/StatisticSearch/.*"
NAVER_SISE = "https://api.finance.naver.com/siseJson.naver"
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/"

# 2026-02-02T01:00:00Z — gmtoffset -14400(뉴욕) 적용 시 현지 날짜는 2026-02-01
_TS0 = 1769994000
_WEEK = 604800


@pytest.fixture(autouse=True)
async def _reset(monkeypatch):
    monkeypatch.setenv("ECOS_API_KEY", ECOS_KEY)
    cache.clear()
    await http.aclose()
    yield
    await http.aclose()


# ---------------------------------------------------------------- core/series


def test_summarize_basic():
    pts = [{"date": "d1", "close": 100.0}, {"date": "d2", "close": 110.0},
           {"date": "d3", "close": 99.0}]
    s = summarize(pts, periods_per_year=52)
    assert s["count"] == 3
    assert s["start_value"] == 100.0 and s["end_value"] == 99.0
    assert s["change_pct"] == pytest.approx(-1.0)
    assert s["high"] == 110.0 and s["high_at"] == "d2"
    assert s["low"] == 99.0 and s["low_at"] == "d3"
    assert s["pct_from_high"] == pytest.approx(-10.0)
    assert s["max_drawdown_pct"] == pytest.approx(-10.0)
    assert s["volatility_pct"] is not None


def test_summarize_skips_missing_values():
    pts = [{"date": "d1", "close": 100.0}, {"date": "d2", "close": None},
           {"date": "d3", "close": 120.0}]
    s = summarize(pts)
    assert s["count"] == 2
    assert s["change_pct"] == pytest.approx(20.0)
    assert "volatility_pct" not in s  # periods_per_year 미지정 → 생략


def test_summarize_empty():
    assert summarize([])["count"] == 0


def test_max_drawdown_uses_running_peak():
    # 100 → 50(-50%) → 200 → 150(-25%): 최악은 -50%
    assert max_drawdown([100, 50, 200, 150]) == pytest.approx(-50.0)
    assert max_drawdown([100]) is None


def test_volatility_needs_three_points():
    assert volatility_pct([100, 110], 252) is None
    assert volatility_pct([100, 110, 105], 252) > 0


def test_recent_changes_pct_and_absolute():
    pts = [{"time": "t1", "value": 2.5}, {"time": "t2", "value": 2.5},
           {"time": "t3", "value": 2.5}, {"time": "t4", "value": 2.75}]
    assert recent_changes(pts, {"3개월": 3})["3개월"] == pytest.approx(10.0)
    diff = recent_changes(pts, {"3개월": 3}, pct=False)
    assert diff["3개월"] == pytest.approx(0.25)  # 금리는 %p로 읽어야 한다
    assert recent_changes(pts, {"99개월": 99}) == {}  # 관측 부족 → 생략


# ---------------------------------------------------------------- 주가 시계열


def _yahoo_hist(granularity="1wk", closes=(100.0, 110.0, 99.0)):
    n = len(closes)
    return {"chart": {"result": [{
        "meta": {"currency": "USD", "symbol": "^GSPC", "shortName": "S&P 500",
                 "gmtoffset": -14400, "dataGranularity": granularity,
                 "fiftyTwoWeekHigh": 120.0, "fiftyTwoWeekLow": 80.0},
        "timestamp": [_TS0 + i * _WEEK for i in range(n)],
        "indicators": {"quote": [{
            "open": [c - 1 for c in closes], "high": [c + 5 for c in closes],
            "low": [c - 5 for c in closes], "close": list(closes),
            "volume": [1000 * (i + 1) for i in range(n)],
        }]},
    }]}}


@respx.mock
async def test_price_history_yahoo_parses_ohlc_and_local_date():
    respx.get(YAHOO_CHART + "%5EGSPC").mock(
        return_value=httpx.Response(200, json=_yahoo_hist()))
    res = await srv.get_price_history("^GSPC", "1y")
    assert res["source"] == "yahoo"
    assert res["currency"] == "USD"
    assert res["count"] == 3
    assert res["interval"] == "1wk"
    # KST가 아니라 거래소 현지(-04:00) 날짜여야 한다
    assert res["points"][0]["date"] == "2026-02-01"
    assert res["points"][0]["open"] == pytest.approx(99.0)
    assert res["stats"]["change_pct"] == pytest.approx(-1.0)
    assert res["stats"]["max_drawdown_pct"] == pytest.approx(-10.0)
    assert res["week52_high"] == 120.0


@respx.mock
async def test_price_history_auto_interval_by_period():
    route = respx.get(YAHOO_CHART + "AAPL").mock(
        return_value=httpx.Response(200, json=_yahoo_hist(granularity="1d")))
    await srv.get_price_history("AAPL", "1mo")
    assert route.calls[0].request.url.params["interval"] == "1d"
    cache.clear()
    await srv.get_price_history("AAPL", "5y")
    assert route.calls[-1].request.url.params["interval"] == "1mo"


@respx.mock
async def test_price_history_skips_null_bars():
    payload = _yahoo_hist()
    payload["chart"]["result"][0]["indicators"]["quote"][0]["close"][1] = None
    respx.get(YAHOO_CHART + "%5EGSPC").mock(
        return_value=httpx.Response(200, json=payload))
    res = await srv.get_price_history("^GSPC", "1y")
    assert res["count"] == 2  # 휴장·결측 봉 제외


_SISE_BODY = """[['날짜', '시가', '고가', '저가', '종가', '거래량', '외국인소진율'],
["20260601", 99, 105, 95, 100, 1000, 50.0],
["20260608", 109, 115, 105, 110, 2000, 51.0],
["20260615", 98, 104, 94, 99, 1500, 49.5]]"""


@respx.mock
async def test_price_history_naver_parses_js_array():
    respx.get(NAVER_SISE).mock(return_value=httpx.Response(200, text=_SISE_BODY))
    res = await srv.get_price_history("005930", "6mo")
    assert res["source"] == "naver"
    assert res["currency"] == "KRW"
    assert res["count"] == 3
    assert res["points"][0]["date"] == "2026-06-01"
    assert res["points"][0]["foreign_ratio"] == pytest.approx(50.0)
    assert res["points"][-1]["close"] == pytest.approx(99.0)
    assert res["stats"]["change_pct"] == pytest.approx(-1.0)


@respx.mock
async def test_price_history_domestic_falls_back_to_yahoo_ks():
    respx.get(NAVER_SISE).mock(return_value=httpx.Response(500))
    respx.get(YAHOO_CHART + "005930.KS").mock(
        return_value=httpx.Response(200, json=_yahoo_hist()))
    res = await srv.get_price_history("005930", "1y")
    assert res["source"] == "yahoo"
    assert res["count"] == 3


@respx.mock
async def test_price_history_all_sources_fail():
    respx.get(NAVER_SISE).mock(return_value=httpx.Response(500))
    respx.get(url__startswith=YAHOO_CHART).mock(return_value=httpx.Response(500))
    respx.get(url__startswith="https://query2.finance.yahoo.com").mock(
        return_value=httpx.Response(500))
    res = await srv.get_price_history("005930", "1y")
    assert res["source"] == "fallback"
    assert "error" in res


# ---------------------------------------------------------------- 거시 시계열


def _ecos_series(rows):
    return {"StatisticSearch": {"list_total_count": len(rows), "row": rows}}


def _rate_rows():
    return [{"STAT_CODE": "722Y001", "ITEM_CODE1": "0101000",
             "UNIT_NAME": "연%", "TIME": t, "DATA_VALUE": v}
            for t, v in [("202604", "2.5"), ("202605", "2.5"),
                         ("202606", "2.5"), ("202607", "2.75")]]


@respx.mock
async def test_macro_series_preset_and_dual_changes():
    respx.get(url__regex=ECOS_SEARCH).mock(
        return_value=httpx.Response(200, json=_ecos_series(_rate_rows())))
    res = await srv.get_macro_series("기준금리", 36)
    assert res["source"] == "ecos"
    assert res["stat_code"] == "722Y001" and res["item_code"] == "0101000"
    assert res["cycle"] == "M" and res["unit"] == "연%"
    assert res["points"][0] == {"time": "202604", "value": 2.5}
    assert res["stats"]["change"] == pytest.approx(0.25)
    # 금리는 %p(changes)와 %(changes_pct)를 모두 제공한다
    assert res["changes"]["3개월"] == pytest.approx(0.25)
    assert res["changes_pct"]["3개월"] == pytest.approx(10.0)


@respx.mock
async def test_macro_series_sorts_by_time():
    rows = list(reversed(_rate_rows()))
    respx.get(url__regex=ECOS_SEARCH).mock(
        return_value=httpx.Response(200, json=_ecos_series(rows)))
    res = await srv.get_macro_series("기준금리", 12)
    assert [p["time"] for p in res["points"]] == ["202604", "202605",
                                                  "202606", "202607"]


@respx.mock
async def test_macro_series_accepts_raw_code_spec():
    route = respx.get(url__regex=ECOS_SEARCH).mock(
        return_value=httpx.Response(200, json=_ecos_series(_rate_rows())))
    res = await srv.get_macro_series("901Y009/0/M", 12)
    assert res["stat_code"] == "901Y009" and res["item_code"] == "0"
    assert "/901Y009/M/" in str(route.calls[0].request.url)


async def test_macro_series_unknown_indicator_lists_presets():
    res = await srv.get_macro_series("없는지표", 12)
    assert res["source"] == "fallback"
    assert "기준금리" in res["error"]  # 프리셋 안내


@respx.mock
async def test_macro_series_error_envelope_not_swallowed():
    respx.get(url__regex=ECOS_SEARCH).mock(return_value=httpx.Response(
        200, json={"RESULT": {"CODE": "INFO-200",
                              "MESSAGE": "해당하는 데이터가 없습니다."}}))
    res = await srv.get_macro_series("기준금리", 12)
    assert res["source"] == "fallback"
    assert "INFO-200" in res["error"]


@respx.mock
async def test_macro_series_masks_path_key_on_error():
    respx.get(url__regex=ECOS_SEARCH).mock(return_value=httpx.Response(500))
    res = await srv.get_macro_series("기준금리", 12)
    assert res["source"] == "fallback"
    assert ECOS_KEY not in res["error"]


# ---------------------------------------------------------------- 부동산 지수


def _index_rows(unit="2025.03=100"):
    return [{"STAT_CODE": "901Y113", "ITEM_CODE1": "H69B", "ITEM_CODE2": "R70F",
             "UNIT_NAME": unit, "TIME": t, "DATA_VALUE": v}
            for t, v in [("202510", "105.0"), ("202511", "106.9"),
                         ("202512", "107.8"), ("202601", "109.0")]]


@respx.mock
async def test_realty_index_uses_type_then_region_order():
    route = respx.get(url__regex=ECOS_SEARCH).mock(
        return_value=httpx.Response(200, json=_ecos_series(_index_rows())))
    res = await srv.get_realty_price_index("서울", "매매", "아파트", 36)
    assert res["org"] == "한국부동산원"
    assert res["stat_code"] == "901Y113"
    # 2차원 통계표는 유형(H69B) → 지역(R70F) 순서여야 데이터가 나온다
    assert res["item_code"] == "H69B/R70F"
    assert "/H69B/R70F" in str(route.calls[0].request.url)
    assert res["unit"] == "2025.03=100"
    assert res["points"][-1]["value"] == pytest.approx(109.0)
    assert res["changes_pct"]["3개월"] == pytest.approx(3.81)  # 105.0 → 109.0


@respx.mock
async def test_realty_index_jeonse_uses_other_table():
    respx.get(url__regex=ECOS_SEARCH).mock(
        return_value=httpx.Response(200, json=_ecos_series(_index_rows())))
    res = await srv.get_realty_price_index("경기", "전세", "아파트", 24)
    assert res["stat_code"] == "901Y114"
    assert res["item_code"] == "H69B/R70G"
    assert res["kind"] == "전세"


@respx.mock
async def test_realty_index_normalizes_aliases():
    respx.get(url__regex=ECOS_SEARCH).mock(
        return_value=httpx.Response(200, json=_ecos_series(_index_rows())))
    res = await srv.get_realty_price_index("서울특별시", "매매", "빌라", 12)
    assert res["region"] == "서울" and res["house_type"] == "연립다세대"
    assert res["item_code"] == "H69C/R70F"


@respx.mock
async def test_realty_index_kb_source_is_one_dimensional():
    route = respx.get(url__regex=ECOS_SEARCH).mock(
        return_value=httpx.Response(200, json=_ecos_series(
            _index_rows(unit="2026.01=100"))))
    res = await srv.get_realty_price_index("전국", "매매", "아파트", 24, "kb")
    assert res["org"] == "KB국민은행"
    assert res["stat_code"] == "901Y062" and res["item_code"] == "P63AC"
    assert "/P63AC" in str(route.calls[0].request.url)


async def test_realty_index_rejects_sigungu_with_guidance():
    res = await srv.get_realty_price_index("강남구")
    assert res["source"] == "fallback"
    assert "get_apt_trade_summary" in res["error"]  # 실거래 tool로 유도


async def test_realty_index_rejects_unsupported_kb_combo():
    res = await srv.get_realty_price_index("부산", "매매", "아파트", 24, "kb")
    assert res["source"] == "fallback"
    assert "부동산원" in res["error"]


async def test_realty_index_rejects_bad_kind():
    res = await srv.get_realty_price_index("서울", "월세")
    assert res["source"] == "fallback"
    assert "매매" in res["error"]
