from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.core.security import get_current_user, MANAGEMENT_ROLES
from app.core.serializers import row
from app.db.database import get_db
from app.db.models import Broadcast

router=APIRouter(prefix="/api/broadcasts",tags=["broadcasts"])

class BroadcastIn(BaseModel):
    title:str
    message:str
    role_targets:list[str]|None=None
    store_ids:list[int]|None=None
    send_time:str
    weekdays:list[int]|None=None
    button_text:str|None=None
    button_url:str|None=None
    active:bool=True

@router.post("")
def create(payload:BroadcastIn,user=Depends(get_current_user),db:Session=Depends(get_db)):
    if user.role not in MANAGEMENT_ROLES:raise HTTPException(403,"Нет доступа")
    obj=Broadcast(**payload.model_dump(),created_by=user.id);db.add(obj);db.commit();db.refresh(obj);return {"id":obj.id}

@router.get("")
def list_all(user=Depends(get_current_user),db:Session=Depends(get_db)):
    if user.role not in MANAGEMENT_ROLES:raise HTTPException(403,"Нет доступа")
    return [row(x,"id","title","message","role_targets","store_ids","send_time","weekdays","button_text","button_url","active","created_by","created_at") for x in db.scalars(select(Broadcast).order_by(Broadcast.created_at.desc())).all()]

@router.patch("/{item_id}")
def update(item_id:int,payload:dict,user=Depends(get_current_user),db:Session=Depends(get_db)):
    if user.role not in MANAGEMENT_ROLES:raise HTTPException(403,"Нет доступа")
    obj=db.get(Broadcast,item_id)
    if not obj:raise HTTPException(404,"Рассылка не найдена")
    for k in ("title","message","role_targets","store_ids","send_time","weekdays","button_text","button_url","active"):
        if k in payload:setattr(obj,k,payload[k])
    db.commit();return {"ok":True}
