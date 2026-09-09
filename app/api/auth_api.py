from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import current_user, issue_session, validate_telegram_init_data
from ..config import settings
from ..db import get_db
from ..models import User, UserRole, UserStatus
from ..permissions import store_ids_for_user

router = APIRouter(prefix="/api/auth", tags=["auth"])


class TelegramAuthIn(BaseModel):
    init_data: str


@router.post("/telegram")
def telegram_auth(payload: TelegramAuthIn, db: Session = Depends(get_db)):
    tg = validate_telegram_init_data(payload.init_data)
    telegram_id = int(tg["id"])
    user = db.scalar(select(User).where(User.telegram_id == telegram_id))
    if not user:
        user = User(
            telegram_id=telegram_id,
            username=tg.get("username"),
            full_name=" ".join(filter(None, [tg.get("first_name"), tg.get("last_name")])),
        )
        if settings.bootstrap_admin_telegram_id and telegram_id == settings.bootstrap_admin_telegram_id:
            user.role = UserRole.admin
            user.status = UserStatus.active
        db.add(user)
        db.commit()
        db.refresh(user)
    else:
        user.username = tg.get("username")
        user.full_name = " ".join(filter(None, [tg.get("first_name"), tg.get("last_name")]))
        if settings.bootstrap_admin_telegram_id and telegram_id == settings.bootstrap_admin_telegram_id:
            user.role = UserRole.admin
            user.status = UserStatus.active
        db.commit()

    if user.status != UserStatus.active:
        return {"status": "pending", "user": {"id": user.id, "telegram_id": user.telegram_id, "full_name": user.full_name}}
    token = issue_session(db, user)
    return {"status": "ok", "token": token, "user": user_payload(db, user)}


def user_payload(db: Session, user: User) -> dict:
    return {
        "id": user.id,
        "telegram_id": user.telegram_id,
        "full_name": user.full_name,
        "username": user.username,
        "role": user.role.value,
        "status": user.status.value,
        "store_ids": store_ids_for_user(db, user),
    }


@router.get("/me")
def me(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return user_payload(db, user)
