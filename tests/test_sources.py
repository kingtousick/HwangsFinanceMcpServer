"""소스/Tool 단위·통합 테스트 (respx로 HTTP 모킹).

검증 항목(설계서 §11):
  - 네이버 KOSPI 정상 파싱 (value, source)
  - Yahoo chart 정상 파싱 + change 계산
  - CoinGecko 정상 파싱
  - 전 소스 실패 → {error, source:"fallback"} (크래시 없음)
  - 캐시 재호출 시 외부 호출 0회
  - get_market_snapshot 8개 지표 일괄
"""
import httpx
import pytest
import respx

import finance_server as srv
from core import cache, http

NAVER_KOSPI = "https://polling.finance.naver.com/api/realtime/domestic/index/KOSPI"
YAHOO_GSPC = "https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC"
CG_SIMPLE = "https://api.coingecko.com/api/v3/simple/price"


@pytest.fixture(autouse=True)
async def _reset():
    """각 테스트 전 캐시·클라이언트 초기화."""
    cache.clear()
    await http.aclose()
    yield
    await http.aclose()


def _naver_payload(close="9167.34", cmp="114.92", pct="1.27", code="2"):
    return {"datas": [{
        "closePriceRaw": close,
        "compareToPreviousClosePriceRaw": cmp,
        "fluctuationsRatioRaw": pct,
        "localTradedAt": "2026-06-22T10:50:47+09:00",
        "compareToPreviousPrice": {"code": code},
        "stockName": "코스피",
    }]}


def _yahoo_payload(price=7500.58, prev=7420.1, cur="USD"):
    return {"chart": {"result": [{"meta": {
        "regularMarketPrice": price,
        "chartPreviousClose": prev,
        "currency": cur,
        "regularMarketTime": 1781815326,
        "shortName": "S&P 500",
    }}]}}


@respx.mock
async def test_naver_kospi_ok():
    respx.get(NAVER_KOSPI).mock(return_value=httpx.Response(200, json=_naver_payload()))
    res = await srv.get_kospi()
    assert res["value"] == pytest.approx(9167.34)
    assert res["value"] >= 9000  # acceptance §10-1
    assert res["source"] == "naver"
    assert res["currency"] == "KRW"
    assert res["change"] == pytest.approx(114.92)


@respx.mock
async def test_naver_down_sign():
    """하락 코드(5)면 change/pct 부호가 음수로 보정된다."""
    respx.get(NAVER_KOSPI).mock(return_value=httpx.Response(
        200, json=_naver_payload(cmp="10.0", pct="0.5", code="5")))
    res = await srv.get_kospi()
    assert res["change"] == pytest.approx(-10.0)
    assert res["change_pct"] == pytest.approx(-0.5)


@respx.mock
async def test_yahoo_change_calc():
    respx.get(YAHOO_GSPC).mock(return_value=httpx.Response(200, json=_yahoo_payload()))
    res = await srv.get_stock_price("^GSPC")
    assert res["value"] == pytest.approx(7500.58)
    assert res["change"] == pytest.approx(7500.58 - 7420.1)
    assert res["source"] == "yahoo"
    assert res["timestamp"].endswith("+09:00")  # KST 변환


@respx.mock
async def test_coingecko_krw():
    respx.get(CG_SIMPLE).mock(return_value=httpx.Response(
        200, json={"bitcoin": {"krw": 99168890, "krw_24h_change": 1.09}}))
    res = await srv.get_crypto("BTC", "KRW")
    assert res["value"] == pytest.approx(99168890)
    assert res["currency"] == "KRW"
    assert res["source"] == "coingecko"
    assert res["change_pct"] == pytest.approx(1.09)


@respx.mock
async def test_all_sources_fail_returns_fallback():
    """전 소스 실패 시 크래시 없이 {error, source:'fallback'}."""
    respx.get(NAVER_KOSPI).mock(return_value=httpx.Response(500))
    res = await srv.get_kospi()  # playwright 미설치 → ImportError까지 강등
    assert "error" in res
    assert res["source"] == "fallback"
    assert res["name"] == "KOSPI"


@respx.mock
async def test_cache_prevents_second_call():
    route = respx.get(NAVER_KOSPI).mock(
        return_value=httpx.Response(200, json=_naver_payload()))
    await srv.get_kospi()
    await srv.get_kospi()  # 30초 내 재호출 → 캐시
    assert route.call_count == 1


@respx.mock
async def test_snapshot_returns_eight():
    respx.get(NAVER_KOSPI).mock(return_value=httpx.Response(200, json=_naver_payload()))
    respx.get("https://polling.finance.naver.com/api/realtime/domestic/index/KOSDAQ").mock(
        return_value=httpx.Response(200, json=_naver_payload(close="850.0")))
    respx.get(url__startswith="https://query1.finance.yahoo.com").mock(
        return_value=httpx.Response(200, json=_yahoo_payload()))
    respx.get(CG_SIMPLE).mock(return_value=httpx.Response(
        200, json={"bitcoin": {"krw": 99168890, "krw_24h_change": 1.09},
                   "ethereum": {"krw": 2678539, "krw_24h_change": 0.93}}))
    res = await srv.get_market_snapshot()
    assert res["count"] == 8
    assert len(res["snapshot"]) == 8
