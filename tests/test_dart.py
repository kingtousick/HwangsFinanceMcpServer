"""DART 소스/Tool 테스트 (respx HTTP 모킹).

검증:
  - corpCode ZIP 파싱 → 상장사 인덱스(비상장 제외)
  - 인덱스 24시간 캐시(재호출 시 다운로드 0회)
  - 비-ZIP(인증키 오류 봉투) 응답 → fallback
  - list.json status 000/013(빈 목록, 에러 아님)/그 외(에러 봉투 비삼킴) 3분기
  - query 해석: 종목명/6자리 코드, 모호 시 후보 안내 실패
"""
import io
import zipfile

import httpx
import pytest
import respx

import finance_server as srv
from core import cache, http

CORP_URL = "https://opendart.fss.or.kr/api/corpCode.xml"
LIST_URL = "https://opendart.fss.or.kr/api/list.json"


@pytest.fixture(autouse=True)
async def _reset(monkeypatch):
    monkeypatch.setenv("DART_API_KEY", "testkey")
    cache.clear()
    await http.aclose()
    yield
    await http.aclose()


def _corp_zip() -> bytes:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<result>
  <list><corp_code>00126380</corp_code><corp_name>삼성전자</corp_name>
        <stock_code>005930</stock_code><modify_date>20260101</modify_date></list>
  <list><corp_code>00149655</corp_code><corp_name>삼성물산</corp_name>
        <stock_code>028260</stock_code><modify_date>20260101</modify_date></list>
  <list><corp_code>00164742</corp_code><corp_name>현대자동차</corp_name>
        <stock_code>005380</stock_code><modify_date>20260101</modify_date></list>
  <list><corp_code>01234567</corp_code><corp_name>삼성전자서비스</corp_name>
        <stock_code> </stock_code><modify_date>20260101</modify_date></list>
</result>"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("CORPCODE.xml", xml)
    return buf.getvalue()


def _mock_corp():
    return respx.get(CORP_URL).mock(
        return_value=httpx.Response(200, content=_corp_zip()))


@respx.mock
async def test_search_exact_first_and_unlisted_excluded():
    _mock_corp()
    res = await srv.search_stock_code("삼성전자")
    assert res["count"] == 1  # 비상장 '삼성전자서비스'는 제외
    assert res["items"][0] == {"name": "삼성전자", "stock_code": "005930",
                               "corp_code": "00126380"}
    assert res["source"] == "dart"


@respx.mock
async def test_corp_index_cached_single_download():
    route = _mock_corp()
    await srv.search_stock_code("삼성전자")
    await srv.search_stock_code("현대자동차")
    assert route.call_count == 1  # 인덱스 24시간 캐시


@respx.mock
async def test_non_zip_auth_error_falls_back():
    respx.get(CORP_URL).mock(return_value=httpx.Response(
        200, text="<result><status>010</status><message>등록되지 않은 키</message></result>"))
    res = await srv.search_stock_code("삼성전자")
    assert res["source"] == "fallback"
    assert "ZIP이 아님" in res["error"]


def _list_payload(status="000", items=None):
    body = {"status": status, "message": "정상" if status == "000" else "msg"}
    if items is not None:
        body["list"] = items
        body["total_count"] = len(items)
    return body


@respx.mock
async def test_disclosures_ok_by_stock_code():
    _mock_corp()
    respx.get(LIST_URL).mock(return_value=httpx.Response(200, json=_list_payload(
        items=[{"corp_code": "00126380", "corp_name": "삼성전자", "stock_code": "005930",
                "corp_cls": "Y", "report_nm": "주요사항보고서(자기주식취득결정)",
                "rcept_no": "20260701000123", "flr_nm": "삼성전자",
                "rcept_dt": "20260701", "rm": "유"}])))
    res = await srv.get_dart_disclosures("005930", days=30)
    assert res["count"] == 1
    assert res["corp_code"] == "00126380"
    assert res["stock_code"] == "005930"
    d = res["disclosures"][0]
    assert d["제목"].startswith("주요사항보고서")
    assert d["시장"] == "유가증권"
    assert d["url"].endswith("rcpNo=20260701000123")


@respx.mock
async def test_disclosures_empty_013_is_not_error():
    _mock_corp()
    respx.get(LIST_URL).mock(return_value=httpx.Response(
        200, json=_list_payload(status="013")))
    res = await srv.get_dart_disclosures("삼성전자")
    assert "error" not in res
    assert res["count"] == 0
    assert res["disclosures"] == []


@respx.mock
async def test_disclosures_error_envelope_not_swallowed():
    _mock_corp()
    respx.get(LIST_URL).mock(return_value=httpx.Response(
        200, json=_list_payload(status="020")))
    res = await srv.get_dart_disclosures("005930")
    assert res["source"] == "fallback"
    assert "020" in res["error"]


@respx.mock
async def test_disclosures_ambiguous_name_guides():
    _mock_corp()
    res = await srv.get_dart_disclosures("삼성")  # 삼성전자·삼성물산 둘 다 일치
    assert res["source"] == "fallback"
    assert "여러 개" in res["error"]
    assert "005930" in res["error"] or "028260" in res["error"]
