import secrets

import bcrypt
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User

SESSION_KEY = "uid"


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def check_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except ValueError:
        return False


def new_invite_code() -> str:
    return secrets.token_urlsafe(12)


def login(request: Request, user: User) -> None:
    request.session[SESSION_KEY] = user.id


def logout(request: Request) -> None:
    request.session.pop(SESSION_KEY, None)


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid = request.session.get(SESSION_KEY)
    user = db.get(User, uid) if uid is not None else None
    if user is None:
        raise HTTPException(status_code=401, detail="Нужно войти")
    return user


def admin_user(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Только для администратора")
    return user
