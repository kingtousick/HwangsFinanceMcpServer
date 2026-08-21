"""공통 httpx 비동기 클라이언트와 재시도 헬퍼.

- 호출당 5초 타임아웃 (설계서 §7).
- 비공식 엔드포인트용 브라우저 User-Agent 기본 설정.
- get_json: 1회 재시도 후 예외 전파(상위에서 다음 소스로 강등).
- 로그는 stderr만 사용(stdout은 MCP 전용).

재시도 루프는 _request() 하나로 모았다. backoff/limiter/status_ok 는 기본값이
꺼진 상태(backoff=0.0, limiter=None, status_ok=None)라 기존 호출부의 동작은
바뀌지 않는다. SEC/야후/FRED 등 신규 소스만 이 인자들을 켜서 쓴다.
"""
from __future__ import annotations

import asyncio
import logging
import random
import ssl
import time

import httpx

from core.schema import scrub_secrets

logger = logging.getLogger("finance-mcp")


def _build_ssl_context() -> "ssl.SSLContext | bool":
    """OS 네이티브 트러스트 저장소를 사용하는 SSLContext.

    사내망 TLS 가로채기(MITM) 환경에서는 사내 루트 CA가 Windows 인증서
    저장소에만 있고 certifi 번들엔 없어 httpx 기본 검증이 실패한다. truststore로
    OS 저장소를 사용하면 검증을 유지하면서 사내 CA도 신뢰한다. truststore 미설치
    시 httpx 기본 검증(certifi)으로 폴백한다.
    """
    try:
        import truststore
        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        return ctx
    except Exception as e:  # noqa: BLE001
        logger.warning("truststore unavailable, using default verify: %s", e)
        return True

DEFAULT_TIMEOUT = 5.0
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 지수 백오프에 섞는 지터 비율(±25%). 동시 재시도가 같은 순간에 몰리는 것을 막는다.
_JITTER = 0.25
# Retry-After 헤더를 그대로 따르되 이 상한(초)을 넘기지 않는다.
_MAX_RETRY_AFTER = 30.0

_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    """프로세스 전역에서 재사용하는 AsyncClient."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT,
            headers={"User-Agent": BROWSER_UA},
            follow_redirects=True,
            verify=_build_ssl_context(),
        )
    return _client


async def aclose() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
        _client = None


# 재시도해도 결과가 같은 클라이언트 오류(4xx). 429/408만 예외로 재시도 대상.
_RETRYABLE_4XX = (408, 429)


def _is_permanent(exc: Exception) -> bool:
    """재시도가 무의미한 오류인지. 404(미공시 태그) 등을 즉시 포기하기 위함."""
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None)
    return isinstance(code, int) and 400 <= code < 500 and code not in _RETRYABLE_4XX


def _retry_delay(exc: Exception, backoff: float, attempt: int) -> float:
    """다음 재시도까지 대기할 초. 429/503의 Retry-After 헤더를 최우선으로 따른다."""
    resp = getattr(exc, "response", None)
    ra = getattr(resp, "headers", {}).get("retry-after") if resp is not None else None
    if ra:
        try:
            return min(float(ra), _MAX_RETRY_AFTER)
        except (TypeError, ValueError):
            pass  # HTTP-date 형식은 무시하고 지수 백오프로
    return backoff * (2 ** attempt) * (1 + random.uniform(-_JITTER, _JITTER))


def _identity(r: httpx.Response) -> httpx.Response:
    return r


async def _request(url: str, *, params: dict | None = None,
                   headers: dict | None = None, retries: int = 1,
                   timeout: float | None = None, backoff: float = 0.0,
                   limiter=None,
                   status_ok: tuple[int, ...] | None = None,
                   parse=_identity):
    """모든 GET의 공통 경로. retries회 재시도 후 최종 실패 시 예외 전파.

    timeout   : 이 호출만 별도 타임아웃(초). 미지정 시 클라이언트 기본 5초.
    backoff   : >0이면 시도 사이 backoff * 2**attempt 초 대기(±25% 지터).
                0.0(기본)이면 대기 없이 즉시 재시도 = 기존 동작.
    limiter   : acquire() 코루틴을 가진 객체(core.ratelimit.RateLimiter).
                None(기본)이면 속도 제한 없음.
    status_ok : raise_for_status를 건너뛸 상태코드. 야후 쿠키 프라이밍처럼
                본문이 아니라 Set-Cookie만 필요해 404가 정상인 경우에 쓴다.
    parse     : 응답 → 반환값 변환 함수. 재시도 루프 **안에서** 호출하므로
                JSON 파싱 실패(잘린 응답·HTML 에러페이지)도 재시도 대상이 된다.
    """
    client = get_client()
    last_exc: Exception | None = None
    kw = {} if timeout is None else {"timeout": timeout}
    for attempt in range(retries + 1):
        if limiter is not None:
            await limiter.acquire()
        t0 = time.perf_counter()
        try:
            r = await client.get(url, params=params, headers=headers, **kw)
            if not (status_ok and r.status_code in status_ok):
                r.raise_for_status()
            out = parse(r)
            logger.info("GET ok url=%s attempt=%d %.0fms",
                        url, attempt, (time.perf_counter() - t0) * 1000)
            return out
        except Exception as e:  # noqa: BLE001 - 의도적으로 모든 예외 포착해 강등
            last_exc = e
            # httpx 예외 메시지에는 쿼리스트링째 URL이 실린다 → 키 마스킹 후 기록.
            logger.warning("GET fail url=%s attempt=%d %.0fms err=%s",
                           url, attempt, (time.perf_counter() - t0) * 1000,
                           scrub_secrets(e))
            if _is_permanent(e):
                break  # 404/400 등은 재시도해도 같다
            if backoff and attempt < retries:
                await asyncio.sleep(_retry_delay(e, backoff, attempt))
    raise last_exc  # type: ignore[misc]


async def get_response(url: str, **kw) -> httpx.Response:
    """응답 객체 자체가 필요할 때(쿠키·헤더 확인용). 인자는 _request와 동일."""
    return await _request(url, **kw)


async def get_json(url: str, *, params: dict | None = None,
                   headers: dict | None = None, retries: int = 1,
                   timeout: float | None = None, **kw) -> dict | list:
    """GET 후 JSON 파싱. retries회 재시도. 최종 실패 시 예외 전파.

    timeout: 이 호출만 별도 타임아웃(초). 대용량 페이지네이션 응답용
             (미지정 시 클라이언트 기본 5초).
    """
    return await _request(url, params=params, headers=headers, retries=retries,
                          timeout=timeout, parse=lambda r: r.json(), **kw)


async def get_bytes(url: str, *, params: dict | None = None,
                    headers: dict | None = None, retries: int = 1,
                    timeout: float | None = None, **kw) -> bytes:
    """GET 후 바이너리 본문 반환(ZIP 등 파일 응답 API용). retries회 재시도, 최종 실패 시 예외.

    timeout: 이 호출만 별도 타임아웃(초). 대용량 파일 다운로드용(미지정 시 기본 5초).
    """
    return await _request(url, params=params, headers=headers, retries=retries,
                          timeout=timeout, parse=lambda r: r.content, **kw)


async def get_text(url: str, *, params: dict | None = None,
                   headers: dict | None = None, retries: int = 1, **kw) -> str:
    """GET 후 본문 텍스트 반환(XML/CSV 응답 API용). retries회 재시도, 최종 실패 시 예외."""
    return await _request(url, params=params, headers=headers, retries=retries,
                          parse=lambda r: r.text, **kw)
