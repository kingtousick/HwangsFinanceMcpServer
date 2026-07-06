"""열린재정 재정정보공개시스템 (openfiscaldata.go.kr) OpenAPI 소스.

철도/광역교통 사업의 '예타·재정' 신호를 잡는다. 세부사업 예산이 잡히고 늘면 '진짜
돈이 가고 있다'는 뜻. 세부사업명(SACTV_NM) 키워드로 연도별 예산 편성 시계열을 조회한다.

--- 실측 확정(2026-07-06, 실호출로 확인) ------------------------------------------
대상 API: **세출/지출 예산편성현황(총지출)** = 경로형 `TotalExpenditure5`.
  요청주소: https://openapi.openfiscaldata.go.kr/TotalExpenditure5  (SERVICE= 쿼리 아님!)
  필수 인자: Key, Type, pIndex, pSize, FSCL_YY(회계연도, 1개씩), BDG_FND_DIV_CD(0전체/1예산
             /5기금), ANEXP_INQ_STND_CD(1총계/2세출순계).
  검색 인자: SACTV_NM(세부사업명, 부분일치 동작 확인) / ACTV_NM(단위사업) / PGM_NM(프로그램)
             / OFFC_NM(소관명).
  응답: {"TotalExpenditure5":[{head:[{list_total_count},{RESULT:{CODE:"INFO-000"..}}]},
         {row:[{FSCL_YY,OFFC_NM,PGM_NM,SACTV_NM,Y_YY_MEDI_KCUR_AMT(당해예산,천원),
                Y_YY_DFN_MEDI_KCUR_AMT(정부안,천원),Y_PREY_FIRST_KCUR_AMT(전년최초),..}]}]}
  **금액 단위는 천원**(예: 230103000 = 2,301억원). 각 연도엔 전년 마감분(예산 0) 중복행이
  섞여 나오므로 (사업명,연도)별 최대 예산액 행만 남긴다.
  FSCL_YY가 필수·단일이라 시계열은 연도별로 반복 호출한다.

API명/필수코드/검색 파라미터명은 환경변수로 덮어쓸 수 있다(기본값이 위 실측값):
  OPEN_FISCAL_API_NAME (기본 'TotalExpenditure5'), OPEN_FISCAL_KW_PARAM (기본 'SACTV_NM'),
  OPEN_FISCAL_BDG_FND_DIV_CD (기본 '1'), OPEN_FISCAL_ANEXP_INQ_STND_CD (기본 '1').
인증키는 OPEN_FISCAL_API_KEY(미설정 시 상위 fallback). 미설정이어도 샘플키로 pSize 5 동작.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime

from core import http
from core.schema import to_float

_BASE_ROOT = "https://openapi.openfiscaldata.go.kr"
_DEFAULT_API_NAME = "TotalExpenditure5"   # 세출/지출 예산편성현황(총지출)
_DEFAULT_KW_PARAM = "SACTV_NM"            # 세부사업명(부분일치)
_DEFAULT_BDG_FND = "1"                    # 0:전체 1:예산 5:기금
_DEFAULT_ANEXP = "1"                      # 1:총계 2:세출순계

# 최근 몇 개 회계연도를 기본으로 훑을지(FSCL_YY가 필수·단일이라 연도별 반복).
_DEFAULT_SPAN_YEARS = 5

# 출력 레코드 필드명(실측). Y_YY_MEDI=당해연도 예산(국회확정 현액),
# Y_YY_DFN=정부안, Y_PREY_FIRST=전년도 최초. 단위 천원.
_F_NAME = ("SACTV_NM", "ACTV_NM", "PGM_NM", "사업명")
_F_YEAR = ("FSCL_YY", "FY", "회계연도")
_F_BUDGET = ("Y_YY_MEDI_KCUR_AMT", "Y_YY_DFN_MEDI_KCUR_AMT", "예산액")
_F_GOVT = ("Y_YY_DFN_MEDI_KCUR_AMT", "정부안")
_F_PREV = ("Y_PREY_FIRST_KCUR_AMT", "전년최초")
_F_PGM = ("PGM_NM", "프로그램명")
_F_MINISTRY = ("OFFC_NM", "소관명", "부처명")


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

    Type=json이어도 본문을 JSON 문자열로 이중 인코딩해 보내는 경우가 있어(str이면)
    한 번 더 json.loads로 풀어준다.
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
    # 표준 구조: {SERVICE: [ {head:[{list_total_count},{RESULT:..}]}, {row:[..]} ]}
    for v in payload.values():
        if isinstance(v, list):
            for block in v:
                if isinstance(block, dict):
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


def _to_eok(thousand_won) -> float | None:
    """천원 단위 금액 → 억원(소수1자리). 1억원 = 100,000천원."""
    v = to_float(thousand_won)
    if v is None:
        return None
    return round(v / 100000.0, 1)


def _project(r: dict) -> dict:
    budget = to_float(_first(r, _F_BUDGET))
    return {
        "사업명": _first(r, _F_NAME),
        "연도": _first(r, _F_YEAR),
        "예산액_억원": _to_eok(_first(r, _F_BUDGET)),
        "예산액_천원": budget,
        "정부안_억원": _to_eok(_first(r, _F_GOVT)),
        "프로그램": _first(r, _F_PGM),
        "부처": _first(r, _F_MINISTRY),
    }


def _default_years() -> list[int]:
    y = datetime.now().year
    return list(range(y - _DEFAULT_SPAN_YEARS + 1, y + 1))


async def _search_one(term: str, year: int, rows: int) -> list[dict]:
    """세부사업명 term으로 특정 연도 예산 조회. term이 '='로 시작하면 정확일치 필터.

    서버 SACTV_NM 검색은 부분일치라 'GTX-A' 원사업명 '수도권광역급행철도'가 B/C노선
    (…B노선/…C노선)까지 끌어온다. '=수도권광역급행철도'처럼 '='를 붙이면 서버엔 값만
    보내고 클라이언트에서 사업명 완전일치 행만 남겨 오염을 제거한다.
    """
    exact = term.startswith("=")
    q = term[1:] if exact else term
    api = os.environ.get("OPEN_FISCAL_API_NAME", _DEFAULT_API_NAME)
    kw_param = os.environ.get("OPEN_FISCAL_KW_PARAM", _DEFAULT_KW_PARAM)
    params = {
        "Key": _key(),   # 미설정 시 RuntimeError → 상위 _cascade가 fallback
        "Type": "json",
        "pIndex": "1",
        "pSize": str(rows),
        "FSCL_YY": str(year),
        "BDG_FND_DIV_CD": os.environ.get(
            "OPEN_FISCAL_BDG_FND_DIV_CD", _DEFAULT_BDG_FND),
        "ANEXP_INQ_STND_CD": os.environ.get(
            "OPEN_FISCAL_ANEXP_INQ_STND_CD", _DEFAULT_ANEXP),
        kw_param: q,
    }
    url = f"{_BASE_ROOT}/{api}"
    payload = await http.get_json(url, params=params, retries=1, timeout=20.0)
    out = _extract_rows(payload)
    if exact:
        out = [r for r in out if (_first(r, _F_NAME) or "") == q]
    return out


async def search_budget(keywords: list[str], year: int | None = None,
                        rows: int = 100) -> dict:
    """세부사업명 키워드로 연도별 예산 편성 시계열 조회 → 병합.

    year 지정 시 그 연도만, 미지정 시 최근 _DEFAULT_SPAN_YEARS개 회계연도를 훑는다.
    (사업명,연도)별로 예산액 최대 행만 남겨 전년 마감분(0원) 중복행을 제거한다.
    일부 (키워드×연도) 실패는 건너뛰고, 전부 실패 시 예외를 올려 상위 fallback.
    """
    years = [year] if year is not None else _default_years()
    tasks = [(_search_one(kw, y, rows)) for kw in keywords for y in years]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    best: dict[tuple, dict] = {}   # (사업명,연도) → 예산액 최대 행
    errors: list[Exception] = []
    for res in results:
        if isinstance(res, Exception):
            errors.append(res)
            continue
        for r in res:
            p = _project(r)
            key = (p["사업명"], str(p["연도"]))
            prev = best.get(key)
            cur_amt = p.get("예산액_천원") or 0
            if prev is None or cur_amt > (prev.get("예산액_천원") or 0):
                best[key] = p
    if not best and errors:
        raise errors[0]

    rows_all = list(best.values())
    rows_all.sort(key=lambda p: (str(p.get("사업명") or ""), str(p.get("연도") or "")))
    return {
        "name": "재정사업 예산편성(세부사업, 천원→억원)",
        "keywords": keywords,
        "year": year,
        "years": years,
        "unit": "억원(원자료 천원)",
        "count": len(rows_all),
        "projects": rows_all,
        "source": "openfiscaldata:TotalExpenditure5",
    }
