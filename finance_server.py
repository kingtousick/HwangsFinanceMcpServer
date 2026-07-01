"""Finance MCP Server — 로컬 실행 금융 시세 조회 (FastMCP, stdio).

소스 우선순위(환경 적응형 강등):
  국내 지수/종목/ETF : 네이버 polling → KRX MDC → Playwright
  미국 주식/지수      : Yahoo chart(query1 → query2)
  USD/KRW 환율        : Yahoo(KRW=X) → EXIM(키) → 네이버
  크립토 KRW/USD      : CoinGecko → 업비트(KRW, 도달 시)

모든 Tool은 정규화 dict(§5)를 반환하며, 전 소스 실패 시 {error, source:"fallback"}.
로그는 stderr만 사용(stdout은 MCP 전용).
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Awaitable, Callable

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from core.cache import cached
from core.schema import fail
from sources import (naver, yahoo, coingecko, upbit, exim, playwright_fb, molit,
                     g2b, fiscal, kr_notice, kr_progress)
from sources.region_codes import resolve_region
from sources.rail_lines import resolve_line

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,  # stdout 오염 금지
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("finance-mcp")

mcp = FastMCP("finance")


async def _cascade(name: str, *fetchers: Callable[[], Awaitable[dict]]) -> dict:
    """소스 순서대로 시도, 첫 성공을 반환. 전부 실패 시 fail()."""
    last_exc: Exception | None = None
    for fetch in fetchers:
        try:
            return await fetch()
        except Exception as e:  # noqa: BLE001 - 다음 소스로 강등
            last_exc = e
            logger.warning("source failed for %s: %s", name, e)
    return fail(name, last_exc or "all sources failed")


# ---------------------------------------------------------------- 국내 지수


@mcp.tool()
async def get_kospi() -> dict:
    """KOSPI 지수 실시간(정규화). 1순위 네이버 polling, 실패 시 Playwright 강등."""
    async def fetch():
        return await _cascade(
            "KOSPI",
            lambda: naver.get_index("KOSPI"),
            lambda: playwright_fb.get_index("KOSPI"),
        )
    return await cached("kospi", fetch)


@mcp.tool()
async def get_kosdaq() -> dict:
    """KOSDAQ 지수 실시간(정규화). 1순위 네이버 polling."""
    async def fetch():
        return await _cascade(
            "KOSDAQ",
            lambda: naver.get_index("KOSDAQ"),
            lambda: playwright_fb.get_index("KOSDAQ"),
        )
    return await cached("kosdaq", fetch)


# ---------------------------------------------------------------- 환율


@mcp.tool()
async def get_exchange_rate(pair: str = "USD/KRW") -> dict:
    """환율 조회. 예: 'USD/KRW'. 1순위 Yahoo(KRW=X), EXIM(키)·네이버 폴백.

    현재 Yahoo 'KRW=X'(USD/KRW)만 1급 지원. 다른 통화쌍은 Yahoo 심볼 규칙을 따른다.
    """
    base, _, quote = pair.partition("/")
    base = base.upper() or "USD"

    if base == "USD":
        symbol = "KRW=X"
    else:
        symbol = f"{base}KRW=X"

    async def fetch():
        return await _cascade(
            pair,
            lambda: yahoo.get_quote(symbol, name=pair),
            lambda: exim.get_rate(base),
        )
    return await cached(f"fx:{pair}", fetch)


# ---------------------------------------------------------------- 주식/지수


@mcp.tool()
async def get_stock_price(ticker: str) -> dict:
    """국내/해외 주식·지수 시세.

    티커 형식:
      - 국내 6자리 코드(예: '005930') → 네이버
      - Yahoo 심볼(예: '^GSPC', '^IXIC', '^SOX', 'AAPL') → Yahoo
    """
    is_domestic = ticker.isdigit() and len(ticker) == 6

    async def fetch():
        if is_domestic:
            return await _cascade(ticker, lambda: naver.get_stock(ticker))
        return await _cascade(ticker, lambda: yahoo.get_quote(ticker))
    return await cached(f"stock:{ticker}", fetch)


@mcp.tool()
async def get_etf_price(code: str) -> dict:
    """KRX ETF 시세. code는 6자리 코드(예: '381180' TIGER 미국필라델피아반도체나스닥).

    1순위 네이버 polling(국내 종목과 동일 엔드포인트).
    """
    async def fetch():
        return await _cascade(code, lambda: naver.get_stock(code))
    return await cached(f"etf:{code}", fetch)


# ---------------------------------------------------------------- 크립토


@mcp.tool()
async def get_crypto(symbol: str = "BTC", quote: str = "KRW") -> dict:
    """크립토 시세. 1순위 CoinGecko(KRW·USD 직접), KRW은 업비트 폴백(도달 시).

    symbol 예: 'BTC', 'ETH'. quote: 'KRW' 또는 'USD'.
    """
    q = quote.upper()

    async def fetch():
        fetchers = [lambda: coingecko.get_price(symbol, q)]
        if q == "KRW":
            fetchers.append(lambda: upbit.get_price(symbol))
        return await _cascade(symbol.upper(), *fetchers)
    return await cached(f"crypto:{symbol.upper()}:{q}", fetch)


# ---------------------------------------------------------------- 부동산 실거래가


def _normalize_ym(deal_ym: str) -> str:
    """'2026-04', '2026.04', '202604' → '202604'."""
    return deal_ym.replace("-", "").replace(".", "").replace("/", "").strip()


@mcp.tool()
async def get_apt_trade(region: str, deal_ym: str, rows: int = 50) -> dict:
    """아파트 매매 실거래가(국토교통부 공공데이터포털). MOLIT_API_KEY 필요.

    region: 지역명 또는 5자리 시군구 법정동코드. 자동 변환 지원 —
            '강남구', '서울 강남구', '수원 영통구', '세종', '11680' 모두 가능.
            모호하면(예: '중구') 시도를 함께 지정('서울 중구').
    deal_ym: 계약 년월. 'YYYYMM'/'YYYY-MM' 모두 허용(예: '202406', '2024-06').
    반환: {name, region_code, deal_ym, count, items:[{apt, deal_amount(만원),
          area(전용㎡), pyeong(전용 평수), price_per_pyeong(전용 평당가 만원/평),
          floor, build_year, dong, jibun, date}], source}.
    평당가는 전용면적 기준(공급면적 기준 시장 평당가보다 높게 나옴).
    """
    try:
        code = resolve_region(region)
    except ValueError as e:
        return fail(f"아파트매매:{region}", e)
    ym = _normalize_ym(deal_ym)

    async def fetch():
        return await _cascade(
            f"아파트매매:{code}:{ym}",
            lambda: molit.apt_trade(code, ym, rows),
        )
    return await cached(f"apt_trade:{code}:{ym}:{rows}", fetch)


@mcp.tool()
async def get_apt_trade_summary(region: str, deal_ym: str, months: int = 1,
                                rows: int = 1000) -> dict:
    """아파트 매매 실거래가를 단지별 평균 평당가로 집계. MOLIT_API_KEY 필요.

    region: 지역명 또는 5자리 코드(get_apt_trade와 동일). deal_ym: 기준월 'YYYYMM'/'YYYY-MM'.
    months: 기준월 포함 직전 N개월을 합산(기본 1, 최대 12). 거래가 적은 지역/단지의
            평균을 안정적으로 내려면 months=3~6 사용.
    거래를 (법정동, 단지)별로 묶어 평균 평당가 내림차순으로 반환.
    반환: {name, region_code, deal_ym, months, period, complex_count, deal_count,
          items:[{apt, dong, count, avg_price_per_pyeong, min_price_per_pyeong,
                  max_price_per_pyeong, avg_deal_amount, avg_pyeong}], source}.
    평당가는 전용면적 기준.
    """
    try:
        code = resolve_region(region)
    except ValueError as e:
        return fail(f"단지별평당가:{region}", e)
    ym = _normalize_ym(deal_ym)

    async def fetch():
        return await _cascade(
            f"단지별평당가:{code}:{ym}:{months}",
            lambda: molit.apt_trade_summary(code, ym, rows, months),
        )
    return await cached(f"apt_trade_summary:{code}:{ym}:{months}:{rows}", fetch)


@mcp.tool()
async def get_apt_rent(region: str, deal_ym: str, rows: int = 50) -> dict:
    """아파트 전월세 실거래가(국토교통부 공공데이터포털). MOLIT_API_KEY 필요.

    region: 지역명 또는 5자리 코드(자동 변환, get_apt_trade와 동일).
    deal_ym: 'YYYYMM'/'YYYY-MM'.
    반환 items: {apt, deposit(보증금 만원), monthly_rent(월세 만원, 0이면 전세),
                area(전용㎡), pyeong(전용 평수), deposit_per_pyeong(전용 보증금 평당가
                만원/평; 월세는 보증금만 반영), floor, build_year, dong, jibun, date}.
    """
    try:
        code = resolve_region(region)
    except ValueError as e:
        return fail(f"아파트전월세:{region}", e)
    ym = _normalize_ym(deal_ym)

    async def fetch():
        return await _cascade(
            f"아파트전월세:{code}:{ym}",
            lambda: molit.apt_rent(code, ym, rows),
        )
    return await cached(f"apt_rent:{code}:{ym}:{rows}", fetch)


@mcp.tool()
async def get_jeonse_ratio(region: str, deal_ym: str, months: int = 1,
                           rows: int = 1000) -> dict:
    """단지별 전세가율 집계(전세가율 = 전세 보증금 평당가 ÷ 매매 평당가 × 100).
    MOLIT_API_KEY 필요(매매·전월세 API 둘 다 활용신청 필요).

    region: 지역명 또는 5자리 코드. deal_ym: 기준월 'YYYYMM'/'YYYY-MM'.
    months: 매칭 표본을 늘리려면 기준월 포함 직전 N개월을 합산(기본 1, 최대 12).
            단일 월은 매매·전세가 같은 달에 모두 난 단지만 매칭돼 표본이 적으므로
            months=3~6을 쓰면 매칭 단지가 늘어난다.
    매매·전세(월세 제외)가 모두 있는 단지만 산출, 전세가율 내림차순.
    반환: {name, region_code, deal_ym, months, period, matched_complex_count,
          avg_jeonse_ratio, items:[{apt, dong, jeonse_ratio(%), sale_price_per_pyeong,
          jeonse_deposit_per_pyeong, jeonse_count}], source}.
    """
    try:
        code = resolve_region(region)
    except ValueError as e:
        return fail(f"전세가율:{region}", e)
    ym = _normalize_ym(deal_ym)

    async def fetch():
        return await _cascade(
            f"전세가율:{code}:{ym}:{months}",
            lambda: molit.jeonse_ratio_summary(code, ym, rows, months),
        )
    return await cached(f"jeonse_ratio:{code}:{ym}:{months}:{rows}", fetch)


# ------------------------------------------------ 공사현황(철도/광역교통)

# 변화가 느린 데이터라 TTL을 길게: 입찰 30분, 고시/공정률 6시간, 예산 1일.
_TTL_BIDS = 1800.0
_TTL_NOTICE = 21600.0
_TTL_PROGRESS = 21600.0
_TTL_BUDGET = 86400.0


@mcp.tool()
async def get_construction_bids(query: str, biz: str = "공사", days: int = 30,
                                rows: int = 50) -> dict:
    """철도/광역교통 발주·착공 신호 — 조달청 나라장터 입찰공고.
    DATA_GO_KR_API_KEY(또는 MOLIT_API_KEY) 필요 + '나라장터 입찰공고정보서비스' 활용신청.

    query: 노선 프리셋 별칭('GTX-A', '신안산선', '7호선 청라연장' 등) 또는 자유 키워드
           (공고명 부분일치). 프리셋이면 여러 표기를 함께 검색해 누락을 줄인다.
    biz: '공사'(기본)/'용역'(설계·감리)/'물품'. days: 직전 N일(기본 30). rows: 키워드당 건수.
    반환: {name, biz, keywords, period, count, bids:[{공고명, 공고번호, 차수, 공고일,
          입찰마감, 개찰일, 추정가격(원), 배정예산(원), 발주기관, 수요기관, 지역제한, url}],
          source}. 입찰공고가 뜨면 착공이 임박했다는 1차 신호.
    """
    line = resolve_line(query)

    async def fetch():
        return await _cascade(
            f"입찰공고:{line['line']}",
            lambda: g2b.search_bids(line["keywords"], biz, days, rows),
        )
    return await cached(f"bids:{line['line']}:{biz}:{days}:{rows}", fetch, _TTL_BIDS)


@mcp.tool()
async def get_project_budget(query: str, year: int | None = None,
                             rows: int = 100) -> dict:
    """철도/광역교통 예타·재정 신호 — 열린재정 재정사업 예산·집행 시계열.
    OPEN_FISCAL_API_KEY 필요(openfiscaldata.go.kr 발급).

    query: 노선 프리셋 별칭 또는 사업명 키워드. year: 특정 회계연도 필터(미지정 시 전체).
    반환: {name, keywords, year, count, projects:[{사업명, 연도, 예산액, 집행액, 부처}],
          source}. 예산이 잡히고 집행이 늘면 '진짜 돈이 가는' 신호.
    주의: 열린재정 API명/검색 파라미터는 환경변수(OPEN_FISCAL_API_NAME/OPEN_FISCAL_KW_PARAM)로
          실제 값에 맞춰야 한다(sources/fiscal.py 참고).
    """
    line = resolve_line(query)

    async def fetch():
        return await _cascade(
            f"재정사업:{line['line']}",
            lambda: fiscal.search_budget(line["keywords"], year, rows),
        )
    return await cached(f"budget:{line['line']}:{year}:{rows}", fetch, _TTL_BUDGET)


@mcp.tool()
async def get_rail_notices(query: str, kind: str = "기본") -> dict:
    """철도 고시·인허가 신호 — 국가철도공단 관보고시(공공데이터포털 파일데이터).
    파일 다운로드 URL 환경변수 필요(KRNA_NOTICE_URL_BASIC 등, sources/kr_notice.py 참고).

    query: 노선 프리셋 별칭 또는 사업명 키워드. kind: '기본'(관보고시 기본정보)/
           '계획'(기본계획 고시)/'세목'(관보고시 세목정보).
    반환: {name, kind, keywords, total_records, count, notices:[{고시명, 고시번호,
          고시일, 사업명, 종류}], source}. 기본계획/실시계획 고시는 법적 확정 신호.
    """
    line = resolve_line(query)

    async def fetch():
        return await _cascade(
            f"관보고시:{line['line']}",
            lambda: kr_notice.search_notices(line["keywords"], kind),
        )
    return await cached(f"notice:{line['line']}:{kind}", fetch, _TTL_NOTICE)


@mcp.tool()
async def get_rail_progress(query: str) -> dict:
    """철도 진행현황·공정률 — 국가철도공단 주요사업현황(HTML 스크래핑, Playwright 필요).

    query: 노선 프리셋 별칭 또는 사업명 키워드. 프리셋이면 광역/일반 구분 페이지를 좁혀
           조회한다. 공식 API가 없어 HTML을 렌더링·정규식 추출하므로 **불안정**하다
           (페이지 구조 변경/Playwright 미설치 시 error 반환).
    반환: {name, keywords, count, progress:[{사업명(컨텍스트 추정), 공정률_pct, 기준월}],
          source, note}.
    """
    line = resolve_line(query)

    async def fetch():
        return await _cascade(
            f"공정률:{line['line']}",
            lambda: kr_progress.get_progress(line["keywords"], line.get("kric_m")),
        )
    return await cached(f"progress:{line['line']}", fetch, _TTL_PROGRESS)


@mcp.tool()
async def get_rail_project_status(query: str) -> dict:
    """한 노선/사업의 공사현황 통합 스냅샷 — 예산·발주·고시·공정률을 병렬 조회.

    query: 노선 프리셋 별칭('GTX-A' 등) 또는 사업명 키워드.
    예산(열린재정)·발주(나라장터)·고시(관보고시)·공정률(국가철도공단)을 한 번에 모은다.
    개별 소스 실패는 해당 섹션 error로만 표기하고 나머지는 정상 반환한다(부분 성공 허용).
    반환: {query, line, preset, budget, bids, notices, progress}.
    """
    line = resolve_line(query)
    tasks = {
        "budget": get_project_budget(query),
        "bids": get_construction_bids(query),
        "notices": get_rail_notices(query),
        "progress": get_rail_progress(query),
    }
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    out: dict = {"query": query, "line": line["line"], "preset": line["preset"]}
    for key, res in zip(tasks.keys(), results):
        out[key] = fail(key, res) if isinstance(res, Exception) else res
    return out


# ---------------------------------------------------------------- 스냅샷


@mcp.tool()
async def get_market_snapshot() -> dict:
    """데일리 리포트용 핵심 지표 일괄 조회.

    KOSPI·KOSDAQ·USD/KRW·S&P500(^GSPC)·나스닥(^IXIC)·SOX(^SOX)·BTC·ETH를
    병렬 조회해 정규화 리스트로 반환. 일부 실패해도 가능한 것만 채운다.
    """
    tasks = {
        "KOSPI": get_kospi(),
        "KOSDAQ": get_kosdaq(),
        "USD/KRW": get_exchange_rate("USD/KRW"),
        "S&P500": get_stock_price("^GSPC"),
        "NASDAQ": get_stock_price("^IXIC"),
        "SOX": get_stock_price("^SOX"),
        "BTC": get_crypto("BTC", "KRW"),
        "ETH": get_crypto("ETH", "KRW"),
    }
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    items = []
    for key, res in zip(tasks.keys(), results):
        if isinstance(res, Exception):
            items.append(fail(key, res))
        else:
            items.append(res)
    return {"snapshot": items, "count": len(items)}


if __name__ == "__main__":
    mcp.run()
