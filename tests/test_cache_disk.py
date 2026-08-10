"""core/cache.py 확장분(ttl_partial/disk)과 core/diskcache.py 단위 테스트."""
from __future__ import annotations

from core import cache, diskcache


def _fetcher(payload, calls: list):
    async def fetch():
        calls.append(1)
        return payload
    return fetch


# ---------------------------------------------------------------- 기존 동작 보존

async def test_success_cached_and_reused():
    calls: list = []
    f = _fetcher({"name": "x", "value": 1}, calls)
    a = await cache.cached("k1", f, 60.0)
    b = await cache.cached("k1", f, 60.0)
    assert a == b
    assert len(calls) == 1


async def test_error_response_not_cached():
    calls: list = []
    f = _fetcher({"name": "x", "error": "boom"}, calls)
    await cache.cached("k2", f, 60.0)
    await cache.cached("k2", f, 60.0)
    assert len(calls) == 2  # 실패는 캐시하지 않으므로 매번 재호출


# ---------------------------------------------------------------- errors[] 규약

def test_is_partial_recognizes_both_conventions():
    assert cache.is_partial({"error": "x"}) is True
    assert cache.is_partial({"errors": [{"field": "pbr"}]}) is True
    assert cache.is_partial({"errors": []}) is False   # 빈 배열은 정상
    assert cache.is_partial({"value": 1}) is False
    assert cache.is_partial("not a dict") is True


async def test_partial_response_not_cached_by_default():
    """errors[]가 있는 부분실패는 ttl_partial 없이는 캐시되지 않는다."""
    calls: list = []
    f = _fetcher({"ticker": "AMZN", "series": [],
                  "errors": [{"field": "capex", "reason": "timeout"}]}, calls)
    await cache.cached("k3", f, 86400.0)
    await cache.cached("k3", f, 86400.0)
    assert len(calls) == 2


async def test_partial_response_uses_short_ttl():
    """부분실패는 ttl_partial 동안만 캐시되고 긴 ttl(24h)에 고착되지 않는다."""
    calls: list = []
    f = _fetcher({"series": [], "errors": [{"field": "capex"}]}, calls)

    await cache.cached("k4", f, 86400.0, ttl_partial=60.0)
    await cache.cached("k4", f, 86400.0, ttl_partial=60.0)
    assert len(calls) == 1  # 60초 안에는 재호출 안 함

    stored_at, data, effective_ttl = cache._cache["k4"]
    assert effective_ttl == 60.0  # 86400이 아니라 ttl_partial이 적용됐다

    # 61초 경과를 흉내 → 만료되어 재호출
    cache._cache["k4"] = (stored_at - 61.0, data, effective_ttl)
    await cache.cached("k4", f, 86400.0, ttl_partial=60.0)
    assert len(calls) == 2


# ---------------------------------------------------------------- 디스크 캐시

async def test_disk_roundtrip():
    await diskcache.put("dk1", {"a": 1})
    assert await diskcache.get("dk1", 60.0) == {"a": 1}


async def test_disk_ttl_expiry():
    await diskcache.put("dk2", {"a": 1})
    assert await diskcache.get("dk2", 0.0) is None   # ttl 0 → 즉시 만료


async def test_disk_miss_returns_none():
    assert await diskcache.get("never-written", 60.0) is None


async def test_cached_disk_survives_memory_clear():
    """메모리 캐시를 비워도 디스크에서 되살아나 fetch가 재호출되지 않는다."""
    calls: list = []
    f = _fetcher({"cik": "0000320193"}, calls)
    await cache.cached("dk3", f, 3600.0, disk=True)
    cache.clear()
    out = await cache.cached("dk3", f, 3600.0, disk=True)
    assert out == {"cik": "0000320193"}
    assert len(calls) == 1


async def test_partial_never_written_to_disk():
    calls: list = []
    f = _fetcher({"errors": [{"field": "x"}]}, calls)
    await cache.cached("dk4", f, 3600.0, ttl_partial=60.0, disk=True)
    assert await diskcache.get("dk4", 3600.0) is None


async def test_disk_isolated_to_tmp(tmp_path):
    """conftest가 FINANCE_MCP_CACHE_DIR를 tmp_path로 돌려놨는지 확인."""
    assert str(tmp_path) in str(diskcache.cache_dir())


async def test_disk_write_failure_is_silent(monkeypatch):
    """직렬화 불가 객체를 넣어도 예외가 나지 않고 미스로 처리된다."""
    await diskcache.put("dk5", {"bad": object()})
    assert await diskcache.get("dk5", 60.0) is None
