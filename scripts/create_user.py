"""Create a user (the first admin) or reset a password.

Usage:
    python -m scripts.create_user USERNAME [--admin]

The password is read from the terminal, never from the command line.
"""

import getpass
import sys

from sqlalchemy import select

from app.auth import hash_password
from app.config import get_settings
from app.database import SessionLocal, init_db
from app.models import User


def main(argv: list[str]) -> None:
    if not argv:
        raise SystemExit(__doc__)
    username, is_admin = argv[0].strip().lower(), "--admin" in argv[1:]
    password = getpass.getpass("Пароль: ")
    if len(password) < 8 or password != getpass.getpass("Ещё раз: "):
        raise SystemExit("Пароли не совпадают или короче 8 символов")
    init_db()
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == username))
        if user is None:
            user = User(username=username, timezone=get_settings().timezone, password_hash="")
            db.add(user)
        user.password_hash = hash_password(password)
        user.is_admin = user.is_admin or is_admin
        db.commit()
        print(f"OK: {username}{' (admin)' if user.is_admin else ''}")


if __name__ == "__main__":
    main(sys.argv[1:])
