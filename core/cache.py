"""메모리 TTL 캐시 (설계서 §7, 기본 30초).

동일 키 중복 호출을 방지한다. 단일 프로세스/단일 이벤트루프 전제이므로
간단한 dict 기반으로 충분하다(DB 영속화는 Non-Goal).
"""
from __future__ import annotations

import time
from typing import Awaitable, Callable

TTL = 30.0

_cache: dict[str, tuple[float, dict]] = {}


async def cached(key: str, fetch: Callable[[], Awaitable[dict]], ttl: float = TTL) -> dict:
    """key가 ttl 이내에 캐시되어 있으면 캐시 반환, 아니면 fetch() 호출 후 저장.

    fetch가 error 응답(error 필드 포함)을 반환하면 캐시하지 않는다
    (실패를 30초간 고착시키지 않기 위함).
    """
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    data = await fetch()
    if isinstance(data, dict) and "error" not in data:
        _cache[key] = (now, data)
    return data


def clear() -> None:
    """테스트용: 캐시 비우기."""
    _cache.clear()
