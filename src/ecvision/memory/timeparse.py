"""Small, deterministic Chinese/English time-window parser.

This is intentionally not a general NLP stack. It extracts the handful of
temporal expressions an operator actually uses in an incident query, converts
them to a half-open local-time window, and falls back to the last 24 hours when
nothing matches. The retriever converts the window to UTC before hitting SQLite.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ecvision.memory.models import TimeWindow

DEFAULT_TIMEZONE = "Asia/Shanghai"

_ISO_DATE = re.compile(
    r"(?P<year>\d{4})-(?P<month>\d{1,2})-(?P<day>\d{1,2})"
    r"(?:[ T](?P<hour>\d{1,2}):(?P<minute>\d{2}))?"
)
_CN_DATE = re.compile(r"(?P<month>\d{1,2})月(?P<day>\d{1,2})日")
_RELATIVE = re.compile(
    r"(?:过去|最近|last)\s*(?P<count>\d+)\s*(?P<unit>分钟|分|小时|钟头|天|minutes?|mins?|hours?|hrs?|days?)",
    re.IGNORECASE,
)
_DAY = re.compile(
    r"(?P<day>今天|昨天|前天|today|yesterday)"
    r"\s*(?P<period>凌晨|上午|中午|下午|傍晚|晚上|morning|afternoon|evening|night)?",
    re.IGNORECASE,
)
_PERIOD_ONLY = re.compile(r"^(?P<period>凌晨|上午|中午|下午|傍晚|晚上)")
_PERIOD_RANGES: dict[str, tuple[int, int]] = {
    "凌晨": (0, 6),
    "上午": (6, 12),
    "morning": (6, 12),
    "中午": (11, 14),
    "下午": (12, 18),
    "afternoon": (12, 18),
    "傍晚": (17, 20),
    "evening": (18, 24),
    "晚上": (18, 24),
    "night": (18, 24),
}


def resolve_timezone(name: str) -> timezone | ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        # Windows may not ship the IANA database. Asia/Shanghai has no DST, so a
        # fixed offset is a safe, explicit fallback for this project.
        if name == DEFAULT_TIMEZONE:
            return timezone(timedelta(hours=8), name="Asia/Shanghai")
        return timezone.utc


def parse_time_window(
    text: str,
    now: datetime,
    *,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> TimeWindow:
    if now.tzinfo is None:
        raise ValueError("now must include a timezone")
    zone = resolve_timezone(timezone_name)
    local_now = now.astimezone(zone)
    source = text.strip()
    if not source:
        return _fallback(local_now)

    match = _ISO_DATE.search(source)
    if match:
        start = datetime(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
            int(match.group("hour") or 0),
            int(match.group("minute") or 0),
            tzinfo=zone,
        )
        end = start + (timedelta(hours=1) if match.group("hour") else timedelta(days=1))
        return TimeWindow(
            start=start,
            end=end,
            label=match.group(0).strip(),
            source="explicit",
        )

    match = _CN_DATE.search(source)
    if match:
        start = datetime(
            local_now.year,
            int(match.group("month")),
            int(match.group("day")),
            tzinfo=zone,
        )
        return TimeWindow(
            start=start,
            end=start + timedelta(days=1),
            label=match.group(0),
            source="explicit",
        )

    match = _RELATIVE.search(source)
    if match:
        count = max(1, int(match.group("count")))
        unit = match.group("unit").lower()
        if unit in {"分钟", "分", "minute", "minutes", "min", "mins"}:
            delta = timedelta(minutes=count)
        elif unit in {"小时", "钟头", "hour", "hours", "hr", "hrs"}:
            delta = timedelta(hours=count)
        else:
            delta = timedelta(days=count)
        return TimeWindow(
            start=local_now - delta,
            end=local_now,
            label=f"过去 {count} {unit}",
            source="relative",
        )

    match = _DAY.search(source)
    if match:
        day_word = match.group("day").lower()
        offset = {"今天": 0, "today": 0, "昨天": -1, "yesterday": -1, "前天": -2}.get(day_word, 0)
        base: date = local_now.date() + timedelta(days=offset)
        period = (match.group("period") or "").strip().lower()
        start_hour, end_hour = _PERIOD_RANGES.get(period, (0, 24))
        return TimeWindow(
            start=datetime.combine(base, time(start_hour), tzinfo=zone),
            end=datetime.combine(base, time(0), tzinfo=zone) + timedelta(hours=end_hour),
            label=match.group(0).strip(),
            source="relative",
        )

    match = _PERIOD_ONLY.search(source.strip())
    if match:
        period = match.group("period").lower()
        start_hour, end_hour = _PERIOD_RANGES.get(period, (0, 24))
        return TimeWindow(
            start=datetime.combine(local_now.date(), time(start_hour), tzinfo=zone),
            end=datetime.combine(local_now.date(), time(0), tzinfo=zone)
            + timedelta(hours=end_hour),
            label=period,
            source="relative",
        )

    return _fallback(local_now)


def _fallback(local_now: datetime) -> TimeWindow:
    return TimeWindow(
        start=local_now - timedelta(hours=24),
        end=local_now,
        label="最近 24 小时",
        source="fallback",
    )
