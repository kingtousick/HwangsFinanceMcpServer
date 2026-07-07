"""ECOS 소스/Tool 테스트 (respx HTTP 모킹).

검증:
  - 정상 파싱 + 기본/지정/빈 키워드 필터
  - 에러 봉투({"RESULT":{...}}) 비삼킴 → fallback
  - 키 미설정 → fallback
  - 경로형 인증키 마스킹(fail 시 에러 메시지에 키 노출 금지)
"""
import httpx
import pytest
import respx

import finance_server as srv
from core import cache, http
from core.schema import fail

ECOS_KEY = "SECRETKEY123"
ECOS_URL = f"https://ecos.bok.or.kr/api/KeyStatisticList/{ECOS_KEY}/json/kr/1/100"


@pytest.fixture(autouse=True)
async def _reset(monkeypatch):
    monkeypatch.setenv("ECOS_API_KEY", ECOS_KEY)
    cache.clear()
    await http.aclose()
    yield
    await http.aclose()


def _payload():
    return {"KeyStatisticList": {"list_total_count": 3, "row": [
        {"CLASS_NAME": "시장금리", "KEYSTAT_NAME": "한국은행 기준금리",
         "DATA_VALUE": "3.00", "CYCLE": "202606", "UNIT_NAME": "%"},
        {"CLASS_NAME": "물가", "KEYSTAT_NAME": "소비자물가지수 등락률",
         "DATA_VALUE": "2.1", "CYCLE": "202605", "UNIT_NAME": "%"},
        {"CLASS_NAME": "고용", "KEYSTAT_NAME": "실업률",
         "DATA_VALUE": "2.8", "CYCLE": "202605", "UNIT_NAME": "%"},
    ]}}


@respx.mock
async def test_default_keywords_filter():
    respx.get(ECOS_URL).mock(return_value=httpx.Response(200, json=_payload()))
    res = await srv.get_macro_indicators()
    assert res["source"] == "ecos"
    assert res["total_available"] == 3
    names = [i["지표명"] for i in res["indicators"]]
    assert "한국은행 기준금리" in names
    assert "소비자물가지수 등락률" in names
    assert "실업률" not in names  # 기본 필터 밖
    base = next(i for i in res["indicators"] if i["지표명"] == "한국은행 기준금리")
    assert base["값"] == pytest.approx(3.0)
    assert base["단위"] == "%"


@respx.mock
async def test_custom_and_empty_keywords():
    respx.get(ECOS_URL).mock(return_value=httpx.Response(200, json=_payload()))
    res = await srv.get_macro_indicators(["실업"])
    assert res["count"] == 1
    res_all = await srv.get_macro_indicators([])
    assert res_all["count"] == 3  # 빈 리스트 = 전체


@respx.mock
async def test_error_envelope_not_swallowed():
    respx.get(ECOS_URL).mock(return_value=httpx.Response(200, json={
        "RESULT": {"CODE": "INFO-100", "MESSAGE": "인증키가 유효하지 않습니다"}}))
    res = await srv.get_macro_indicators()
    assert res["source"] == "fallback"
    assert "INFO-100" in res["error"]


async def test_no_key_falls_back(monkeypatch):
    monkeypatch.delenv("ECOS_API_KEY", raising=False)
    res = await srv.get_macro_indicators()
    assert res["source"] == "fallback"
    assert "ECOS_API_KEY" in res["error"]


@respx.mock
async def test_path_key_masked_in_http_error():
    """httpx 예외 메시지의 URL 경로에 박힌 인증키가 fail()에서 마스킹된다."""
    respx.get(ECOS_URL).mock(return_value=httpx.Response(500))
    res = await srv.get_macro_indicators()
    assert res["source"] == "fallback"
    assert ECOS_KEY not in res["error"]
    assert "***" in res["error"]


def test_fail_scrubs_ecos_path_key_directly():
    msg = (f"Server error '500' for url "
           f"'https://ecos.bok.or.kr/api/KeyStatisticList/{ECOS_KEY}/json/kr/1/100'")
    res = fail("x", msg)
    assert ECOS_KEY not in res["error"]
    assert "KeyStatisticList/***" in res["error"]
