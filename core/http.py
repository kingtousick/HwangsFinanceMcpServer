"""공통 httpx 비동기 클라이언트와 재시도 헬퍼.

- 호출당 5초 타임아웃 (설계서 §7).
- 비공식 엔드포인트용 브라우저 User-Agent 기본 설정.
- get_json: 1회 재시도 후 예외 전파(상위에서 다음 소스로 강등).
- 로그는 stderr만 사용(stdout은 MCP 전용).
"""
from __future__ import annotations

import logging
import ssl
import time

import httpx

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


async def get_json(url: str, *, params: dict | None = None,
                   headers: dict | None = None, retries: int = 1,
                   timeout: float | None = None) -> dict | list:
    """GET 후 JSON 파싱. retries회 재시도. 최종 실패 시 예외 전파.

    timeout: 이 호출만 별도 타임아웃(초). 대용량 페이지네이션 응답용
             (미지정 시 클라이언트 기본 5초).
    """
    client = get_client()
    last_exc: Exception | None = None
    kw = {} if timeout is None else {"timeout": timeout}
    for attempt in range(retries + 1):
        t0 = time.perf_counter()
        try:
            r = await client.get(url, params=params, headers=headers, **kw)
            r.raise_for_status()
            data = r.json()
            logger.info("GET ok url=%s attempt=%d %.0fms",
                        url, attempt, (time.perf_counter() - t0) * 1000)
            return data
        except Exception as e:  # noqa: BLE001 - 의도적으로 모든 예외 포착해 강등
            last_exc = e
            logger.warning("GET fail url=%s attempt=%d %.0fms err=%s",
                           url, attempt, (time.perf_counter() - t0) * 1000, e)
    raise last_exc  # type: ignore[misc]


async def get_text(url: str, *, params: dict | None = None,
                   headers: dict | None = None, retries: int = 1) -> str:
    """GET 후 본문 텍스트 반환(XML 응답 API용). retries회 재시도, 최종 실패 시 예외."""
    client = get_client()
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        t0 = time.perf_counter()
        try:
            r = await client.get(url, params=params, headers=headers)
            r.raise_for_status()
            logger.info("GET ok url=%s attempt=%d %.0fms",
                        url, attempt, (time.perf_counter() - t0) * 1000)
            return r.text
        except Exception as e:  # noqa: BLE001
            last_exc = e
            logger.warning("GET fail url=%s attempt=%d %.0fms err=%s",
                           url, attempt, (time.perf_counter() - t0) * 1000, e)
    raise last_exc  # type: ignore[misc]
