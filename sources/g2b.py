"""조달청 나라장터 입찰공고정보서비스 (공공데이터포털 data.go.kr) 소스.

철도/광역교통 '발주·착공' 신호를 잡는다. 공사 입찰공고가 뜨면 착공이 임박했다는 뜻.
응답은 JSON(type=json).

엔드포인트(공공데이터포털 1230000 조달청 입찰공고정보서비스, data.go.kr/data/15129394):
  공사 입찰공고: {BASE}/getBidPblancListInfoCnstwk
파라미터: serviceKey, type=json, numOfRows, pageNo,
          inqryDiv=1(공고게시일시 기준), inqryBgnDt/inqryEndDt(YYYYMMDDHHMM).

**중요(실측 확인, 2026-07)**: 이 API는 공고명/기관명 검색 파라미터(bidNtceNm·ntceInsttNm·
dminsttNm)를 **서버에서 무시**한다 — 날짜범위 내 공사공고를 전량 방출한다. 따라서 노선/
사업 키워드 필터는 **클라이언트에서** 공고명 부분일치로 수행한다. 또 조회기간에 최대 범위
제한(약 30일 미만; 20일 OK / 30일 초과 시 resultCode 07)이 있어 날짜를 청크로 나눠 조회한다.

인증키는 data.go.kr Decoding(일반) 키 — core.datago.data_go_key()로 molit과 공유한다.
**'조달청_나라장터 입찰공고정보서비스'를 별도 활용신청**해야 정상 응답이 온다(미신청 시
게이트웨이/403 에러). data.go.kr WAF 대응 브라우저 UA는 core.http가 처리.
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

# 조회기간 최대 범위 제한(실측: 20일 OK, 30일 초과 거부) → 여유 두고 15일씩 청크.
_MAX_SPAN_DAYS = 15
# 청크당 페이지네이션 상한(전량 방출 API라 폭주 방지). 999건×페이지.
_PAGE_SIZE = 999
_MAX_PAGES_PER_WINDOW = 8      # 청크당 최대 ~8000건 스캔
_FETCH_TIMEOUT = 20.0         # 대용량 페이지용(기본 5초로는 부족)


def _ymd_hm(dt: datetime) -> str:
    return dt.strftime("%Y%m%d%H%M")


def _check_error(payload) -> None:
    """게이트웨이/서비스 에러 봉투를 감지해 예외로 올린다(빈 결과로 위장 방지).

    나라장터는 정상 실패 시 {"nkoneps.com.response.ResponseError": {"header":{resultCode,
    resultMsg}}} 또는 data.go.kr 공통 {"OpenAPI_ServiceResponse": {...cmmMsgHeader...}}로
    응답한다. 이 구조를 header 검사 전에 먼저 잡는다.
    """
    if not isinstance(payload, dict):
        return
    for k, v in payload.items():
        if "ResponseError" in k or "ServiceResponse" in k:
            hdr = v.get("header", v) if isinstance(v, dict) else {}
            code = hdr.get("resultCode") or hdr.get("returnReasonCode")
            msg = hdr.get("resultMsg") or hdr.get("returnAuthMsg") or hdr.get("errMsg")
            raise RuntimeError(f"g2b error {code}: {msg}")


def _items(payload) -> tuple[list[dict], int]:
    """response.body.items를 (list[dict], totalCount)로 정규화.

    items가 dict({item:..})/list 모두 대응. 에러 봉투는 _check_error가 먼저 처리.
    """
    _check_error(payload)
    if not isinstance(payload, dict):
        raise RuntimeError(f"unexpected g2b payload type: {type(payload).__name__}")
    resp = payload.get("response", payload)
    header = resp.get("header", {}) if isinstance(resp, dict) else {}
    code = header.get("resultCode")
    if code not in (None, "00", "000"):
        raise RuntimeError(f"g2b error {code}: {header.get('resultMsg')}")
    body = resp.get("body", {}) if isinstance(resp, dict) else {}
    total = int(to_float(body.get("totalCount")) or 0)
    items = body.get("items")
    if items in (None, "", []):
        return [], total
    if isinstance(items, dict):       # {item: [...]} 또는 {item: {...}}
        items = items.get("item", [])
    if isinstance(items, dict):       # 단건이 dict로 올 때
        items = [items]
    return (items if isinstance(items, list) else []), total


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


def _windows(days: int) -> list[tuple[str, str]]:
    """오늘~직전 days일을 _MAX_SPAN_DAYS 이하 청크의 (bgn, end) 리스트로 분할."""
    now = datetime.now(tz=KST)
    start = now - timedelta(days=max(1, days))
    out: list[tuple[str, str]] = []
    cur = start
    while cur < now:
        nxt = min(cur + timedelta(days=_MAX_SPAN_DAYS), now)
        out.append((_ymd_hm(cur), _ymd_hm(nxt)))
        cur = nxt
    return out


async def _fetch_page(op: str, bgn: str, end: str, page: int) -> tuple[list[dict], int]:
    params = {
        "serviceKey": data_go_key(),
        "type": "json",
        "numOfRows": str(_PAGE_SIZE),
        "pageNo": str(page),
        "inqryDiv": "1",            # 공고게시일시 기준
        "inqryBgnDt": bgn,
        "inqryEndDt": end,
    }
    payload = await http.get_json(f"{_BASE}/{op}", params=params,
                                  retries=1, timeout=_FETCH_TIMEOUT)
    return _items(payload)


async def _fetch_window(op: str, bgn: str, end: str) -> tuple[list[dict], int, bool]:
    """한 날짜 청크의 공사공고를 수집. page1로 totalCount를 얻고 나머지는 병렬 조회.

    (items, total, truncated). 전량 방출 API라 청크당 페이지 상한을 둔다.
    """
    first, total = await _fetch_page(op, bgn, end, 1)
    all_items = list(first)
    if not first or len(all_items) >= total:
        return all_items, total, False

    import math
    need = math.ceil(total / _PAGE_SIZE)
    last = min(need, _MAX_PAGES_PER_WINDOW)
    truncated = need > _MAX_PAGES_PER_WINDOW
    rest = await asyncio.gather(
        *(_fetch_page(op, bgn, end, p) for p in range(2, last + 1)),
        return_exceptions=True,
    )
    for res in rest:
        if isinstance(res, Exception):
            continue
        items, _ = res
        all_items.extend(items)
    return all_items, total, truncated


def _matches(name: str | None, keywords: list[str]) -> bool:
    if not name:
        return False
    low = name.lower()
    return any(kw.lower() in low for kw in keywords)


def _agency_ok(it: dict, agencies: list[str] | None) -> bool:
    """agencies가 주어지면 발주기관/수요기관에 그 중 하나가 포함돼야 통과.

    숫자 노선명("9호선")이 도로 노선번호(국도79호선·소로2-9호선 등)에 부분일치로
    걸리는 노이즈를, 발주/수요기관으로 걸러낸다.
    """
    if not agencies:
        return True
    blob = f"{it.get('ntceInsttNm') or ''} {it.get('dminsttNm') or ''}"
    return any(a in blob for a in agencies)


async def search_bids(keywords: list[str], biz: str = "공사", days: int = 30,
                      rows: int = 50, agencies: list[str] | None = None) -> dict:
    """공사공고를 날짜범위로 전량 수집한 뒤 공고명 키워드로 **클라이언트 필터**.

    biz: '공사'(기본)/'용역'/'물품'. days: 오늘 기준 직전 N일(청크로 분할 조회).
    rows: 필터 후 반환 최대 건수. 서버가 키워드 필터를 무시하므로 여기서 부분일치로 거른다.
    agencies: 주어지면 공고명 매칭 + 발주/수요기관 매칭을 AND로 요구(노이즈 제거).
    일부 청크가 실패해도 가능한 것만 모으되, 전부 실패하면 예외를 올려 상위 fallback.
    """
    op = _BIZ_OP.get(biz, _OP_CNSTWK)
    windows = _windows(days)

    results = await asyncio.gather(
        *(_fetch_window(op, bgn, end) for bgn, end in windows),
        return_exceptions=True,
    )
    merged: dict[tuple, dict] = {}
    scanned = 0
    truncated = False
    errors: list[Exception] = []
    for res in results:
        if isinstance(res, Exception):
            errors.append(res)
            continue
        items, _total, trunc = res
        truncated = truncated or trunc
        scanned += len(items)
        for it in items:
            if _matches(it.get("bidNtceNm"), keywords) and _agency_ok(it, agencies):
                b = _bid(it)
                merged[(b["공고번호"], b["차수"])] = b
    if not merged and errors and scanned == 0:
        raise errors[0]

    bids = sorted(merged.values(), key=lambda b: (b.get("공고일") or ""), reverse=True)
    out = {
        "name": "나라장터 입찰공고",
        "biz": biz,
        "keywords": keywords,
        "agencies": agencies,
        "period": f"{windows[0][0]}~{windows[-1][1]}" if windows else "",
        "scanned": scanned,          # 필터 전 스캔한 공사공고 총건수
        "count": len(bids),
        "bids": bids[:rows],
        "source": "g2b",
    }
    if truncated:
        out["note"] = (f"스캔 상한(청크당 {_PAGE_SIZE*_MAX_PAGES_PER_WINDOW}건)에 걸려 "
                       "일부 기간이 잘렸을 수 있음. days를 줄이면 정확도가 오른다.")
    return out
