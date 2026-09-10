from datetime import date
from fastapi import APIRouter,Depends,HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.core.security import get_current_user,MANAGEMENT_ROLES,assert_store_access,user_store_ids
from app.core.serializers import row
from app.db.database import get_db
from app.db.models import PlanFact

router=APIRouter(prefix="/api/plans",tags=["plans"])

class PlanIn(BaseModel):
    store_id:int
    period_type:str="monthly"
    period_date:date
    plan:float
    fact:float=0

@router.post("")
def upsert(payload:PlanIn,user=Depends(get_current_user),db:Session=Depends(get_db)):
    if user.role not in MANAGEMENT_ROLES:raise HTTPException(403,"Нет доступа")
    assert_store_access(db,user,payload.store_id)
    q=select(PlanFact).where(PlanFact.store_id==payload.store_id,PlanFact.period_type==payload.period_type,PlanFact.period_date==payload.period_date)
    obj=db.scalar(q)
    if not obj:obj=PlanFact(**payload.model_dump(),source="manual",updated_by=user.id);db.add(obj)
    else:obj.plan=payload.plan;obj.fact=payload.fact;obj.updated_by=user.id;obj.source="manual"
    db.commit();return {"ok":True}

@router.get("")
def list_all(period_type:str|None=None,user=Depends(get_current_user),db:Session=Depends(get_db)):
    q=select(PlanFact).order_by(PlanFact.period_date.desc()).limit(300)
    allowed=user_store_ids(db,user)
    if user.role not in {"operations_director","leader","admin"}:q=q.where(PlanFact.store_id.in_(allowed or [-1]))
    if period_type:q=q.where(PlanFact.period_type==period_type)
    return [row(x,"id","store_id","period_type","period_date","plan","fact","source","updated_at") for x in db.scalars(q).all()]
