"""K-apt 단지 기본정보(세대수) 소스/Tool 테스트 (respx HTTP 모킹).

핵심 검증:
  - 단지목록/기본정보 응답을 JSON·XML 어느 쪽이 와도 파싱하는지
  - 실거래 단지명 ↔ K-apt 단지명 표기 차이를 정규화가 흡수하는지
  - apt_trade_summary에 세대수/회전율이 붙는지
  - 활용 미신청(403·reasonCode 30) 시 실거래 집계를 깨뜨리지 않고 조용히 넘어가는지
"""
import httpx
import pytest
import respx

import finance_server as srv
from sources import kapt

APT_TRADE = ("https://apis.data.go.kr/1613000/RTMSDataSvcAptTradeDev/"
             "getRTMSDataSvcAptTradeDev")
LIST_URL = "https://apis.data.go.kr/1613000/AptListService3/getSigunguAptList3"
BASIS_URL = ("https://apis.data.go.kr/1613000/AptBasisInfoServiceV4/"
             "getAphusBassInfoV4")


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("MOLIT_API_KEY", "dummy-key")
    monkeypatch.setenv("DATA_GO_KR_API_KEY", "dummy-key")


# 단지목록은 포털 명세대로 JSON, 기본정보는 게이트웨이가 XML을 주는 경우를 각각 흉내낸다.
_LIST_JSON = {
    "response": {
        "header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE."},
        "body": {"items": {"item": [
            {"kaptCode": "A13822003", "kaptName": "래미안대치팰리스1단지",
             "as1": "서울특별시", "as2": "강남구", "as3": "대치동",
             "bjdCode": "1168010600"},
            {"kaptCode": "A13800001", "kaptName": "은마아파트",
             "as1": "서울특별시", "as2": "강남구", "as3": "대치동",
             "bjdCode": "1168010600"},
        ]}, "numOfRows": 1000, "pageNo": 1, "totalCount": 2},
    }
}

_BASIS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<response><header><resultCode>00</resultCode><resultMsg>OK</resultMsg></header>
<body><item>
<kaptCode>A13822003</kaptCode><kaptName>래미안대치팰리스1단지</kaptName>
<kaptAddr>서울특별시 강남구 대치동</kaptAddr><kaptdaCnt>1,278</kaptdaCnt>
<kaptDongCnt>15</kaptDongCnt><hoCnt>1278</hoCnt><kaptUsedate>20150925</kaptUsedate>
<kaptTopFloor>35</kaptTopFloor><kaptTarea>235000.5</kaptTarea>
<codeHallNm>계단식</codeHallNm><bjdCode>1168010600</bjdCode>
</item></body></response>"""

_TRADE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<response><header><resultCode>000</resultCode><resultMsg>OK</resultMsg></header>
<body><items>
<item><aptNm>래미안 대치팰리스(1단지)</aptNm><dealAmount>350,000</dealAmount>
<dealYear>2024</dealYear><dealMonth>6</dealMonth><dealDay>15</dealDay>
<excluUseAr>84.9</excluUseAr><floor>10</floor><buildYear>2015</buildYear>
<umdNm>대치동</umdNm><jibun>670</jibun></item>
<item><aptNm>래미안 대치팰리스(1단지)</aptNm><dealAmount>360,000</dealAmount>
<dealYear>2024</dealYear><dealMonth>6</dealMonth><dealDay>20</dealDay>
<excluUseAr>84.9</excluUseAr><floor>12</floor><buildYear>2015</buildYear>
<umdNm>대치동</umdNm><jibun>670</jibun></item>
</items><numOfRows>10</numOfRows><pageNo>1</pageNo><totalCount>2</totalCount></body>
</response>"""

_DENIED_JSON = """{"OpenAPI_ServiceResponse":{"cmmMsgHeader":{
"errMsg":"SERVICE_KEY_IS_NOT_REGISTERED_ERROR",
"returnAuthMsg":"등록되지 않은 서비스키","returnReasonCode":"30"}}}"""


def _mock_kapt():
    respx.get(LIST_URL).mock(return_value=httpx.Response(200, json=_LIST_JSON))
    respx.get(BASIS_URL).mock(return_value=httpx.Response(200, text=_BASIS_XML))


# ------------------------------------------------------------- 정규화/매칭


@pytest.mark.parametrize("raw, expected", [
    # 차수 표기 통일 — 실거래/K-apt가 '(1단지)'·'제1단지아파트'·'1차'로 제각각 적는다
    ("래미안 대치팰리스(1단지)", "래미안대치팰리스1"),
    ("래미안대치팰리스제1단지아파트", "래미안대치팰리스1"),
    ("개포우성2", "개포우성2"),
    ("개포우성2차", "개포우성2"),
    # 괄호 안 별칭/동번호는 버리고, 동 번호 나열도 제거
    ("한양1차(영동한양)", "한양1"),
    ("한신(개포)", "한신"),
    ("현대14차(203,204,205,206동)", "현대14"),
    ("선경1차(1동-7동)", "선경1"),
    ("대치우성아파트1동,2동,3동,5동", "대치우성"),
    ("e편한세상 강일", "E편한세상강일"),
    (None, ""),
])
def test_norm_name(raw, expected):
    assert kapt.norm_name(raw) == expected


def test_norm_name_keeps_complex_number():
    """차수 '숫자'까지 지우면 개포주공 1/2/3단지가 한 덩어리로 뭉개진다."""
    assert kapt.norm_name("개포주공1단지") != kapt.norm_name("개포주공2단지")


def test_match_complex_prefers_same_dong_and_gives_up_when_ambiguous():
    complexes = [
        {"kapt_code": "A1", "name": "푸르지오1단지", "dong": "대치동"},
        {"kapt_code": "A2", "name": "푸르지오2단지", "dong": "대치동"},
        {"kapt_code": "B1", "name": "은마아파트", "dong": "대치동"},
    ]
    idx = kapt._build_index(complexes)
    # 완전일치(표기 차이 흡수)
    assert kapt.match_complex(idx, "대치동", "은마")["kapt_code"] == "B1"
    # 부분일치 후보가 둘이면 오매칭 대신 포기
    assert kapt.match_complex(idx, "대치동", "푸르지오") is None


# ------------------------------------------------------------- 소스 단위


@respx.mock
async def test_sigungu_complexes_parses_json():
    _mock_kapt()
    items = await kapt.sigungu_complexes("11680")
    assert len(items) == 2
    assert items[0]["kapt_code"] == "A13822003"
    assert items[0]["dong"] == "대치동"


@respx.mock
async def test_basis_info_parses_xml_and_caches():
    route = respx.get(BASIS_URL).mock(
        return_value=httpx.Response(200, text=_BASIS_XML))
    info = await kapt.basis_info("A13822003")
    assert info["households"] == 1278          # '1,278' → int
    assert info["dong_count"] == 15
    assert info["use_date"] == "2015-09-25"    # 8자리 → ISO
    assert info["top_floor"] == 35
    # 두 번째 호출은 디스크 캐시 히트 → 네트워크 재호출 없음
    again = await kapt.basis_info("A13822003")
    assert again["households"] == 1278
    assert route.call_count == 1
    # cached_only는 캐시에 없으면 호출하지 않고 None
    assert await kapt.basis_info("NOPE", cached_only=True) is None


@respx.mock
async def test_complex_info_returns_candidates_when_ambiguous():
    _mock_kapt()
    res = await kapt.complex_info("11680", "래미안")
    # '래미안'은 1개 단지에만 걸리므로 매칭 성공
    assert res["matched"]["households"] == 1278
    res2 = await kapt.complex_info("11680", "없는단지명")
    assert res2["matched"] is None
    assert res2["candidates"] == []


# ------------------------------------------------------------- Tool 통합


@respx.mock
async def test_trade_summary_attaches_households():
    respx.get(APT_TRADE).mock(return_value=httpx.Response(200, text=_TRADE_XML))
    _mock_kapt()
    res = await srv.get_apt_trade_summary("강남구", "2024-06")
    assert res["households_source"] == "kapt"
    assert res["households_matched"] == 1
    assert res["households_pending"] == 0
    item = res["items"][0]
    assert item["apt"] == "래미안 대치팰리스(1단지)"   # 실거래 원문 이름은 유지
    assert item["households"] == 1278
    assert item["dong_count"] == 15
    assert item["use_date"] == "2015-09-25"
    assert item["turnover_rate"] == pytest.approx(2 / 1278 * 100, abs=5e-4)


@respx.mock
async def test_attach_households_respects_fetch_limit():
    """호출 상한에 걸린 단지는 결측이 아니라 households_pending으로 드러난다."""
    _mock_kapt()
    summary = {"items": [{"apt": "은마아파트", "dong": "대치동", "count": 2}]}
    await kapt.attach_households("11680", summary, max_fetch=0)
    assert summary["households_matched"] == 0
    assert summary["households_pending"] == 1   # 이름은 매칭됐으나 못 받음
    assert summary["items"][0].get("households") is None


@respx.mock
async def test_trade_summary_survives_kapt_denied():
    """K-apt 활용 미신청(403)이어도 실거래 집계는 그대로 나와야 한다."""
    respx.get(APT_TRADE).mock(return_value=httpx.Response(200, text=_TRADE_XML))
    respx.get(LIST_URL).mock(return_value=httpx.Response(403, text=_DENIED_JSON))
    res = await srv.get_apt_trade_summary("강남구", "2024-06")
    assert res["source"] == "molit"
    assert res["deal_count"] == 2
    assert res["households_source"] is None
    assert res["items"][0].get("households") is None


@respx.mock
async def test_complex_info_tool_denied_falls_back():
    respx.get(LIST_URL).mock(return_value=httpx.Response(403, text=_DENIED_JSON))
    res = await srv.get_apt_complex_info("강남구", "은마")
    assert res["source"] == "fallback"


@respx.mock
async def test_complex_info_tool_ok():
    _mock_kapt()
    res = await srv.get_apt_complex_info("강남구", "은마아파트")
    assert res["matched"]["kapt_code"] == "A13822003"  # 모킹 응답이 단일 item
    assert res["matched"]["dong"] == "대치동"


async def test_complex_info_bad_region_fails():
    res = await srv.get_apt_complex_info("없는동네", "은마")
    assert res["source"] == "fallback"
