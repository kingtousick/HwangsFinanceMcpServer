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
`.env` 또는 config의 `env`로 주입. 없으면 해당 폴백 소스는 건너뛴다.
- `EXIM_API_KEY`: 한국수출입은행 환율 API(가정용 PC 환율 폴백).
- `FINNHUB_KEY`: 미국 주식 폴백(예약, 현재 미사용).

## 비기능
- 메모리 TTL 캐시 30초(동일 키 중복 호출 방지)
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
