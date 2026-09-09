from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import PendingPhotoUpload, User
from .telegram import send_message


async def create_photo_request(db: Session, user: User, entity_type: str, entity_id: int, field_key: str | None = None) -> PendingPhotoUpload:
    token = secrets.token_urlsafe(24)
    req = PendingPhotoUpload(
        token=token,
        user_id=user.id,
        entity_type=entity_type,
        entity_id=entity_id,
        field_key=field_key,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    await send_message(
        user.telegram_id,
        f"📷 Отправьте фотографию для {entity_type} #{entity_id}. Запрос действует 15 минут.",
    )
    return req


def active_photo_request(db: Session, user_id: int) -> PendingPhotoUpload | None:
    now = datetime.now(timezone.utc)
    return db.scalar(
        select(PendingPhotoUpload)
        .where(
            PendingPhotoUpload.user_id == user_id,
            PendingPhotoUpload.status == "pending",
            PendingPhotoUpload.expires_at > now,
        )
        .order_by(PendingPhotoUpload.created_at.desc())
    )
