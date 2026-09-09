from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import Session as UserSession, User, UserRole, UserStatus


def validate_telegram_init_data(init_data: str) -> dict:
    if not settings.bot_token:
        raise HTTPException(status_code=500, detail="BOT_TOKEN is not configured")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", "")
    if not received_hash:
        raise HTTPException(status_code=401, detail="Telegram hash is missing")

    auth_date = int(pairs.get("auth_date", "0") or 0)
    if not auth_date:
        raise HTTPException(status_code=401, detail="Telegram auth_date is missing")
    if int(time.time()) - auth_date > settings.telegram_auth_max_age_seconds:
        raise HTTPException(status_code=401, detail="Telegram authorization expired")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", settings.bot_token.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated_hash, received_hash):
        raise HTTPException(status_code=401, detail="Invalid Telegram signature")

    try:
        user = json.loads(pairs.get("user", "{}"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=401, detail="Invalid Telegram user payload") from exc
    if not user.get("id"):
        raise HTTPException(status_code=401, detail="Telegram user is missing")
    return user


def issue_session(db: Session, user: User) -> str:
    raw = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    expires = datetime.now(timezone.utc) + timedelta(hours=settings.session_ttl_hours)
    db.add(UserSession(user_id=user.id, token_hash=token_hash, expires_at=expires))
    db.commit()
    return raw


def _bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    return authorization.split(" ", 1)[1].strip()


def current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    raw = _bearer_token(authorization)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    now = datetime.now(timezone.utc)
    session = db.scalar(select(UserSession).where(UserSession.token_hash == token_hash))
    if not session or session.expires_at.replace(tzinfo=timezone.utc) < now:
        raise HTTPException(status_code=401, detail="Session expired")
    user = db.get(User, session.user_id)
    if not user or user.status != UserStatus.active:
        raise HTTPException(status_code=403, detail="User is not active")
    return user


def allow_roles(*roles: UserRole):
    def dependency(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user
    return dependency


def internal_secret(x_internal_secret: str | None = Header(default=None)) -> None:
    if not settings.internal_api_secret or not hmac.compare_digest(x_internal_secret or "", settings.internal_api_secret):
        raise HTTPException(status_code=403, detail="Invalid internal secret")
