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


MOLIT_TRADE = ("https://apis.data.go.kr/1613000/RTMSDataSvcAptTradeDev/"
               "getRTMSDataSvcAptTradeDev")

_MOLIT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<response><header><resultCode>000</resultCode><resultMsg>OK</resultMsg></header>
<body><items>
<item><aptNm>래미안</aptNm><dealAmount>250,000</dealAmount>
<dealYear>2024</dealYear><dealMonth>6</dealMonth><dealDay>15</dealDay>
<excluUseAr>84.97</excluUseAr><floor>10</floor><buildYear>2005</buildYear>
<umdNm>역삼동</umdNm><jibun>700</jibun></item>
<item><aptNm>개포자이</aptNm><dealAmount>310,000</dealAmount>
<dealYear>2024</dealYear><dealMonth>6</dealMonth><dealDay>3</dealDay>
<excluUseAr>59.92</excluUseAr><floor>7</floor><buildYear>2019</buildYear>
<umdNm>개포동</umdNm><jibun>12</jibun></item>
</items><numOfRows>10</numOfRows><pageNo>1</pageNo><totalCount>2</totalCount></body>
</response>"""


@respx.mock
async def test_apt_trade_ok(monkeypatch):
    monkeypatch.setenv("MOLIT_API_KEY", "dummy-key")
    respx.get(MOLIT_TRADE).mock(return_value=httpx.Response(200, text=_MOLIT_XML))
    res = await srv.get_apt_trade("11680", "202406")
    assert res["source"] == "molit"
    assert res["count"] == 2
    first = res["items"][0]
    assert first["apt"] == "래미안"
    assert first["deal_amount"] == 250000  # 만원
    assert first["area"] == pytest.approx(84.97)
    assert first["floor"] == 10
    assert first["date"] == "2024-06-15"
    # 전용 평당가 = 250000 / (84.97/3.305785)
    expected_pyeong = 84.97 / 3.305785
    assert first["pyeong"] == pytest.approx(expected_pyeong, rel=1e-3)
    assert first["price_per_pyeong"] == pytest.approx(250000 / expected_pyeong, rel=1e-3)


def test_summarize_trades():
    from sources.molit import summarize_trades
    items = [
        {"apt": "A", "dong": "역삼동", "price_per_pyeong": 1000, "deal_amount": 50000, "pyeong": 50},
        {"apt": "A", "dong": "역삼동", "price_per_pyeong": 2000, "deal_amount": 60000, "pyeong": 30},
        {"apt": "B", "dong": "개포동", "price_per_pyeong": 3000, "deal_amount": 90000, "pyeong": 30},
        {"apt": "C", "dong": "x", "price_per_pyeong": None, "deal_amount": 1, "pyeong": 1},  # 제외
    ]
    out = summarize_trades(items)
    assert len(out) == 2                       # C 제외(평당가 없음)
    assert out[0]["apt"] == "B"                # 평균 평당가 내림차순
    assert out[1]["apt"] == "A"
    assert out[1]["count"] == 2
    assert out[1]["avg_price_per_pyeong"] == 1500.0
    assert out[1]["min_price_per_pyeong"] == 1000.0
    assert out[1]["max_price_per_pyeong"] == 2000.0
    assert out[1]["avg_deal_amount"] == 55000.0


@respx.mock
async def test_apt_trade_summary_groups(monkeypatch):
    monkeypatch.setenv("MOLIT_API_KEY", "dummy-key")
    respx.get(MOLIT_TRADE).mock(return_value=httpx.Response(200, text=_MOLIT_XML))
    res = await srv.get_apt_trade_summary("강남구", "2024-06")
    assert res["source"] == "molit"
    assert res["deal_count"] == 2
    assert res["complex_count"] == 2           # 래미안, 개포자이
    assert all("avg_price_per_pyeong" in c for c in res["items"])


def test_per_pyeong():
    from sources.molit import _per_pyeong
    pyeong, ppp = _per_pyeong(250000, 84.97)
    assert pyeong == pytest.approx(25.70, abs=0.05)
    assert ppp == pytest.approx(9726.7, rel=1e-3)
    assert _per_pyeong(250000, 0) == (None, None)      # 면적 0
    assert _per_pyeong(None, 84.97) == (None, None)    # 금액 없음


@respx.mock
async def test_apt_trade_region_name_and_ym_normalize(monkeypatch):
    """지역명 '서울 강남구' → 11680 변환, 'YYYY-MM' 정규화."""
    monkeypatch.setenv("MOLIT_API_KEY", "dummy-key")
    route = respx.get(MOLIT_TRADE).mock(
        return_value=httpx.Response(200, text=_MOLIT_XML))
    res = await srv.get_apt_trade("서울 강남구", "2024-06")
    assert res["region_code"] == "11680"
    assert res["deal_ym"] == "202406"
    assert route.calls[0].request.url.params["LAWD_CD"] == "11680"
    assert route.calls[0].request.url.params["DEAL_YMD"] == "202406"


async def test_apt_trade_ambiguous_region_returns_fallback():
    """'중구'는 여러 시도에 있어 변환 실패 → fallback."""
    res = await srv.get_apt_trade("중구", "202406")
    assert "error" in res
    assert res["source"] == "fallback"
    assert "여러 지역" in res["error"]


async def test_apt_trade_no_key_returns_fallback(monkeypatch):
    monkeypatch.delenv("MOLIT_API_KEY", raising=False)
    res = await srv.get_apt_trade("11680", "202406")
    assert "error" in res
    assert res["source"] == "fallback"


def test_fail_scrubs_api_key():
    """error 메시지의 serviceKey/authkey는 마스킹된다."""
    from core.schema import fail
    leaked = "403 for url ...?serviceKey=SECRET123&LAWD_CD=11680"
    res = fail("매매", leaked)
    assert "SECRET123" not in res["error"]
    assert "serviceKey=***" in res["error"]


def test_resolve_region():
    from sources.region_codes import resolve_region
    assert resolve_region("11680") == "11680"
    assert resolve_region("1168010100") == "11680"        # 10자리 → 앞 5자리
    assert resolve_region("강남구") == "11680"
    assert resolve_region("서울 강남구") == "11680"
    assert resolve_region("서울특별시 강남구") == "11680"
    assert resolve_region("수원 영통구") == "41117"
    assert resolve_region("영통구") == "41117"
    assert resolve_region("성남 분당구") == "41135"
    assert resolve_region("세종") == "36110"
    assert resolve_region("부천") == "41190"
    with pytest.raises(ValueError):
        resolve_region("중구")          # 모호
    with pytest.raises(ValueError):
        resolve_region("없는동네구")     # 미수록


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
