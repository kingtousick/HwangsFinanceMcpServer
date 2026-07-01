"""국가철도공단 관보고시 (공공데이터포털 data.go.kr **파일데이터**) 소스.

철도/광역교통 사업의 '고시·인허가' 신호를 잡는다. 기본계획/실시계획 고시가 관보에
실리면 사업이 법적으로 확정된 것. 사업명/노선 키워드로 고시 레코드를 필터링한다.

**중요**: 이 데이터는 OpenAPI가 아니라 파일데이터(CSV/JSON/XML)다. data.go.kr의 파일
다운로드 URL에서 JSON 본문을 받아 메모리에서 키워드 필터한다. 실시간 API가 아니라
기관이 주기적으로 갱신하는 스냅샷이므로 캐시 TTL을 길게(6시간) 가져간다.

data.go.kr 데이터셋(국가철도공단 1611000 계열):
  - 국가철도공단_관보고시 기본정보   (kind='기본')
  - 국가철도공단_기본계획 고시       (kind='계획')
  - 국가철도공단_관보고시 세목정보   (kind='세목')

--- needs-verification ---------------------------------------------------------
파일데이터의 직접 다운로드 URL은 데이터셋 페이지에서 확정해야 한다(고정 URL이 갱신될 수
있음). 코드 변경 없이 환경변수로 주입한다(JSON 형식 URL 권장):
  KRNA_NOTICE_URL_BASIC  : 관보고시 기본정보 JSON 다운로드 URL  (kind='기본')
  KRNA_NOTICE_URL_PLAN   : 기본계획 고시 JSON 다운로드 URL      (kind='계획')
  KRNA_NOTICE_URL_DETAIL : 관보고시 세목정보 JSON 다운로드 URL  (kind='세목')
"""
from __future__ import annotations

import json
import os

from core import http

_KIND_ENV = {
    "기본": "KRNA_NOTICE_URL_BASIC",
    "계획": "KRNA_NOTICE_URL_PLAN",
    "세목": "KRNA_NOTICE_URL_DETAIL",
}

# 레코드 필드명 후보(파일 컬럼 표기 차이 흡수).
_F_TITLE = ("고시명", "고시제목", "NOTICE_NM", "GOSI_NM", "TITLE")
_F_NO = ("고시번호", "관보고시번호", "NOTICE_NO", "GOSI_NO")
_F_DATE = ("고시일", "고시일자", "관보게재일", "NOTICE_DE", "GOSI_DT", "PUBLIC_DE")
_F_PROJECT = ("사업명", "노선명", "PROJECT_NM", "BIZ_NM", "LINE_NM")
_F_TYPE = ("고시구분", "고시종류", "NOTICE_SE", "GOSI_SE", "구분")


def _url(kind: str) -> str:
    env = _KIND_ENV.get(kind, _KIND_ENV["기본"])
    u = os.environ.get(env)
    if not u:
        raise RuntimeError(f"{env} not set (관보고시 파일데이터 다운로드 URL 미설정)")
    return u


def _first(d: dict, keys: tuple):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _records(text: str) -> list[dict]:
    """파일 본문(JSON)을 레코드 리스트로. {records:[...]}/{data:[...]}/평면 list 대응."""
    data = json.loads(text)
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for k in ("records", "data", "rows", "items", "row"):
            v = data.get(k)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
        # 단일 리스트 값을 가진 dict
        for v in data.values():
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
    return []


def _notice(r: dict) -> dict:
    return {
        "고시명": _first(r, _F_TITLE),
        "고시번호": _first(r, _F_NO),
        "고시일": _first(r, _F_DATE),
        "사업명": _first(r, _F_PROJECT),
        "종류": _first(r, _F_TYPE),
    }


def _matches(r: dict, keywords: list[str]) -> bool:
    blob = " ".join(str(v) for v in r.values() if v is not None)
    return any(kw in blob for kw in keywords)


async def search_notices(keywords: list[str], kind: str = "기본") -> dict:
    """관보고시 파일을 받아 키워드(노선/사업명)로 필터링.

    kind: '기본'(관보고시 기본정보)/'계획'(기본계획 고시)/'세목'(세목정보).
    """
    text = await http.get_text(_url(kind), retries=1)
    records = _records(text)
    hits = [_notice(r) for r in records if _matches(r, keywords)]
    hits.sort(key=lambda n: str(n.get("고시일") or ""), reverse=True)
    return {
        "name": "국가철도공단 관보고시",
        "kind": kind,
        "keywords": keywords,
        "total_records": len(records),
        "count": len(hits),
        "notices": hits,
        "source": "krna_notice",
    }
