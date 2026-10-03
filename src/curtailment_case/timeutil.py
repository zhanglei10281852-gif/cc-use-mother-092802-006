"""统一时间轴工具:15 分钟结算时段,时间戳一律为带时区的 ISO-8601。"""

from __future__ import annotations

from datetime import datetime, timezone

BUCKET_SECONDS = 900  # 15 分钟一个结算时段
KWH_PER_MW_BUCKET = BUCKET_SECONDS / 3600.0 * 1000.0  # 1 MW 持续一个时段 = 250 kWh


def parse_ts(text: str) -> int:
    """ISO-8601(必须带时区偏移)-> epoch 秒。"""
    if not isinstance(text, str):
        raise ValueError(f"时间戳必须是字符串,收到 {type(text).__name__}")
    dt = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"时间戳缺少时区偏移: {text!r}")
    return int(dt.timestamp())


def iso(ts: int, tz: timezone = timezone.utc) -> str:
    """epoch 秒 -> ISO-8601 字符串。"""
    return datetime.fromtimestamp(int(ts), tz).isoformat()


def bucket_start(ts: int) -> int:
    """对齐到所在 15 分钟时段的起点。"""
    return int(ts) - (int(ts) % BUCKET_SECONDS)


def period_of(ts: int, tz: timezone = timezone.utc) -> str:
    """epoch 秒所属的结算周期 'YYYY-MM'(按服务时区)。"""
    dt = datetime.fromtimestamp(int(ts), tz)
    return f"{dt.year:04d}-{dt.month:02d}"


def period_bounds(period: str, tz: timezone = timezone.utc) -> tuple[int, int]:
    """结算周期 'YYYY-MM' -> [起始, 结束) epoch 秒。"""
    try:
        year_s, month_s = str(period).split("-")
        year, month = int(year_s), int(month_s)
        start = datetime(year, month, 1, tzinfo=tz)
    except ValueError as exc:
        raise ValueError(f"结算周期格式应为 'YYYY-MM',收到 {period!r}") from exc
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=tz)
    else:
        end = datetime(year, month + 1, 1, tzinfo=tz)
    return int(start.timestamp()), int(end.timestamp())
