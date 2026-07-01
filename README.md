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
| `get_apt_trade(region, deal_ym)` | 아파트 매매 실거래가(평수·평당가 포함) | `"강남구"`, `"2024-06"` |
| `get_apt_trade_summary(region, deal_ym, months=1)` | 단지별 평균 평당가 집계 | `"강남구"`, `"2024-06"`, `6` |
| `get_apt_rent(region, deal_ym)` | 아파트 전월세 실거래가 | `"강남구"`, `"2024-06"` |
| `get_jeonse_ratio(region, deal_ym, months=1)` | 단지별 전세가율 집계 | `"강남구"`, `"2024-06"`, `6` |
| `get_construction_bids(query, biz="공사", days=30, agency=None)` | 나라장터 입찰공고(발주·착공) | `"GTX-A"`, `"9호선 연장"` |
| `get_project_budget(query, year=None)` | 열린재정 재정사업 예산·집행(예타·재정) | `"신안산선"` |
| `get_rail_notices(query, kind="기본")` | 국가철도공단 관보고시(고시·인허가) | `"7호선 청라연장"` |
| `get_rail_progress(query)` | 국가철도공단 공정률(진행현황, Playwright) | `"GTX-A"` |
| `get_rail_project_status(query)` | 한 노선의 예산·발주·고시·공정률 통합 스냅샷 | `"GTX-A"` |

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

### 부동산 실거래가 사용법
1. [공공데이터포털](https://www.data.go.kr)에서 **"국토교통부_아파트 매매 실거래가 자료"**,
   **"국토교통부_아파트 전월세 실거래가 자료"** 활용신청(무료).
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
> - **열린재정**(키 OK, SERVICE명 필요): 열린재정은 API명을 **`SERVICE` 쿼리 파라미터**로 받고
>   응답을 JSON 문자열로 이중 인코딩한다(코드가 처리). 로그인 후 데이터셋 'OPEN API 탭'의
>   요청인자에서 **실제 SERVICE명·사업명 검색 파라미터명**을 확인해 `OPEN_FISCAL_API_NAME`/
>   `OPEN_FISCAL_KW_PARAM`에 넣어야 한다(미교정 시 `ERROR-310` → fallback).
> - **관보고시**(엔드포인트 URL 필요): 파일데이터가 **odcloud.kr 오픈API로 자동변환**된다.
>   데이터셋 'OpenAPI/미리보기' 탭의 `https://api.odcloud.kr/api/15114027/v1/uddi:...` URL을
>   `KRNA_NOTICE_URL_BASIC`에 넣는다(키 아님 — serviceKey는 코드가 첨부). 캐시 6시간.
> - **공정률**(✅ 동작): 공식 API 없음 → 국가철도공단 주요사업현황 HTML을 Playwright로 스크래핑
>   (사업별 아코디언 `li.news`에서 제목+공정률 추출, 월 단위). 사내망은 chromium 다운로드가
>   TLS MITM으로 막히므로 `channel="msedge"`(시스템 Edge)로 폴백 — `pip install playwright`만
>   하면 되고 `playwright install`은 불필요. 페이지 구조 변경 시 깨질 수 있음(미설치·실패 시
>   `{error}`, 서버 크래시 없음). 참고: 페이지 표기는 "수도권 광역급행철도 B/C노선"(GTX 문자 없음).

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
