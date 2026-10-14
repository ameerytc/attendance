"""School-local date/time helpers. Timestamps in the DB default to UTC
(datetime('now')); *dates that define a school day* are school-local."""

from datetime import date, datetime
from zoneinfo import ZoneInfo


def school_now(settings) -> datetime:
    """Naive datetime in the school's timezone."""
    tz = (settings["timezone"] if settings else None) or "Africa/Lagos"
    try:
        return datetime.now(ZoneInfo(tz)).replace(tzinfo=None)
    except Exception:  # unknown tz name -> fall back safely
        return datetime.now(ZoneInfo("Africa/Lagos")).replace(tzinfo=None)


def school_today(settings) -> str:
    return school_now(settings).strftime("%Y-%m-%d")


def parse_date(value):
    """ISO date string -> date, or None if blank/invalid."""
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        return None
