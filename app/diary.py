"""Facts about a user's diary that the API and the reminder scheduler both need."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import usage
from app.models import Meal, User

STREAK_LOOKBACK_DAYS = 400


def today(user: User) -> date:
    return datetime.now(ZoneInfo(user.timezone)).date()


def local_day(dt: datetime, tz: str) -> date:
    """A stored (naive UTC) time as the user's calendar day."""
    return dt.replace(tzinfo=UTC).astimezone(ZoneInfo(tz)).date()


def logged_days(db: Session, user: User, since: date) -> set[date]:
    start, _ = usage.day_bounds_utc(user.timezone, since)
    return {
        local_day(dt, user.timezone)
        for dt in db.scalars(select(Meal.eaten_at).where(Meal.user_id == user.id, Meal.eaten_at >= start))
    }


def streak(db: Session, user: User, on: date) -> tuple[int, bool]:
    """Days in a row with at least one meal, ending `on`, or the day before if `on` is
    still empty (the streak isn't lost until midnight). Also: is `on` logged yet."""
    logged = logged_days(db, user, on - timedelta(days=STREAK_LOOKBACK_DAYS))
    day = on if on in logged else on - timedelta(days=1)
    n = 0
    while day in logged:
        n += 1
        day -= timedelta(days=1)
    return n, on in logged
