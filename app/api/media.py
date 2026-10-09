from datetime import datetime, timedelta
from uuid import uuid4
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.core.security import get_current_user
from app.db.database import get_db
from app.db.models import MediaUpload, MediaAudit, Photo, TaskAttachment, KnowledgeMedia, TrainingQuestion
from app.services.media import authorize, locked, MAX_BYTES, PREFIX
from app.services.storage import _s3_client
from app.core.config import S3_BUCKET

router=APIRouter(prefix='/api/media',tags=['media'])
class UploadIn(BaseModel):
    entity_type: Literal['profile_avatar','shift_report_field','inspection','inspection_value','task_attachment','knowledge_media','test_question']
    entity_id: int = Field(gt=0)
    size_bytes: int = Field(gt=0)
    request_key: str = Field(min_length=8,max_length=64)
    assignee_id: int | None = None
    checklist_item_id: int | None = None
    label: str | None = Field(default=None,max_length=240)

def response(row):
    return {'upload_id':row.id,'status':row.status.removeprefix('cleaned_'),'error':row.error,'result':row.result_json}

def signed(row):
    result=response(row)
    if row.status=='pending' and row.expires_at>datetime.utcnow():
        seconds=max(1,int((row.expires_at-datetime.utcnow()).total_seconds()))
        result.update(url=_s3_client().generate_presigned_url('put_object',Params={'Bucket':S3_BUCKET,'Key':row.temporary_key},ExpiresIn=seconds),method='PUT',expires_at=row.expires_at.isoformat()+'Z')
    return result

@router.post('/uploads')
def create(payload:UploadIn,user=Depends(get_current_user),db:Session=Depends(get_db)):
    if payload.size_bytes>MAX_BYTES:raise HTTPException(413,'Фото слишком большое')
    old=db.scalar(select(MediaUpload).where(MediaUpload.user_id==user.id,MediaUpload.request_key==payload.request_key))
    if old:
        if (old.entity_type,old.entity_id,old.expected_size)!=(payload.entity_type,payload.entity_id,payload.size_bytes):raise HTTPException(409,'Ключ загрузки уже использован')
        return signed(old)
    context={'assignee_id':payload.assignee_id,'checklist_item_id':payload.checklist_item_id,'label':payload.label}
    parent=authorize(db,user,payload.entity_type,payload.entity_id,context)
    # Serialization on user row makes per-user quotas meaningful under concurrency.
    from app.db.models import User
    locked(db,User,user.id)
    count=db.scalar(select(func.count(MediaUpload.id)).where(MediaUpload.user_id==user.id,MediaUpload.created_at>datetime.utcnow()-timedelta(hours=1)))
    if count>=100:raise HTTPException(429,'Слишком много загрузок. Повторите позже')
    if payload.entity_type=='shift_report_field':context['revision']=parent.revision
    identity=str(uuid4())
    row=MediaUpload(id=identity,user_id=user.id,request_key=payload.request_key,entity_type=payload.entity_type,entity_id=payload.entity_id,context_json=context,temporary_key=f'{PREFIX}/tmp/{identity}',expected_size=payload.size_bytes,expires_at=datetime.utcnow()+timedelta(minutes=10))
    db.add(row)
    try:db.commit()
    except IntegrityError:
        db.rollback();row=db.scalar(select(MediaUpload).where(MediaUpload.user_id==user.id,MediaUpload.request_key==payload.request_key))
        if not row:raise
    return signed(row)

@router.post('/uploads/{upload_id}/complete')
def complete(upload_id:str,user=Depends(get_current_user),db:Session=Depends(get_db)):
    row=locked(db,MediaUpload,upload_id)
    if not row or row.user_id!=user.id:raise HTTPException(404,'Загрузка не найдена')
    if row.status in {'ready','cleaned_ready','queued','processing'}:return response(row)
    if row.status!='pending' or row.expires_at<datetime.utcnow():raise HTTPException(409,'Создайте новую загрузку')
    authorize(db,user,row.entity_type,row.entity_id,row.context_json)
    row.status='queued';row.updated_at=datetime.utcnow();db.commit()
    return response(row)

@router.get('/uploads/{upload_id}')
def status(upload_id:str,user=Depends(get_current_user),db:Session=Depends(get_db)):
    row=db.get(MediaUpload,upload_id)
    if not row or row.user_id!=user.id:raise HTTPException(404,'Загрузка не найдена')
    return response(row)

class DeleteIn(BaseModel):
    entity_type:str
    entity_id:int
    attachment_id:int
    assignee_id:int|None=None
    checklist_item_id:int|None=None

@router.post('/detach')
def detach(payload:DeleteIn,user=Depends(get_current_user),db:Session=Depends(get_db)):
    context=payload.model_dump()
    parent=authorize(db,user,payload.entity_type,payload.entity_id,context)
    kind=payload.entity_type
    if kind=='test_question':
        row=parent;mid=row.media_id;row.media_id=None;row.image_telegram_file_id=None
    else:
        model=TaskAttachment if kind=='task_attachment' else KnowledgeMedia if kind=='knowledge_media' else Photo
        row=locked(db,model,payload.attachment_id)
        valid=row is not None
        if isinstance(row,Photo):valid=valid and row.entity_type==kind and row.entity_id==payload.entity_id
        elif isinstance(row,TaskAttachment):valid=valid and row.task_id==payload.entity_id and row.assignee_id==payload.assignee_id and row.kind=='photo'
        elif isinstance(row,KnowledgeMedia):valid=valid and row.article_id==payload.entity_id and row.kind=='photo'
        if not valid:raise HTTPException(404,'Фото не найдено')
        mid=row.media_id
        if kind=="profile_avatar":
            for old in db.scalars(select(Photo).where(Photo.entity_type==kind,Photo.entity_id==payload.entity_id)).all():db.delete(old)
        else:db.delete(row)
    # Unlink only. Other entities may reference the same immutable asset.
    db.add(MediaAudit(media_id=mid,user_id=user.id,entity_type=kind,entity_id=payload.entity_id,action='detach'))
    db.commit();return {'ok':True}

@router.get('/attachments')
def attachments(entity_type:str,entity_id:int,assignee_id:int|None=None,checklist_item_id:int|None=None,user=Depends(get_current_user),db:Session=Depends(get_db)):
    parent=authorize(db,user,entity_type,entity_id,{'assignee_id':assignee_id,'checklist_item_id':checklist_item_id})
    if entity_type=='test_question':
        return [{'id':parent.id,'url':f'/api/testing/questions/{parent.id}/image'}] if parent.media_id or parent.image_telegram_file_id else []
    if entity_type=='task_attachment':
        q=select(TaskAttachment).where(TaskAttachment.task_id==entity_id,TaskAttachment.assignee_id==assignee_id,TaskAttachment.kind=='photo')
        if checklist_item_id:q=q.where(TaskAttachment.checklist_item_id==checklist_item_id)
        rows=db.scalars(q).all()
        return [{'id':r.id,'url':f'/api/tasks-v2/{entity_id}/attachments/{r.id}/content'} for r in rows]
    if entity_type=='knowledge_media':
        rows=db.scalars(select(KnowledgeMedia).where(KnowledgeMedia.article_id==entity_id,KnowledgeMedia.kind=='photo')).all()
        return [{'id':r.id,'url':f'/api/knowledge/media/{r.id}/content'} for r in rows]
    rows=db.scalars(select(Photo).where(Photo.entity_type==entity_type,Photo.entity_id==entity_id).order_by(Photo.created_at.desc(),Photo.id.desc())).all()
    if entity_type=='profile_avatar':rows=rows[:1]
    return [{'id':r.id,'url':'/api/profile/avatar' if entity_type=='profile_avatar' else f'/api/photos/{r.id}/content'} for r in rows]
