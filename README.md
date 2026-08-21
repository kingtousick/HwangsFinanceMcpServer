# Finance MCP Server

Claude(Cowork/Desktop)의 WebSearch·WebFetch가 차단당하거나(네이버 금융) 못 가져오는(CoinGecko 등) 금융 시세를, **로컬에서 실행되는 MCP 서버의 Tool 호출**로 안정적·정규화된 형태로 조회한다. 데일리 투자 리포트의 시세 정확도 보장이 목적이다.

## Tool 목록

| Tool | 설명 | 예시 |
|---|---|---|
| `get_kospi()` | KOSPI 지수 | — |
| `get_kosdaq()` | KOSDAQ 지수 | — |
| `get_exchange_rate(pair="USD/KRW")` | 환율 | `"USD/KRW"` |
| `get_stock_price(ticker)` | 국내/해외 주식·지수 | `"005930"`, `"^GSPC"`, `"^SOX"` |
| `get_etf_price(code)` | KRX ETF | `"381180"` |
| `get_crypto(symbol="BTC", quote="KRW")` | 크립토 | `"BTC"/"KRW"`, `"ETH"/"USD"` |
| `get_market_snapshot()` | 8개 핵심 지표 일괄(리포트용) | — |
| `get_price_history(ticker, period="1y", interval=None)` | 과거 시세 시계열 + 수익률·MDD·변동성 | `"005930"`, `"^GSPC"`, `"BTC-USD"` |
| `get_apt_trade(region, deal_ym)` | 아파트 매매 실거래가(평수·평당가 포함) | `"강남구"`, `"2024-06"` |
| `get_apt_trade_summary(region, deal_ym, months=1)` | 단지별 평균 평당가 집계(+세대수·회전율) | `"강남구"`, `"2024-06"`, `6` |
| `get_apt_complex_info(region, complex_name)` | 단지 기본정보(세대수·동수·사용승인일) | `"강남구"`, `"은마아파트"` |
| `get_apt_rent(region, deal_ym)` | 아파트 전월세 실거래가 | `"강남구"`, `"2024-06"` |
| `get_jeonse_ratio(region, deal_ym, months=1)` | 단지별 전세가율 집계 | `"강남구"`, `"2024-06"`, `6` |
| `get_offi_trade(region, deal_ym)` | 오피스텔 매매 실거래가(평수·평당가) | `"강남구"`, `"2024-06"` |
| `get_offi_trade_summary(region, deal_ym, months=1)` | 오피스텔 단지별 평균 평당가 집계 | `"강남구"`, `"2024-06"`, `6` |
| `get_offi_rent(region, deal_ym)` | 오피스텔 전월세 실거래가 | `"강남구"`, `"2024-06"` |
| `get_construction_bids(query, biz="공사", days=30, agency=None)` | 나라장터 입찰공고(발주·착공) | `"GTX-A"`, `"9호선 연장"` |
| `get_project_budget(query, year=None)` | 열린재정 재정사업 예산·집행(예타·재정) | `"신안산선"` |
| `get_rail_notices(query, kind="기본")` | 국가철도공단 관보고시(고시·인허가) | `"7호선 청라연장"` |
| `get_rail_progress(query)` | 국가철도공단 공정률(진행현황, Playwright) | `"GTX-A"` |
| `get_rail_project_status(query)` | 한 노선의 예산·발주·고시·공정률 통합 스냅샷 | `"GTX-A"` |
| `search_stock_code(name)` | 종목명→6자리 코드 검색(DART 상장사 인덱스) | `"삼성전자"` |
| `get_dart_disclosures(query, days=90)` | 최근 DART 전자공시(리스크 신호) | `"005930"`, `"에코프로"` |
| `get_stock_valuation(ticker)` | PER/PBR/EPS/BPS/배당수익률/시가총액 (국내) | `"005930"` |
| `get_global_valuation(ticker)` | **해외** 밸류에이션 PER/PBR/PSR/PEG/EV-EBITDA/ROE·마진 | `"NVDA"`, `"AVGO"` |
| `compare_valuation(tickers, metrics=None, normalize_krw=False)` | 국내·해외 **횡단 비교표**(최대 10종목, 자동 라우팅) | `["NVDA","000660"]` |
| `get_implied_useful_life(ticker, years=3)` | 감가상각 **내용연수 역산**(연장/단축 감지) | `"AMZN"`, `"META"` |
| `get_capex_series(ticker, quarters=8)` | 분기별 **CAPEX 실제 집행액**·OCF·FCF·매출대비 비중 | `"GOOGL"`, `8` |
| `get_sec_fundamentals(ticker, concepts, years=3)` | SEC XBRL **원자료**(us-gaap 태그 직접 조회) | `"NVDA"`, `["Revenues"]` |
| `get_rpo_backlog(ticker, quarters=8)` | 잔여 이행의무(RPO) **수주잔고** | `"MSFT"` |
| `get_credit_spreads(series=None, period="1y")` | 미국 **신용스프레드·금리곡선**(FRED, 백분위 포함) | — |
| `get_macro_indicators(keywords=None)` | 한은 ECOS 100대 통계지표(금리·물가·M2 등) | `["기준금리","가계신용"]` |
| `get_macro_series(indicator, periods=36)` | 거시지표 **시계열**(추이·변화율) | `"기준금리"`, `"국고채3년"` |
| `get_realty_price_index(region, kind, house_type, months, source)` | 주택 매매/전세 **가격지수 시계열** | `"서울"`, `"매매"`, `"아파트"` |
| `get_portfolio_snapshot(path=None)` | 로컬 파일 기반 보유자산 평가·손익·배분 | `"./portfolio.json"` |

### 티커 형식
- **국내 주식/ETF**: 6자리 코드 (`005930`, `381180`) → 네이버 polling
- **해외 주식/지수**: Yahoo 심볼 (`^GSPC`, `^IXIC`, `^SOX`, `AAPL`) → Yahoo chart API
  - 클래스 주식은 하이픈 (`BRK-B`). SEC 계열 Tool은 `BRK.B`도 자동 정규화한다.
- **환율**: `USD/KRW` (Yahoo `KRW=X`)
- **크립토**: 심볼 + quote (`BTC`/`KRW`, `ETH`/`USD`) → CoinGecko

### 응답 스키마
성공:
```json
{"name":"KOSPI","value":9165.58,"change":113.16,"change_pct":1.25,
 "timestamp":"2026-06-22T11:27:14+09:00","currency":"KRW","source":"naver"}
```
실패(Claude가 WebSearch로 폴백):
```json
{"name":"KOSPI","error":"timeout","source":"fallback"}
```
미국 밸류에이션·펀더멘털 Tool(`get_global_valuation`, SEC·FRED 계열)은 **부분 성공**
스키마를 쓴다. 예외를 던지지 않고, 못 채운 값은 `null`로 두고 사유를 `errors[]`에 남긴다
(**추정치로 채우지 않는다**). 모든 응답에 `timestamp`·`source`·`data_kind`가 붙는다.
```json
{"symbol":"NVDA","trailing_pe":34.24,"peg":null,
 "timestamp":"2026-08-10T15:54:01+09:00","source":"yahoo_quote_summary",
 "data_kind":"prev_close",
 "errors":[{"field":"financialData","reason":"quoteSummary 모듈 미제공","source":"yahoo"}]}
```
`data_kind`는 `realtime` | `intraday` | `prev_close` | `filing` 중 하나로, 그 숫자가
어느 시점의 것인지 알려준다(`filing`은 공시 원문 기준).

## 소스 우선순위 (자동 강등)

| 데이터 | 1순위 | 2순위 | Fallback |
|---|---|---|---|
| 국내 지수 | 네이버 polling | — | Playwright |
| 국내 주식/ETF | 네이버 polling | — | — |
| 해외 주식/지수 | Yahoo(query1) | Yahoo(query2) | — |
| USD/KRW | Yahoo `KRW=X` | EXIM(키) | — |
| 크립토 | CoinGecko | 업비트(KRW, 도달 시) | — |
| 해외 밸류에이션 | Yahoo quoteSummary(crumb) | Yahoo chart v8(부분) | `errors[]` |
| 미국 재무 원자료 | SEC EDGAR XBRL companyconcept | — | `errors[]` |
| 신용스프레드·금리 | FRED CSV | — | `errors[]` |

상위 실패 시 다음 소스로 자동 강등, 전부 실패 시 `{error, source:"fallback"}`.

## 설치

```bash
py -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
# (선택) 최후 수단 렌더링 fallback
.venv\Scripts\python -m pip install playwright && .venv\Scripts\python -m playwright install chromium
```

### 사내망(TLS 가로채기) 환경 주의
일부 사내망은 TLS를 가로채(MITM) 사내 루트 CA로 재서명한다. 이 CA는 Windows 인증서 저장소에만 있고 Python(certifi)엔 없어 기본 검증이 실패한다. 본 서버는 `truststore`로 **OS 인증서 저장소를 사용**해 검증을 유지하면서 사내 CA도 신뢰한다(requirements에 포함). 사내망에서 동작 확인됨.

## Claude Desktop 연동

`claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "finance": {
      "command": "C:\\{project}\\workspace\\agentHwang\\finaceMcp\\.venv\\Scripts\\python.exe",
      "args": ["C:\\{project}\\workspace\\agentHwang\\finaceMcp\\finance_server.py"],
      "env": { "EXIM_API_KEY": "", "FINNHUB_KEY": "" }
    }
  }
}
```

## 환경변수 (선택)
`.env` 또는 config의 `env`로 주입. 없으면 해당 소스는 건너뛴다.
- `EXIM_API_KEY`: 한국수출입은행 환율 API(가정용 PC 환율 폴백).
- `FINNHUB_KEY`: 미국 주식 폴백(예약, 현재 미사용).
- `MOLIT_API_KEY`: 국토교통부 실거래가 API(`get_apt_trade`/`get_apt_rent`에 필수).
  세대수 도구(`get_apt_complex_info`, `get_apt_trade_summary`의 세대수 필드)도 같은
  키를 쓴다(`DATA_GO_KR_API_KEY` 우선). 단, K-apt 데이터셋 2종 활용신청 별도 필요.
- `DART_API_KEY`: DART 전자공시(`search_stock_code`/`get_dart_disclosures`에 필수).
  [opendart.fss.or.kr](https://opendart.fss.or.kr)에서 무료 발급.
- `ECOS_API_KEY`: 한국은행 ECOS(`get_macro_indicators`/`get_macro_series`/
  `get_realty_price_index`에 필수). [ecos.bok.or.kr](https://ecos.bok.or.kr) >
  Open API에서 무료 발급.
- `PORTFOLIO_FILE_PATH`: 포트폴리오 파일 경로(선택, 기본 `./portfolio.json`).

### 부동산 실거래가 사용법
1. [공공데이터포털](https://www.data.go.kr)에서 **"국토교통부_아파트 매매 실거래가 자료"**,
   **"국토교통부_아파트 전월세 실거래가 자료"** 활용신청(무료). 오피스텔 도구를 쓰려면
   **"국토교통부_오피스텔 매매 실거래가 자료"**, **"국토교통부_오피스텔 전월세 실거래가 자료"**도
   추가 활용신청(같은 키/계정, 데이터셋별 승인 필요 — 미신청 데이터셋은 403).
2. 마이페이지 → 인증키에서 **Decoding(일반) 키**를 복사해 `MOLIT_API_KEY`에 설정
   (Encoding 키를 쓰면 이중 인코딩으로 인증 실패).
3. `region_code`는 **5자리 시군구 법정동코드**(예: 강남구 `11680`, 송파구 `11710`).
   전체 목록은 행정표준코드관리시스템(code.go.kr)의 법정동코드 참고.
4. `deal_amount`/`deposit`/`monthly_rent` 단위는 **만원**. 예: `deal_amount=250000` → 25억.
5. `deal_ym`은 `"YYYYMM"`/`"YYYY-MM"` 모두 허용. 평당가는 **전용면적 기준**(공급면적
   기준 시장 평당가보다 높게 나옴).

#### `months` 옵션 (집계 Tool)
`get_apt_trade_summary`·`get_jeonse_ratio`는 `months`로 **기준월 포함 직전 N개월**(기본 1,
최대 12)을 합산해 표본을 늘린다. 단일 월은 거래가 적어 평균이 흔들리거나(평당가 집계),
같은 달에 매매·전세가 모두 난 단지만 매칭돼(전세가율) 표본이 적다. `months=3~6` 권장.

```python
# 단일 월
get_apt_trade_summary("강남구", "2026-04")            # 411건 / 165단지
get_jeonse_ratio("강남구", "2026-04")                 # 86단지 매칭

# 6개월 합산 (period: "202511~202604")
get_apt_trade_summary("강남구", "2026-04", months=6)  # 1,442건 / 327단지
get_jeonse_ratio("강남구", "2026-04", months=6)       # 250단지 매칭, 평균 전세가율 43.6%
```
반환에 `months`와 합산 구간 `period` 필드가 포함된다. `get_apt_trade`/`get_apt_rent`
(원시 거래 조회)는 단일 월만 지원한다.

> **API별 활용신청 필요**: 매매(`get_apt_trade`)와 전월세(`get_apt_rent`)는 별개 API다.
> 둘 다 쓰려면 data.go.kr에서 각각 활용신청해야 한다(한쪽만 신청 시 다른 쪽은 403).
> data.go.kr WAF가 curl 기본 UA를 차단하므로 서버는 브라우저 UA로 호출한다(코드 내 처리됨).
> 응답 XML은 stdlib로 파싱하며 전월세 필드는 실데이터로 검증됨(2026-06-22).

#### 세대수·회전율 (K-apt 공동주택 API)
실거래가 API는 **세대수를 주지 않는다**. 단지 규모를 알아야 거래건수를 정규화할 수
있으므로 K-apt(공동주택관리정보시스템) 2개 API를 결합한다(`sources/kapt.py`).

| 용도 | 데이터셋(활용신청 이름) | 엔드포인트 |
|---|---|---|
| 단지목록(시군구→단지코드) | **국토교통부_공동주택 단지 목록제공 서비스** | `AptListService3/getSigunguAptList3` |
| 기본정보(단지코드→세대수) | **국토교통부_공동주택 기본 정보제공 서비스** | `AptBasisInfoServiceV4/getAphusBassInfoV4` |

- 키는 `DATA_GO_KR_API_KEY`(없으면 `MOLIT_API_KEY`)를 그대로 쓰지만 **두 데이터셋 모두
  별도 활용신청**이 필요하다(미신청 시 403, `returnReasonCode 30`).
- 신청 전이거나 K-apt 미등록 단지면 세대수 필드는 `None`이고 실거래 집계는 평소대로
  나온다 — 세대수는 부가 지표라 실패해도 강등되지 않는다.
- `get_apt_trade_summary` items에 추가되는 필드:
  `households`(세대수), `dong_count`(동수), `use_date`(사용승인일),
  `turnover_rate`(= 기간 거래건수 ÷ 세대수 × 100, %),
  `households_shared`(통합 등록 단지에만 True — 아래 참고).
  요약에는 `households_matched`(채운 단지 수), `households_pending`(호출 상한에 걸려
  다음 조회로 밀린 단지 수, 보통 0).
- 세대수는 준공 후 불변이라 디스크 캐시 30일. 첫 호출만 5~10초(강남구 6개월 기준 8초),
  이후 같은 지역은 캐시로 1초 내.

```python
get_apt_complex_info("강남구", "은마아파트")
# → matched: {households: 4424, dong_count: 28, use_date: "1979-08-30",
#             hall_type: "복도식", builder: "한보", area_band: {"60~85": 24, "85~135": 4400}}

get_apt_trade_summary("강남구", "2026-04", months=6)
# → 328단지 / 1,463건, 세대수 137단지 매칭
# → items[i]: {..., households: 1403, turnover_rate: 1.283}
```

**매칭률은 약 50%** (강남구 2026-04 실측: 168단지 중 86단지). 실거래 단지명과 K-apt
단지명을 (법정동, 정규화 단지명)으로 붙이는데, 정규화가 흡수하는 차이는 이렇다:

| 실거래 표기 | K-apt 표기 | 처리 |
|---|---|---|
| `미성2차` | `압구정미성2차` | 동명 접두 → 부분일치로 매칭 |
| `개포우성2` | `개포우성8차` | `N차`/`N단지` → `N`으로 통일 |
| `현대14차(203,204,205,206동)` | — | 괄호 안 동번호·별칭 제거 |
| `대치우성아파트1동,2동,3동` | `대치우성1차아파트` | 동 번호 나열 제거 |

못 붙는 나머지는 **K-apt가 여러 차수를 한 단지로 통합 등록**했거나(`압구정 현대(10,13,14차)`
↔ 실거래 `현대14차`) 애초에 미등록(소규모 단지)인 경우다. 정규화로 더 짜낼 수 있지만
남의 세대수가 붙는 오매칭이 결측보다 나쁘므로 **후보가 둘 이상이면 포기**하고, 탐색도
**같은 법정동으로 한정**한다(시군구 전체로 넓히면 `현대1`(대치동)이 `개포현대1차`(개포동)에
붙는 오매칭이 실제로 발생했다).

> `households_shared: true`가 붙은 단지는 한 K-apt 단지에 실거래 단지 여러 개가 매칭된
> 경우다(`LG선릉에클라트(A)`·`(B)` → `선릉에클라트`). 세대수가 통합값이라 **회전율이
> 실제보다 낮게** 나오니 그대로 믿지 말 것.

### 공사현황(철도/광역교통) 사용법
부동산 가치의 선행지표인 교통 인프라 진행상황을 노선/사업명 하나로 조회한다. 자동화
가능한 4개 공공 데이터 카테고리를 다룬다.

| 신호 | Tool | 소스 | 필요 설정 |
|---|---|---|---|
| 돈이 가나(예타·재정) | `get_project_budget` | 열린재정 OpenAPI | `OPEN_FISCAL_API_KEY` |
| 삽 떴나(발주·착공) | `get_construction_bids` | 나라장터 OpenAPI | `DATA_GO_KR_API_KEY` + 서비스 활용신청 |
| 확정됐나(고시·인허가) | `get_rail_notices` | 관보고시 파일데이터 | `KRNA_NOTICE_URL_*` |
| 얼마나 됐나(공정률) | `get_rail_progress` | kr.or.kr HTML | Playwright 설치 |
| 통합 | `get_rail_project_status` | 위 4개 병렬 | (각 소스 설정) |

```python
get_construction_bids("GTX-A")                 # 최근 30일 공사 입찰공고
get_construction_bids("9호선 연장")             # 프리셋 기관 힌트로 도로 노이즈 자동 제거
get_construction_bids("9호선", agency="서울교통공사")  # 자유 키워드 + 수동 기관 필터
get_project_budget("신안산선")                  # 연도별 예산/집행 시계열
get_rail_notices("7호선 청라연장")               # 관보고시 현황
get_rail_progress("GTX-A")                     # 공정률%(Playwright)
get_rail_project_status("GTX-A")               # 4개 통합(일부 실패해도 나머지 반환)
```

**노선 프리셋 + 키워드**: `query`는 프리셋 별칭(GTX-A/B/C, 신안산선, 7호선 청라연장,
1호선 검단연장, 별내선, 서해선, **9호선 연장** 등)이면 여러 표기를 함께 검색하고, 아니면
입력 자체를 키워드로 검색한다(`sources/rail_lines.py`). 미수록 노선은 자유 키워드로 조회.

**기관 필터**: 숫자 노선명("9호선")은 도로 노선번호(국도79호선·소로2-9호선 등)에 부분일치로
걸리는 노이즈가 심하다. `agency`(예: `"서울교통공사"`)를 주면 발주/수요기관으로 걸러내고,
프리셋(예: "9호선 연장")은 기관 힌트를 내장해 자동 적용한다.

> **설정·확정 필요(needs-verification)** — 각 소스 실측 확인(2026-07):
> - **나라장터**(✅ 동작): data.go.kr '입찰공고정보서비스' 활용신청(키는 `MOLIT_API_KEY`와
>   공용 — `DATA_GO_KR_API_KEY` 미설정 시 폴백). 이 API는 **공고명/기관명 검색 파라미터를
>   서버에서 무시**하고 날짜범위(≤약 30일) 내 공사공고를 전량 방출하므로, 서버는 날짜를 15일
>   청크로 나눠 전량 수집 후 **공고명 부분일치로 클라이언트 필터**한다. 스캔량이 커서 첫 호출은
>   수십 초(이후 30분 캐시). `days`를 줄이면 빨라진다.
> - **열린재정**(✅ 동작): 데이터셋 **"세출/지출 예산편성현황(총지출)"** = 경로형 API
>   `TotalExpenditure5`를 세부사업명(`SACTV_NM`) 부분일치로 조회한다. 필수 파라미터
>   `FSCL_YY`(회계연도, 단일)라 **연도별 반복 호출로 시계열**을 만든다(기본 최근 5년).
>   금액 단위는 **천원**(코드가 억원으로 변환 제공). GTX-A 본선명 '수도권광역급행철도'가
>   B/C노선의 substring이라, 프리셋 `budget_keywords`에서 `=` 접두어로 **정확일치 격리**한다.
>   API명/필수코드/검색 파라미터명은 `OPEN_FISCAL_*` 환경변수로 덮어쓸 수 있다(기본값이 실측값).
> - **관보고시**(✅ 동작): 파일데이터가 **odcloud.kr 오픈API로 자동변환**된다. 데이터셋
>   'OpenAPI/미리보기' 탭의 `https://api.odcloud.kr/api/15114027/v1/uddi:...` URL을
>   `KRNA_NOTICE_URL_BASIC`에 넣는다(키 아님 — serviceKey는 코드가 첨부). 전량(≈827건) 받아
>   고시명 부분일치로 클라 필터, 종류(실시계획/기본계획 등)는 고시명에서 파생. 캐시 6시간.
>   **주의**: 이 DB는 **국가철도공단 재정사업** 고시라 **민자(BTO) 노선은 없다**(신안산선·
>   GTX-B/C·별내선 → 0건이 정상). 이들은 예산·발주·공정률로 판단.
> - **공정률**(✅ 동작): 공식 API 없음 → 국가철도공단 주요사업현황 HTML을 Playwright로 스크래핑
>   (사업별 아코디언 `li.news`에서 제목+공정률 추출, 월 단위). 사내망은 chromium 다운로드가
>   TLS MITM으로 막히므로 `channel="msedge"`(시스템 Edge)로 폴백 — `pip install playwright`만
>   하면 되고 `playwright install`은 불필요. 페이지 구조 변경 시 깨질 수 있음(미설치·실패 시
>   `{error}`, 서버 크래시 없음). 참고: 페이지 표기는 "수도권 광역급행철도 B/C노선"(GTX 문자 없음).

### 시계열 도구 사용법
스냅샷(현재가)만으로는 "올랐나/빠졌나"를 못 본다. 아래 3개는 **추이**를 준다.

```python
get_price_history("005930", "1y")            # 국내 주식 1년 주봉 + 수익률/MDD/변동성
get_price_history("^GSPC", "6mo")            # 해외 지수
get_price_history("KRW=X", "1y")             # 환율도 같은 tool
get_price_history("BTC-USD", "1y")           # 크립토는 야후 심볼로
get_macro_series("기준금리", 36)              # 최근 36개월 기준금리 추이
get_realty_price_index("서울", "매매", "아파트")  # 서울 아파트 매매가격지수 추이
```

**`get_price_history`** — 국내 6자리 코드는 네이버 일별시세(외국인소진율 포함),
그 외는 Yahoo chart. 네이버 실패 시 Yahoo `.KS`→`.KQ`로 강등한다. `period`는
`5d/1mo/3mo/6mo/ytd/1y/2y/5y/10y/max`, `interval`은 `1d/1wk/1mo`이며 미지정 시
period에 맞춰 자동 선택해 **관측 수를 50~120점으로 유지**한다(1y→주봉). `stats`에
기간수익률·최고/최저·고점대비(%)·최대낙폭(MDD)·연율화 변동성이 들어온다.
현재가만 필요하면 응답이 훨씬 짧은 `get_stock_price`를 쓴다.

**`get_macro_series`** — 프리셋: `기준금리`, `콜금리`, `CD금리`, `국고채3년`,
`국고채10년`, `소비자물가`, `M2`, `가계신용`. 프리셋에 없으면
`"통계표코드/항목코드/주기"`(예: `"722Y001/0101000/M"`)로 직접 지정한다.
반환에 `changes`(절대 변화)와 `changes_pct`(변화율)가 **둘 다** 들어온다 —
금리는 `changes`(%p)로 읽어야 한다(기준금리 2.50→2.75는 "+10%"가 아니라 "+0.25%p").

**`get_realty_price_index`** — 실거래가는 단지·평형 편차가 커서 시장 방향을 보기
어렵다. 지수와 함께 봐야 해석이 된다.

| source | 통계표 | 지역 | 최신성 | 기준월 |
|---|---|---|---|---|
| `부동산원`(기본) | 901Y113(매매)/901Y114(전세) | 시도 24개 | 공표 지연(수개월) | 2025.03=100 |
| `kb` | 901Y062/901Y063 | 전국·서울만 | 빠름 | 2026.01=100 |

> 기준월·표본이 달라 **두 지수의 수치를 직접 비교하면 안 된다**(변화율로 비교).
> 시군구(강남구 등) 단위 지수는 ECOS에 없다 — `get_apt_trade_summary`(실거래)를 쓴다.
> 부동산원 통계표는 유형→지역 **2차원**이라 순서가 바뀌면 빈 결과(INFO-200)가 된다.

### 포트폴리오 점검 도구 사용법

**주식 심화**: `search_stock_code`로 이름→코드를 찾고, `get_stock_valuation`(밸류에이션,
키 불필요·네이버)과 `get_dart_disclosures`(공시 리스크: 유상증자·CB·감사보고서·최대주주변경
등)를 함께 본다. DART 상장사 인덱스는 최초 호출 시 전체 목록(ZIP, 수 MB)을 받아 24시간
캐시하므로 **첫 호출만 수 초** 걸린다.

**거시경제**: `get_macro_indicators()`는 ECOS **100대 통계지표**를 키워드로 필터한다
(기본: 기준금리/국고채/CD/콜금리/소비자물가/M2/가계신용/원달러/경제성장). `keywords=[]`로
전체 100개를 볼 수 있다. ⚠️ ECOS 응답 필드명(`KEYSTAT_NAME` 등)은 문서 기준 구현이므로
첫 실호출에서 확인 필요.

**포트폴리오 스냅샷** (`get_portfolio_snapshot`): 계좌 연동 없이 로컬 파일로 보유자산을
정의하면 기존 시세 Tool들로 병렬 평가해 손익·자산배분을 계산한다.

`portfolio.json` 예시(**개인 자산 정보 — 커밋 금지**, .gitignore 등록됨):
```json
{
  "base_currency": "KRW",
  "holdings": [
    {"type": "stock",  "ticker": "005930", "quantity": 10, "avg_price": 68000},
    {"type": "stock",  "ticker": "AAPL",   "quantity": 3,  "avg_price": 180},
    {"type": "etf",    "ticker": "381180", "quantity": 5,  "avg_price": 15200},
    {"type": "crypto", "ticker": "BTC", "quote": "KRW", "quantity": 0.05, "avg_price": 92000000},
    {"type": "apt", "region": "강남구", "complex": "래미안", "quantity": 1,
     "avg_price": 250000, "pyeong": 25, "months": 6}
  ],
  "cash": {"KRW": 5000000, "USD": 1000}
}
```
- `avg_price` 단위: 주식/ETF/크립토는 **거래 통화 기준 1단위 가격**(미국 주식은 USD —
  환율은 자동으로 1회 조회해 원화 환산), `apt`는 **만원 단위 호당 총 매입가**.
- `apt` 평가는 `get_apt_trade_summary`의 최근 N개월(기본 6) 단지평균 평당가 × `pyeong`
  기반 **추정치**다. `complex`는 실거래 단지명 부분일치로 매칭된다.
- 개별 종목 시세 실패는 해당 보유분 `price_error`로만 남고 나머지는 계속 평가된다
  (`errors` 배열과 총계 제외로 확인).
- YAML(`.yml`/`.yaml`)로 쓰려면 `pip install pyyaml`.

### 미국 종목 밸류에이션·펀더멘털 사용법

무료·키불필요 소스만 쓴다(Yahoo / SEC EDGAR / FRED). **SEC 계열 3종만
`SEC_USER_AGENT`가 필요**하다.

| 질문 | Tool | 소스 | 필요 설정 |
|---|---|---|---|
| 비싼가(밸류) | `get_global_valuation` | Yahoo quoteSummary | 없음 |
| 대안 대비 비싼가 | `compare_valuation` | 국내+해외 자동 라우팅 | 없음 |
| 회계로 이익을 부풀렸나 | `get_implied_useful_life` | SEC XBRL | `SEC_USER_AGENT` |
| 실제로 얼마 쓰고 있나 | `get_capex_series` | SEC XBRL | `SEC_USER_AGENT` |
| 수주잔고는 | `get_rpo_backlog` | SEC XBRL | `SEC_USER_AGENT` |
| 그 밖의 재무 항목 | `get_sec_fundamentals` | SEC XBRL | `SEC_USER_AGENT` |
| 돈줄이 조이나 | `get_credit_spreads` | FRED | 없음 |

```python
# 밸류 4축 비교 — 6자리 코드는 네이버, 그 외는 Yahoo로 자동 라우팅
compare_valuation(["NVDA", "AVGO", "MU", "000660"])
# → rows[].currency가 USD/KRW로 갈리므로 market_cap 절대비교는 하지 말 것
#    (필요하면 normalize_krw=True로 market_cap_krw 필드를 추가로 받는다)

get_implied_useful_life("AMZN")     # flag: extended | shortened | stable
get_capex_series("GOOGL", 8)        # capex_derived=true면 YTD 누적 차분값
get_credit_spreads()                # percentile_1y/5y와 함께 읽을 것
```

읽을 때 반드시 지킬 것:
- **적자 기업의 `trailing_pe`/`peg`가 `null`인 것은 정상**이다(오류 아님).
- 국내 종목은 PSR/PEG/ROE·마진을 소스가 주지 않아 `null`이며, 그 사유가 행
  `errors[]`에 남는다. **임의로 계산해 채우지 않는다**(정의가 달라 비교가 왜곡됨).
- `get_capex_series`의 `*_derived=true`는 분기 단독 공시가 없어 **YTD 누적 차분**으로
  만든 값이라는 뜻이다. 앞 분기 누적이 없으면 아예 값을 만들지 않는다.
- `get_sec_fundamentals`의 `fy`/`fp`는 SEC 원본 라벨이라 **'그 사실의 기간'이 아니라
  '그 사실이 실린 보고서'의 회계연도/분기**다. 기간 판단은 `start`/`end`로 한다.
- `get_credit_spreads`의 `change_1m/3m`은 비율(%)이 아니라 **절대차(%p)**다.

### SEC EDGAR 설정

SEC는 `"이름 이메일"` 형식의 User-Agent를 요구하며 없으면 **403**을 준다.
미설정 시 서버는 403을 그대로 던지지 않고 조치 안내를 `errors[]`로 돌려준다.

```bash
# .env
SEC_USER_AGENT="Hong Gildong hong@example.com"
```
요청은 초당 8건으로 자동 제한한다(SEC 하드리밋 10 req/s에서 20% 마진).

## 비기능
- 메모리 TTL 캐시 30초(동일 키 중복 호출 방지). 공사현황은 길게(입찰 30분/고시·공정률 6시간/예산 1일),
  해외 밸류에이션 15분 / FRED 6시간 / SEC 공시 24시간
- SEC·FRED 응답은 **파일 캐시**도 함께 쓴다(MCP는 stdio라 프로세스가 자주 재시작된다).
  위치는 `FINANCE_MCP_CACHE_DIR` > `%LOCALAPPDATA%\finance-mcp\cache` > `~/.cache/finance-mcp`
- 실패·부분실패 응답은 캐시하지 않는다(부분실패는 60초만 — 재시도 폭주 방지)
- 호출당 5초 타임아웃, 1순위 1회 재시도 후 강등. 신규 해외 소스는 10초·3회 지수백오프
  (429/503은 `Retry-After` 우선). **4xx는 재시도하지 않는다**(404 = 미공시 태그는 영구 사실)
- 전 Tool 예외 포착 → fallback 또는 `errors[]` 반환(서버 크래시 없음)
- 로그는 stderr만(stdout은 MCP 전용)

## 테스트
```bash
.venv\Scripts\python -m pytest -q
```
HTTP는 `respx`로 모킹한다(네트워크 불필요).

## 비대상 (Non-Goals)
매매/계좌 연동, 실시간 스트리밍, DB 영속화(계좌·거래내역), 차트 이미지 생성.
(SEC/FRED **HTTP 응답 파일 캐시**는 DB 영속화가 아니라 재기동 시 재다운로드를 막는
최적화이며, 지워도 동작에 영향이 없다.)

미국 종목 관련으로 **의도적으로 구현하지 않은 것** — MCP로 검증 불가하므로
WebSearch로만 처리하고, 리포트에는 반드시 출처를 병기한다:

| 항목 | 이유 |
|---|---|
| DRAM 현물·계약가 | TrendForce/DRAMeXchange 전량 유료. 무료 소스 없음(**영구 제외**) |
| 애널리스트 컨센서스 목표주가 | 무료 소스 신뢰도 미달 |
| 10-Q 서술형 각주 원문 파싱 | XBRL로 안 잡힘 → `get_implied_useful_life` 역산 프록시로 대체. 원문이 필요하면 EDGAR 파일링을 직접 열 것 |

어떤 Tool도 값을 **추정·보간하지 않는다**. 모르면 `null` + `errors[]`다.
