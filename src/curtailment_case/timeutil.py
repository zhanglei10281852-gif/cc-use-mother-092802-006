"""统一时间轴工具：所有时间戳规范化为 UTC、秒级精度的 ISO 字符串。

SQLite 中按字符串存储即可正确按字典序比较（同一偏移量、同一精度）。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

_PERIOD_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


def parse_ts(value: str | datetime) -> datetime:
    """解析 ISO 8601 时间戳（支持 'Z' 后缀），统一为 UTC  aware datetime。"""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"无法解析时间戳: {value!r}") from exc
    else:
        raise ValueError(f"无法解析时间戳: {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0)


def fmt_ts(value: str | datetime) -> str:
    """格式化为规范存储形式，如 '2026-03-01T22:00:00+00:00'。"""
    return parse_ts(value).isoformat()


def hours_between(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 3600.0


def clip_interval(
    a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime
) -> tuple[datetime, datetime] | None:
    """半开区间 [start, end) 求交，不相交返回 None。"""
    start = max(a_start, b_start)
    end = min(a_end, b_end)
    return (start, end) if start < end else None


def period_window(period: str) -> tuple[datetime, datetime]:
    """结算周期 'YYYY-MM' -> [月初, 次月初)。"""
    match = _PERIOD_RE.match(period.strip())
    if not match:
        raise ValueError(f"结算周期格式应为 YYYY-MM: {period!r}")
    year, month = int(match.group(1)), int(match.group(2))
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    return start, end
