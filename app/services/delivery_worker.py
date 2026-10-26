"""Durable notification delivery with bounded retry; no DB tx during HTTP."""
from datetime import datetime, timedelta
import logging
from sqlalchemy import select, or_
from app.db.database import session_scope
from app.db.models import TelegramDeliveryLog, MediaAsset
from app.services.telegram import send_message, send_photo, send_document
from app.services.storage import _s3_client, media_download_url
from app.core.config import S3_BUCKET
log=logging.getLogger(__name__)

def process_deliveries():
    for _ in range(10):
        if not _process_one_delivery():break

def _process_one_delivery():
    now=datetime.utcnow()
    with session_scope() as db:
        rows=list(db.scalars(select(TelegramDeliveryLog).where(or_(TelegramDeliveryLog.status=='pending',(TelegramDeliveryLog.status=='retry')&(TelegramDeliveryLog.sent_at<now-timedelta(minutes=1)),(TelegramDeliveryLog.status=='sending')&(TelegramDeliveryLog.sent_at<now-timedelta(minutes=5)))).order_by(TelegramDeliveryLog.created_at).limit(1).with_for_update(skip_locked=True)).all())
        if not rows:return False
        items=[]
        for r in rows:
            payload=dict(r.payload_json or {});attempt=int(payload.get('_attempt',0))+1
            if attempt>5:
                r.status='error';r.error='Доставка не подтверждена после 5 попыток'
                for child in db.scalars(select(TelegramDeliveryLog).where(TelegramDeliveryLog.id.in_(payload.get('grouped_log_ids', [])))):
                    if (child.payload_json or {}).get('group_log_id')==r.id:
                        child.status='error';child.error=r.error
                continue
            payload['_attempt']=attempt;r.payload_json=payload;r.status='sending';r.sent_at=now
            refs=[]
            for ref in payload.get('photos',[]):
                if isinstance(ref,dict) and ref.get('media_id'):
                    asset=db.get(MediaAsset,ref['media_id']);refs.append(('s3',asset.s3_key) if asset else ('missing',''))
                elif isinstance(ref,dict):refs.append(('telegram',ref.get('telegram_file_id')))
                else:refs.append(('telegram',ref))
            items.append((r.id,r.chat_id,payload,refs))
    for identity,chat,payload,refs in items:
        status='sent';message_id=payload.get('_text_message_id')
        def checkpoint():
            with session_scope() as db:
                current=db.get(TelegramDeliveryLog,identity)
                if current and current.status=='sending':current.payload_json=dict(payload);current.sent_at=datetime.utcnow()
        try:
            def photos():
                for kind,value in refs:
                    if kind=='missing' or not value:raise ValueError('Фото не найдено')
                    url=_s3_client().generate_presigned_url('get_object',Params={'Bucket':S3_BUCKET,'Key':value},ExpiresIn=300) if kind=='s3' else value
                    send_photo(chat,url,payload.get('photo_caption'))
            if payload.get('media_order')=='media_first':photos()
            if not payload.get('_text_sent'):
                data=send_message(chat,payload.get('text',''),payload.get('reply_markup'));message_id=(data or {}).get('result',{}).get('message_id')
                payload['_text_sent']=True;payload['_text_message_id']=message_id;checkpoint()
            if payload.get('media_order')!='media_first':photos()
            done=set(payload.get('_documents_sent',[]))
            for doc in payload.get('documents',[]):
                if doc['id'] in done:continue
                body=_s3_client().get_object(Bucket=S3_BUCKET,Key=doc['s3_key'])['Body']
                try:content=body.read(10*1024*1024+1)
                finally:body.close()
                if len(content)>10*1024*1024:raise ValueError('File exceeds limit')
                send_document(chat,doc['filename'],content,doc['mime_type'],doc.get('caption',''))
                done.add(doc['id']);payload['_documents_sent']=sorted(done);checkpoint()
        except Exception as exc:
            log.warning('Notification delivery failed %s (%s)',identity,type(exc).__name__);status='retry' if payload.get('_attempt',0)<5 else 'error'
        with session_scope() as db:
            r=db.get(TelegramDeliveryLog,identity)
            if r and r.status=='sending':
                r.status=status;r.telegram_message_id=message_id;r.sent_at=datetime.utcnow();r.error='Не удалось отправить уведомление' if status in {'error','retry'} else None
                for child in db.scalars(select(TelegramDeliveryLog).where(TelegramDeliveryLog.id.in_(payload.get('grouped_log_ids', [])))):
                    if (child.payload_json or {}).get('group_log_id') == r.id:
                        child.status = 'grouped' if status == 'retry' else status
                        child.telegram_message_id=message_id;child.sent_at=r.sent_at;child.error=r.error

    return True
