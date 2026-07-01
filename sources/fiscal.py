"""열린재정 재정정보공개시스템 (openfiscaldata.go.kr) OpenAPI 소스.

철도/광역교통 사업의 '예타·재정' 신호를 잡는다. 사업별 예산이 잡히고 집행액이 늘면
'진짜 돈이 가고 있다'는 뜻. 사업명 키워드로 연도별 예산/집행 시계열을 조회한다.

응답은 서울/열린재정 계열 OpenAPI 표준 구조(JSON):
  {"<SERVICE>": [ {"head":[{"list_total_count":N},{"RESULT":{"CODE":..,"MESSAGE":..}}]},
                  {"row":[ {레코드}, ... ]} ]}
_extract_rows()가 이 구조와 평면 {data:[...]} 구조 모두를 방어적으로 파싱한다.

--- needs-verification ---------------------------------------------------------
열린재정 OpenAPI는 인증 후 'OPEN API' 탭에서만 정확한 (API명, 요청 파라미터명)을 볼 수
있다. 아래 _API_NAME / 검색 파라미터명(_KW_PARAM)은 합리적 기본값이며, 실제 값과 다르면
환경변수로 코드 변경 없이 덮어쓴다:
  OPEN_FISCAL_API_NAME : 재정사업 예산 시계열 API명 (예: 'ExpenseBudgetTimeSeries')
  OPEN_FISCAL_KW_PARAM : 사업명 검색 파라미터명 (기본 'OFFC_NM' 사용, 실제 명칭으로 교체)
인증키는 OPEN_FISCAL_API_KEY 환경변수(키 미설정/미구성 시 상위에서 fallback).
"""
from __future__ import annotations

import asyncio
import os

from core import http
from core.schema import to_float

_BASE = "https://openapi.openfiscaldata.go.kr/openapi"
# 재정사업 예산 시계열 API명(기본 추정값). 실제 값은 OPEN_FISCAL_API_NAME으로 덮어쓴다.
_DEFAULT_API_NAME = "ExpenseBudgetTimeSeries"
# 사업명 검색 파라미터명(추정값). 실제 값은 OPEN_FISCAL_KW_PARAM으로 덮어쓴다.
_DEFAULT_KW_PARAM = "OFFC_NM"

# 레코드 필드명 후보(출처별 표기 차이 흡수). 앞에서부터 매칭되는 첫 키를 사용.
_F_NAME = ("OFFC_NM", "FSCL_NM", "BIZ_NM", "사업명", "PGM_NM")
_F_YEAR = ("FY", "FSCL_YY", "YR", "회계연도", "연도")
_F_BUDGET = ("Y_PRES_DRYR_BD_AMT", "BUDGET_AMT", "예산액", "AMT", "BDGT_AMT")
_F_EXEC = ("EXE_AMT", "EXEC_AMT", "집행액", "EXEC")
_F_MINISTRY = ("DEPT_NM", "OFFC_NM2", "부처명", "MINIST_NM", "소관")


def _key() -> str:
    k = os.environ.get("OPEN_FISCAL_API_KEY")
    if not k:
        raise RuntimeError("OPEN_FISCAL_API_KEY not set")
    return k


def _first(d: dict, keys: tuple) -> object:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _extract_rows(payload) -> list[dict]:
    """열린재정 표준 응답에서 레코드 리스트 추출. 결과코드 비정상이면 예외.

    열린재정은 Type=json이어도 본문을 JSON 문자열로 이중 인코딩해 보내는 경우가 있어
    (r.json()이 dict가 아닌 str 반환) str이면 한 번 더 json.loads로 풀어준다.
    """
    if isinstance(payload, str):
        import json
        try:
            payload = json.loads(payload)
        except ValueError as e:
            raise RuntimeError(f"fiscal non-JSON response: {payload[:80]}") from e
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if not isinstance(payload, dict):
        raise RuntimeError(f"unexpected fiscal payload: {type(payload).__name__}")
    # 최상위 에러 봉투({"RESULT":{"CODE":"ERROR-310",..}})는 빈 결과로 위장 말고 예외로.
    top = payload.get("RESULT")
    if isinstance(top, dict):
        code = top.get("CODE", "")
        if code and "00" not in code and "INFO" not in code:
            raise RuntimeError(f"fiscal error {code}: {top.get('MESSAGE')}")
    # 평면 구조 우선
    for k in ("row", "data", "items", "list"):
        v = payload.get(k)
        if isinstance(v, list):
            return [r for r in v if isinstance(r, dict)]
    # 서울/열린재정 표준 구조: {SERVICE: [ {head:[...]}, {row:[...]} ]}
    for v in payload.values():
        if isinstance(v, list):
            for block in v:
                if isinstance(block, dict):
                    # 결과코드 검증(있으면)
                    head = block.get("head")
                    if isinstance(head, list):
                        for h in head:
                            res = h.get("RESULT") if isinstance(h, dict) else None
                            if isinstance(res, dict):
                                code = res.get("CODE", "")
                                if code and "00" not in code and "INFO" not in code:
                                    raise RuntimeError(
                                        f"fiscal error {code}: {res.get('MESSAGE')}")
                    row = block.get("row")
                    if isinstance(row, list):
                        return [r for r in row if isinstance(r, dict)]
    return []


def _project(r: dict) -> dict:
    return {
        "사업명": _first(r, _F_NAME),
        "연도": _first(r, _F_YEAR),
        "예산액": to_float(_first(r, _F_BUDGET)),
        "집행액": to_float(_first(r, _F_EXEC)),
        "부처": _first(r, _F_MINISTRY),
    }


async def _search_one(keyword: str, rows: int) -> list[dict]:
    api = os.environ.get("OPEN_FISCAL_API_NAME", _DEFAULT_API_NAME)
    kw_param = os.environ.get("OPEN_FISCAL_KW_PARAM", _DEFAULT_KW_PARAM)
    # 열린재정은 API명을 경로가 아니라 SERVICE 쿼리 파라미터로 받는다(실측: 경로형은 404,
    # base?SERVICE=... 형식만 유효. 잘못된 이름이면 RESULT.CODE=ERROR-310).
    params = {
        "Key": _key(),
        "Type": "json",
        "SERVICE": api,
        "pIndex": "1",
        "pSize": str(rows),
        kw_param: keyword,
    }
    payload = await http.get_json(_BASE, params=params, retries=1)
    return _extract_rows(payload)


async def search_budget(keywords: list[str], year: int | None = None,
                        rows: int = 100) -> dict:
    """사업명 키워드로 재정사업 예산/집행 시계열 조회 → 병합. year로 특정 연도 필터.

    일부 키워드 실패는 건너뛰고, 전부 실패 시 예외를 올려 상위 fallback.
    """
    results = await asyncio.gather(
        *(_search_one(kw, rows) for kw in keywords),
        return_exceptions=True,
    )
    rows_all: list[dict] = []
    errors: list[Exception] = []
    seen: set = set()
    for res in results:
        if isinstance(res, Exception):
            errors.append(res)
            continue
        for r in res:
            p = _project(r)
            key = (p["사업명"], p["연도"])
            if key in seen:
                continue
            seen.add(key)
            rows_all.append(p)
    if not rows_all and errors:
        raise errors[0]

    if year is not None:
        ys = str(year)
        rows_all = [p for p in rows_all if str(p.get("연도") or "") == ys]

    rows_all.sort(key=lambda p: (str(p.get("사업명") or ""), str(p.get("연도") or "")))
    return {
        "name": "재정사업 예산·집행",
        "keywords": keywords,
        "year": year,
        "count": len(rows_all),
        "projects": rows_all,
        "source": "openfiscaldata",
    }
