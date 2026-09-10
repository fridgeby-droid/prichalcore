from datetime import datetime,timedelta
from fastapi import APIRouter,Depends,HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import StreamingResponse
import httpx

from app.core.config import PHOTO_REQUEST_TTL_MINUTES
from app.core.security import get_current_user,assert_store_access
from app.db.database import get_db
from app.db.models import PhotoRequest,Photo
from app.services.telegram import send_message,get_file_path,file_download_url

router=APIRouter(prefix="/api/photos",tags=["photos"])

class PhotoRequestIn(BaseModel):
    entity_type:str
    entity_id:int
    label:str|None=None

@router.post("/request")
def request_photo(payload:PhotoRequestIn,user=Depends(get_current_user),db:Session=Depends(get_db)):
    for old in db.scalars(select(PhotoRequest).where(PhotoRequest.user_id==user.id,PhotoRequest.status=="waiting")).all():old.status="cancelled"
    req=PhotoRequest(user_id=user.id,entity_type=payload.entity_type,entity_id=payload.entity_id,label=payload.label,status="waiting",expires_at=datetime.utcnow()+timedelta(minutes=PHOTO_REQUEST_TTL_MINUTES))
    db.add(req);db.commit();db.refresh(req)
    send_message(user.telegram_id,f"📷 Отправьте фотографию для: <b>{payload.label or payload.entity_type}</b>\nФото будет автоматически прикреплено к записи.")
    return {"id":req.id,"status":"waiting"}

@router.get("")
def list_photos(entity_type:str,entity_id:int,user=Depends(get_current_user),db:Session=Depends(get_db)):
    rows=db.scalars(select(Photo).where(Photo.entity_type==entity_type,Photo.entity_id==entity_id).order_by(Photo.created_at)).all()
    return [{"id":x.id,"label":x.label,"created_at":x.created_at.isoformat()} for x in rows]

@router.get("/{photo_id}/content")
def photo_content(photo_id:int,user=Depends(get_current_user),db:Session=Depends(get_db)):
    p=db.get(Photo,photo_id)
    if not p:raise HTTPException(404,"Фото не найдено")
    if p.store_id is not None: assert_store_access(db,user,p.store_id)
    elif p.uploaded_by!=user.id and user.role not in {"operations_director","leader","admin"}: raise HTTPException(403,"Нет доступа к фото")
    path=get_file_path(p.telegram_file_id)
    url=file_download_url(path)
    client=httpx.Client(timeout=30)
    response=client.stream("GET",url)
    response.__enter__()
    if response.status_code!=200:
        response.__exit__(None,None,None);client.close();raise HTTPException(502,"Не удалось получить фото из Telegram")
    ctype=response.headers.get("content-type","image/jpeg")
    def gen():
        try:
            for chunk in response.iter_bytes():yield chunk
        finally:
            response.__exit__(None,None,None);client.close()
    return StreamingResponse(gen(),media_type=ctype)
