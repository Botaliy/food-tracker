"""The reminder loop: every few minutes, send what's due in each user's own timezone.

- Evening: at the hour the user picked, if nothing is logged today. With a streak at
  stake the message says so.
- Monday morning: make the weekly review and say it's ready.

Each kind goes out at most once a day per user (Profile.last_*_push), so a restart or a
slow tick never sends twice.
"""

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import diary, push, review
from app.database import SessionLocal
from app.models import Profile, PushSubscription, User

logger = logging.getLogger(__name__)

TICK_SECONDS = 300
WEEKLY_HOUR = 10  # Monday, local time


def evening_text(streak: int) -> tuple[str, str]:
    if streak >= 2:
        return f"🔥 Серия {streak} дн. под угрозой", "Сегодня ещё ничего не записано — серия сгорит в полночь."
    return "🍽️ Что ел сегодня?", "Сегодня ещё ничего не записано. Пара фото — и день посчитан."


def run_due(db: Session, now: datetime, send: Callable = push.send) -> list[tuple[int, str]]:
    """Send everything due at `now` (aware UTC). Returns (user_id, kind) of what went out."""
    done = []
    users = db.scalars(
        select(User).where(User.id.in_(select(PushSubscription.user_id).distinct()))
    ).all()
    for user in users:
        profile = db.get(Profile, user.id)
        if profile is None:
            profile = Profile(user_id=user.id)
            db.add(profile)
            db.flush()
        local = now.astimezone(ZoneInfo(user.timezone))
        today = local.date()

        if profile.remind_evening and local.hour >= profile.remind_hour and profile.last_evening_push != today:
            profile.last_evening_push = today
            db.commit()
            streak, logged_today = diary.streak(db, user, today)
            if not logged_today:
                title, body = evening_text(streak)
                if send(db, user, title, body):
                    done.append((user.id, "evening"))

        if (profile.weekly_review_push and local.weekday() == 0 and local.hour >= WEEKLY_HOUR
                and profile.last_weekly_push != today):
            profile.last_weekly_push = today
            db.commit()
            try:
                made = review.create_for(db, user)
            except review.ReviewError as exc:
                logger.info("No weekly review for user %s: %s", user.id, exc)
                continue
            if send(db, user, "✨ Обзор недели готов", review_summary(made.content), "/#reports"):
                done.append((user.id, "weekly"))
    return done


def review_summary(content: str) -> str:
    summary = json.loads(content).get("summary", "")
    return summary if len(summary) <= 140 else summary[:137] + "…"


async def loop() -> None:
    while True:
        try:
            await asyncio.to_thread(_tick)
        except Exception:
            logger.exception("Reminder tick failed")
        await asyncio.sleep(TICK_SECONDS)


def _tick() -> None:
    with SessionLocal() as db:
        for user_id, kind in run_due(db, datetime.now(UTC)):
            logger.info("Sent %s reminder to user %s", kind, user_id)
