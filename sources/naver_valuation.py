"""네이버 금융 밸류에이션 소스 — PER/PBR/EPS/BPS/배당수익률/시가총액.

sources/naver.py(실시간 시세 polling)와 관심사가 달라 분리: 여기는 재무지표라
갱신이 느리고 파싱 대상도 다르다.

소스 2단 강등(둘 다 키 불필요):
  1) 모바일 통합 API: https://m.stock.naver.com/api/stock/{code}/integration
     → totalInfos:[{code:"per",key:"PER",value:"12.35배"}, ...] (비공식 JSON.
       필드명은 실호출로 확정 필요 — code/key 양쪽 매칭으로 방어)
  2) PC 종목 페이지 정적 HTML: https://finance.naver.com/item/main.naver?code={code}
     → <em id="_per">12.35</em> 등 고정 id 요소를 정규식 추출(JS 렌더링 불필요,
       Playwright 불요). 시가총액(<em id="_market_sum">430조 4,613</em>)은 억원 단위.

값 표기가 '12.35배', '2.05%', '430조 4,613억' 등 문자열이라 단위 제거/한글 금액
파싱 헬퍼를 둔다. 핵심 지표(PER/PBR/시총)가 전부 None이면 파싱 실패로 간주하고
예외를 올려 다음 소스로 강등한다(빈 성공 응답 방지).
"""
from __future__ import annotations

import re

from core import http
from core.schema import now_kst_iso, to_float

_M_URL = "https://m.stock.naver.com/api/stock/{code}/integration"
_PC_URL = "https://finance.naver.com/item/main.naver"


def _num(v) -> float | None:
    """'12.35배', '2.05%', '69,900원' → 숫자만. 조/억 단위 금액은 _eok 사용."""
    if v is None:
        return None
    s = str(v)
    if "조" in s or "억" in s:
        return None  # 자릿수 붕괴 방지 — 금액은 _eok로
    s = re.sub(r"[^\d.\-]", "", s)
    if s in ("", "-", ".", "-."):
        return None
    return to_float(s)


def _eok(v) -> float | None:
    """'430조 4,613억', '4,613억원', '430조', '4613' → 억원 float.

    단위 없는 순수 숫자는 억원으로 간주(PC _market_sum이 억원 숫자만 출력).
    """
    if v is None:
        return None
    s = str(v).replace(",", "").replace("원", "").strip()
    m = re.match(r"^(?:(\d+(?:\.\d+)?)\s*조)?\s*(?:(\d+(?:\.\d+)?)\s*억?)?$", s)
    if not m or (m.group(1) is None and m.group(2) is None):
        return None
    jo = float(m.group(1) or 0)
    eok = float(m.group(2) or 0)
    return jo * 10000 + eok


def _build(code: str, name, market_cap_eok, per, pbr, eps, bps, dvr,
           close_price, source: str) -> dict:
    if market_cap_eok is None and per is None and pbr is None:
        raise RuntimeError(f"밸류에이션 파싱 실패({source}): 핵심 지표 없음")
    return {
        "code": code,
        "name": name,
        "market_cap_eok": market_cap_eok,   # 시가총액(억원)
        "per": per,
        "pbr": pbr,
        "eps": eps,
        "bps": bps,
        "dividend_yield_pct": dvr,
        "close_price": close_price,
        "currency": "KRW",
        "timestamp": now_kst_iso(),
        "source": source,
    }


# ------------------------------------------------------------- 1) 모바일 API


def _pick(infos: list, codes: tuple[str, ...] = (), key_subs: tuple[str, ...] = ()):
    """totalInfos에서 code 완전일치 또는 key 부분일치로 값 추출."""
    for it in infos:
        if not isinstance(it, dict):
            continue
        c = str(it.get("code") or "").lower()
        k = str(it.get("key") or "")
        if c in codes or any(s in k for s in key_subs):
            return it.get("value")
    return None


async def from_mobile_api(code: str) -> dict:
    data = await http.get_json(_M_URL.format(code=code))
    if not isinstance(data, dict):
        raise RuntimeError("naver mobile integration: 비정상 응답")
    infos = data.get("totalInfos") or []
    return _build(
        code,
        name=data.get("stockName") or code,
        market_cap_eok=_eok(_pick(infos, ("marketvalue", "marketsum"), ("시총", "시가총액"))),
        per=_num(_pick(infos, ("per",), ("PER",))),
        pbr=_num(_pick(infos, ("pbr",), ("PBR",))),
        eps=_num(_pick(infos, ("eps",), ("EPS",))),
        bps=_num(_pick(infos, ("bps",), ("BPS",))),
        dvr=_num(_pick(infos, ("dividendrate", "dividend", "divyield"), ("배당수익률",))),
        close_price=_num(data.get("closePrice")),
        source="naver_m",
    )


# ------------------------------------------------------- 2) PC 정적 HTML 폴백

_RE_TITLE = re.compile(r"<title>\s*([^:<]+?)\s*:")
_RE_MARKET_SUM = re.compile(r'id="_market_sum"[^>]*>\s*([^<]+?)\s*</em>', re.S)


def _em(html: str, em_id: str) -> float | None:
    m = re.search(rf'id="{em_id}"[^>]*>\s*([\d,.\-]+)', html)
    return to_float(m.group(1).replace(",", "")) if m else None


def parse_pc_html(code: str, html: str) -> dict:
    """PC 종목 페이지 HTML에서 지표 추출(테스트 용이하게 분리)."""
    name_m = _RE_TITLE.search(html)
    sum_m = _RE_MARKET_SUM.search(html)
    return _build(
        code,
        name=name_m.group(1).strip() if name_m else code,
        market_cap_eok=_eok(sum_m.group(1)) if sum_m else None,
        per=_em(html, "_per"),
        pbr=_em(html, "_pbr"),
        eps=_em(html, "_eps"),
        bps=_em(html, "_bps"),
        dvr=_em(html, "_dvr"),
        close_price=None,  # PC 메인 페이지 현재가는 구조가 복잡해 미추출(시세는 get_stock_price로)
        source="naver_pc",
    )


async def from_pc_html(code: str) -> dict:
    html = await http.get_text(_PC_URL, params={"code": code})
    return parse_pc_html(code, html)
