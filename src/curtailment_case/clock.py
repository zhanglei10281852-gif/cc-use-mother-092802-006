"""时钟抽象:服务内所有 recorded_at/created_at 都取自注入的时钟。

测试与演示使用 FixedClock,可手动推进,从而验证"迟到数据""月末后到达"
等行为而不依赖真实时间。
"""

from __future__ import annotations

import time


class SystemClock:
    def now(self) -> int:
        return int(time.time())


class FixedClock:
    """固定时钟:now() 恒定,直到 set()/advance() 推进。"""

    def __init__(self, start: int):
        self._now = int(start)

    def now(self) -> int:
        return self._now

    def set(self, ts: int) -> None:
        self._now = int(ts)

    def advance(self, seconds: int) -> int:
        self._now += int(seconds)
        return self._now
