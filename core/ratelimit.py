"""요청 속도 제한기.

SEC EDGAR는 "10 requests/second"를 하드 리밋으로 명시하고 초과 시 IP를 차단한다.
토큰버킷은 버스트를 허용해 슬라이딩 윈도우를 순간적으로 넘길 수 있으므로,
호출 간격을 균등하게 벌리는 고정간격 스케줄러를 쓴다.
"""
from __future__ import annotations

import asyncio
import time


class RateLimiter:
    """초당 rate_per_sec건으로 호출 간격을 균등 분산한다(버스트 없음).

    asyncio.gather로 N건을 동시에 띄워도 실제 요청은 interval 간격으로 흘러나간다.
    락을 잡은 채로 대기하므로 대기자들이 줄을 서서 순차 진입한다.
    """

    def __init__(self, rate_per_sec: float):
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be positive")
        self._interval = 1.0 / rate_per_sec
        self._lock: asyncio.Lock | None = None
        self._loop = None
        self._next = 0.0

    def _get_lock(self) -> asyncio.Lock:
        """실행 중인 이벤트루프에 묶인 락을 돌려준다.

        asyncio.Lock은 첫 사용 시점의 루프에 바인딩되고 다른 루프에서 쓰면
        RuntimeError를 낸다. 모듈 전역 리미터를 테스트(테스트마다 새 루프)와
        운영에서 함께 쓰기 위해 루프가 바뀌면 락을 새로 만든다.
        """
        loop = asyncio.get_running_loop()
        if self._lock is None or self._loop is not loop:
            self._loop = loop
            self._lock = asyncio.Lock()
            self._next = 0.0
        return self._lock

    async def acquire(self) -> None:
        async with self._get_lock():
            wait = self._next - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            # 오래 쉰 뒤에는 과거 시각이 누적되지 않도록 현재 시각을 기준으로 잡는다.
            self._next = max(time.monotonic(), self._next) + self._interval
