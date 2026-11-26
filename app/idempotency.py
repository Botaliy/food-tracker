"""Idempotency-Key for the quick writes the app may retry from its offline queue.

The client sends a random key with a write; a retry after a lost response carries the same
key, and the server answers with what the first request did instead of doing it again.
Keys are kept KEEP_DAYS days: far longer than any queue waits.
"""

import logging
import re
import time
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import IdempotencyKey, User, utcnow

logger = logging.getLogger(__name__)

KEY_RE = re.compile(r"[A-Za-z0-9_-]{16,64}")
KEEP_DAYS = 14
SWEEP_EVERY_SECONDS = 24 * 3600

_last_sweep = 0.0  # time.monotonic() of the last sweep; 0 = not yet


def checked(value: str | None) -> str | None:
    """The header's key, None without one; a malformed key is a 400, never silently ignored
    (ignoring it would quietly turn a retry into a second write)."""
    if value is None:
        return None
    if not KEY_RE.fullmatch(value):
        raise HTTPException(status_code=400, detail="Idempotency-Key: 16–64 символа A-Z, a-z, 0-9, _ и -")
    return value


def used(db: Session, user: User, key: str | None) -> IdempotencyKey | None:
    if key is None:
        return None
    return db.scalar(select(IdempotencyKey).where(IdempotencyKey.user_id == user.id, IdempotencyKey.key == key))


def remember(db: Session, user: User, key: str | None, ref_id: int | None = None) -> None:
    """Part of the write's own transaction: the key exists exactly when the write does."""
    if key is not None:
        db.add(IdempotencyKey(user_id=user.id, key=key, ref_id=ref_id))


def sweep(db: Session, now: float | None = None) -> None:
    """Drop old keys, at most once a day, from the request path; never raises."""
    global _last_sweep
    now = time.monotonic() if now is None else now
    if _last_sweep and now - _last_sweep < SWEEP_EVERY_SECONDS:
        return
    _last_sweep = now
    try:
        db.execute(delete(IdempotencyKey).where(IdempotencyKey.created_at < utcnow() - timedelta(days=KEEP_DAYS)))
        db.commit()
    except Exception:  # noqa: BLE001 - housekeeping must not break the request
        logger.exception("Idempotency key cleanup failed")
        db.rollback()
