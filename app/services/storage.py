from __future__ import annotations

from datetime import datetime
import re
from functools import lru_cache

import boto3
from botocore.config import Config
import httpx
from sqlalchemy import select

from app.core.config import (
    PHOTO_STORAGE, S3_ENDPOINT_URL, S3_REGION, S3_BUCKET,
    S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY, S3_PREFIX,
)
from app.db.models import StoredObject, MediaAsset
from app.services.telegram import get_file_path, file_download_url


def _s3_enabled() -> bool:
    return PHOTO_STORAGE == "s3"


@lru_cache(maxsize=1)
def _s3_client():
    if not _s3_enabled():
        return None
    missing=[]
    for name,value in {
        "S3_ENDPOINT_URL":S3_ENDPOINT_URL,
        "S3_BUCKET":S3_BUCKET,
        "S3_ACCESS_KEY_ID":S3_ACCESS_KEY_ID,
        "S3_SECRET_ACCESS_KEY":S3_SECRET_ACCESS_KEY,
    }.items():
        if not value: missing.append(name)
    if missing:
        raise RuntimeError("S3 configuration is incomplete: " + ", ".join(missing))
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT_URL,
        region_name=S3_REGION or None,
        aws_access_key_id=S3_ACCESS_KEY_ID,
        aws_secret_access_key=S3_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4", s3={"addressing_style":"path"}, connect_timeout=5, read_timeout=30, retries={"max_attempts":2}, max_pool_connections=20),
    )


def _safe_token(value: str | None) -> str:
    value=(value or "file").strip()
    value=re.sub(r"[^A-Za-z0-9._-]+","_",value)
    return value[:180] or "file"


def mirror_telegram_photo_to_s3(db, telegram_file_id: str, telegram_file_unique_id: str | None = None) -> StoredObject | None:
    """Copy a Telegram photo into S3 and remember the mapping.

    Existing mappings are reused, so duplicate Telegram deliveries do not create
    duplicate S3 objects. When PHOTO_STORAGE is not 's3' this is a no-op.
    """
    if not _s3_enabled():
        return None
    existing=db.scalar(select(StoredObject).where(StoredObject.telegram_file_id==telegram_file_id))
    if existing:
        return existing

    tg_path=get_file_path(telegram_file_id)
    tg_url=file_download_url(tg_path)
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        r=client.get(tg_url)
        r.raise_for_status()
        payload=r.content
        content_type=(r.headers.get("content-type") or "image/jpeg").split(";")[0].strip()

    now=datetime.utcnow()
    token=_safe_token(telegram_file_unique_id or telegram_file_id)
    ext=".jpg"
    if content_type=="image/png": ext=".png"
    elif content_type=="image/webp": ext=".webp"
    elif content_type=="image/gif": ext=".gif"
    prefix=(S3_PREFIX or "core/photos").strip("/")
    key=f"{prefix}/{now:%Y/%m/%d}/{token}{ext}"

    s3=_s3_client()
    s3.put_object(Bucket=S3_BUCKET,Key=key,Body=payload,ContentType=content_type)
    row=StoredObject(
        telegram_file_id=telegram_file_id,
        telegram_file_unique_id=telegram_file_unique_id,
        s3_key=key,
        mime_type=content_type,
        size_bytes=len(payload),
    )
    db.add(row)
    db.flush()
    return row


def media_download_url(db, telegram_file_id: str, expires_seconds: int = 300) -> str:
    """Return private S3 URL when mirrored, otherwise the Telegram file URL."""
    if _s3_enabled():
        row=db.scalar(select(StoredObject).where(StoredObject.telegram_file_id==telegram_file_id))
        if row:
            return _s3_client().generate_presigned_url(
                "get_object",
                Params={"Bucket":S3_BUCKET,"Key":row.s3_key},
                ExpiresIn=max(60,min(int(expires_seconds),3600)),
            )
    path=get_file_path(telegram_file_id)
    return file_download_url(path)


def get_s3_photo_object(db, telegram_file_id: str | None, media_id: str | None = None, thumbnail: bool = False):
    """Fetch a previously mirrored photo directly using the S3 SDK.

    Returns None only when the photo is not mirrored (legacy Telegram photo).
    Storage errors propagate so the caller can return a proper error.
    """
    if not _s3_enabled():
        return None
    row=db.get(MediaAsset,media_id) if media_id else db.scalar(select(StoredObject).where(StoredObject.telegram_file_id==telegram_file_id))
    if row is None:
        return None
    key=(getattr(row,"thumbnail_key",None) if thumbnail else None) or row.s3_key
    return _s3_client().get_object(Bucket=S3_BUCKET, Key=key)


# All private photo content is served via our authenticated API endpoints.
# Telegram is an upload transport only, never a read fallback for photos.
def s3_photo_response(db, telegram_file_id: str | None = None, *, media_type: str | None = None, media_id: str | None = None, thumbnail: bool = False):
    import logging
    from fastapi import HTTPException
    from starlette.responses import StreamingResponse
    if not _s3_enabled():
        raise HTTPException(503, "S3-хранилище фотографий не настроено")
    try:
        obj=get_s3_photo_object(db, telegram_file_id,media_id,thumbnail)
        if obj is None:
            raise HTTPException(404, "Фотография не найдена в S3")
    except HTTPException:
        raise
    except Exception:
        logging.getLogger(__name__).exception("S3 photo retrieval failed")
        raise HTTPException(502,"Ошибка чтения фотографии из S3")
    # Release DB connection before a slow client consumes the body. All callers
    # have already checked access and this path is read-only.
    db.close()
    body=obj["Body"]
    def chunks():
        try:
            while True:
                block=body.read(64*1024)
                if not block: break
                yield block
        finally:
            body.close()
    return StreamingResponse(chunks(), media_type=obj.get("ContentType") or media_type or "image/jpeg",headers={"Cache-Control":"private, no-store","X-Content-Type-Options":"nosniff"})
