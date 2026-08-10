"""Yahoo quoteSummary 기반 해외(미국) 종목 밸류에이션.

yfinance를 쓰지 않는다 — 자체 requests 세션을 만들어 core/http.py의 truststore
SSL 컨텍스트(사내망 TLS 가로채기 대응)를 우회하고, 동기 블로킹이라 stdio
이벤트루프를 막으며, respx로 모킹할 수 없어 테스트 인프라가 이원화된다.

인증 절차(실측):
  1) GET https://fc.yahoo.com/          → 404가 정상. 목적은 Set-Cookie(A1/A3)뿐.
     쿠키 도메인이 .yahoo.com이라 전역 AsyncClient의 jar에 담겨 이후 query2
     요청에 자동 첨부된다(수동 헤더 조작 불필요).
  2) GET https://query1.finance.yahoo.com/v1/test/getcrumb
     → 본문이 crumb 문자열 그 자체(JSON 아님).
  3) GET https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}
         ?modules=...&crumb={crumb}

이 모듈의 진입점 valuation()은 **예외를 던지지 않는다**(부분성공 dict).
quoteSummary가 막히면 chart v8 meta로 강등해 이름·통화·52주 고저만 채우고
펀더멘털은 null + errors[]로 남긴다.
"""
from __future__ import annotations

import logging
import time
import urllib.parse

from core import http
from core.schema import (INTRADAY, PREV_CLOSE, err_item, now_kst_iso, to_float)
from sources import yahoo

logger = logging.getLogger("finance-mcp")

_COOKIE_URL = "https://fc.yahoo.com/"
_CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"
_QS_URL = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
_MODULES = "price,summaryDetail,defaultKeyStatistics,financialData"

_CRUMB_TTL = 1800.0
# (획득시각, crumb, 획득에 쓴 클라이언트 id)
_crumb_cache: tuple[float, str, int] | None = None


def reset() -> None:
    """테스트용: crumb 캐시 비우기."""
    global _crumb_cache
    _crumb_cache = None


async def _crumb(force: bool = False) -> str:
    """crumb 문자열. 클라이언트가 교체되면(쿠키 jar 소실) 자동 재획득한다."""
    global _crumb_cache
    client = http.get_client()
    if (not force and _crumb_cache
            and _crumb_cache[2] == id(client)
            and time.time() - _crumb_cache[0] < _CRUMB_TTL):
        return _crumb_cache[1]

    # 404/500이 정상 — 본문이 아니라 Set-Cookie만 쓴다.
    await http.get_response(_COOKIE_URL, retries=1, timeout=10.0,
                            status_ok=(400, 401, 403, 404, 500, 502, 503))
    c = (await http.get_text(_CRUMB_URL, retries=2, backoff=0.5, timeout=10.0)).strip()
    if not c or "<" in c or len(c) > 64:
        raise RuntimeError(f"yahoo crumb 획득 실패(응답: {c[:40]!r})")
    _crumb_cache = (time.time(), c, id(client))
    return c


async def quote_summary(symbol: str) -> dict:
    """quoteSummary 원본 result dict. 실패 시 raise."""
    enc = urllib.parse.quote(symbol, safe="")
    url = _QS_URL.format(symbol=enc)
    last_exc: Exception | None = None
    for force in (False, True):   # 401/403이면 crumb 강제 재획득 후 1회만 재시도
        try:
            data = await http.get_json(
                url, params={"modules": _MODULES, "crumb": await _crumb(force)},
                retries=2, backoff=0.5, timeout=10.0)
            results = ((data or {}).get("quoteSummary") or {}).get("result") or []
            if not results:
                raise RuntimeError(f"quoteSummary 결과 없음: {symbol}")
            return results[0]
        except Exception as e:  # noqa: BLE001
            last_exc = e
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (401, 403) and not force:
                continue          # crumb 만료 → 재획득 후 재시도
            break
    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------- 파싱 헬퍼

def _raw(node, key) -> float | None:
    """야후 수치 필드 → float.

    {'raw':1.23,'fmt':'1.23'} / {} / 스칼라 세 형태가 섞여 온다. 값이 없으면
    키 자체가 빈 dict로 오는 경우가 많아 None으로 정규화한다.
    """
    if not isinstance(node, dict):
        return None
    v = node.get(key)
    if isinstance(v, dict):
        return to_float(v.get("raw"))
    return to_float(v)


def _str(node, *keys) -> str | None:
    if not isinstance(node, dict):
        return None
    for k in keys:
        v = node.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _pct(node, key) -> float | None:
    """비율 필드(0.147) → 퍼센트(14.7)."""
    v = _raw(node, key)
    return None if v is None else round(v * 100, 4)


def _fmt_pct(node, key) -> float | None:
    """fmt가 '0.44%' 형태면 그 숫자를 취한다. 아니면 None."""
    if not isinstance(node, dict):
        return None
    v = node.get(key)
    if isinstance(v, dict):
        f = v.get("fmt")
        if isinstance(f, str) and f.strip().endswith("%"):
            return to_float(f.strip()[:-1])
    return None


def _dividend_yield_pct(sd: dict, price: float | None) -> float | None:
    """배당수익률(%).

    야후는 시기에 따라 dividendYield를 비율(0.0044)로도, 퍼센트(0.44)로도 준다.
    fmt('0.44%')가 있으면 그것을 신뢰하고, 없으면 연간 배당금/주가로 검산해
    3배 이상 어긋나면 검산값을 채택한다(추정이 아니라 같은 소스의 다른 필드).
    """
    from_fmt = _fmt_pct(sd, "dividendYield")
    if from_fmt is not None:
        return round(from_fmt, 4)

    raw = _raw(sd, "dividendYield")
    rate = _raw(sd, "trailingAnnualDividendRate")
    check = (rate / price * 100) if (rate and price) else None
    if raw is None:
        return round(check, 4) if check is not None else None
    # raw가 비율인지 퍼센트인지 검산값으로 판별
    for cand in (raw * 100, raw):
        if check is None or (check and 1 / 3 <= cand / check <= 3):
            return round(cand, 4)
    return round(check, 4)


def _market_state_kind(price_node: dict) -> str:
    """marketState → data_kind. 장중이면 지연 시세, 그 외는 전일/직전 종가."""
    state = _str(price_node, "marketState") or ""
    return INTRADAY if state.upper() == "REGULAR" else PREV_CLOSE


def _skeleton(symbol: str) -> dict:
    return {
        "symbol": symbol, "name": None, "currency": None, "exchange": None,
        "market_cap": None, "enterprise_value": None,
        "trailing_pe": None, "forward_pe": None, "pbr": None, "psr": None,
        "peg": None, "ev_ebitda": None,
        "roe_pct": None, "gross_margin_pct": None,
        "operating_margin_pct": None, "net_margin_pct": None,
        "dividend_yield_pct": None,
        "eps_ttm": None, "revenue_ttm": None, "fcf_ttm": None,
        "price": None, "week52_high": None, "week52_low": None,
        "pct_from_52wk_high": None,
        "timestamp": now_kst_iso(), "source": None,
        "data_kind": PREV_CLOSE, "errors": [],
    }


def parse_quote_summary(symbol: str, result: dict) -> dict:
    """quoteSummary result → 정규화 dict. 순수 함수(테스트 용이).

    적자 기업은 trailingPE/pegRatio 키가 빈 dict로 오므로 null이 된다(정상).
    """
    out = _skeleton(symbol)
    pr = result.get("price") or {}
    sd = result.get("summaryDetail") or {}
    ks = result.get("defaultKeyStatistics") or {}
    fd = result.get("financialData") or {}

    out["symbol"] = _str(pr, "symbol") or symbol
    out["name"] = _str(pr, "longName", "shortName")
    out["currency"] = _str(pr, "currency") or _str(sd, "currency")
    out["exchange"] = _str(pr, "exchangeName", "fullExchangeName")

    out["market_cap"] = _raw(pr, "marketCap") or _raw(sd, "marketCap")
    out["enterprise_value"] = _raw(ks, "enterpriseValue")

    out["trailing_pe"] = _raw(sd, "trailingPE")
    out["forward_pe"] = _raw(sd, "forwardPE") or _raw(ks, "forwardPE")
    out["pbr"] = _raw(ks, "priceToBook")
    out["psr"] = _raw(sd, "priceToSalesTrailing12Months")
    out["peg"] = _raw(ks, "pegRatio")
    if out["peg"] is None:
        out["peg"] = _raw(ks, "trailingPegRatio")
    out["ev_ebitda"] = _raw(ks, "enterpriseToEbitda")

    out["roe_pct"] = _pct(fd, "returnOnEquity")
    out["gross_margin_pct"] = _pct(fd, "grossMargins")
    out["operating_margin_pct"] = _pct(fd, "operatingMargins")
    out["net_margin_pct"] = _pct(fd, "profitMargins")
    if out["net_margin_pct"] is None:
        out["net_margin_pct"] = _pct(ks, "profitMargins")

    out["eps_ttm"] = _raw(ks, "trailingEps")
    out["revenue_ttm"] = _raw(fd, "totalRevenue")
    out["fcf_ttm"] = _raw(fd, "freeCashflow")

    price = _raw(fd, "currentPrice") or _raw(pr, "regularMarketPrice")
    out["price"] = price
    out["dividend_yield_pct"] = _dividend_yield_pct(sd, price)

    hi = _raw(sd, "fiftyTwoWeekHigh") or _raw(ks, "fiftyTwoWeekHigh")
    lo = _raw(sd, "fiftyTwoWeekLow") or _raw(ks, "fiftyTwoWeekLow")
    out["week52_high"], out["week52_low"] = hi, lo
    if price is not None and hi:
        out["pct_from_52wk_high"] = round((price / hi - 1) * 100, 4)

    out["data_kind"] = _market_state_kind(pr)
    out["source"] = "yahoo_quote_summary"

    # 모듈 단위 결손만 errors로 남긴다(개별 지표 결손은 null로 충분).
    for key, node, fields in (("financialData", fd, "roe/마진/매출/FCF"),
                              ("defaultKeyStatistics", ks, "PBR/PEG/EV")):
        if not node:
            out["errors"].append(
                err_item(key, f"quoteSummary 모듈 미제공({fields})", "yahoo"))
    return out


def parse_chart_meta(symbol: str, meta: dict, reason) -> dict:
    """chart v8 meta만으로 만든 부분 응답(펀더멘털 없음)."""
    out = _skeleton(symbol)
    out["symbol"] = meta.get("symbol") or symbol
    out["name"] = meta.get("longName") or meta.get("shortName") or symbol
    out["currency"] = meta.get("currency")
    out["exchange"] = meta.get("exchangeName") or meta.get("fullExchangeName")
    price = to_float(meta.get("regularMarketPrice"))
    hi = to_float(meta.get("fiftyTwoWeekHigh"))
    lo = to_float(meta.get("fiftyTwoWeekLow"))
    out["price"], out["week52_high"], out["week52_low"] = price, hi, lo
    if price is not None and hi:
        out["pct_from_52wk_high"] = round((price / hi - 1) * 100, 4)
    out["source"] = "yahoo_chart(partial)"
    out["errors"].append(err_item(
        "fundamentals", f"quoteSummary 조회 실패로 밸류에이션 지표 없음: {reason}",
        "yahoo_quote_summary"))
    return out


async def valuation(symbol: str) -> dict:
    """해외 종목 밸류에이션. 예외를 던지지 않고 부분성공 dict를 반환한다."""
    sym = (symbol or "").strip().upper()
    if not sym:
        out = _skeleton(symbol or "")
        out["errors"].append(err_item("symbol", "티커가 비어 있습니다", "input"))
        return out

    qs_exc: Exception | None = None
    try:
        return parse_quote_summary(sym, await quote_summary(sym))
    except Exception as e:  # noqa: BLE001 - chart v8로 강등
        # except 블록을 벗어나면 as 변수는 삭제되므로 따로 보관한다.
        qs_exc = e
        logger.warning("quoteSummary failed for %s: %s", sym, e)

    try:
        return parse_chart_meta(sym, await yahoo.get_meta(sym), qs_exc)
    except Exception as chart_exc:  # noqa: BLE001 - 전부 실패
        out = _skeleton(sym)
        out["source"] = "yahoo"
        out["errors"].append(err_item(
            "fundamentals", qs_exc, "yahoo_quote_summary"))
        out["errors"].append(err_item(
            "price", f"{chart_exc} (심볼이 잘못됐거나 상장폐지일 수 있음)", "yahoo_chart"))
        return out
