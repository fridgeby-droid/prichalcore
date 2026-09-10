from datetime import date
from fastapi import APIRouter,Depends,HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.core.security import get_current_user,MANAGEMENT_ROLES
from app.core.serializers import row
from app.db.database import get_db
from app.db.models import Mentorship

router=APIRouter(prefix="/api/mentoring",tags=["mentoring"])

class MentorshipIn(BaseModel):
    mentor_id:int
    trainee_id:int
    store_id:int|None=None
    start_date:date|None=None
    stage:str="start"

@router.post("")
def create(payload:MentorshipIn,user=Depends(get_current_user),db:Session=Depends(get_db)):
    if user.role not in MANAGEMENT_ROLES:raise HTTPException(403,"Нет доступа")
    data=payload.model_dump();
    if data["start_date"] is None:data["start_date"]=date.today()
    obj=Mentorship(**data);db.add(obj);db.commit();db.refresh(obj);return {"id":obj.id}

@router.get("")
def list_all(user=Depends(get_current_user),db:Session=Depends(get_db)):
    q=select(Mentorship).order_by(Mentorship.created_at.desc()).limit(200)
    if user.role=="mentor":q=q.where(Mentorship.mentor_id==user.id)
    elif user.role=="seller":q=q.where(Mentorship.trainee_id==user.id)
    return [row(x,"id","store_id","mentor_id","trainee_id","start_date","stage","result_comment","status","created_at") for x in db.scalars(q).all()]

@router.patch("/{item_id}")
def update(item_id:int,payload:dict,user=Depends(get_current_user),db:Session=Depends(get_db)):
    obj=db.get(Mentorship,item_id)
    if not obj:raise HTTPException(404,"Связка не найдена")
    if user.role not in MANAGEMENT_ROLES and user.id!=obj.mentor_id:raise HTTPException(403,"Нет доступа")
    for k in ("stage","result_comment","status"):
        if k in payload:setattr(obj,k,payload[k])
    db.commit();return {"ok":True}
