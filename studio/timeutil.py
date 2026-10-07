"""Conversions between stored naive-UTC datetimes and the org's local time."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from .db import settings, utcnow


def to_local(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC).astimezone(settings().timezone)


def local_now() -> datetime:
    return to_local(utcnow())


def parse_local(value: str | None) -> datetime | None:
    """'2026-10-14T18:00' (local, or with an explicit offset) -> naive UTC."""
    if not value or not value.strip():
        return None
    dt = datetime.fromisoformat(value.strip())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=settings().timezone)
    return dt.astimezone(UTC).replace(tzinfo=None)


def fmt_local(dt: datetime | None, pattern: str = "%a %b %-d, %-I:%M %p") -> str:
    local = to_local(dt)
    return local.strftime(pattern) if local else ""


def input_value(dt: datetime | None) -> str:
    """Value for an <input type="datetime-local">."""
    local = to_local(dt)
    return local.strftime("%Y-%m-%dT%H:%M") if local else ""


def cycle_start(day: date) -> date:
    """First day of the batch cycle containing `day`."""
    s = settings()
    offset = (day - s.batch_anchor).days % s.batch_cycle_days
    return day - timedelta(days=offset)


def local_day_bounds_utc(day: date) -> tuple[datetime, datetime]:
    tz = settings().timezone
    start = datetime.combine(day, datetime.min.time(), tzinfo=tz)
    end = start + timedelta(days=1)
    return (
        start.astimezone(UTC).replace(tzinfo=None),
        end.astimezone(UTC).replace(tzinfo=None),
    )
