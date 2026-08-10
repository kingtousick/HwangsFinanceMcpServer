"""메모리 TTL 캐시 (설계서 §7, 기본 30초).

동일 키 중복 호출을 방지한다. 단일 프로세스/단일 이벤트루프 전제이므로
간단한 dict 기반으로 충분하다(DB 영속화는 Non-Goal).

SEC/FRED처럼 갱신 주기가 하루 단위인 소스는 disk=True로 파일 캐시까지 쓴다
(MCP는 stdio라 프로세스가 자주 재시작된다). core/diskcache.py 참고.
"""
from __future__ import annotations

import time
from typing import Awaitable, Callable

from core import diskcache

TTL = 30.0

# key -> (저장시각, 데이터, 이 엔트리에 적용할 TTL)
_cache: dict[str, tuple[float, dict, float]] = {}


def is_partial(data) -> bool:
    """실패/부분실패 응답인지. 캐시 정책 판단에 쓴다.

    두 가지 실패 표기를 모두 인식한다.
    - 기존 규약: fail()이 만드는 최상위 "error" 필드
    - 신규 규약: 미국 밸류에이션/SEC 툴이 쓰는 비어있지 않은 "errors" 배열
    부분실패를 정상으로 오인해 장시간 캐시하면 일시 장애가 TTL 내내 고착된다.
    """
    if not isinstance(data, dict):
        return True
    return "error" in data or bool(data.get("errors"))


async def cached(key: str, fetch: Callable[[], Awaitable[dict]], ttl: float = TTL,
                 *, ttl_partial: float | None = None, disk: bool = False) -> dict:
    """key가 ttl 이내에 캐시되어 있으면 캐시 반환, 아니면 fetch() 호출 후 저장.

    fetch가 실패/부분실패 응답을 반환하면 캐시하지 않는다
    (실패를 TTL 내내 고착시키지 않기 위함).

    ttl_partial: 지정하면 부분실패 응답도 이 짧은 TTL(초) 동안만 메모리에 둔다.
                 오류가 나는 동안 호출이 폭주하는 것을 막되 곧 재시도되게 한다.
                 부분실패는 디스크에는 절대 쓰지 않는다.
    disk       : 메모리 미스 시 파일 캐시도 조회하고, 성공 응답은 파일에도 남긴다.
    """
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < hit[2]:
        return hit[1]
    if disk:
        d = await diskcache.get(key, ttl)
        if d is not None:
            _cache[key] = (now, d, ttl)
            return d
    data = await fetch()
    if isinstance(data, dict) and not is_partial(data):
        _cache[key] = (now, data, ttl)
        if disk:
            await diskcache.put(key, data)
    elif ttl_partial and isinstance(data, dict):
        _cache[key] = (now, data, ttl_partial)
    return data


def clear() -> None:
    """테스트용: 캐시 비우기."""
    _cache.clear()
