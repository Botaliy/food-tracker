"""Web Push to the installed app: reminders on the lock screen.

On an iPhone this works for the app added to the Home Screen (iOS 16.4+), not for a
Safari tab. Each device that allowed notifications has a PushSubscription row.
"""

import base64
import json
import logging

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid
from pywebpush import WebPushException, webpush
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import PushSubscription, User

logger = logging.getLogger(__name__)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def generate_keys() -> tuple[str, str]:
    """(private, public) as base64url: the raw private number and the uncompressed public point."""
    v = Vapid()
    v.generate_keys()
    private = v.private_key.private_numbers().private_value.to_bytes(32, "big")
    public = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return _b64(private), _b64(public)


def enabled() -> bool:
    s = get_settings()
    return bool(s.vapid_public_key and s.vapid_private_key and s.vapid_subject)


def send(db: Session, user: User, title: str, body: str, url: str = "/") -> int:
    """Send to every device of the user; returns how many took it. A gone device is forgotten."""
    if not enabled():
        return 0
    s = get_settings()
    vapid = Vapid.from_string(private_key=s.vapid_private_key)
    payload = json.dumps({"title": title, "body": body, "url": url}, ensure_ascii=False)
    sent = 0
    for sub in db.scalars(select(PushSubscription).where(PushSubscription.user_id == user.id)).all():
        try:
            webpush(
                subscription_info={"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}},
                data=payload,
                vapid_private_key=vapid,
                vapid_claims={"sub": s.vapid_subject},
                ttl=6 * 3600,
                timeout=10,
            )
            sent += 1
        except WebPushException as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status in (404, 410):  # unsubscribed or the app was removed
                db.delete(sub)
            else:
                logger.warning("Push to user %s failed: %s", user.id, status or exc)
        except Exception:
            logger.exception("Push to user %s failed", user.id)
    db.commit()
    return sent
