"""국토교통부 실거래가 공개시스템 (공공데이터포털 data.go.kr) 소스.

아파트 매매/전월세 실거래가를 조회한다. 응답은 XML(stdlib ElementTree로 파싱).

엔드포인트(공공데이터포털 1613000 국토교통부):
  매매  : https://apis.data.go.kr/1613000/RTMSDataSvcAptTradeDev/getRTMSDataSvcAptTradeDev
  전월세: https://apis.data.go.kr/1613000/RTMSDataSvcAptRent/getRTMSDataSvcAptRent
파라미터: serviceKey(인증키), LAWD_CD(5자리 시군구 법정동코드),
          DEAL_YMD(YYYYMM), pageNo, numOfRows.

인증키는 data.go.kr 활용신청 후 발급되는 **Decoding(일반) 키**를 MOLIT_API_KEY 환경변수로
주입한다(httpx가 자동 URL 인코딩하므로 Encoding 키를 쓰면 이중 인코딩으로 깨진다).

검증(2026-06-22):
- 전월세(apt_rent): 실데이터로 element명 확인(보증금/월세 정상 파싱).
- 매매(apt_trade): 공식 기술문서(docs/)와 _trade_item 필드 전부 대조 일치
  (aptNm/dealAmount/excluUseAr/floor/buildYear/umdNm/jibun/dealYear·Month·Day).
  실호출은 403(에러코드 20: 활용승인 안 됨)이라 매매 API 별도 활용신청 필요 —
  승인되면 코드 변경 없이 동작.

참고: data.go.kr WAF가 curl 기본 User-Agent를 차단하므로 core.http의 브라우저 UA
클라이언트로 호출해야 한다. 단일 소스이므로 실패 시 상위에서 fallback.
"""
from __future__ import annotations

import os
import xml.etree.ElementTree as ET

from core import http
from core.schema import to_float

_TRADE_URL = ("https://apis.data.go.kr/1613000/RTMSDataSvcAptTradeDev/"
              "getRTMSDataSvcAptTradeDev")
_RENT_URL = ("https://apis.data.go.kr/1613000/RTMSDataSvcAptRent/"
             "getRTMSDataSvcAptRent")


def _key() -> str:
    k = os.environ.get("MOLIT_API_KEY")
    if not k:
        raise RuntimeError("MOLIT_API_KEY not set")
    return k


def _t(item: ET.Element, tag: str) -> str | None:
    el = item.find(tag)
    if el is not None and el.text is not None:
        s = el.text.strip()
        return s or None
    return None


def _int(v: str | None) -> int | None:
    try:
        return int(v) if v not in (None, "") else None
    except ValueError:
        return None


def _date(item: ET.Element) -> str | None:
    y, m, d = _t(item, "dealYear"), _t(item, "dealMonth"), _t(item, "dealDay")
    if y and m and d:
        return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
    return None


def _check_header(root: ET.Element) -> None:
    # 정상 봉투: <response><header><resultCode>000</resultCode>...
    code = root.findtext(".//header/resultCode")
    if code not in (None, "000", "00"):
        msg = root.findtext(".//header/resultMsg")
        raise RuntimeError(f"MOLIT error {code}: {msg}")
    # 게이트웨이 에러 봉투: <OpenAPI_ServiceResponse><cmmMsgHeader><returnReasonCode>..
    reason = root.findtext(".//cmmMsgHeader/returnReasonCode")
    if reason is not None:
        msg = root.findtext(".//cmmMsgHeader/returnAuthMsg") or \
            root.findtext(".//cmmMsgHeader/errMsg")
        raise RuntimeError(f"MOLIT gateway error {reason}: {msg}")


def _trade_item(it: ET.Element) -> dict:
    return {
        "apt": _t(it, "aptNm"),
        "deal_amount": to_float((_t(it, "dealAmount") or "").replace(",", "")),  # 만원
        "area": to_float(_t(it, "excluUseAr")),  # 전용면적 m^2
        "floor": _int(_t(it, "floor")),
        "build_year": _int(_t(it, "buildYear")),
        "dong": _t(it, "umdNm"),
        "jibun": _t(it, "jibun"),
        "date": _date(it),
    }


def _rent_item(it: ET.Element) -> dict:
    return {
        "apt": _t(it, "aptNm"),
        "deposit": to_float((_t(it, "deposit") or "").replace(",", "")),       # 보증금 만원
        "monthly_rent": to_float((_t(it, "monthlyRent") or "").replace(",", "")),  # 월세 만원
        "area": to_float(_t(it, "excluUseAr")),
        "floor": _int(_t(it, "floor")),
        "build_year": _int(_t(it, "buildYear")),
        "dong": _t(it, "umdNm"),
        "jibun": _t(it, "jibun"),
        "date": _date(it),
    }


async def _fetch(url: str, name: str, region_code: str, deal_ym: str,
                 rows: int, parse_item) -> dict:
    params = {
        "serviceKey": _key(),
        "LAWD_CD": region_code,
        "DEAL_YMD": deal_ym,
        "pageNo": "1",
        "numOfRows": str(rows),
    }
    text = await http.get_text(url, params=params, retries=1)
    root = ET.fromstring(text)
    _check_header(root)
    items = [parse_item(it) for it in root.findall(".//item")]
    return {
        "name": name,
        "region_code": region_code,
        "deal_ym": deal_ym,
        "count": len(items),
        "items": items,
        "source": "molit",
    }


async def apt_trade(region_code: str, deal_ym: str, rows: int = 50) -> dict:
    """아파트 매매 실거래가. region_code=5자리 시군구코드, deal_ym='YYYYMM'."""
    return await _fetch(_TRADE_URL, "아파트매매실거래", region_code, deal_ym,
                        rows, _trade_item)


async def apt_rent(region_code: str, deal_ym: str, rows: int = 50) -> dict:
    """아파트 전월세 실거래가. 보증금/월세(만원). 월세 0이면 전세."""
    return await _fetch(_RENT_URL, "아파트전월세실거래", region_code, deal_ym,
                        rows, _rent_item)
