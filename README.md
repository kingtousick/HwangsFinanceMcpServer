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
| `get_apt_trade_summary(region, deal_ym, months=1)` | 단지별 평균 평당가 집계 | `"강남구"`, `"2024-06"`, `6` |
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
| `get_stock_valuation(ticker)` | PER/PBR/EPS/BPS/배당수익률/시가총액 | `"005930"` |
| `get_macro_indicators(keywords=None)` | 한은 ECOS 100대 통계지표(금리·물가·M2 등) | `["기준금리","가계신용"]` |
| `get_macro_series(indicator, periods=36)` | 거시지표 **시계열**(추이·변화율) | `"기준금리"`, `"국고채3년"` |
| `get_realty_price_index(region, kind, house_type, months, source)` | 주택 매매/전세 **가격지수 시계열** | `"서울"`, `"매매"`, `"아파트"` |
| `get_portfolio_snapshot(path=None)` | 로컬 파일 기반 보유자산 평가·손익·배분 | `"./portfolio.json"` |

### 티커 형식
- **국내 주식/ETF**: 6자리 코드 (`005930`, `381180`) → 네이버 polling
- **해외 주식/지수**: Yahoo 심볼 (`^GSPC`, `^IXIC`, `^SOX`, `AAPL`) → Yahoo chart API
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

## 소스 우선순위 (자동 강등)

| 데이터 | 1순위 | 2순위 | Fallback |
|---|---|---|---|
| 국내 지수 | 네이버 polling | — | Playwright |
| 국내 주식/ETF | 네이버 polling | — | — |
| 해외 주식/지수 | Yahoo(query1) | Yahoo(query2) | — |
| USD/KRW | Yahoo `KRW=X` | EXIM(키) | — |
| 크립토 | CoinGecko | 업비트(KRW, 도달 시) | — |

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
      "command": "C:\\Chamomile\\workspace\\agentHwang\\finaceMcp\\.venv\\Scripts\\python.exe",
      "args": ["C:\\Chamomile\\workspace\\agentHwang\\finaceMcp\\finance_server.py"],
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

## 비기능
- 메모리 TTL 캐시 30초(동일 키 중복 호출 방지). 공사현황은 길게(입찰 30분/고시·공정률 6시간/예산 1일)
- 호출당 5초 타임아웃, 1순위 1회 재시도 후 강등
- 전 Tool 예외 포착 → fallback 반환(서버 크래시 없음)
- 로그는 stderr만(stdout은 MCP 전용)

## 테스트
```bash
.venv\Scripts\python -m pytest -q
```
HTTP는 `respx`로 모킹한다(네트워크 불필요).

## 비대상 (Non-Goals)
매매/계좌 연동, 실시간 스트리밍, DB 영속화, 차트 이미지 생성.
