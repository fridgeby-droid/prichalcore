"""Durable inbox; ACK only after persistence, retry failed processing."""
from datetime import datetime, timedelta
import logging
from sqlalchemy import select, or_
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException
from app.db.database import session_scope
from app.db.models import TelegramInbox
log=logging.getLogger(__name__)

def accept_update(payload):
    identity=payload.get('update_id')
    if not isinstance(identity,int):raise HTTPException(400,'update_id required')
    try:
        with session_scope() as db:
            if db.get(TelegramInbox,identity):return
            db.add(TelegramInbox(update_id=identity,payload_json=payload))
    except IntegrityError:pass

def process_inbox():
    from app.services.telegram_webhook import handle_update
    with session_scope() as db:
        rows=list(db.scalars(select(TelegramInbox).where(or_(TelegramInbox.status=='pending',(TelegramInbox.status=='failed')&(TelegramInbox.updated_at<datetime.utcnow()-timedelta(minutes=1)),(TelegramInbox.status=='processing')&(TelegramInbox.updated_at<datetime.utcnow()-timedelta(minutes=5)))).where(TelegramInbox.attempts<10).order_by(TelegramInbox.created_at).limit(10).with_for_update(skip_locked=True)).all())
        items=[]
        for r in rows:
            if r.attempts>=10:continue
            r.status='processing';r.attempts+=1;r.updated_at=datetime.utcnow();items.append((r.update_id,r.payload_json))
    for identity,payload in items:
        try:handle_update(payload);status='done'
        except Exception:log.exception('Telegram update processing failed: %s',identity);status='failed'
        with session_scope() as db:
            row=db.get(TelegramInbox,identity);row.status=status;row.updated_at=datetime.utcnow()
