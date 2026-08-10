"""전 테스트 공통 픽스처.

- 캐시(메모리/디스크)와 HTTP 클라이언트를 테스트마다 초기화한다.
- 디스크 캐시는 tmp_path로 격리해 사용자 홈/LOCALAPPDATA를 오염시키지 않는다.
"""
from __future__ import annotations

import pytest

from core import cache, diskcache, http


@pytest.fixture(autouse=True)
async def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_MCP_CACHE_DIR", str(tmp_path / "cache"))
    cache.clear()
    diskcache.clear()
    await http.aclose()
    yield
    cache.clear()
    diskcache.clear()
    await http.aclose()
