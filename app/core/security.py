from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime, timedelta
from urllib.parse import parse_qsl

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select, delete
from sqlalchemy.orm import Session

from app.core.config import BOT_TOKEN, BOOTSTRAP_ADMIN_TELEGRAM_ID, SESSION_TTL_HOURS, TELEGRAM_AUTH_MAX_AGE_SECONDS
from app.db.database import get_db
from app.db.models import AuthSession, Employee, User, UserStore

ALL_ROLES = {"seller", "mentor", "manager", "operations_director", "leader", "admin"}
LEADERSHIP_ROLES = {"operations_director", "leader", "admin"}
MANAGEMENT_ROLES = {"manager", "operations_director", "leader", "admin"}
TASK_CREATOR_ROLES = {"manager", "operations_director", "leader", "admin"}
ADMIN_ROLES = {"admin"}


def validate_telegram_init_data(init_data: str) -> dict:
    if not BOT_TOKEN:
        raise HTTPException(status_code=500, detail="BOT_TOKEN is not configured")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="Telegram hash is missing")
    data_check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise HTTPException(status_code=401, detail="Invalid Telegram signature")
    auth_date = int(pairs.get("auth_date", "0") or 0)
    if not auth_date or abs(int(time.time()) - auth_date) > TELEGRAM_AUTH_MAX_AGE_SECONDS:
        raise HTTPException(status_code=401, detail="Telegram authorization expired")
    try:
        user_data = json.loads(pairs.get("user", "{}"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=401, detail="Invalid Telegram user payload")
    if not user_data.get("id"):
        raise HTTPException(status_code=401, detail="Telegram user is missing")
    return user_data


def upsert_telegram_user(db: Session, tg: dict) -> User:
    telegram_id = int(tg["id"])
    is_bootstrap = bool(BOOTSTRAP_ADMIN_TELEGRAM_ID and str(telegram_id) == str(BOOTSTRAP_ADMIN_TELEGRAM_ID))
    user = db.scalar(select(User).where(User.telegram_id == telegram_id))
    if not user:
        bootstrap = is_bootstrap
        user = User(
            telegram_id=telegram_id,
            username=tg.get("username"),
            first_name=tg.get("first_name"),
            last_name=tg.get("last_name"),
            full_name=" ".join(x for x in [tg.get("first_name"), tg.get("last_name")] if x) or str(telegram_id),
            role="admin" if bootstrap else "seller",
            status="active" if bootstrap else "pending",
            active=True,
        )
        db.add(user)
        db.flush()
    else:
        user.username = tg.get("username")
        user.first_name = tg.get("first_name")
        user.last_name = tg.get("last_name")
        user.full_name = " ".join(x for x in [tg.get("first_name"), tg.get("last_name")] if x) or user.full_name

    # Employee is a separate HR entity. If an employee card already contains this
    # Telegram ID, connect the account automatically on first login.
    employee = db.scalar(select(Employee).where(Employee.telegram_id == telegram_id))
    if employee and employee.user_id is None:
        employee.user_id = user.id
    elif not employee and is_bootstrap:
        db.add(Employee(
            full_name=user.full_name or str(telegram_id),
            position="admin",
            employment_status="working",
            telegram_id=telegram_id,
            user_id=user.id,
            active=True,
        ))
    return user


def create_session(db: Session, user: User) -> str:
    raw = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    expires = datetime.utcnow() + timedelta(hours=SESSION_TTL_HOURS)
    db.add(AuthSession(token_hash=token_hash, user_id=user.id, expires_at=expires))
    db.flush()
    return raw


def user_store_ids(db: Session, user: User) -> list[int]:
    if user.role in LEADERSHIP_ROLES:
        from app.db.models import Store
        return list(db.scalars(select(Store.id).where(Store.active.is_(True))).all())
    return list(db.scalars(select(UserStore.store_id).where(UserStore.user_id == user.id)).all())


def get_current_user(authorization: str | None = Header(default=None), db: Session = Depends(get_db)) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Authorization required")
    raw = authorization.split(" ", 1)[1].strip()
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    session = db.scalar(select(AuthSession).where(AuthSession.token_hash == token_hash))
    if not session or session.expires_at < datetime.utcnow():
        if session:
            db.delete(session)
            db.commit()
        raise HTTPException(status_code=401, detail="Session expired")
    user = db.get(User, session.user_id)
    if not user or not user.active:
        raise HTTPException(status_code=403, detail="User is inactive")
    if user.status != "active":
        raise HTTPException(status_code=403, detail="User is awaiting approval")
    return user


def require_roles(*roles: str):
    def dependency(user: User = Depends(get_current_user)) -> User:
        if user.role not in set(roles):
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user
    return dependency


def assert_store_access(db: Session, user: User, store_id: int):
    if user.role in LEADERSHIP_ROLES:
        return
    allowed = db.scalar(select(UserStore).where(UserStore.user_id == user.id, UserStore.store_id == store_id))
    if not allowed:
        raise HTTPException(status_code=403, detail="Store is not assigned to this user")
