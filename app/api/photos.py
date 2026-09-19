from datetime import datetime,timedelta
from fastapi import APIRouter,Depends,HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import StreamingResponse
import httpx

from app.core.config import PHOTO_REQUEST_TTL_MINUTES
from app.core.security import get_current_user
from app.core.permissions import require_access, assert_store_scope, has_access, role_key
from app.db.database import get_db
from app.db.models import PhotoRequest,Photo,ShiftReportValue,ShiftReport,InspectionValue,Inspection
from app.services.telegram import send_message,get_file_path,file_download_url

router=APIRouter(prefix="/api/photos",tags=["photos"])

class PhotoRequestIn(BaseModel):
    entity_type:str
    entity_id:int
    label:str|None=None

def _authorize_entity(db:Session,user,entity_type:str,entity_id:int,write:bool=False):
    minimum="edit" if write else "view"
    if entity_type=="shift_report_field":
        value=db.get(ShiftReportValue,entity_id)
        report=db.get(ShiftReport,value.report_id) if value else None
        if not report: raise HTTPException(404,"Пересменка не найдена")
        if report.submitted_by==user.id:
            require_access(db,user,"shifts.submit",minimum); return
        require_access(db,user,"shifts.control",minimum if write else "view")
        assert_store_scope(db,user,"shifts.control",report.store_id,minimum=minimum if write else "view",own_as_assigned=True); return
    if entity_type in {"inspection","inspection_value"}:
        if entity_type=="inspection": insp=db.get(Inspection,entity_id)
        else:
            value=db.get(InspectionValue,entity_id); insp=db.get(Inspection,value.inspection_id) if value else None
        if not insp: raise HTTPException(404,"Проверка не найдена")
        if insp.manager_id==user.id:
            require_access(db,user,"inspections.conduct",minimum); return
        key="inspections.control" if has_access(db,user,"inspections.control") else "inspections.history"
        require_access(db,user,key,minimum if write else "view")
        assert_store_scope(db,user,key,insp.store_id,minimum=minimum if write else "view",own_as_assigned=True); return
    if role_key(user)=="admin": return
    raise HTTPException(403,"Нет доступа к этому типу вложения")


@router.post("/request")
def request_photo(payload:PhotoRequestIn,user=Depends(get_current_user),db:Session=Depends(get_db)):
    _authorize_entity(db,user,payload.entity_type,payload.entity_id,write=True)
    for old in db.scalars(select(PhotoRequest).where(PhotoRequest.user_id==user.id,PhotoRequest.status=="waiting")).all():old.status="cancelled"
    req=PhotoRequest(user_id=user.id,entity_type=payload.entity_type,entity_id=payload.entity_id,label=payload.label,status="waiting",expires_at=datetime.utcnow()+timedelta(minutes=PHOTO_REQUEST_TTL_MINUTES))
    db.add(req);db.commit();db.refresh(req)
    send_message(user.telegram_id,f"📷 Отправьте фотографию для: <b>{payload.label or payload.entity_type}</b>\nФото будет автоматически прикреплено к записи.")
    return {"id":req.id,"status":"waiting"}

@router.get("")
def list_photos(entity_type:str,entity_id:int,user=Depends(get_current_user),db:Session=Depends(get_db)):
    _authorize_entity(db,user,entity_type,entity_id,write=False)
    rows=db.scalars(select(Photo).where(Photo.entity_type==entity_type,Photo.entity_id==entity_id).order_by(Photo.created_at)).all()
    return [{"id":x.id,"label":x.label,"created_at":x.created_at.isoformat()} for x in rows]

@router.get("/{photo_id}/content")
def photo_content(photo_id:int,user=Depends(get_current_user),db:Session=Depends(get_db)):
    p=db.get(Photo,photo_id)
    if not p:raise HTTPException(404,"Фото не найдено")
    _authorize_entity(db,user,p.entity_type,p.entity_id,write=False)
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
