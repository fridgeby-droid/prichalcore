from io import BytesIO
from pathlib import PurePosixPath
from uuid import uuid4
from urllib.parse import quote
from zipfile import ZipFile, BadZipFile
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from starlette.responses import StreamingResponse
from app.db.database import get_db
from app.db.models import Order, OrderAttachment
from app.core.security import get_current_user
from app.core.permissions import assert_own_or_scope
from app.core.config import S3_BUCKET, S3_PREFIX
from app.services import storage

router=APIRouter(prefix='/api/orders',tags=['order-files'])
MAX_BYTES=10*1024*1024

def allowed_order(db, oid, user, edit=False):
    stmt=select(Order).where(Order.id==oid)
    if edit:stmt=stmt.with_for_update()
    order=db.scalar(stmt)
    if not order:raise HTTPException(404,'Заявка не найдена')
    assert_own_or_scope(db,user,'orders.edit_new' if edit else 'orders.list',owner_user_id=order.created_by,store_id=order.store_id,minimum='edit' if edit else 'view')
    if edit and order.status not in {'new','comment_only'}:raise HTTPException(409,'Файлы можно менять только до принятия заявки')
    return order

def serialize(row):
    return {'id':row.id,'filename':row.filename,'size_bytes':row.size_bytes}

def validate_excel(filename,data):
    ext=PurePosixPath(filename).suffix.lower()
    if ext=='.xls' and data.startswith(bytes.fromhex('D0CF11E0A1B11AE1')):
        return 'application/vnd.ms-excel'
    if ext=='.xlsx':
        try:
            with ZipFile(BytesIO(data)) as z:
                infos=z.infolist();names=set(z.namelist())
                if len(infos)>10000 or sum(i.file_size for i in infos)>100*1024*1024:raise HTTPException(400,'Слишком большой распакованный Excel')
                if {'[Content_Types].xml','xl/workbook.xml'}<=names and not any('vbaproject' in n.lower() for n in names):
                    return 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        except (BadZipFile,ValueError):pass
    raise HTTPException(400,'Выберите файл Excel .xlsx или .xls с корректным форматом')

@router.get('/{oid}/attachments')
def listing(oid:int,user=Depends(get_current_user),db:Session=Depends(get_db)):
    allowed_order(db,oid,user)
    return [serialize(x) for x in db.scalars(select(OrderAttachment).where(OrderAttachment.order_id==oid,OrderAttachment.active.is_(True)).order_by(OrderAttachment.id))]

@router.post('/{oid}/attachments')
def upload(oid:int,file:UploadFile=File(...),user=Depends(get_current_user),db:Session=Depends(get_db)):
    allowed_order(db,oid,user,True)
    count=db.scalar(select(func.count()).select_from(OrderAttachment).where(OrderAttachment.order_id==oid,OrderAttachment.active.is_(True)))
    if count>=5:raise HTTPException(400,'К заявке можно прикрепить не более 5 файлов')
    data=file.file.read(MAX_BYTES+1)
    if not data or len(data)>MAX_BYTES:raise HTTPException(400,'Файл должен быть непустым и не больше 10 МБ')
    filename=PurePosixPath((file.filename or 'file').replace('\\','/')).name
    filename=''.join(c for c in filename if ord(c)>=32)[:180]
    mime=validate_excel(filename,data)
    key=f"{S3_PREFIX.strip('/')}/order-files/{oid}/{uuid4()}{PurePosixPath(filename).suffix.lower()}"
    try:
        client=storage._s3_client()
        if client is None:raise RuntimeError('S3 required')
        client.put_object(Bucket=S3_BUCKET,Key=key,Body=data,ContentType=mime)
    except Exception:raise HTTPException(503,'Не удалось сохранить файл в хранилище. Повторите загрузку') from None
    row=OrderAttachment(order_id=oid,uploaded_by=user.id,filename=filename,mime_type=mime,size_bytes=len(data),s3_key=key)
    db.add(row)
    try:db.commit();db.refresh(row)
    except Exception:
        db.rollback()
        try:client.delete_object(Bucket=S3_BUCKET,Key=key)
        except Exception:pass
        raise
    return serialize(row)

@router.get('/{oid}/attachments/{aid}/content')
def download(oid:int,aid:int,user=Depends(get_current_user),db:Session=Depends(get_db)):
    allowed_order(db,oid,user)
    row=db.scalar(select(OrderAttachment).where(OrderAttachment.id==aid,OrderAttachment.order_id==oid,OrderAttachment.active.is_(True)))
    if not row:raise HTTPException(404,'Файл не найден')
    try:body=storage._s3_client().get_object(Bucket=S3_BUCKET,Key=row.s3_key)['Body']
    except Exception:raise HTTPException(503,'Файл временно недоступен') from None
    def chunks():
        try:
            while chunk:=body.read(65536):yield chunk
        finally:body.close()
    return StreamingResponse(chunks(),media_type=row.mime_type,headers={'Content-Disposition':"attachment; filename*=UTF-8''"+quote(row.filename,safe=''),'Cache-Control':'private, no-store'})

@router.delete('/{oid}/attachments/{aid}')
def remove(oid:int,aid:int,user=Depends(get_current_user),db:Session=Depends(get_db)):
    allowed_order(db,oid,user,True)
    row=db.scalar(select(OrderAttachment).where(OrderAttachment.id==aid,OrderAttachment.order_id==oid,OrderAttachment.active.is_(True)))
    if not row:raise HTTPException(404,'Файл не найден')
    # Keep immutable object for already queued deliveries and history.
    row.active=False;db.commit()
    return {'ok':True}
