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
                     g2b, fiscal, kr_notice, kr_progress,
                     dart, ecos, naver_valuation, portfolio, realty_index)
from sources.region_codes import resolve_region
from sources.rail_lines import resolve_line

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,  # stdout 오염 금지
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("finance-mcp")

# httpx INFO 로그는 요청 URL 전체(API 키 포함)를 stderr에 남기므로 억제
logging.getLogger("httpx").setLevel(logging.WARNING)

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


# ---------------------------------------------------------------- 시계열

_TTL_HISTORY = 300.0  # 일/주/월봉은 장중에도 자주 안 바뀜


@mcp.tool()
async def get_price_history(ticker: str, period: str = "1y",
                            interval: str | None = None) -> dict:
    """주식·지수·환율의 과거 시세 시계열 + 수익률/변동성/MDD. 키 불필요.

    ticker: 국내 6자리 코드('005930') → 네이버 일별시세, 실패 시 Yahoo(.KS/.KQ) 강등.
            Yahoo 심볼('^GSPC', 'AAPL', 'KRW=X') → Yahoo chart.
            크립토도 Yahoo 심볼로 조회 가능('BTC-USD', 'ETH-USD').
    period: '5d','1mo','3mo','6mo','ytd','1y','2y','5y','10y','max'(기본 '1y').
    interval: '1d'(일봉)/'1wk'(주봉)/'1mo'(월봉). 미지정 시 period에 맞춰 자동
              선택(1y→주봉 등)해 관측 수를 50~120점으로 유지한다.
    반환: {name, symbol, period, interval, currency, count,
          points:[{date, open, high, low, close, volume}],
          stats:{start_value, end_value, change_pct(기간수익률),
                 high/low(+시점), pct_from_high, max_drawdown_pct,
                 volatility_pct(연율화)}, week52_high/low(야후), source}.
    국내 종목은 points에 foreign_ratio(외국인소진율)가 함께 온다.
    현재가만 필요하면 get_stock_price를 쓴다(응답이 훨씬 짧다).
    """
    is_domestic = ticker.isdigit() and len(ticker) == 6
    iv = interval or yahoo.auto_interval(period)

    async def fetch():
        if is_domestic:
            return await _cascade(
                f"시계열:{ticker}",
                lambda: naver.get_history(ticker, period, iv),
                lambda: yahoo.get_history(f"{ticker}.KS", period, iv, name=ticker),
                lambda: yahoo.get_history(f"{ticker}.KQ", period, iv, name=ticker),
            )
        return await _cascade(
            f"시계열:{ticker}",
            lambda: yahoo.get_history(ticker, period, iv),
        )
    return await cached(f"hist:{ticker}:{period}:{iv}", fetch, _TTL_HISTORY)


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


# ---------------------------------------------------------------- 오피스텔


@mcp.tool()
async def get_offi_trade(region: str, deal_ym: str, rows: int = 50) -> dict:
    """오피스텔 매매 실거래가(국토교통부 공공데이터포털). MOLIT_API_KEY 필요
    (오피스텔 매매 API 별도 활용신청 필요 — 미신청 시 403).

    region: 지역명 또는 5자리 시군구 법정동코드(get_apt_trade와 동일 자동 변환).
    deal_ym: 계약 년월 'YYYYMM'/'YYYY-MM'.
    반환 items: {apt(오피스텔명), deal_amount(만원), area(전용㎡), pyeong(전용 평수),
                price_per_pyeong(전용 평당가 만원/평), floor, build_year, dong, jibun, date}.
    평당가는 전용면적 기준.
    """
    try:
        code = resolve_region(region)
    except ValueError as e:
        return fail(f"오피스텔매매:{region}", e)
    ym = _normalize_ym(deal_ym)

    async def fetch():
        return await _cascade(
            f"오피스텔매매:{code}:{ym}",
            lambda: molit.offi_trade(code, ym, rows),
        )
    return await cached(f"offi_trade:{code}:{ym}:{rows}", fetch)


@mcp.tool()
async def get_offi_trade_summary(region: str, deal_ym: str, months: int = 1,
                                 rows: int = 1000) -> dict:
    """오피스텔 매매 실거래가를 단지(건물)별 평균 평당가로 집계. MOLIT_API_KEY 필요.

    region: 지역명 또는 5자리 코드. deal_ym: 기준월 'YYYYMM'/'YYYY-MM'.
    months: 기준월 포함 직전 N개월 합산(기본 1, 최대 12). 거래가 적은 오피스텔은
            months=3~6으로 표본을 늘린다.
    반환: {name, region_code, deal_ym, months, period, complex_count, deal_count,
          items:[{apt(오피스텔명), dong, count, avg_price_per_pyeong,
                  min/max_price_per_pyeong, avg_deal_amount, avg_pyeong}], source}.
    포트폴리오 오피스텔 평가(get_portfolio_snapshot의 apt type)에도 활용 가능.
    """
    try:
        code = resolve_region(region)
    except ValueError as e:
        return fail(f"오피스텔단지평당가:{region}", e)
    ym = _normalize_ym(deal_ym)

    async def fetch():
        return await _cascade(
            f"오피스텔단지평당가:{code}:{ym}:{months}",
            lambda: molit.offi_trade_summary(code, ym, rows, months),
        )
    return await cached(f"offi_trade_summary:{code}:{ym}:{months}:{rows}", fetch)


@mcp.tool()
async def get_offi_rent(region: str, deal_ym: str, rows: int = 50) -> dict:
    """오피스텔 전월세 실거래가(국토교통부 공공데이터포털). MOLIT_API_KEY 필요
    (오피스텔 전월세 API 별도 활용신청 필요).

    region: 지역명 또는 5자리 코드. deal_ym: 'YYYYMM'/'YYYY-MM'.
    반환 items: {apt(오피스텔명), deposit(보증금 만원), monthly_rent(월세 만원, 0이면 전세),
                area(전용㎡), pyeong, deposit_per_pyeong(전용 보증금 평당가 만원/평),
                floor, build_year, dong, jibun, date}.
    """
    try:
        code = resolve_region(region)
    except ValueError as e:
        return fail(f"오피스텔전월세:{region}", e)
    ym = _normalize_ym(deal_ym)

    async def fetch():
        return await _cascade(
            f"오피스텔전월세:{code}:{ym}",
            lambda: molit.offi_rent(code, ym, rows),
        )
    return await cached(f"offi_rent:{code}:{ym}:{rows}", fetch)


# ------------------------------------------------ 공사현황(철도/광역교통)

# 변화가 느린 데이터라 TTL을 길게: 입찰 30분, 고시/공정률 6시간, 예산 1일.
_TTL_BIDS = 1800.0
_TTL_NOTICE = 21600.0
_TTL_PROGRESS = 21600.0
_TTL_BUDGET = 86400.0


@mcp.tool()
async def get_construction_bids(query: str, biz: str = "공사", days: int = 30,
                                rows: int = 50, agency: str | None = None) -> dict:
    """철도/광역교통 발주·착공 신호 — 조달청 나라장터 입찰공고.
    DATA_GO_KR_API_KEY(또는 MOLIT_API_KEY) 필요 + '나라장터 입찰공고정보서비스' 활용신청.

    query: 노선 프리셋 별칭('GTX-A', '신안산선', '7호선 청라연장', '9호선 연장' 등) 또는
           자유 키워드(공고명 부분일치). 프리셋이면 여러 표기를 함께 검색해 누락을 줄인다.
    biz: '공사'(기본)/'용역'(설계·감리)/'물품'. days: 직전 N일(기본 30). rows: 최대 건수.
    agency: 발주/수요기관 필터(예: '서울교통공사'). 숫자 노선명('9호선')이 도로 노선번호
            (국도79호선 등)에 걸리는 노이즈 제거용. 미지정 시 프리셋의 기관 힌트를 쓴다.
    반환: {name, biz, keywords, agencies, period, count, bids:[{공고명, 공고번호, 차수,
          공고일, 입찰마감, 개찰일, 추정가격(원), 배정예산(원), 발주기관, 수요기관,
          지역제한, url}], source}. 입찰공고가 뜨면 착공이 임박했다는 1차 신호.
    """
    line = resolve_line(query)
    agencies = [agency] if agency else line.get("agencies")

    async def fetch():
        return await _cascade(
            f"입찰공고:{line['line']}",
            lambda: g2b.search_bids(line["keywords"], biz, days, rows, agencies),
        )
    key = f"bids:{line['line']}:{biz}:{days}:{rows}:{agency or ''}"
    return await cached(key, fetch, _TTL_BIDS)


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
    # 열린재정은 '세부사업명'(예: 수도권광역급행철도B노선)으로 검색한다. 대중 노선명
    # (GTX-B 등)과 표기가 달라, 프리셋의 budget_keywords가 있으면 그걸 우선 사용한다.
    budget_kws = line.get("budget_keywords") or line["keywords"]

    async def fetch():
        return await _cascade(
            f"재정사업:{line['line']}",
            lambda: fiscal.search_budget(budget_kws, year, rows),
        )
    return await cached(f"budget:{line['line']}:{year}:{rows}", fetch, _TTL_BUDGET)


@mcp.tool()
async def get_rail_notices(query: str, kind: str = "기본") -> dict:
    """철도 고시·인허가 신호 — 국가철도공단 관보고시(공공데이터포털 파일데이터).
    파일 다운로드 URL 환경변수 필요(KRNA_NOTICE_URL_BASIC 등, sources/kr_notice.py 참고).

    query: 노선 프리셋 별칭 또는 사업명 키워드. kind: '기본'(관보고시 기본정보)/
           '계획'(기본계획 고시)/'세목'(용지 세목 — 지번별 토지 편입 내역).
    반환: {name, kind, keywords, total_records, count, notices:[{고시명, 고시번호,
          고시일, 사업명, 종류}], source}. 기본계획/실시계획 고시는 법적 확정 신호.
    '세목'은 노선 고시를 경유해 각 고시번호에 딸린 필지를 조인, notices에 용지세목
    {필지수, 총편입면적_㎡, 지역본부, 구분}을 붙인다(수용 대상 토지 = 부동산 직접 신호).
    """
    line = resolve_line(query)
    # 고시 원문은 사업 구간명 표기라 대중 노선명과 다르다(GTX-A→'삼성~동탄 광역급행철도').
    # 프리셋의 고시 전용 별칭(notice_keywords)을 검색 키워드에 덧붙여 누락을 막는다.
    notice_kws = line["keywords"] + line.get("notice_keywords", [])

    async def fetch():
        return await _cascade(
            f"관보고시:{line['line']}",
            lambda: kr_notice.search_notices(notice_kws, kind),
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


# ------------------------------------------------ 주식 심화(DART·밸류에이션)

_TTL_VALUATION = 120.0     # 재무지표는 실시간성보다 안정성
_TTL_DISCLOSURE = 21600.0  # 공시 목록 6시간
_TTL_MACRO = 21600.0       # 거시지표 6시간(정책금리/월간 지표)


@mcp.tool()
async def search_stock_code(name: str, limit: int = 10) -> dict:
    """종목명으로 6자리 종목코드 검색(DART 상장회사 인덱스). DART_API_KEY 필요.

    name: 종목명 부분일치(예: '삼성전자', '에코프로'). 완전일치를 최상위로 정렬.
    최초 호출 시 DART 전체 상장사 목록(ZIP, 수 MB)을 받아 24시간 캐시하므로
    첫 호출만 수 초 걸릴 수 있다.
    반환: {query, count, items:[{name, stock_code, corp_code}], source}.
    corp_code는 DART 고유번호(get_dart_disclosures에서 사용 가능).
    """
    async def fetch():
        return await _cascade(f"종목검색:{name}",
                              lambda: dart.search(name, limit))
    return await cached(f"stock_search:{name}:{limit}", fetch, _TTL_DISCLOSURE)


@mcp.tool()
async def get_dart_disclosures(query: str, days: int = 90, rows: int = 20) -> dict:
    """종목의 최근 DART 전자공시 목록 — 보유 종목 리스크 신호. DART_API_KEY 필요.

    query: 종목명('삼성전자') / 6자리 종목코드('005930') / 8자리 DART corp_code.
           종목명이 여러 종목과 일치하면 후보를 안내하고 실패한다(코드로 재시도).
    days: 오늘 기준 직전 N일(기본 90). rows: 최대 건수(기본 20, 최대 100).
    반환: {name, corp_code, stock_code, period, total_count, count,
          disclosures:[{제목, 접수일, 제출인, 시장, 비고, 접수번호, url}], source}.
    유상증자·전환사채·감사보고서·최대주주변경 등 제목에서 리스크 신호를 읽는다.
    """
    async def fetch():
        return await _cascade(f"공시:{query}",
                              lambda: dart.disclosures(query, days, rows))
    return await cached(f"dart_list:{query}:{days}:{rows}", fetch, _TTL_DISCLOSURE)


@mcp.tool()
async def get_stock_valuation(ticker: str) -> dict:
    """국내 종목 밸류에이션 — PER/PBR/EPS/BPS/배당수익률/시가총액(억원). 키 불필요.

    ticker: 6자리 종목코드(예: '005930'). 1순위 네이버 모바일 통합 API,
    실패 시 네이버 PC 종목페이지 정적 HTML 파싱으로 강등.
    반환: {code, name, market_cap_eok(억원), per, pbr, eps, bps,
          dividend_yield_pct, close_price, currency, timestamp, source}.
    적자 기업은 per가 None일 수 있다. 시세는 get_stock_price를 함께 사용.
    """
    async def fetch():
        return await _cascade(
            f"밸류에이션:{ticker}",
            lambda: naver_valuation.from_mobile_api(ticker),
            lambda: naver_valuation.from_pc_html(ticker),
        )
    return await cached(f"valuation:{ticker}", fetch, _TTL_VALUATION)


# ---------------------------------------------------------------- 거시경제


@mcp.tool()
async def get_macro_indicators(keywords: list[str] | None = None) -> dict:
    """한국은행 ECOS 100대 통계지표 — 거시경제 스냅샷. ECOS_API_KEY 필요.

    keywords: 지표명/분류명 부분일치 필터. 미지정 시 기본 관심지표
    (기준금리/국고채/CD/콜금리/소비자물가/M2/가계신용/원달러/경제성장).
    빈 리스트([])를 주면 100개 전체 반환.
    반환: {name, keywords, total_available, count,
          indicators:[{분류, 지표명, 값, 단위, 기준시점}], source}.
    부동산(금리·가계신용)과 주식(성장·물가) 판단의 공통 기초 지표.
    """
    async def fetch():
        return await _cascade("거시지표",
                              lambda: ecos.key_statistics(keywords))
    kw_key = ",".join(keywords) if keywords else ("_all" if keywords == [] else "_default")
    return await cached(f"macro:{kw_key}", fetch, _TTL_MACRO)


@mcp.tool()
async def get_macro_series(indicator: str = "기준금리", periods: int = 36) -> dict:
    """거시지표 **시계열**(한국은행 ECOS 통계표). ECOS_API_KEY 필요.

    indicator: 프리셋 이름 — '기준금리', '콜금리', 'CD금리', '국고채3년',
               '국고채10년', '소비자물가', 'M2', '가계신용'.
               프리셋에 없으면 '통계표코드/항목코드/주기'로 직접 지정
               (예: '722Y001/0101000/M'). 주기는 M(월)/Q(분기)/D(일)/A(년).
    periods: 조회할 관측 수(월 계열이면 개월 수, 기본 36).
    반환: {name, stat_code, item_code, cycle, unit, count,
          points:[{time, value}], stats:{start_value, end_value, change,
          change_pct, high/low(+시점)}, changes/changes_pct:{'3개월','6개월','12개월'},
          source}.
    금리(연%)는 changes(절대 %p)로, 지수·잔액은 changes_pct(%)로 읽는다
    (기준금리 2.50→2.75는 '+10%'가 아니라 '+0.25%p').
    최신값 여러 개를 한눈에 보려면 get_macro_indicators(스냅샷)를 쓴다.
    """
    async def fetch():
        return await _cascade(f"거시시계열:{indicator}",
                              lambda: ecos.series(indicator, periods))
    return await cached(f"macro_series:{indicator}:{periods}", fetch, _TTL_MACRO)


@mcp.tool()
async def get_realty_price_index(region: str = "전국", kind: str = "매매",
                                 house_type: str = "아파트", months: int = 36,
                                 source: str = "부동산원") -> dict:
    """주택 매매·전세 **가격지수 시계열**(ECOS 중계). ECOS_API_KEY 필요.

    실거래가(get_apt_trade_summary)는 단지·평형 편차가 커서 시장 방향을 보기
    어렵다. 지수와 함께 봐야 해석이 된다.

    region: 시도 단위 — '전국','수도권','지방','서울','경기','인천','부산',
            '대구','광주','대전','울산','세종','강원','충북','충남','전북',
            '전남','경북','경남','제주','5대광역시','6대광역시','8개도','9개도'.
            **시군구(강남구 등)는 지수가 없다** — 실거래 tool을 쓴다.
    kind: '매매'(기본) 또는 '전세'. house_type: '아파트'(기본)/'종합'/
          '연립다세대'/'단독주택'.
    source: '부동산원'(기본, 지역 세분·공표 지연 있음) 또는 'kb'(전국·서울만,
            최신월 반영이 빠름). 기준월이 달라 두 지수의 수치를 직접 비교하면 안 된다.
    반환: {name, region, kind, house_type, org, unit(기준월=100), count,
          points:[{time, value}], stats, changes/changes_pct:{'3개월','6개월','12개월'},
          note, source}.
    """
    async def fetch():
        return await _cascade(
            f"주택가격지수:{region}:{kind}",
            lambda: realty_index.price_index(region, kind, house_type, months, source),
        )
    key = f"realty_idx:{region}:{kind}:{house_type}:{months}:{source}"
    return await cached(key, fetch, _TTL_MACRO)


# ---------------------------------------------------------------- 포트폴리오


@mcp.tool()
async def get_portfolio_snapshot(path: str | None = None) -> dict:
    """로컬 보유자산 파일 기반 포트폴리오 평가·손익·자산배분 스냅샷.

    path: 포트폴리오 파일 경로(JSON, .yml/.yaml은 PyYAML 필요). 미지정 시
          PORTFOLIO_FILE_PATH 환경변수 → './portfolio.json' 순. 스키마는 README 참고
          (holdings:[{type: stock|etf|crypto|apt, ticker/region, quantity,
           avg_price, ...}], cash:{KRW,USD}).
    종목별 시세는 기존 tool(네이버/야후/코인게코/국토부)로 병렬 조회하며, 개별 실패는
    해당 보유분의 price_error로만 남기고 나머지는 계속 평가한다(부분 성공).
    USD 자산이 있으면 환율을 1회 조회해 원화 환산. apt는 최근 N개월 단지평균
    평당가 기반 추정치(정확한 시세 아님).
    반환: {as_of, usd_krw, holdings(평가액·손익·수익률), cash_value_krw,
          allocation_pct(자산군별 %), totals, errors, source}.
    """
    try:
        data, abspath = portfolio.load_file(path)
    except Exception as e:  # noqa: BLE001 - 파일 문제는 안내 메시지로
        return fail("포트폴리오", e)

    fetchers = {
        "stock": get_stock_price,
        "etf": get_etf_price,
        "crypto": get_crypto,
        "apt": lambda region, ym, months: get_apt_trade_summary(region, ym, months),
        "fx": lambda: get_exchange_rate("USD/KRW"),
    }
    try:
        snap = await portfolio.snapshot(data, fetchers)
    except Exception as e:  # noqa: BLE001
        return fail("포트폴리오", e)
    snap["file"] = abspath
    return snap


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
