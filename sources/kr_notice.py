"""국가철도공단 관보고시 (공공데이터포털 data.go.kr **파일데이터**) 소스.

철도/광역교통 사업의 '고시·인허가' 신호를 잡는다. 기본계획/실시계획 고시가 관보에
실리면 사업이 법적으로 확정된 것. 사업명/노선 키워드로 고시 레코드를 필터링한다.

**중요**: 이 데이터는 파일데이터지만, 공공데이터포털이 3단계+ 파일데이터를 **odcloud.kr
오픈API(REST JSON)로 자동변환**해 제공한다(data.go.kr/data/15114027). 따라서 실제 호출은
  https://api.odcloud.kr/api/15114027/v1/uddi:<uuid>?serviceKey=<KEY>&page=1&perPage=..
형식이며 응답은 {"data":[...], "totalCount":.., ...}. serviceKey는 data.go.kr Decoding
키(core.datago)로 코드가 붙인다 — 환경변수엔 **엔드포인트 URL만**(uddi 포함) 넣으면 된다.
실시간이 아니라 주기 갱신 스냅샷이므로 캐시 TTL을 길게(6시간) 가져간다.

data.go.kr 데이터셋(국가철도공단 관보고시 기본정보 15114027 등):
  - 국가철도공단_관보고시 기본정보   (kind='기본')
  - 국가철도공단_기본계획 고시       (kind='계획')
  - 국가철도공단_관보고시 세목정보   (kind='세목')

--- needs-verification ---------------------------------------------------------
데이터셋의 'OpenAPI/미리보기' 탭에서 보이는 **odcloud.kr 엔드포인트 URL(uddi 포함)**을
환경변수로 주입한다(키 없이 URL만 — serviceKey는 코드가 첨부):
  KRNA_NOTICE_URL_BASIC  : 관보고시 기본정보 엔드포인트 URL  (kind='기본')
  KRNA_NOTICE_URL_PLAN   : 기본계획 고시 엔드포인트 URL      (kind='계획')
  KRNA_NOTICE_URL_DETAIL : 관보고시 세목정보 엔드포인트 URL  (kind='세목')
"""
from __future__ import annotations

import os

from core import http
from core.datago import data_go_key

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


def _records(data) -> list[dict]:
    """응답(JSON dict/list)을 레코드 리스트로. {data:[...]}/{records:[...]}/평면 list 대응.

    문자열이 오면(파일 직접 다운로드 등) json.loads로 먼저 파싱.
    """
    if isinstance(data, str):
        import json
        data = json.loads(data)
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
    url = _url(kind)
    # odcloud.kr 자동변환 API는 serviceKey + page/perPage 파라미터가 필요.
    if "odcloud.kr" in url or "/api/" in url:
        payload = await http.get_json(url, params={
            "serviceKey": data_go_key(), "page": "1", "perPage": "1000",
        }, retries=1)
    else:
        payload = await http.get_text(url, retries=1)   # 원시 파일 URL 직접 다운로드
    records = _records(payload)
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
