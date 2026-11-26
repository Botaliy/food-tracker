from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import auth, usage
from app.config import get_settings
from app.database import get_db
from app.models import Invite, User

router = APIRouter(prefix="/api/admin")


@router.get("/users")
def users(_: User = Depends(auth.admin_user), db: Session = Depends(get_db)):
    return {
        "month_total": round(usage.spent_month(db), 4),
        "monthly_limit": get_settings().monthly_limit_usd,
        "users": [
            {
                "id": u.id,
                "username": u.username,
                "is_admin": u.is_admin,
                "spent_today": round(usage.spent_today(db, u), 4),
                "spent_month": round(usage.spent_month(db, u), 4),
                "daily_limit": usage.daily_limit(u),
                "own_limit": u.daily_limit_usd is not None,
            }
            for u in db.scalars(select(User).order_by(User.id))
        ],
    }


class LimitIn(BaseModel):
    # None resets to the default.
    daily_limit_usd: float | None = Field(default=None, ge=0, le=50)


@router.patch("/users/{user_id}")
def set_limit(user_id: int, body: LimitIn, _: User = Depends(auth.admin_user), db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Нет такого пользователя")
    user.daily_limit_usd = body.daily_limit_usd
    db.commit()
    return {"ok": True}


@router.get("/invites")
def invites(_: User = Depends(auth.admin_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(Invite).order_by(Invite.created_at.desc()).limit(50))
    names = {u.id: u.username for u in db.scalars(select(User))}
    return [
        {"code": i.code, "used_by": names.get(i.used_by), "created_at": i.created_at.isoformat()}
        for i in rows
    ]


@router.post("/invites")
def create_invite(admin: User = Depends(auth.admin_user), db: Session = Depends(get_db)):
    invite = Invite(code=auth.new_invite_code(), created_by=admin.id)
    db.add(invite)
    db.commit()
    return {"code": invite.code}


@router.delete("/invites/{code}")
def delete_invite(code: str, _: User = Depends(auth.admin_user), db: Session = Depends(get_db)):
    invite = db.get(Invite, code)
    if invite is None or invite.used_by is not None:
        raise HTTPException(status_code=400, detail="Нельзя удалить")
    db.delete(invite)
    db.commit()
    return {"ok": True}
