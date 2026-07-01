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


@respx.mock
async def test_apt_trade_summary_multi_month(monkeypatch):
    """months=3이면 3개월 거래가 합산돼 단지별 건수가 누적된다."""
    monkeypatch.setenv("MOLIT_API_KEY", "dummy-key")
    respx.get(MOLIT_TRADE).mock(return_value=httpx.Response(200, text=_MOLIT_XML))
    res = await srv.get_apt_trade_summary("강남구", "2026-04", months=3)
    assert res["months"] == 3
    assert res["period"] == "202602~202604"
    assert res["deal_count"] == 6              # 2건 × 3개월
    assert res["complex_count"] == 2           # 단지는 여전히 2개
    assert res["items"][0]["count"] == 3       # 단지별 3건으로 누적


MOLIT_RENT = ("https://apis.data.go.kr/1613000/RTMSDataSvcAptRent/"
              "getRTMSDataSvcAptRent")

# 래미안(역삼동) 전세 보증금 15억 / 동일 전용 84.97㎡ → 매매 25억 대비 전세가율 60%
_MOLIT_RENT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<response><header><resultCode>000</resultCode><resultMsg>OK</resultMsg></header>
<body><items>
<item><aptNm>래미안</aptNm><deposit>150,000</deposit><monthlyRent>0</monthlyRent>
<excluUseAr>84.97</excluUseAr><floor>5</floor><buildYear>2005</buildYear>
<umdNm>역삼동</umdNm><jibun>700</jibun>
<dealYear>2024</dealYear><dealMonth>6</dealMonth><dealDay>10</dealDay></item>
<item><aptNm>래미안</aptNm><deposit>5,000</deposit><monthlyRent>200</monthlyRent>
<excluUseAr>84.97</excluUseAr><floor>3</floor><buildYear>2005</buildYear>
<umdNm>역삼동</umdNm><jibun>700</jibun>
<dealYear>2024</dealYear><dealMonth>6</dealMonth><dealDay>12</dealDay></item>
</items><numOfRows>10</numOfRows><pageNo>1</pageNo><totalCount>2</totalCount></body>
</response>"""


def test_jeonse_ratio():
    from sources.molit import jeonse_ratio
    trade = [{"dong": "역삼동", "apt": "A", "price_per_pyeong": 2000},
             {"dong": "개포동", "apt": "B", "price_per_pyeong": 3000}]  # 매매만
    rent = [{"dong": "역삼동", "apt": "A", "monthly_rent": 0, "deposit_per_pyeong": 1200},
            {"dong": "역삼동", "apt": "A", "monthly_rent": 50, "deposit_per_pyeong": 9999},  # 월세 제외
            {"dong": "수서동", "apt": "C", "monthly_rent": 0, "deposit_per_pyeong": 800}]   # 전세만
    out = jeonse_ratio(trade, rent)
    assert len(out) == 1                      # A만 매매·전세 모두 존재
    assert out[0]["apt"] == "A"
    assert out[0]["jeonse_ratio"] == 60.0     # 1200/2000*100
    assert out[0]["jeonse_count"] == 1        # 월세 1건 제외


@respx.mock
async def test_get_jeonse_ratio_e2e(monkeypatch):
    monkeypatch.setenv("MOLIT_API_KEY", "dummy-key")
    respx.get(MOLIT_TRADE).mock(return_value=httpx.Response(200, text=_MOLIT_XML))
    respx.get(MOLIT_RENT).mock(return_value=httpx.Response(200, text=_MOLIT_RENT_XML))
    res = await srv.get_jeonse_ratio("강남구", "2024-06")
    assert res["source"] == "molit"
    assert res["matched_complex_count"] == 1   # 래미안만 매칭
    assert res["items"][0]["apt"] == "래미안"
    assert res["items"][0]["jeonse_ratio"] == pytest.approx(60.0, abs=0.2)


def test_months_back():
    from sources.molit import _months_back
    assert _months_back("202604", 1) == ["202604"]
    assert _months_back("202602", 3) == ["202602", "202601", "202512"]  # 연도 경계
    assert _months_back("202604", 0) == ["202604"]                       # 최소 1


@respx.mock
async def test_get_jeonse_ratio_multi_month(monkeypatch):
    """months=3이면 같은 단지가 여러 달에서 합산돼도 단지수는 1로 집계."""
    monkeypatch.setenv("MOLIT_API_KEY", "dummy-key")
    respx.get(MOLIT_TRADE).mock(return_value=httpx.Response(200, text=_MOLIT_XML))
    respx.get(MOLIT_RENT).mock(return_value=httpx.Response(200, text=_MOLIT_RENT_XML))
    res = await srv.get_jeonse_ratio("강남구", "2026-04", months=3)
    assert res["months"] == 3
    assert res["period"] == "202602~202604"
    assert res["matched_complex_count"] == 1        # 래미안 단지 1개로 묶임
    assert res["items"][0]["jeonse_count"] == 3     # 3개월 × 전세 1건


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


# ------------------------------------------------ 공사현황(철도/광역교통)


def test_resolve_line():
    from sources.rail_lines import resolve_line
    a = resolve_line("GTX-A")
    assert a["preset"] is True
    assert a["line"] == "GTX-A"
    assert "GTX-A" in a["keywords"] and "수도권광역급행철도 A" in a["keywords"]
    assert resolve_line("gtx a")["line"] == "GTX-A"          # 공백/대소문자 무시
    assert resolve_line("삼성동탄")["line"] == "GTX-A"       # 키워드로도 매칭
    free = resolve_line("어떤신설노선")                      # 미수록 → passthrough
    assert free["preset"] is False
    assert free["keywords"] == ["어떤신설노선"]


_G2B_JSON = {"response": {"header": {"resultCode": "00", "resultMsg": "정상"},
    "body": {"pageNo": 1, "numOfRows": 10, "totalCount": 1, "items": [
        {"bidNtceNo": "20260601234", "bidNtceOrd": "00",
         "bidNtceNm": "수도권광역급행철도 A노선 OO공구 건설공사",
         "ntceInsttNm": "국가철도공단", "dminsttNm": "국토교통부",
         "bidNtceDt": "2026-06-20 10:00:00", "opengDt": "2026-07-01 11:00:00",
         "presmptPrce": "120000000000", "asignBdgtAmt": "150000000000",
         "bidNtceUrl": "https://www.g2b.go.kr/x"}]}}}


@respx.mock
async def test_construction_bids_ok(monkeypatch):
    monkeypatch.setenv("DATA_GO_KR_API_KEY", "dummy-key")
    route = respx.get(url__startswith="https://apis.data.go.kr/1230000").mock(
        return_value=httpx.Response(200, json=_G2B_JSON))
    res = await srv.get_construction_bids("GTX-A")
    assert res["source"] == "g2b"
    assert res["count"] == 1
    b = res["bids"][0]
    assert b["공고번호"] == "20260601234"
    assert b["추정가격"] == 120000000000.0
    assert b["발주기관"] == "국가철도공단"
    # 프리셋이면 여러 키워드로 검색하므로 호출이 1회 이상
    assert route.call_count >= 1
    assert route.calls[0].request.url.params["bidNtceNm"]  # 공고명 키워드 전달


async def test_construction_bids_no_key_returns_fallback(monkeypatch):
    monkeypatch.delenv("DATA_GO_KR_API_KEY", raising=False)
    monkeypatch.delenv("MOLIT_API_KEY", raising=False)
    res = await srv.get_construction_bids("GTX-A")
    assert "error" in res
    assert res["source"] == "fallback"


_FISCAL_JSON = {"ExpenseBudgetTimeSeries": [
    {"head": [{"list_total_count": 1}, {"RESULT": {"CODE": "INFO-000", "MESSAGE": "정상"}}]},
    {"row": [{"OFFC_NM": "신안산선 복선전철", "FY": "2026",
              "Y_PRES_DRYR_BD_AMT": "500000", "EXE_AMT": "120000", "DEPT_NM": "국토교통부"}]}]}


@respx.mock
async def test_project_budget_ok(monkeypatch):
    monkeypatch.setenv("OPEN_FISCAL_API_KEY", "dummy-key")
    respx.get(url__startswith="https://openapi.openfiscaldata.go.kr").mock(
        return_value=httpx.Response(200, json=_FISCAL_JSON))
    res = await srv.get_project_budget("신안산선")
    assert res["source"] == "openfiscaldata"
    assert res["count"] == 1
    p = res["projects"][0]
    assert p["사업명"] == "신안산선 복선전철"
    assert p["연도"] == "2026"
    assert p["예산액"] == 500000.0
    assert p["집행액"] == 120000.0


@respx.mock
async def test_project_budget_year_filter(monkeypatch):
    monkeypatch.setenv("OPEN_FISCAL_API_KEY", "dummy-key")
    respx.get(url__startswith="https://openapi.openfiscaldata.go.kr").mock(
        return_value=httpx.Response(200, json=_FISCAL_JSON))
    res = await srv.get_project_budget("신안산선", year=2025)  # 2026만 있으므로 0건
    assert res["count"] == 0


async def test_project_budget_no_key_returns_fallback(monkeypatch):
    monkeypatch.delenv("OPEN_FISCAL_API_KEY", raising=False)
    res = await srv.get_project_budget("신안산선")
    assert "error" in res
    assert res["source"] == "fallback"


_NOTICE_JSON = {"records": [
    {"고시명": "7호선 청라연장 기본계획 변경고시", "고시번호": "2026-100",
     "고시일": "2026-05-10", "사업명": "도시철도 7호선 청라국제도시 연장", "고시구분": "기본계획"},
    {"고시명": "관련없는 노선 고시", "고시번호": "2026-101",
     "고시일": "2026-05-11", "사업명": "다른 사업", "고시구분": "실시계획"}]}


@respx.mock
async def test_rail_notices_filter(monkeypatch):
    monkeypatch.setenv("KRNA_NOTICE_URL_BASIC", "https://example.gov/notice.json")
    respx.get("https://example.gov/notice.json").mock(
        return_value=httpx.Response(200, json=_NOTICE_JSON))
    res = await srv.get_rail_notices("7호선 청라연장")
    assert res["source"] == "krna_notice"
    assert res["total_records"] == 2
    assert res["count"] == 1                       # 청라 키워드 매칭 1건만
    assert res["notices"][0]["고시번호"] == "2026-100"


async def test_rail_notices_no_url_returns_fallback(monkeypatch):
    monkeypatch.delenv("KRNA_NOTICE_URL_BASIC", raising=False)
    res = await srv.get_rail_notices("GTX-A")
    assert "error" in res
    assert res["source"] == "fallback"


def test_kr_progress_extract():
    from sources.kr_progress import _extract
    text = ("기타 내용 ... 수도권광역급행철도 A노선 건설사업 '26.3월 기준 공정률 19.4% 이며 "
            "일반철도 어쩌고 공정률 3.9%")
    out = _extract(text, ["수도권광역급행철도 A", "GTX-A"])
    assert len(out) == 1
    assert out[0]["공정률_pct"] == 19.4
    assert out[0]["기준월"] == "2026-03"


def test_fail_scrubs_fiscal_key():
    """열린재정 Key= 파라미터도 마스킹된다(보강된 _SECRET_RE)."""
    from core.schema import fail
    res = fail("재정", "401 ...?Key=FISCALSECRET&Type=json")
    assert "FISCALSECRET" not in res["error"]
    assert "Key=***" in res["error"]


@respx.mock
async def test_rail_project_status_partial(monkeypatch):
    """통합 스냅샷: 일부 소스만 설정돼도 나머지는 error로 표기하고 반환(크래시 없음)."""
    monkeypatch.setenv("DATA_GO_KR_API_KEY", "dummy-key")
    monkeypatch.delenv("OPEN_FISCAL_API_KEY", raising=False)
    monkeypatch.delenv("KRNA_NOTICE_URL_BASIC", raising=False)
    respx.get(url__startswith="https://apis.data.go.kr/1230000").mock(
        return_value=httpx.Response(200, json=_G2B_JSON))
    res = await srv.get_rail_project_status("GTX-A")
    assert res["line"] == "GTX-A"
    assert res["bids"]["source"] == "g2b"          # 발주는 정상
    assert "error" in res["budget"]                # 예산은 키 없어 error
    assert "error" in res["notices"]               # 고시는 URL 없어 error


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
