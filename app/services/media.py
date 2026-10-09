"""Private direct uploads. Only validated, server-owned objects become attachments."""
from __future__ import annotations
from datetime import datetime, timedelta
from io import BytesIO
from uuid import uuid4
import logging
import os
import threading
import warnings
from PIL import Image, ImageOps
from fastapi import HTTPException
from sqlalchemy import select, func, or_, update
from app.db import database
from app.db.models import (MediaUpload, MediaAsset, MediaAudit, Photo, Employee, User,
    ShiftReportValue, ShiftReport, Inspection, InspectionValue, TaskV2, TaskAssignee,
    TaskChecklistItem, TaskAttachment, KnowledgeArticle, KnowledgeMedia, TrainingQuestion, TrainingAttempt, TrainingTest)
from app.core.permissions import require_access, assert_store_scope, current_employee, effective_permission
from app.core.config import S3_BUCKET, S3_PREFIX
from app.services.storage import _s3_client

log=logging.getLogger(__name__)
MAX_BYTES=int(os.getenv("MEDIA_MAX_BYTES",str(12*1024*1024)))
MAX_PIXELS=40_000_000
PREFIX=(S3_PREFIX or 'core/photos').strip('/')
_stop=threading.Event()
_thread=None
_message_thread=None

def locked(db,model,identity):
    return db.scalar(select(model).where(model.id==identity).with_for_update().execution_options(populate_existing=True))

def authorize(db,user,kind,identity,context=None):
    """All writes lock the parent; called both at issue and atomic attachment."""
    context=context or {}
    if kind=='profile_avatar':
        emp=current_employee(db,user)
        if not emp or emp.id!=identity: raise HTTPException(403,'Можно изменить только свой аватар')
        return locked(db,Employee,emp.id)
    if kind=='shift_report_field':
        value=db.get(ShiftReportValue,identity)
        obj=locked(db,ShiftReport,value.report_id) if value else None
        if not obj: raise HTTPException(404,'Пересменка не найдена')
        if obj.submitted_by!=user.id: raise HTTPException(403,'Фото добавляет автор пересменки')
        require_access(db,user,'shifts.submit','edit')
        assert_store_scope(db,user,'shifts.submit',obj.store_id,minimum='edit',own_as_assigned=True)
        if obj.status not in {'draft','rejected'}: raise HTTPException(409,'Пересменка уже отправлена на проверку')
        if context.get('revision') is not None and context['revision']!=obj.revision: raise HTTPException(409,'Версия пересменки изменилась')
        return obj
    if kind in {'inspection','inspection_value'}:
        value=db.get(InspectionValue,identity) if kind=='inspection_value' else None
        obj=locked(db,Inspection,value.inspection_id if value else identity) if kind=='inspection' or value else None
        if not obj: raise HTTPException(404,'Проверка не найдена')
        require_access(db,user,'inspections.conduct','edit')
        assert_store_scope(db,user,'inspections.conduct',obj.store_id,minimum='edit',own_as_assigned=True)
        if effective_permission(db,user,'inspections.conduct')['data_scope']=='own' and obj.manager_id!=user.id: raise HTTPException(403,'Доступна только своя проверка')
        if obj.status!='draft': raise HTTPException(409,'Проверка завершена')
        return obj
    if kind=='task_attachment':
        obj=locked(db,TaskV2,identity)
        a=locked(db,TaskAssignee,context.get('assignee_id'))
        emp=current_employee(db,user)
        require_access(db,user,'tasks.my','edit')
        if not obj or not a or a.task_id!=identity: raise HTTPException(404,'Задача не найдена')
        if not emp or a.employee_id!=emp.id: raise HTTPException(403,'Фото добавляет исполнитель')
        if obj.cancelled or a.status not in {'new','in_progress','rejected'}: raise HTTPException(409,'Результат уже отправлен или задача закрыта')
        if context.get('checklist_item_id'):
            item=db.get(TaskChecklistItem,context['checklist_item_id'])
            if not item or item.task_id!=identity: raise HTTPException(400,'Пункт не относится к задаче')
        return obj
    if kind=='knowledge_media':
        from app.api.knowledge import _ensure_section_access, _article_store_allowed
        require_access(db,user,'knowledge.edit','edit')
        obj=locked(db,KnowledgeArticle,identity)
        if not obj: raise HTTPException(404,'Статья не найдена')
        _ensure_section_access(db,user,obj.section_id)
        if not _article_store_allowed(db,user,obj,'knowledge.edit'): raise HTTPException(403,'Нет доступа к статье')
        return obj
    if kind=='test_question':
        require_access(db,user,'testing.questions','edit')
        question=db.get(TrainingQuestion,identity)
        if question: locked(db,TrainingTest,question.test_id)
        obj=locked(db,TrainingQuestion,identity)
        if not obj: raise HTTPException(404,'Вопрос не найден')
        # Historical attempts use live question IDs; changing an already-used image
        # would change evidence. Create a new question instead.
        attempts=db.scalars(select(TrainingAttempt).where(TrainingAttempt.test_id==obj.test_id)).all()
        if any(identity in (a.question_order_json or []) for a in attempts): raise HTTPException(409,'Вопрос уже использован в тесте. Создайте новый вопрос для другого фото')
        return obj
    raise HTTPException(400,'Неизвестное назначение фото')

def attach(db,row,asset,parent):
    ctx=row.context_json or {};kind=row.entity_type
    if kind=='task_attachment':
        link=TaskAttachment(task_id=row.entity_id,assignee_id=ctx['assignee_id'],checklist_item_id=ctx.get('checklist_item_id'),uploaded_by=row.user_id,kind='photo',media_id=asset.id,mime_type=asset.mime_type,label=ctx.get('label'))
    elif kind=='knowledge_media':
        link=KnowledgeMedia(article_id=row.entity_id,kind='photo',media_id=asset.id,mime_type=asset.mime_type,file_size=asset.size_bytes,uploaded_by=row.user_id)
    elif kind=='test_question':
        parent.media_id=asset.id;parent.image_telegram_file_id=None;parent.image_mime_type=asset.mime_type
        link=parent
    else:
        link=Photo(entity_type=kind,entity_id=row.entity_id,store_id=getattr(parent,'store_id',None),uploaded_by=row.user_id,media_id=asset.id,label=ctx.get('label'))
    db.add(link);db.flush()
    db.add(MediaAudit(media_id=asset.id,user_id=row.user_id,entity_type=kind,entity_id=row.entity_id,action='attach'))
    row.result_json={'id':link.id,'media_id':asset.id,'entity_type':kind,'entity_id':row.entity_id}
    row.status='ready';row.error=None

def normalize(payload):
    with warnings.catch_warnings():
        warnings.simplefilter('error',Image.DecompressionBombWarning)
        with Image.open(BytesIO(payload)) as src:
            if src.format not in {'JPEG','PNG','WEBP'}: raise ValueError('Поддерживаются JPEG, PNG и WebP')
            if src.width*src.height>MAX_PIXELS: raise ValueError('Слишком большое разрешение фотографии')
            src.load()
            im=ImageOps.exif_transpose(src).convert('RGB')
    im.thumbnail((3200,3200))
    full=BytesIO();im.save(full,format='JPEG',quality=90,optimize=True)
    size=im.size
    im.thumbnail((480,480));thumb=BytesIO();im.save(thumb,format='JPEG',quality=82,optimize=True)
    return full.getvalue(),thumb.getvalue(),size

def process_one(upload_id):
    """Lease prevents two workers publishing the same upload. Network outside DB tx."""
    token=str(uuid4()); now=datetime.utcnow()
    with database.session_scope() as db:
        row=locked(db,MediaUpload,upload_id)
        if not row or row.status not in {'queued','processing'}: return
        if row.status=='processing' and row.updated_at>now-timedelta(minutes=5): return
        row.status='processing';row.lease_token=token;row.updated_at=now
        base=f'{PREFIX}/{now:%Y/%m/%d}/{token}'
        candidate_keys=[base+'.jpg',base+'.thumb.jpg']
        row.context_json={**(row.context_json or {}),'candidate_keys':list((row.context_json or {}).get('candidate_keys',[]))+candidate_keys}
        temporary=row.temporary_key;expected=row.expected_size
    keys=[];s3=None
    try:
        s3=_s3_client()
        # Read a bounded snapshot. A later replay of presigned PUT cannot change
        # the validated bytes or the final server-only keys.
        obj=s3.get_object(Bucket=S3_BUCKET,Key=temporary)
        body=obj['Body']
        try:
            if int(obj.get('ContentLength',0))>MAX_BYTES: raise ValueError('Файл превышает допустимый размер')
            payload=body.read(MAX_BYTES+1)
        finally: body.close()
        if len(payload)!=expected or len(payload)>MAX_BYTES: raise ValueError('Размер загруженного файла не совпадает')
        full,thumb,(width,height)=normalize(payload)
        asset_id=token
        keys=candidate_keys
        for key,data in zip(keys,[full,thumb]):
            s3.put_object(Bucket=S3_BUCKET,Key=key,Body=data,ContentType='image/jpeg')
        with database.session_scope() as db:
            row=locked(db,MediaUpload,upload_id)
            if row.status!='processing' or row.lease_token!=token: raise ValueError('Загрузка отменена или обработана другим процессом')
            user=db.get(User,row.user_id)
            if not user or not user.active or user.status!='active': raise HTTPException(403,'Доступ сотрудника отключён')
            parent=authorize(db,user,row.entity_type,row.entity_id,row.context_json)
            asset=MediaAsset(id=asset_id,s3_key=keys[0],thumbnail_key=keys[1],mime_type='image/jpeg',size_bytes=len(full),width=width,height=height,uploaded_by=user.id)
            db.add(asset);db.flush();attach(db,row,asset,parent)
        keys=[] # published objects are never deleted by failure cleanup
    except Exception as exc:
        message=str(exc.detail) if isinstance(exc,HTTPException) else str(exc) if isinstance(exc,ValueError) else 'Не удалось обработать фото. Повторите отправку'
        log.warning('Media processing failed: %s (%s)',upload_id,type(exc).__name__)
        with database.session_scope() as db:
            row=locked(db,MediaUpload,upload_id)
            if row and row.lease_token==token and row.status=='processing': row.status='failed';row.error=message[:240]
    finally:
        for key in keys:
            try:s3.delete_object(Bucket=S3_BUCKET,Key=key)
            except Exception:log.warning('Media cleanup deferred')

def cleanup_expired():
    # Keep temp until signature expiry: otherwise replay can recreate it after deletion.
    with database.session_scope() as db:
        rows=list(db.scalars(select(MediaUpload).where(MediaUpload.expires_at<datetime.utcnow()-timedelta(hours=1),MediaUpload.status.in_(['pending','ready','failed','cancelled'])).limit(100)).all())
        items=[(x.id,x.temporary_key,(x.context_json or {}).get('candidate_keys',[])) for x in rows if not (x.context_json or {}).get('temporary_cleaned')]
    for identity,key,candidates in items:
        if not key.startswith(PREFIX+'/tmp/'): continue
        try:
            _s3_client().delete_object(Bucket=S3_BUCKET,Key=key)
            with database.session_scope() as db:
                used=set(db.scalars(select(MediaAsset.s3_key).where(MediaAsset.s3_key.in_(candidates))).all())|set(db.scalars(select(MediaAsset.thumbnail_key).where(MediaAsset.thumbnail_key.in_(candidates))).all())
            for orphan in candidates:
                if orphan not in used and orphan.startswith(PREFIX+'/'):
                    _s3_client().delete_object(Bucket=S3_BUCKET,Key=orphan)
            with database.session_scope() as db:
                row=locked(db,MediaUpload,identity)
                if row and row.status in {'pending','ready','failed','cancelled'}:
                    if row.status=='pending':row.status='expired'
                    row.context_json={**(row.context_json or {}),'temporary_cleaned':True}
                    if row.status in {'ready','failed','cancelled'}: row.status='cleaned_'+row.status
        except Exception:log.warning('Temporary upload cleanup failed')

def process_pending():
    with database.session_scope() as db:
        ids=list(db.scalars(select(MediaUpload.id).where(or_(MediaUpload.status=='queued',(MediaUpload.status=='processing')&(MediaUpload.updated_at<datetime.utcnow()-timedelta(minutes=5)))).order_by(MediaUpload.created_at).limit(5)).all())
    for identity in ids: process_one(identity)

def start_worker():
    global _thread,_message_thread
    if _thread and _thread.is_alive():return
    _stop.clear()
    def loop():
        if os.getenv("MEDIA_AUTO_CORS","true").lower() in {"1","true","yes"}:
            try:
                from app.services.media_cors import configure
                log.info("Media CORS: %s",configure())
            except Exception as exc:log.warning("Не удалось настроить CORS для фото (%s). Выполните scripts/configure_media_cors.py с правом настройки CORS",type(exc).__name__)
        ticks=0
        while not _stop.is_set():
            try:
                process_pending()
                if ticks%5==0:process_legacy_thumbnail()
                if ticks%300==0:cleanup_expired()
            except Exception: log.exception('Media worker tick failed')
            ticks+=1;_stop.wait(2)
    def message_loop():
        from app.services.telegram_inbox import process_inbox
        from app.services.delivery_worker import process_deliveries
        while not _stop.is_set():
            try:process_inbox();process_deliveries()
            except Exception:log.exception('Message worker tick failed')
            _stop.wait(2)
    _thread=threading.Thread(target=loop,name='core-media',daemon=True);_thread.start()
    _message_thread=threading.Thread(target=message_loop,name='core-messages',daemon=True);_message_thread.start()

def stop_worker():
    _stop.set()
    if _thread:_thread.join(timeout=3)
    if _message_thread:_message_thread.join(timeout=3)

def process_legacy_thumbnail():
    """Gradually add previews for existing S3 assets without moving originals."""
    with database.session_scope() as db:
        row=db.scalar(select(MediaAsset).where(MediaAsset.thumbnail_key.is_(None)).limit(1))
        if not row:return
        identity,key=row.id,row.s3_key
    try:
        obj=_s3_client().get_object(Bucket=S3_BUCKET,Key=key);body=obj['Body']
        try:payload=body.read(MAX_BYTES+1)
        finally:body.close()
        if len(payload)>MAX_BYTES:raise ValueError('legacy image too large')
        _,thumb,_=normalize(payload)
        thumb_key=f'{PREFIX}/previews/{identity}.jpg'
        _s3_client().put_object(Bucket=S3_BUCKET,Key=thumb_key,Body=thumb,ContentType='image/jpeg')
    except Exception:
        thumb_key='' # keep original readable; avoid retrying the same broken object endlessly
    with database.session_scope() as db:
        row=db.get(MediaAsset,identity)
        if row and row.thumbnail_key is None:row.thumbnail_key=thumb_key
