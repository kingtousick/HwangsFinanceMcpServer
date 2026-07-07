"""네이버 밸류에이션 소스/Tool 테스트 (respx HTTP 모킹).

검증:
  - 모바일 통합 API 정상 파싱(단위 문자열 '12.35배'/'2.05%'/'430조 4,613억' 처리)
  - 모바일 실패 → PC 정적 HTML 폴백
  - 전 소스 실패 → fallback
  - 한글 금액(_eok)·단위 숫자(_num) 파서 단위 테스트
"""
import httpx
import pytest
import respx

import finance_server as srv
from core import cache, http
from sources import naver_valuation as nv

M_URL = "https://m.stock.naver.com/api/stock/005930/integration"
PC_URL = "https://finance.naver.com/item/main.naver"


@pytest.fixture(autouse=True)
async def _reset():
    cache.clear()
    await http.aclose()
    yield
    await http.aclose()


def _mobile_payload():
    return {
        "stockName": "삼성전자",
        "itemCode": "005930",
        "closePrice": "69,900",
        "totalInfos": [
            {"code": "marketValue", "key": "시총", "value": "430조 4,613억원"},
            {"code": "per", "key": "PER", "value": "12.35배"},
            {"code": "pbr", "key": "PBR", "value": "1.41배"},
            {"code": "eps", "key": "EPS", "value": "5,656원"},
            {"code": "bps", "key": "BPS", "value": "49,538원"},
            {"code": "dividendRate", "key": "배당수익률", "value": "2.05%"},
        ],
    }


_PC_HTML = """<html><head><title>삼성전자 : 네이버 금융</title></head><body>
<em id="_market_sum">
\t\t430조 4,613
</em>
<em id="_per">12.35</em> <em id="_pbr">1.41</em>
<em id="_eps">5,656</em> <em id="_bps">49,538</em>
<em id="_dvr">2.05</em>
</body></html>"""


@respx.mock
async def test_mobile_api_ok():
    respx.get(M_URL).mock(return_value=httpx.Response(200, json=_mobile_payload()))
    res = await srv.get_stock_valuation("005930")
    assert res["source"] == "naver_m"
    assert res["name"] == "삼성전자"
    assert res["per"] == pytest.approx(12.35)
    assert res["pbr"] == pytest.approx(1.41)
    assert res["eps"] == pytest.approx(5656)
    assert res["dividend_yield_pct"] == pytest.approx(2.05)
    assert res["market_cap_eok"] == pytest.approx(4304613)  # 430조 4,613억
    assert res["close_price"] == pytest.approx(69900)


@respx.mock
async def test_pc_html_fallback():
    respx.get(M_URL).mock(return_value=httpx.Response(500))
    respx.get(PC_URL).mock(return_value=httpx.Response(200, text=_PC_HTML))
    res = await srv.get_stock_valuation("005930")
    assert res["source"] == "naver_pc"
    assert res["name"] == "삼성전자"
    assert res["per"] == pytest.approx(12.35)
    assert res["market_cap_eok"] == pytest.approx(4304613)
    assert res["dividend_yield_pct"] == pytest.approx(2.05)


@respx.mock
async def test_all_fail_returns_fallback():
    respx.get(M_URL).mock(return_value=httpx.Response(500))
    respx.get(PC_URL).mock(return_value=httpx.Response(500))
    res = await srv.get_stock_valuation("005930")
    assert res["source"] == "fallback"
    assert "error" in res


@respx.mock
async def test_empty_metrics_degrades_to_next_source():
    """모바일 응답에 핵심 지표가 없으면 빈 성공 대신 PC 폴백으로 강등."""
    respx.get(M_URL).mock(return_value=httpx.Response(
        200, json={"stockName": "삼성전자", "totalInfos": []}))
    respx.get(PC_URL).mock(return_value=httpx.Response(200, text=_PC_HTML))
    res = await srv.get_stock_valuation("005930")
    assert res["source"] == "naver_pc"


def test_eok_parser():
    assert nv._eok("430조 4,613억") == pytest.approx(4304613)
    assert nv._eok("430조 613억") == pytest.approx(4300613)  # 자릿수 붕괴 없음
    assert nv._eok("4,613억원") == pytest.approx(4613)
    assert nv._eok("430조") == pytest.approx(4300000)
    assert nv._eok("4613") == pytest.approx(4613)
    assert nv._eok("12.35배") is None


def test_num_parser():
    assert nv._num("12.35배") == pytest.approx(12.35)
    assert nv._num("2.05%") == pytest.approx(2.05)
    assert nv._num("69,900원") == pytest.approx(69900)
    assert nv._num("N/A") is None
    assert nv._num("430조 4,613억") is None  # 금액은 _eok로만
