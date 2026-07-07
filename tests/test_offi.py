"""오피스텔 실거래가 소스/Tool 테스트 (respx HTTP 모킹).

아파트와 필드가 동일하고 건물명 태그만 offiNm으로 다르므로, 핵심은:
  - offiNm이 items의 'apt' 키로 매핑돼 summarize_trades를 재사용하는지
  - 매매/전월세 각 엔드포인트가 올바로 호출되고 평당가/보증금 파싱이 되는지
  - 활용 미신청(403·에러코드) 시 fallback으로 강등되는지
"""
import httpx
import pytest
import respx

import finance_server as srv
from core import cache, http

OFFI_TRADE = ("https://apis.data.go.kr/1613000/RTMSDataSvcOffiTrade/"
              "getRTMSDataSvcOffiTrade")
OFFI_RENT = ("https://apis.data.go.kr/1613000/RTMSDataSvcOffiRent/"
             "getRTMSDataSvcOffiRent")


@pytest.fixture(autouse=True)
async def _reset(monkeypatch):
    monkeypatch.setenv("MOLIT_API_KEY", "dummy-key")
    cache.clear()
    await http.aclose()
    yield
    await http.aclose()


_OFFI_TRADE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<response><header><resultCode>000</resultCode><resultMsg>OK</resultMsg></header>
<body><items>
<item><offiNm>강남역서희스타힐스</offiNm><dealAmount>32,000</dealAmount>
<dealYear>2024</dealYear><dealMonth>6</dealMonth><dealDay>15</dealDay>
<excluUseAr>29.7</excluUseAr><floor>10</floor><buildYear>2018</buildYear>
<umdNm>역삼동</umdNm><jibun>820</jibun></item>
<item><offiNm>역삼푸르지오시티</offiNm><dealAmount>28,500</dealAmount>
<dealYear>2024</dealYear><dealMonth>6</dealMonth><dealDay>3</dealDay>
<excluUseAr>23.1</excluUseAr><floor>7</floor><buildYear>2015</buildYear>
<umdNm>역삼동</umdNm><jibun>737</jibun></item>
</items><numOfRows>10</numOfRows><pageNo>1</pageNo><totalCount>2</totalCount></body>
</response>"""

_OFFI_RENT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<response><header><resultCode>000</resultCode><resultMsg>OK</resultMsg></header>
<body><items>
<item><offiNm>강남역서희스타힐스</offiNm><deposit>20,000</deposit><monthlyRent>0</monthlyRent>
<excluUseAr>29.7</excluUseAr><floor>5</floor><buildYear>2018</buildYear>
<umdNm>역삼동</umdNm><jibun>820</jibun>
<dealYear>2024</dealYear><dealMonth>6</dealMonth><dealDay>10</dealDay></item>
<item><offiNm>강남역서희스타힐스</offiNm><deposit>1,000</deposit><monthlyRent>90</monthlyRent>
<excluUseAr>29.7</excluUseAr><floor>3</floor><buildYear>2018</buildYear>
<umdNm>역삼동</umdNm><jibun>820</jibun>
<dealYear>2024</dealYear><dealMonth>6</dealMonth><dealDay>12</dealDay></item>
</items><numOfRows>10</numOfRows><pageNo>1</pageNo><totalCount>2</totalCount></body>
</response>"""

_OFFI_403_XML = """<?xml version="1.0" encoding="UTF-8"?>
<OpenAPI_ServiceResponse><cmmMsgHeader>
<returnReasonCode>20</returnReasonCode>
<returnAuthMsg>SERVICE ACCESS DENIED ERROR</returnAuthMsg>
</cmmMsgHeader></OpenAPI_ServiceResponse>"""


@respx.mock
async def test_offi_trade_ok():
    respx.get(OFFI_TRADE).mock(return_value=httpx.Response(200, text=_OFFI_TRADE_XML))
    res = await srv.get_offi_trade("11680", "202406")
    assert res["source"] == "molit"
    assert res["count"] == 2
    first = res["items"][0]
    assert first["apt"] == "강남역서희스타힐스"   # offiNm → apt 키로 매핑
    assert first["deal_amount"] == 32000          # 만원
    assert first["area"] == pytest.approx(29.7)
    assert first["floor"] == 10
    assert first["date"] == "2024-06-15"
    expected_pyeong = 29.7 / 3.305785
    assert first["pyeong"] == pytest.approx(expected_pyeong, rel=1e-3)
    assert first["price_per_pyeong"] == pytest.approx(32000 / expected_pyeong, rel=1e-3)


@respx.mock
async def test_offi_trade_summary_groups():
    respx.get(OFFI_TRADE).mock(return_value=httpx.Response(200, text=_OFFI_TRADE_XML))
    res = await srv.get_offi_trade_summary("강남구", "2024-06")
    assert res["name"] == "오피스텔매매 단지별 평균평당가"
    assert res["deal_count"] == 2
    assert res["complex_count"] == 2
    assert all("avg_price_per_pyeong" in c for c in res["items"])


@respx.mock
async def test_offi_trade_summary_multi_month():
    respx.get(OFFI_TRADE).mock(return_value=httpx.Response(200, text=_OFFI_TRADE_XML))
    res = await srv.get_offi_trade_summary("강남구", "2026-04", months=3)
    assert res["months"] == 3
    assert res["period"] == "202602~202604"
    assert res["deal_count"] == 6      # 2건 × 3개월
    assert res["complex_count"] == 2


@respx.mock
async def test_offi_rent_ok():
    respx.get(OFFI_RENT).mock(return_value=httpx.Response(200, text=_OFFI_RENT_XML))
    res = await srv.get_offi_rent("11680", "202406")
    assert res["source"] == "molit"
    assert res["count"] == 2
    jeonse, wolse = res["items"]
    assert jeonse["apt"] == "강남역서희스타힐스"
    assert jeonse["deposit"] == 20000
    assert jeonse["monthly_rent"] == 0        # 전세
    assert wolse["monthly_rent"] == 90        # 월세


@respx.mock
async def test_offi_trade_access_denied_falls_back():
    respx.get(OFFI_TRADE).mock(return_value=httpx.Response(200, text=_OFFI_403_XML))
    res = await srv.get_offi_trade("11680", "202406")
    assert res["source"] == "fallback"
    assert "20" in res["error"]


@respx.mock
async def test_offi_trade_bad_region_fails():
    res = await srv.get_offi_trade("없는동네", "202406")
    assert res["source"] == "fallback"
