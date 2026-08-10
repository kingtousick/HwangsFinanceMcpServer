"""HTTP 응답 파일 캐시 (SEC/FRED처럼 하루 단위로만 갱신되는 소스용).

MCP 서버는 stdio로 뜨고 지므로 메모리 캐시는 프로세스가 죽으면 사라진다.
SEC의 company_tickers.json(약 1MB)이나 companyconcept 응답을 재시작마다
다시 받는 것을 막으려고 파일로도 남긴다.

- 캐시는 정합성 요소가 아니라 최적화다. IO 예외는 전부 삼키고 미스로 처리한다.
- TTL은 파일에 저장하지 않는다. 읽는 쪽이 stored_at으로 판단하므로 코드에서
  TTL 정책을 바꾸면 기존 파일에도 즉시 반영된다.
- 저장소 디렉터리 안에는 두지 않는다(.gitignore 오염, 다중 클론 충돌).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import pathlib
import time

logger = logging.getLogger("finance-mcp")

_ENV = "FINANCE_MCP_CACHE_DIR"


def cache_dir() -> pathlib.Path:
    """캐시 루트. 환경변수 > %LOCALAPPDATA% > ~/.cache 순.

    ※ lru_cache로 감싸지 말 것 — 테스트가 monkeypatch.setenv로 경로를 바꾼다.
    """
    env = os.environ.get(_ENV, "").strip()
    if env:
        return pathlib.Path(env)
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        return pathlib.Path(local) / "finance-mcp" / "cache"
    xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
    base = pathlib.Path(xdg) if xdg else pathlib.Path.home() / ".cache"
    return base / "finance-mcp"


def _path(key: str) -> pathlib.Path:
    """키 → 파일 경로. 앞 2자로 샤딩해 한 디렉터리에 파일이 몰리는 것을 막는다."""
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return cache_dir() / h[:2] / f"{h}.json"


def _read(key: str, ttl: float) -> dict | None:
    p = _path(key)
    try:
        with p.open("r", encoding="utf-8") as f:
            env = json.load(f)
    except (OSError, ValueError):
        return None
    stored_at = env.get("stored_at")
    if not isinstance(stored_at, (int, float)) or time.time() - stored_at >= ttl:
        return None
    data = env.get("data")
    return data if isinstance(data, dict) else None


def _write(key: str, data: dict) -> None:
    p = _path(key)
    env = {"key": key, "stored_at": time.time(), "data": data}
    # 같은 키를 동시에 쓰면 마지막 승자. os.replace가 원자적이라 별도 락은 불필요.
    tmp = p.with_suffix(f".{os.getpid()}.tmp")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(env, f, ensure_ascii=False)
        os.replace(tmp, p)
    except (OSError, ValueError, TypeError) as e:  # 직렬화 불가 객체 포함
        logger.warning("diskcache write failed key=%s err=%s", key, e)
        try:
            tmp.unlink(missing_ok=True)  # 반쯤 쓰인 임시 파일 정리
        except OSError:
            pass


async def get(key: str, ttl: float) -> dict | None:
    """ttl 이내에 저장된 값이 있으면 반환, 없으면 None. IO는 스레드로 넘긴다."""
    try:
        return await asyncio.to_thread(_read, key, ttl)
    except Exception as e:  # noqa: BLE001 - 캐시 실패는 미스로 간주
        logger.warning("diskcache read failed key=%s err=%s", key, e)
        return None


async def put(key: str, data: dict) -> None:
    """값을 저장한다. 실패해도 조용히 넘어간다."""
    try:
        await asyncio.to_thread(_write, key, data)
    except Exception as e:  # noqa: BLE001
        logger.warning("diskcache put failed key=%s err=%s", key, e)


def purge(older_than: float = 7 * 86400) -> int:
    """older_than초보다 오래된 캐시 파일을 지운다. 지운 개수를 반환."""
    root = cache_dir()
    cutoff = time.time() - older_than
    n = 0
    try:
        for p in root.glob("*/*.json"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    n += 1
            except OSError:
                continue
    except OSError:
        return n
    return n


def clear() -> None:
    """테스트용: 캐시 파일 전부 삭제."""
    root = cache_dir()
    try:
        for p in root.glob("*/*.json"):
            try:
                p.unlink()
            except OSError:
                continue
    except OSError:
        pass
