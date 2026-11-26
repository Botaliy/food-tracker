"""Meal photo thumbnails: what the meal card shows instead of an icon.

A recognized photo leaves a small JPEG (320 px, ~20 KB) in <data dir>/photos under a
random name. The draft carries the name; saving the meal claims the file. Files no meal
claimed within a day are drafts that were never saved, and everything older than
PHOTO_KEEP_DAYS goes, so the folder stays small on a server with little disk.
"""

import io
import logging
import os
import re
import secrets
import time
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Meal

logger = logging.getLogger(__name__)

THUMB_EDGE = 320
THUMB_QUALITY = 75
UNCLAIMED_SECONDS = 24 * 3600
CLEANUP_EVERY_SECONDS = 24 * 3600
# The only names ever served or deleted: no dots, slashes or anything a path could hide in.
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{16,40}\.jpg$")

_last_cleanup = 0.0  # time.monotonic() of the last run; 0 = not yet


def enabled() -> bool:
    return get_settings().photo_keep_days > 0


def folder() -> Path:
    return get_settings().data_dir / "photos"


def path_of(name: str | None) -> Path | None:
    """The file for a valid name (it may not exist); None for anything else."""
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        return None
    return folder() / name


def save_thumbnail(jpeg: bytes) -> str | None:
    """Store a thumbnail of an (already shrunk) JPEG; its file name, or None if photos are off."""
    if not enabled():
        return None
    try:
        with Image.open(io.BytesIO(jpeg)) as img:
            img.draft("RGB", (THUMB_EDGE, THUMB_EDGE))  # decode at a fraction of the size
            img = img.convert("RGB")
            img.thumbnail((THUMB_EDGE, THUMB_EDGE))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=THUMB_QUALITY, optimize=True)
    except (UnidentifiedImageError, OSError) as exc:
        logger.warning("Thumbnail failed: %s", exc)
        return None
    folder().mkdir(parents=True, exist_ok=True)
    name = secrets.token_urlsafe(16) + ".jpg"
    path = folder() / name
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(buf.getvalue())
    tmp.replace(path)  # never a half-written file under a real name
    return name


def claimable(db: Session, name: str | None, meal_id: int | None = None) -> bool:
    """A name a meal may take: well-formed, on disk, and not another meal's."""
    path = path_of(name)
    if path is None or not path.is_file():
        return False
    other = select(Meal.id).where(Meal.photo == name)
    if meal_id is not None:
        other = other.where(Meal.id != meal_id)
    return db.scalar(other.limit(1)) is None


def delete(name: str | None) -> None:
    path = path_of(name)
    if path is not None:
        path.unlink(missing_ok=True)


def cleanup(db: Session, now: float | None = None) -> int:
    """Delete files past the keep limit (and forget them on their meals) and unclaimed
    files older than a day. Reads only names and times, never file contents."""
    global _last_cleanup
    _last_cleanup = time.monotonic()
    base = folder()
    if not base.is_dir():
        return 0
    now = time.time() if now is None else now
    keep_days = get_settings().photo_keep_days
    claimed = set(db.scalars(select(Meal.photo).where(Meal.photo.is_not(None))))
    removed, expired = 0, []
    with os.scandir(base) as entries:
        for e in entries:
            if not e.is_file(follow_symlinks=False):
                continue
            age = now - e.stat(follow_symlinks=False).st_mtime
            if e.name.endswith(".tmp") and age > 3600:  # left by a crash mid-write
                os.unlink(e.path)
                continue
            if not NAME_RE.fullmatch(e.name):
                continue
            if age > keep_days * 86400 or (e.name not in claimed and age > UNCLAIMED_SECONDS):
                os.unlink(e.path)
                removed += 1
                if e.name in claimed:
                    expired.append(e.name)
    for chunk in range(0, len(expired), 500):
        db.execute(update(Meal).where(Meal.photo.in_(expired[chunk:chunk + 500])).values(photo=None))
    if expired:
        db.commit()
    return removed


def cleanup_safely(db: Session) -> None:
    """cleanup() for request paths and startup: a failure is logged, never raised."""
    try:
        cleanup(db)
    except Exception:  # noqa: BLE001 - housekeeping must not break the request
        logger.exception("Photo cleanup failed")
        db.rollback()


def maybe_cleanup(db: Session) -> None:
    """At most once a day, from the recognize path: no scheduler needed."""
    if _last_cleanup and time.monotonic() - _last_cleanup < CLEANUP_EVERY_SECONDS:
        return
    cleanup_safely(db)
