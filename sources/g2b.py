"""조달청 나라장터 입찰공고정보서비스 (공공데이터포털 data.go.kr) 소스.

철도/광역교통 '발주·착공' 신호를 잡는다. 공사 입찰공고가 뜨면 착공이 임박했다는 뜻.
응답은 JSON(type=json). 공고명(bidNtceNm) 부분일치로 노선/사업명을 검색한다.

엔드포인트(공공데이터포털 1230000 조달청 입찰공고정보서비스, data.go.kr/data/15129394):
  공사 입찰공고: {BASE}/getBidPblancListInfoCnstwk
파라미터: serviceKey, type=json, numOfRows, pageNo,
          inqryDiv=1(공고게시일시 기준), inqryBgnDt/inqryEndDt(YYYYMMDDHHMM),
          bidNtceNm(공고명 부분일치).

인증키는 data.go.kr Decoding(일반) 키 — core.datago.data_go_key()로 molit과 공유한다.
**'조달청_나라장터 입찰공고정보서비스'를 별도 활용신청**해야 정상 응답이 온다(미신청 시
게이트웨이 에러). data.go.kr WAF 대응 브라우저 UA는 core.http가 처리.

주의: 입찰공고정보서비스는 버전 세그먼트가 갱신될 수 있다(현재 'ad/BidPublicInfoService').
404/NODATA가 지속되면 data.go.kr 서비스 페이지에서 최신 엔드포인트로 _BASE를 갱신한다.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from core import http
from core.datago import data_go_key
from core.schema import to_float

KST = timezone(timedelta(hours=9))

_BASE = "https://apis.data.go.kr/1230000/ad/BidPublicInfoService"
_OP_CNSTWK = "getBidPblancListInfoCnstwk"   # 공사
_OP_SERVC = "getBidPblancListInfoServc"     # 용역(설계/감리 등)
_OP_THNG = "getBidPblancListInfoThng"       # 물품

_BIZ_OP = {"공사": _OP_CNSTWK, "용역": _OP_SERVC, "물품": _OP_THNG}


def _ymd_hm(dt: datetime) -> str:
    return dt.strftime("%Y%m%d%H%M")


def _items(payload) -> list[dict]:
    """response.body.items를 list[dict]로 정규화. items가 dict({item:..})/list 모두 대응."""
    if not isinstance(payload, dict):
        raise RuntimeError(f"unexpected g2b payload type: {type(payload).__name__}")
    resp = payload.get("response", payload)
    header = resp.get("header", {}) if isinstance(resp, dict) else {}
    code = header.get("resultCode")
    if code not in (None, "00", "000"):
        raise RuntimeError(f"g2b error {code}: {header.get('resultMsg')}")
    body = resp.get("body", {}) if isinstance(resp, dict) else {}
    items = body.get("items")
    if items in (None, "", []):
        return []
    if isinstance(items, dict):       # {item: [...]} 또는 {item: {...}}
        items = items.get("item", [])
    if isinstance(items, dict):       # 단건이 dict로 올 때
        items = [items]
    return items if isinstance(items, list) else []


def _bid(it: dict) -> dict:
    """입찰공고 1건을 정규화. 금액은 만원 아님 — 원 단위 그대로(추정가격/배정예산)."""
    return {
        "공고명": it.get("bidNtceNm"),
        "공고번호": it.get("bidNtceNo"),
        "차수": it.get("bidNtceOrd"),
        "공고일": it.get("bidNtceDt"),
        "입찰마감": it.get("bidClseDt"),
        "개찰일": it.get("opengDt"),
        "추정가격": to_float(it.get("presmptPrce")),       # 원
        "배정예산": to_float(it.get("asignBdgtAmt")),      # 원
        "발주기관": it.get("ntceInsttNm"),
        "수요기관": it.get("dminsttNm"),
        "지역제한": it.get("rgnLmtBidLocplcJdgmBssNm"),
        "url": it.get("bidNtceUrl"),
    }


async def _search_one(op: str, keyword: str, bgn: str, end: str, rows: int) -> list[dict]:
    params = {
        "serviceKey": data_go_key(),
        "type": "json",
        "numOfRows": str(rows),
        "pageNo": "1",
        "inqryDiv": "1",            # 공고게시일시 기준
        "inqryBgnDt": bgn,
        "inqryEndDt": end,
        "bidNtceNm": keyword,
    }
    payload = await http.get_json(f"{_BASE}/{op}", params=params, retries=1)
    return _items(payload)


async def search_bids(keywords: list[str], biz: str = "공사", days: int = 30,
                      rows: int = 50) -> dict:
    """공고명 키워드 리스트로 입찰공고 검색 → 병합·중복제거(공고번호+차수 기준).

    biz: '공사'(기본)/'용역'/'물품'. days: 오늘 기준 직전 N일. rows: 키워드당 최대 건수.
    일부 키워드 호출이 실패해도 가능한 것만 모으되, 전부 실패하면 예외를 올려 상위 fallback.
    """
    op = _BIZ_OP.get(biz, _OP_CNSTWK)
    now = datetime.now(tz=KST)
    bgn = _ymd_hm(now - timedelta(days=max(1, days)))
    end = _ymd_hm(now)

    results = await asyncio.gather(
        *(_search_one(op, kw, bgn, end, rows) for kw in keywords),
        return_exceptions=True,
    )
    merged: dict[tuple, dict] = {}
    errors: list[Exception] = []
    for res in results:
        if isinstance(res, Exception):
            errors.append(res)
            continue
        for it in res:
            b = _bid(it)
            merged[(b["공고번호"], b["차수"])] = b
    if not merged and errors:
        raise errors[0]

    bids = sorted(merged.values(), key=lambda b: (b.get("공고일") or ""), reverse=True)
    return {
        "name": "나라장터 입찰공고",
        "biz": biz,
        "keywords": keywords,
        "period": f"{bgn}~{end}",
        "count": len(bids),
        "bids": bids,
        "source": "g2b",
    }
