"""DART 전자공시 OpenAPI (opendart.fss.or.kr) 소스.

두 가지를 제공한다:
  1) 상장회사 인덱스: corpCode.xml(ZIP)을 받아 종목명↔종목코드↔DART 고유번호(corp_code)
     매핑을 메모리에 구축(24시간 캐시). search_stock_code의 백엔드.
  2) 공시 목록: list.json으로 corp_code의 최근 공시를 조회. 보유 종목의 리스크 신호
     (유상증자·감사의견·최대주주변경 등) 확인용.

corpCode.xml 주의(실측 필요하지만 문서화된 동작):
  - 정상 응답은 ZIP 바이너리(내부에 CORPCODE.xml 1개). 인증키 오류 시에는 ZIP이 아니라
    평문 XML 에러 봉투(<result><status>..</status>..)가 오므로 매직바이트 'PK'로 판별한다.
  - 전체 회사(비상장 포함) 목록이라 크다(수 MB). stock_code가 6자리인 상장사만 남긴다.

list.json status 코드: '000' 정상, '013' 조회 데이터 없음(에러 아님 → 빈 목록),
그 외('010' 등록되지 않은 키, '020' 사용한도 초과, '100' 필드 오류 등)는 예외로 올려
상위 _cascade가 fallback 처리한다(에러 봉투를 빈 결과로 위장하지 않는다 — fiscal.py와 동일 원칙).

인증키: DART_API_KEY (https://opendart.fss.or.kr 발급, 무료).
"""
from __future__ import annotations

import io
import os
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta

from core import http
from core.cache import cached
from core.schema import KST

_CORP_URL = "https://opendart.fss.or.kr/api/corpCode.xml"
_LIST_URL = "https://opendart.fss.or.kr/api/list.json"
_VIEWER_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"

_TTL_INDEX = 86400.0  # 상장사 목록은 하루 한 번이면 충분(최초 다운로드 수 MB)

_CORP_CLS = {"Y": "유가증권", "K": "코스닥", "N": "코넥스", "E": "기타"}


def _key() -> str:
    k = os.environ.get("DART_API_KEY")
    if not k:
        raise RuntimeError("DART_API_KEY not set (opendart.fss.or.kr에서 발급)")
    return k


async def _download_corp_index() -> dict:
    """corpCode ZIP 다운로드 → 상장사만 인덱스 구축."""
    raw = await http.get_bytes(_CORP_URL, params={"crtfc_key": _key()},
                               retries=1, timeout=30.0)
    if not raw.startswith(b"PK"):
        head = raw[:200].decode("utf-8", errors="replace")
        raise RuntimeError(f"corpCode 응답이 ZIP이 아님(인증키 오류 가능성): {head}")
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        xml_bytes = zf.read(zf.namelist()[0])  # CORPCODE.xml
    root = ET.fromstring(xml_bytes)

    items: list[dict] = []
    by_stock: dict[str, dict] = {}
    for el in root.iter("list"):
        stock = (el.findtext("stock_code") or "").strip()
        if len(stock) != 6:  # 비상장은 stock_code 공백
            continue
        item = {
            "name": (el.findtext("corp_name") or "").strip(),
            "stock_code": stock,
            "corp_code": (el.findtext("corp_code") or "").strip(),
        }
        items.append(item)
        by_stock[stock] = item
    if not items:
        raise RuntimeError("corpCode 파싱 결과 상장사 0건 — 응답 구조 변경 가능성")
    return {"items": items, "by_stock": by_stock}


async def corp_index() -> dict:
    return await cached("dart:corp_index", _download_corp_index, _TTL_INDEX)


async def search(name: str, limit: int = 10) -> dict:
    """종목명 부분일치 검색. 완전일치를 최우선 정렬."""
    q = name.strip()
    if not q:
        raise ValueError("검색어가 비었습니다")
    idx = await corp_index()
    exact = [it for it in idx["items"] if it["name"] == q]
    partial = [it for it in idx["items"] if q in it["name"] and it["name"] != q]
    found = (exact + sorted(partial, key=lambda it: len(it["name"])))[:limit]
    return {"query": name, "count": len(found), "items": found, "source": "dart"}


async def resolve_corp(query: str) -> dict:
    """종목명/6자리 종목코드/8자리 corp_code → {name, stock_code, corp_code}.

    모호하면 ValueError(후보 안내 포함) — region_codes.resolve_region과 동일 철학.
    """
    q = query.strip()
    if q.isdigit() and len(q) == 8:
        return {"name": None, "stock_code": None, "corp_code": q}
    idx = await corp_index()
    if q.isdigit() and len(q) == 6:
        item = idx["by_stock"].get(q)
        if not item:
            raise ValueError(f"상장 종목코드를 찾을 수 없습니다: '{q}'")
        return item
    exact = [it for it in idx["items"] if it["name"] == q]
    if len(exact) == 1:
        return exact[0]
    partial = exact or [it for it in idx["items"] if q in it["name"]]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        raise ValueError(f"종목을 찾을 수 없습니다: '{query}'. "
                         "search_stock_code로 먼저 검색하세요.")
    names = ", ".join(f"{it['name']}({it['stock_code']})" for it in partial[:5])
    raise ValueError(f"'{query}'와 일치하는 종목이 여러 개입니다: {names} … "
                     "종목코드(6자리)로 지정하세요.")


async def disclosures(query: str, days: int = 90, rows: int = 20) -> dict:
    """최근 N일 공시 목록. query는 종목명/6자리 종목코드/8자리 corp_code."""
    corp = await resolve_corp(query)
    end = datetime.now(tz=KST)
    bgn = end - timedelta(days=max(1, days))
    params = {
        "crtfc_key": _key(),
        "corp_code": corp["corp_code"],
        "bgn_de": bgn.strftime("%Y%m%d"),
        "end_de": end.strftime("%Y%m%d"),
        "page_no": "1",
        "page_count": str(min(max(1, rows), 100)),
    }
    data = await http.get_json(_LIST_URL, params=params)
    status = str(data.get("status", ""))
    if status == "013":  # 조회된 데이터 없음 — 정상 케이스
        rows_list: list[dict] = []
    elif status == "000":
        rows_list = [r for r in (data.get("list") or []) if isinstance(r, dict)]
    else:
        raise RuntimeError(f"dart error {status}: {data.get('message')}")

    items = [{
        "제목": r.get("report_nm"),
        "접수일": r.get("rcept_dt"),
        "제출인": r.get("flr_nm"),
        "시장": _CORP_CLS.get(r.get("corp_cls"), r.get("corp_cls")),
        "비고": r.get("rm") or None,
        "접수번호": r.get("rcept_no"),
        "url": _VIEWER_URL.format(rcept_no=r.get("rcept_no")),
    } for r in rows_list]
    return {
        "name": f"{corp.get('name') or query} 공시",
        "corp_code": corp["corp_code"],
        "stock_code": corp.get("stock_code"),
        "period": f"{params['bgn_de']}~{params['end_de']}",
        "total_count": data.get("total_count", len(items)),
        "count": len(items),
        "disclosures": items,
        "source": "dart",
    }
