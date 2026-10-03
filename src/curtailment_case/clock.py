"""时钟抽象：生产用系统时钟，测试用固定时钟。

所有 recorded_at / opened_at / confirmed_at 等事务时间均取自注入的时钟，
使“迟到数据”“复核结论变更”等场景可在测试中精确重演。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .timeutil import parse_ts


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc).replace(microsecond=0)


class FixedClock:
    """固定时钟：now() 恒定返回设定时刻，可显式推进。"""

    def __init__(self, now: str | datetime):
        self._now = parse_ts(now)

    def now(self) -> datetime:
        return self._now

    def set(self, now: str | datetime) -> None:
        self._now = parse_ts(now)

    def advance(self, **kwargs) -> datetime:
        self._now = self._now + timedelta(**kwargs)
        return self._now
