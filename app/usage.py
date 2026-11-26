"""What the Claude API costs, per user, and the caps that keep it bounded.

Prices are Anthropic's list prices in USD per million tokens: an estimate of
the bill, not the bill itself (the Anthropic Console is the source of truth).
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import ApiCall, User

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Price:
    input: float
    output: float

    @property
    def cache_write(self) -> float:  # 5-minute cache writes cost 1.25x input
        return self.input * 1.25

    @property
    def cache_read(self) -> float:  # cache hits cost 0.1x input
        return self.input * 0.1


PRICES = {
    "claude-haiku-5-5": Price(0.10, 0.50),
    "claude-sonnet-5-5": Price(2.00, 10.00),
    "claude-sonnet-5": Price(2.00, 10.00),
    "claude-opus-5-5": Price(4.00, 20.00),
    "claude-opus-5": Price(5.00, 25.00),
}
# Unknown models are priced like the dearest one, so a cap still holds.
FALLBACK_PRICE = Price(5.00, 25.00)


def price_for(model: str) -> Price:
    matches = [key for key in PRICES if model == key or model.startswith(key + "-")]
    return PRICES[max(matches, key=len)] if matches else FALLBACK_PRICE


def cost_usd(model: str, input_tokens: int, output_tokens: int,
             cache_write_tokens: int = 0, cache_read_tokens: int = 0) -> float:
    p = price_for(model)
    return (
        input_tokens * p.input
        + output_tokens * p.output
        + cache_write_tokens * p.cache_write
        + cache_read_tokens * p.cache_read
    ) / 1_000_000


def record_call(db: Session, user: User, response=None, model: str = "", error: str | None = None) -> float:
    """Log one API call. Never raises: logging mustn't break the request."""
    try:
        tokens = {"input_tokens": 0, "output_tokens": 0, "cache_write_tokens": 0, "cache_read_tokens": 0}
        if response is not None:
            model = response.model or model
            u = response.usage
            tokens = {
                "input_tokens": getattr(u, "input_tokens", 0) or 0,
                "output_tokens": getattr(u, "output_tokens", 0) or 0,
                "cache_write_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
                "cache_read_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
            }
        cost = cost_usd(model, **tokens)
        db.add(ApiCall(user_id=user.id, model=model, cost_usd=cost, error=error, **tokens))
        db.commit()
        return cost
    except Exception:
        logger.exception("Could not record API usage")
        db.rollback()
        return 0.0


def day_bounds_utc(tz_name: str, day=None) -> tuple[datetime, datetime]:
    """Start and end of a local calendar day, as naive UTC datetimes."""
    tz = ZoneInfo(tz_name)
    if day is None:
        day = datetime.now(tz).date()
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = start + timedelta(days=1)
    return (start.astimezone(UTC).replace(tzinfo=None), end.astimezone(UTC).replace(tzinfo=None))


def _spent(db: Session, *conditions) -> float:
    return db.scalar(select(func.coalesce(func.sum(ApiCall.cost_usd), 0.0)).where(*conditions)) or 0.0


def spent_today(db: Session, user: User) -> float:
    start, end = day_bounds_utc(user.timezone)
    return _spent(db, ApiCall.user_id == user.id, ApiCall.created_at >= start, ApiCall.created_at < end)


def spent_month(db: Session, user: User | None = None) -> float:
    now = datetime.now(UTC).replace(tzinfo=None)
    start = datetime(now.year, now.month, 1)
    conditions = [ApiCall.created_at >= start]
    if user is not None:
        conditions.append(ApiCall.user_id == user.id)
    return _spent(db, *conditions)


def daily_limit(user: User) -> float:
    if user.daily_limit_usd is not None:
        return user.daily_limit_usd
    return get_settings().default_daily_limit_usd


def limit_problem(db: Session, user: User) -> str | None:
    """Why this user can't make another call right now, or None if they can."""
    if spent_today(db, user) >= daily_limit(user):
        return "Дневной лимит распознаваний исчерпан. Можно ввести продукты вручную или подождать до завтра."
    if spent_month(db) >= get_settings().monthly_limit_usd:
        return "Общий месячный лимит на распознавание исчерпан. Напиши администратору."
    return None
