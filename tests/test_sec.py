"""sources/sec.py + get_implied_useful_life 테스트 (respx 모킹, 실네트워크 없음).

수용기준 T10(UA 미설정 시 403이 아니라 안내 메시지)과 SEC 10 req/s 준수,
그리고 내용연수 역산의 연장/단축/복원 경로를 검증한다.
"""
from __future__ import annotations

import time

import httpx
import pytest
import respx

import finance_server as srv
from core.ratelimit import RateLimiter
from sources import sec

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
CC = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/us-gaap/{tag}.json"

GROSS = "PropertyPlantAndEquipmentGross"
NET = "PropertyPlantAndEquipmentNet"
ACCUM = ("AccumulatedDepreciationDepletionAndAmortization"
         "PropertyPlantAndEquipment")
LIFE = "PropertyPlantAndEquipmentUsefulLife"
DDA = "DepreciationDepletionAndAmortization"

CIK_AMZN = "0001018724"


@pytest.fixture(autouse=True)
def _ua(monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", "Test User test@example.com")
    # 테스트에서 요청 간격 125ms를 기다리지 않도록 리미터를 느슨하게.
    monkeypatch.setattr(sec, "_LIMITER", RateLimiter(10_000.0))


def _mock_tickers(**extra):
    rows = {"0": {"cik_str": 1018724, "ticker": "AMZN", "title": "AMAZON COM INC"}}
    rows.update(extra)
    respx.get(TICKERS_URL).mock(return_value=httpx.Response(200, json=rows))


def _cc(unit: str, facts: list[dict], tag: str = "X") -> dict:
    return {"cik": 1018724, "taxonomy": "us-gaap", "tag": tag,
            "units": {unit: facts}}


def _mock_concept(tag: str, body, status: int = 200, cik: str = CIK_AMZN):
    return respx.get(CC.format(cik=cik, tag=tag)).mock(
        return_value=httpx.Response(status, json=body))


def _dur(start, end, val, form="10-K", filed="2026-02-01"):
    return {"start": start, "end": end, "val": val, "form": form,
            "filed": filed, "accn": f"a-{end}", "fy": 2026, "fp": "FY"}


def _inst(end, val, form="10-K", filed="2026-02-01"):
    return {"end": end, "val": val, "form": form, "filed": filed,
            "accn": f"i-{end}"}


def _dda_facts():
    """3개 회계연도 연간 D&A(달력연도 결산)."""
    return [
        _dur("2023-01-01", "2023-12-31", 100.0),
        _dur("2024-01-01", "2024-12-31", 100.0),
        _dur("2025-01-01", "2025-12-31", 125.0),   # 내용연수 단축 → 상각비 급증
    ]


# ---------------------------------------------------------------- 설정 오류

async def test_missing_user_agent_returns_guidance_not_403(monkeypatch):
    """수용기준 T10: UA 미설정 시 403이 아니라 조치 안내가 errors로 온다."""
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    r = await srv.get_implied_useful_life("AMZN")
    assert r["cik"] is None
    assert r["flag"] == "insufficient_data"
    e = r["errors"][0]
    assert e["field"] == "sec_user_agent"
    assert "SEC_USER_AGENT" in e["reason"] and "403" in e["reason"]


async def test_blank_user_agent_is_rejected(monkeypatch):
    """이메일이 없는 UA도 SEC가 거부하므로 미리 걸러 안내한다."""
    monkeypatch.setenv("SEC_USER_AGENT", "just-a-name")
    r = await srv.get_implied_useful_life("AMZN")
    assert r["errors"][0]["field"] == "sec_user_agent"


@respx.mock
async def test_unknown_ticker_reports_error():
    _mock_tickers()
    r = await srv.get_implied_useful_life("NOTATICKER")
    assert r["errors"][0]["field"] == "cik"
    assert "등재되지 않은" in r["errors"][0]["reason"]


@respx.mock
async def test_offline_returns_errors_not_exception():
    """수용기준 T11: 네트워크가 죽어도 예외 없이 errors로 반환."""
    respx.get(TICKERS_URL).mock(side_effect=httpx.ConnectError("offline"))
    r = await srv.get_implied_useful_life("AMZN")
    assert r["errors"] and r["series"] == []


# ---------------------------------------------------------------- CIK 조회

@respx.mock
async def test_resolve_cik_zero_pads_and_normalizes():
    _mock_tickers(**{"1": {"cik_str": 1067983, "ticker": "BRK-B",
                           "title": "BERKSHIRE HATHAWAY INC"}})
    assert await sec.resolve_cik("amzn") == (CIK_AMZN, "AMAZON COM INC")
    # 사용자는 BRK.B로 넣는 일이 잦다 → SEC 표기(BRK-B)로 정규화
    cik, _ = await sec.resolve_cik("BRK.B")
    assert cik == "0001067983"


@respx.mock
async def test_cik_map_cached_across_calls():
    route = respx.get(TICKERS_URL).mock(return_value=httpx.Response(
        200, json={"0": {"cik_str": 1018724, "ticker": "AMZN", "title": "A"}}))
    await sec.resolve_cik("AMZN")
    await sec.resolve_cik("AMZN")
    assert len(route.calls) == 1


@respx.mock
async def test_concept_404_is_cached_as_missing():
    """미공시 태그는 영구 사실이라 '없음'을 캐시해 반복 요청하지 않는다."""
    _mock_tickers()
    route = _mock_concept(GROSS, {}, status=404)
    for _ in range(2):
        with pytest.raises(sec.ConceptNotFound):
            await sec.concept(CIK_AMZN, GROSS)
    assert len(route.calls) == 1


@respx.mock
async def test_concept_any_falls_through_to_next_tag():
    _mock_concept("DepreciationDepletionAndAmortization", {}, status=404)
    _mock_concept("DepreciationAmortizationAndAccretionNet",
                  _cc("USD", _dda_facts()))
    tag, _ = await sec.concept_any(
        CIK_AMZN, ["DepreciationDepletionAndAmortization",
                   "DepreciationAmortizationAndAccretionNet"])
    assert tag == "DepreciationAmortizationAndAccretionNet"


# ---------------------------------------------------------------- 레이트리밋

async def test_rate_limiter_spaces_requests():
    """SEC 10 req/s 준수: 5건을 동시에 띄워도 간격이 벌어진다."""
    limiter = RateLimiter(20.0)   # 50ms 간격
    t0 = time.monotonic()
    import asyncio
    await asyncio.gather(*[limiter.acquire() for _ in range(5)])
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.15   # 첫 건은 즉시, 나머지 4건 × 50ms


# ---------------------------------------------------------------- 내용연수 역산

@respx.mock
async def test_implied_life_shortened():
    """gross PP&E는 늘었는데 D&A가 더 빨리 늘면 내용연수 단축으로 잡힌다."""
    _mock_tickers()
    _mock_concept(DDA, _cc("USD", _dda_facts()))
    _mock_concept(GROSS, _cc("USD", [
        _inst("2023-12-31", 600.0),
        _inst("2024-12-31", 600.0),
        _inst("2025-12-31", 625.0),
    ]))
    _mock_concept(NET, {}, status=404)
    _mock_concept(ACCUM, {}, status=404)
    _mock_concept(LIFE, {}, status=404)

    r = await srv.get_implied_useful_life("AMZN")
    assert r["cik"] == CIK_AMZN and r["entity_name"] == "AMAZON COM INC"
    assert [s["fy"] for s in r["series"]] == [2023, 2024, 2025]
    assert [s["implied_life_years"] for s in r["series"]] == [6.0, 6.0, 5.0]
    assert r["latest_life"] == 5.0 and r["prior_life"] == 6.0
    assert r["delta_years"] == -1.0
    assert r["flag"] == "shortened"
    assert r["dda_tag"] == DDA
    assert r["data_kind"] == "filing"
    assert all(s["gross_source"] == "reported" for s in r["series"])


@respx.mock
async def test_implied_life_extended():
    _mock_tickers()
    _mock_concept(DDA, _cc("USD", [
        _dur("2024-01-01", "2024-12-31", 100.0),
        _dur("2025-01-01", "2025-12-31", 80.0),    # 상각비 감소 = 내용연수 연장
    ]))
    _mock_concept(GROSS, _cc("USD", [
        _inst("2024-12-31", 500.0), _inst("2025-12-31", 500.0)]))
    _mock_concept(NET, {}, status=404)
    _mock_concept(ACCUM, {}, status=404)
    _mock_concept(LIFE, {}, status=404)

    r = await srv.get_implied_useful_life("AMZN")
    assert r["latest_life"] == 6.25 and r["prior_life"] == 5.0
    assert r["delta_years"] == 1.25
    assert r["flag"] == "extended"


@respx.mock
async def test_implied_life_stable_below_threshold():
    _mock_tickers()
    _mock_concept(DDA, _cc("USD", [
        _dur("2024-01-01", "2024-12-31", 100.0),
        _dur("2025-01-01", "2025-12-31", 102.0),
    ]))
    _mock_concept(GROSS, _cc("USD", [
        _inst("2024-12-31", 500.0), _inst("2025-12-31", 505.0)]))
    _mock_concept(NET, {}, status=404)
    _mock_concept(ACCUM, {}, status=404)
    _mock_concept(LIFE, {}, status=404)

    r = await srv.get_implied_useful_life("AMZN")
    assert abs(r["delta_years"]) < 0.3
    assert r["flag"] == "stable"


@respx.mock
async def test_implied_life_restores_gross_from_net_plus_accum():
    """Gross 미공시 기업은 Net + 감가상각누계액으로 복원한다."""
    _mock_tickers()
    _mock_concept(DDA, _cc("USD", [
        _dur("2024-01-01", "2024-12-31", 100.0),
        _dur("2025-01-01", "2025-12-31", 100.0)]))
    _mock_concept(GROSS, {}, status=404)
    _mock_concept(NET, _cc("USD", [
        _inst("2024-12-31", 300.0), _inst("2025-12-31", 320.0)]))
    _mock_concept(ACCUM, _cc("USD", [
        _inst("2024-12-31", 300.0), _inst("2025-12-31", 280.0)]))
    _mock_concept(LIFE, {}, status=404)

    r = await srv.get_implied_useful_life("AMZN")
    assert [s["gross_ppe"] for s in r["series"]] == [600.0, 600.0]
    assert all(s["gross_source"] == "restored(net+accum)" for s in r["series"])
    assert r["flag"] == "stable"


@respx.mock
async def test_implied_life_insufficient_data_when_no_ppe():
    """PP&E를 어느 태그로도 못 구하면 추정하지 않고 insufficient_data."""
    _mock_tickers()
    _mock_concept(DDA, _cc("USD", _dda_facts()))
    for tag in (GROSS, NET, ACCUM, LIFE):
        _mock_concept(tag, {}, status=404)

    r = await srv.get_implied_useful_life("AMZN")
    assert r["series"] == []
    assert r["flag"] == "insufficient_data"
    assert r["errors"][0]["field"] == "gross_ppe"


@respx.mock
async def test_implied_life_single_year_cannot_judge():
    _mock_tickers()
    _mock_concept(DDA, _cc("USD", [_dur("2025-01-01", "2025-12-31", 100.0)]))
    _mock_concept(GROSS, _cc("USD", [_inst("2025-12-31", 500.0)]))
    _mock_concept(NET, {}, status=404)
    _mock_concept(ACCUM, {}, status=404)
    _mock_concept(LIFE, {}, status=404)

    r = await srv.get_implied_useful_life("AMZN")
    assert r["latest_life"] == 5.0
    assert r["delta_years"] is None and r["flag"] == "insufficient_data"
    assert r["errors"][0]["field"] == "delta_years"


@respx.mock
async def test_implied_life_includes_direct_tag_when_present():
    """회사가 직접 태깅한 내용연수가 있으면 함께 준다(자산군별 다중값은 ambiguous)."""
    _mock_tickers()
    _mock_concept(DDA, _cc("USD", _dda_facts()))
    _mock_concept(GROSS, _cc("USD", [
        _inst("2024-12-31", 600.0), _inst("2025-12-31", 625.0)]))
    _mock_concept(NET, {}, status=404)
    _mock_concept(ACCUM, {}, status=404)
    _mock_concept(LIFE, _cc("Y", [
        {"end": "2025-12-31", "val": 5.0, "accn": "x", "filed": "2026-02-01"},
        {"end": "2025-12-31", "val": 10.0, "accn": "x", "filed": "2026-02-01"},
    ]))

    r = await srv.get_implied_useful_life("AMZN")
    d = r["direct_useful_life"]
    assert d["values"] == [5.0, 10.0] and d["ambiguous"] is True
    assert d["unit"] == "Y" and d["end"] == "2025-12-31"
