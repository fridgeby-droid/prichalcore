"""Durable notification delivery with bounded retry; no DB tx during HTTP."""
from datetime import datetime, timedelta
import logging
from sqlalchemy import select, or_
from app.db.database import session_scope
from app.db.models import TelegramDeliveryLog, MediaAsset
from app.services.telegram import send_message, send_photo
from app.services.storage import _s3_client, media_download_url
from app.core.config import S3_BUCKET
log=logging.getLogger(__name__)

def process_deliveries():
    now=datetime.utcnow()
    with session_scope() as db:
        rows=list(db.scalars(select(TelegramDeliveryLog).where(or_(TelegramDeliveryLog.status=='pending',(TelegramDeliveryLog.status=='retry')&(TelegramDeliveryLog.sent_at<now-timedelta(minutes=1)),(TelegramDeliveryLog.status=='sending')&(TelegramDeliveryLog.sent_at<now-timedelta(minutes=5)))).order_by(TelegramDeliveryLog.created_at).limit(10).with_for_update(skip_locked=True)).all())
        items=[]
        for r in rows:
            payload=dict(r.payload_json or {});attempt=int(payload.get('_attempt',0))+1
            if attempt>5:r.status='error';r.error='Доставка не подтверждена после 5 попыток';continue
            payload['_attempt']=attempt;r.payload_json=payload;r.status='sending';r.sent_at=now
            refs=[]
            for ref in payload.get('photos',[]):
                if isinstance(ref,dict) and ref.get('media_id'):
                    asset=db.get(MediaAsset,ref['media_id']);refs.append(('s3',asset.s3_key) if asset else ('missing',''))
                elif isinstance(ref,dict):refs.append(('telegram',ref.get('telegram_file_id')))
                else:refs.append(('telegram',ref))
            items.append((r.id,r.chat_id,payload,refs))
    for identity,chat,payload,refs in items:
        status='sent';message_id=None
        try:
            def photos():
                for kind,value in refs:
                    if kind=='missing' or not value:raise ValueError('Фото не найдено')
                    url=_s3_client().generate_presigned_url('get_object',Params={'Bucket':S3_BUCKET,'Key':value},ExpiresIn=300) if kind=='s3' else value
                    send_photo(chat,url,payload.get('photo_caption'))
            if payload.get('media_order')=='media_first':photos()
            data=send_message(chat,payload.get('text',''),payload.get('reply_markup'));message_id=(data or {}).get('result',{}).get('message_id')
            if payload.get('media_order')!='media_first':photos()
        except Exception as exc:
            log.warning('Notification delivery failed %s (%s)',identity,type(exc).__name__);status='retry' if payload.get('_attempt',0)<5 else 'error'
        with session_scope() as db:
            r=db.get(TelegramDeliveryLog,identity)
            if r and r.status=='sending':
                r.status=status;r.telegram_message_id=message_id;r.sent_at=datetime.utcnow();r.error='Не удалось отправить уведомление' if status in {'error','retry'} else None
