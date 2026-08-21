"""K-apt(공동주택관리정보시스템) 단지 기본정보 소스 — 세대수/동수/사용승인일.

실거래가 API(sources/molit.py)는 단지 규모를 전혀 주지 않는다. 세대수를 알아야
거래건수 정규화(회전율)나 대단지 필터가 가능하므로 별도 소스로 결합한다.

엔드포인트(공공데이터포털 1613000 국토교통부):
  단지목록: https://apis.data.go.kr/1613000/AptListService3/getSigunguAptList3
            (sigunguCode=5자리 시군구코드 → kaptCode/kaptName/as1~as4/bjdCode)
  기본정보: https://apis.data.go.kr/1613000/AptBasisInfoServiceV4/getAphusBassInfoV4
            (kaptCode → kaptdaCnt=세대수, kaptDongCnt=동수, kaptUsedate=사용승인일 등)

실거래 API가 시군구 5자리(LAWD_CD)를 쓰므로 단지목록도 시군구 단위로 받아
region_codes를 그대로 재사용한다(10자리 법정동코드 테이블 불필요).

인증키는 core.datago.data_go_key()(= DATA_GO_KR_API_KEY 또는 MOLIT_API_KEY)를 쓰지만
**두 서비스 모두 data.go.kr에서 별도 활용신청**해야 200이 떨어진다(미신청 시 403,
returnReasonCode 30). README '부동산 실거래가 사용법' 참고.

검증(2026-08-21): 두 경로 모두 실호출로 존재 확인(활용 미신청 상태라 reasonCode 30).
응답 포맷은 포털 명세상 JSON이지만 data.go.kr 게이트웨이는 서비스에 따라 XML을
돌려주므로 첫 글자로 판별해 양쪽 다 파싱한다.

캐시: 세대수/동수/사용승인일은 준공 후 사실상 불변이라 디스크 캐시 30일.
단지 기본정보는 단지당 1콜이라 집계 결과 전체를 매번 조회하면 호출이 폭주한다.
그래서 attach_households()는 **캐시에 있는 단지는 공짜로 채우고, 미캐시 단지는
호출 상한(_MAX_BASIS_FETCH)까지만** 새로 받는다(반복 호출하면 점진적으로 채워짐).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import xml.etree.ElementTree as ET
from collections import Counter

from core import diskcache, http
from core.datago import data_go_key
from core.ratelimit import RateLimiter
from core.schema import scrub_secrets, to_float

logger = logging.getLogger("finance-mcp")

_LIST_URL = ("https://apis.data.go.kr/1613000/AptListService3/"
             "getSigunguAptList3")
_BASIS_URL = ("https://apis.data.go.kr/1613000/AptBasisInfoServiceV4/"
              "getAphusBassInfoV4")

TTL_LIST = 30 * 86400.0    # 단지목록: 신규 준공 반영 주기로 충분
TTL_BASIS = 30 * 86400.0   # 기본정보: 세대수는 불변

_LIST_ROWS = 1000          # 단지목록 페이지 크기
_MAX_LIST_PAGES = 10       # 시군구 최대 1만 단지(무한 루프 방지)
# 1회 집계에서 새로 받을 단지 기본정보 수 상한. 강남구 6개월치(327단지)에서 매칭되는
# 단지가 150개 내외라, 첫 호출에 대부분을 채우고 이후는 캐시로 즉시 응답한다.
_MAX_BASIS_FETCH = 150

# 단지 기본정보를 다건 조회할 때 게이트웨이를 때리지 않도록 초당 8건으로 분산
# (data.go.kr 개발계정 제한은 일 트래픽이고 초당 제한은 명시돼 있지 않다).
_LIMITER = RateLimiter(8.0)


# ------------------------------------------------------------- 응답 파싱


def _gateway_error(code, msg) -> RuntimeError:
    return RuntimeError(f"KAPT gateway error {code}: {msg}")


def _parse_json_items(text: str) -> list[dict]:
    obj = json.loads(text)
    if not isinstance(obj, dict):
        return []
    gw = (obj.get("OpenAPI_ServiceResponse") or {}).get("cmmMsgHeader")
    if isinstance(gw, dict):
        raise _gateway_error(gw.get("returnReasonCode"),
                             gw.get("returnAuthMsg") or gw.get("errMsg"))
    resp = obj.get("response") or obj
    header = resp.get("header") or {}
    code = str(header.get("resultCode", "")).strip()
    if code and code not in ("00", "000"):
        raise RuntimeError(f"KAPT error {code}: {header.get('resultMsg')}")
    body = resp.get("body") or {}
    item = body.get("item")
    if item is None:
        items = body.get("items")
        item = items.get("item") if isinstance(items, dict) else items
    if item is None:
        return []
    rows = item if isinstance(item, list) else [item]
    return [r for r in rows if isinstance(r, dict)]


def _parse_xml_items(text: str) -> list[dict]:
    root = ET.fromstring(text)
    reason = root.findtext(".//cmmMsgHeader/returnReasonCode")
    if reason is not None:
        raise _gateway_error(reason,
                             root.findtext(".//cmmMsgHeader/returnAuthMsg")
                             or root.findtext(".//cmmMsgHeader/errMsg"))
    code = root.findtext(".//header/resultCode")
    if code not in (None, "00", "000"):
        raise RuntimeError(f"KAPT error {code}: {root.findtext('.//header/resultMsg')}")
    out: list[dict] = []
    for it in root.findall(".//item"):
        out.append({ch.tag: (ch.text or "").strip() for ch in it})
    return out


async def _get_items(url: str, params: dict) -> list[dict]:
    """공통 GET. JSON/XML 어느 쪽이 와도 item 목록(dict 리스트)으로 돌려준다."""
    p = {"serviceKey": data_go_key(), "_type": "json", **params}
    text = await http.get_text(url, params=p, retries=1, limiter=_LIMITER)
    return (_parse_json_items(text) if text.lstrip().startswith("{")
            else _parse_xml_items(text))


# ------------------------------------------------------------- 값 변환


def _int(v) -> int | None:
    """'1,004' → 1004. 숫자로 못 읽으면 None."""
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _date8(v) -> str | None:
    """사용승인일 '19970825' → '1997-08-25'. 형식이 다르면 원문 그대로."""
    s = (str(v).strip() if v is not None else "")
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return s


_PAREN = re.compile(r"\(([^)]*)\)")
# 괄호 안이 차수 표기('1단지', '제2차')일 때만 살린다.
_PAREN_ORDINAL = re.compile(r"^\s*제?\s*\d+\s*(?:차|단지)\s*$")
# 이름 뒤에 붙은 동 번호 나열: '203,204,205,206동', '61~64동', '1동,2동,3동'
_DONG_LIST = re.compile(r"\d+\s*동?(?:\s*[~\-,·]\s*\d+\s*동?)*\s*동")
_NON_NAME = re.compile(r"[^0-9A-Za-z가-힣]+")
_JE_NUM = re.compile(r"제(\d+)")
# 'N차'/'N단지'를 'N'으로 통일 — 실거래 '개포우성2' ↔ K-apt '개포우성2차'
_ORDINAL = re.compile(r"(\d+)(?:차|단지)")


def _paren_sub(m: re.Match) -> str:
    inner = m.group(1)
    return inner if _PAREN_ORDINAL.match(inner) else ""


def norm_name(name: str | None) -> str:
    """단지명 정규화. 실거래 aptNm과 K-apt kaptName의 표기 차이를 흡수한다.

    실데이터(강남구 2026-04)에서 확인된 표기 차이를 순서대로 제거한다:
      1) 괄호 — '한양1차(영동한양)'·'현대14차(203,204동)'처럼 별칭/동번호가 들어간다.
         단 '(1단지)'류 차수 표기는 단지를 구분하는 정보라 내용을 살린다.
      2) 동 번호 나열 — '대치우성아파트1동,2동,3동', '선경1차(1동-7동)'
      3) 공백·구두점 → 제거, '제N' → 'N'
      4) 'N차'/'N단지' → 'N' — 실거래는 '개포우성2', K-apt는 '개포우성2차'로 적는다
      5) 접미 '아파트' 제거

    예) '래미안 대치팰리스(1단지)' / '래미안대치팰리스제1단지아파트' → '래미안대치팰리스1'

    ※ 차수 '숫자'까지 지우지는 않는다. '개포주공1/2/3단지'가 한 덩어리로 뭉개져
      서로 다른 단지에 남의 세대수가 붙는 편이 결측보다 나쁘다.
    """
    if not name:
        return ""
    s = _PAREN.sub(_paren_sub, str(name))
    s = _DONG_LIST.sub("", s)
    s = _NON_NAME.sub("", s)
    s = _JE_NUM.sub(r"\1", s)
    s = _ORDINAL.sub(r"\1", s)
    if s.endswith("아파트") and len(s) > 3:
        s = s[:-3]
    return s.upper()


# ------------------------------------------------------------- 단지목록


async def sigungu_complexes(region_code: str) -> list[dict]:
    """시군구(5자리) 내 K-apt 등록 단지 목록. 디스크 캐시 30일.

    반환 item: {kapt_code, name, sido, sigungu, dong, bjd_code}.
    """
    key = f"kapt:list:{region_code}"
    hit = await diskcache.get(key, TTL_LIST)
    if hit is not None:
        return hit.get("items") or []

    items: list[dict] = []
    for page in range(1, _MAX_LIST_PAGES + 1):
        rows = await _get_items(_LIST_URL, {
            "sigunguCode": region_code,
            "pageNo": str(page),
            "numOfRows": str(_LIST_ROWS),
        })
        items.extend({
            "kapt_code": r.get("kaptCode"),
            "name": r.get("kaptName"),
            "sido": r.get("as1"),
            "sigungu": r.get("as2"),
            "dong": r.get("as3"),
            "bjd_code": r.get("bjdCode"),
        } for r in rows if r.get("kaptCode"))
        if len(rows) < _LIST_ROWS:
            break
    await diskcache.put(key, {"items": items})
    return items


# ------------------------------------------------------------- 기본정보


async def basis_info(kapt_code: str, *, cached_only: bool = False) -> dict | None:
    """단지코드로 기본정보 조회. 디스크 캐시 30일.

    cached_only=True면 캐시에 없을 때 네트워크를 타지 않고 None을 돌려준다
    (집계 보강에서 호출 상한을 지키기 위한 용도).
    """
    key = f"kapt:basis:{kapt_code}"
    hit = await diskcache.get(key, TTL_BASIS)
    if hit is not None:
        return hit
    if cached_only:
        return None

    rows = await _get_items(_BASIS_URL, {"kaptCode": kapt_code})
    if not rows:
        return None
    r = rows[0]
    out = {
        "kapt_code": r.get("kaptCode") or kapt_code,
        "name": r.get("kaptName"),
        "households": _int(r.get("kaptdaCnt")),      # 세대수
        "dong_count": _int(r.get("kaptDongCnt")),    # 동수
        "ho_count": _int(r.get("hoCnt")),            # 호수
        "use_date": _date8(r.get("kaptUsedate")),    # 사용승인일
        "top_floor": _int(r.get("kaptTopFloor")),    # 최고층수
        "total_area": to_float(r.get("kaptTarea")),  # 건축물대장상 연면적(㎡)
        "priv_area": to_float(r.get("privArea")),    # 단지 전용면적합(㎡)
        "sale_type": r.get("codeSaleNm"),            # 분양형태
        "heat_type": r.get("codeHeatNm"),            # 난방방식
        "hall_type": r.get("codeHallNm"),            # 복도유형(계단식/복도식)
        "builder": r.get("kaptBcompany"),            # 시공사
        "addr": r.get("kaptAddr"),
        "road_addr": r.get("doroJuso"),
        "bjd_code": r.get("bjdCode"),
        # 전용면적 구간별 '세대수'. 실데이터로 확인함 — 은마(4,424세대)가
        # 60~85: 24 + 85~135: 4,400 = 4,424로 kaptdaCnt와 정확히 일치(2026-08-21).
        "area_band": {
            "~60": to_float(r.get("kaptMparea60")),
            "60~85": to_float(r.get("kaptMparea85")),
            "85~135": to_float(r.get("kaptMparea135")),
            "135~": to_float(r.get("kaptMparea136")),
        },
        "source": "kapt",
    }
    await diskcache.put(key, out)
    return out


# ------------------------------------------------------------- 매칭/보강


def _build_index(complexes: list[dict]) -> dict[str, dict[str, list[dict]]]:
    """(정규화 동명) → (정규화 단지명) → [단지...] 2단 인덱스.

    같은 동 안에서만 매칭해 동명이 단지(예: 여러 구의 '푸르지오')의 오매칭을 막는다.
    동을 못 찾을 때를 대비해 빈 문자열 키('')에 시군구 전체를 함께 담는다.
    """
    idx: dict[str, dict[str, list[dict]]] = {"": {}}
    for c in complexes:
        for dong_key in ("", norm_name(c.get("dong"))):
            bucket = idx.setdefault(dong_key, {})
            bucket.setdefault(norm_name(c.get("name")), []).append(c)
    return idx


def match_complex(idx: dict, dong: str | None, apt: str | None) -> dict | None:
    """실거래 (법정동, 단지명)에 대응하는 K-apt 단지. 애매하면 None.

    1) 정규화 완전일치 → 유일하면 채택
    2) 실패 시 부분일치(포함 관계)가 **유일할 때만** 채택. K-apt는 단지명 앞에 동을
       붙이는 일이 많아(실거래 '미성2차' ↔ K-apt '압구정미성2차') 부분일치가 꼭 필요하다.
       '래미안대치팰리스'가 1단지/2단지 둘 다에 걸리면 포기 — 오매칭보다 결측이 낫다.

    탐색 범위는 **dong이 주어지면 그 동으로 한정**한다. 시군구 전체로 넓히면 부분일치가
    다른 동의 동명이 단지를 물어온다(실측: '현대1'(대치동) → '개포현대1차'(개포동),
    '진흥아파트'(삼성동) → '청담삼성진흥'(청담동)). 동을 모를 때만 전체에서 찾는다.
    """
    target = norm_name(apt)
    if not target:
        return None
    for dong_key in ([norm_name(dong)] if dong else [""]):
        bucket = idx.get(dong_key)
        if not bucket:
            continue
        exact = bucket.get(target)
        if exact and len(exact) == 1:
            return exact[0]
        if exact:
            continue  # 동일 이름이 여러 개면 특정 불가
        partial = [c for name, rows in bucket.items()
                   if name and (target in name or name in target)
                   for c in rows]
        if len(partial) == 1:
            return partial[0]
    return None


def _turnover(count: int | None, households: int | None) -> float | None:
    """기간 내 거래건수 ÷ 세대수 × 100(%). 단지 규모로 정규화한 손바뀜 비율.

    월 단위로는 보통 0.1~2% 범위라 소수 3자리까지 남긴다(2자리면 대단지가 뭉갠다).
    """
    if not households or count is None:
        return None
    return round(count / households * 100, 3)


async def attach_households(region_code: str, summary: dict, *,
                            max_fetch: int = _MAX_BASIS_FETCH) -> dict:
    """apt_trade_summary 결과의 items에 세대수/회전율을 채워 넣는다(in-place).

    세대수는 부가 지표이므로 **실패해도 예외를 올리지 않는다**(활용 미신청·장애 시
    실거래 집계는 그대로 반환). 채워지는 키:
      households, dong_count, use_date, turnover_rate(%)
      households_shared : 한 K-apt 단지에 실거래 단지 여러 개가 매칭됐을 때만 True
                          (통합 등록 단지 — 회전율이 실제보다 낮게 나온다)
    요약에 추가되는 키:
      households_matched : 세대수를 채운 단지 수
      households_pending : 이름은 매칭됐지만 호출 상한에 걸려 못 받은 단지 수
                           (>0이면 같은 조회를 한 번 더 하면 채워진다)
      households_source  : 'kapt' / 실패 시 None
    """
    items = summary.get("items") or []
    if not items:
        return summary
    try:
        complexes = await sigungu_complexes(region_code)
    except Exception as e:  # noqa: BLE001 - 부가 지표라 조용히 강등
        # 예외 문자열에 요청 URL(serviceKey 포함)이 실리므로 마스킹 후 기록.
        logger.warning("kapt list failed region=%s: %s", region_code,
                       scrub_secrets(e))
        summary["households_matched"] = 0
        summary["households_pending"] = 0
        summary["households_source"] = None
        return summary

    idx = _build_index(complexes)
    matched = [(it, m) for it in items
               if (m := match_complex(idx, it.get("dong"), it.get("apt")))]

    # 1차: 캐시에 있는 단지만 공짜로 채운다.
    todo: list[tuple[dict, dict]] = []
    for it, m in matched:
        info = await basis_info(m["kapt_code"], cached_only=True)
        if info is None:
            todo.append((it, m))
        else:
            _apply(it, info)

    # 2차: 미캐시 단지는 상한까지만 새로 받는다(나머지는 다음 호출에서 채워짐).
    pending = max(0, len(todo) - max(0, max_fetch))
    todo = todo[:max(0, max_fetch)]
    if todo:
        infos = await asyncio.gather(
            *(basis_info(m["kapt_code"]) for _, m in todo),
            return_exceptions=True,
        )
        for (it, _), info in zip(todo, infos):
            if isinstance(info, dict):
                _apply(it, info)
            elif isinstance(info, Exception):
                logger.warning("kapt basis failed: %s", scrub_secrets(info))

    # K-apt가 여러 차수를 한 단지로 묶어 등록한 경우(예: 'LG선릉에클라트(A)'와 '(B)'가
    # 모두 '선릉에클라트' 하나에 매칭) 세대수는 통합값이라 차수별 회전율이 과소평가된다.
    # 값을 버리진 않되 그대로 믿지 않도록 표시한다.
    shared = {code for code, n in
              Counter(m["kapt_code"] for _, m in matched).items() if n > 1}
    for it, m in matched:
        if it.get("households") and m["kapt_code"] in shared:
            it["households_shared"] = True

    summary["households_matched"] = sum(1 for it in items if it.get("households"))
    summary["households_pending"] = pending
    summary["households_source"] = "kapt"
    return summary


def _apply(item: dict, info: dict) -> None:
    """집계 item 한 건에 단지 기본정보를 반영."""
    item["households"] = info.get("households")
    item["dong_count"] = info.get("dong_count")
    item["use_date"] = info.get("use_date")
    item["turnover_rate"] = _turnover(item.get("count"), info.get("households"))


async def complex_info(region_code: str, name: str) -> dict:
    """단지명으로 K-apt 기본정보 조회(세대수 단독 조회용).

    이름이 애매하면 후보 목록을 candidates로 돌려준다.
    """
    complexes = await sigungu_complexes(region_code)
    idx = _build_index(complexes)
    hit = match_complex(idx, None, name)
    if hit is None:
        target = norm_name(name)
        candidates = [c["name"] for c in complexes
                      if target and target in norm_name(c.get("name"))][:20]
        return {
            "name": "공동주택 단지 기본정보",
            "region_code": region_code,
            "query": name,
            "matched": None,
            "candidates": candidates,
            "source": "kapt",
        }
    info = await basis_info(hit["kapt_code"])
    return {
        "name": "공동주택 단지 기본정보",
        "region_code": region_code,
        "query": name,
        "matched": {**(info or {}), "dong": hit.get("dong")},
        "candidates": [],
        "source": "kapt",
    }
