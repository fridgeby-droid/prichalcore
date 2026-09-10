from datetime import datetime
from fastapi import APIRouter,Depends,HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.core.security import get_current_user,assert_store_access,MANAGEMENT_ROLES,user_store_ids
from app.core.serializers import row
from app.db.database import get_db
from app.db.models import CashCollection,User

router=APIRouter(prefix="/api/cash",tags=["cash"])

class CashIn(BaseModel):
    store_id:int
    amount:float
    comment:str|None=None

@router.post("")
def create(payload:CashIn,user=Depends(get_current_user),db:Session=Depends(get_db)):
    if user.role not in MANAGEMENT_ROLES:raise HTTPException(403,"Инкассация доступна управляющим и руководству")
    assert_store_access(db,user,payload.store_id)
    obj=CashCollection(store_id=payload.store_id,amount=payload.amount,collected_by=user.id,comment=payload.comment,status="collected")
    db.add(obj);db.commit();db.refresh(obj);return {"id":obj.id}

@router.get("")
def list_cash(user=Depends(get_current_user),db:Session=Depends(get_db)):
    q=select(CashCollection).order_by(CashCollection.collected_at.desc()).limit(200)
    allowed=user_store_ids(db,user)
    if user.role not in {"operations_director","leader","admin"}:q=q.where(CashCollection.store_id.in_(allowed or [-1]))
    return [row(x,"id","store_id","collected_by","handed_to","amount","status","comment","collected_at","handed_at") for x in db.scalars(q).all()]

@router.patch("/{cash_id}/hand-over")
def hand_over(cash_id:int,payload:dict,user=Depends(get_current_user),db:Session=Depends(get_db)):
    if user.role not in MANAGEMENT_ROLES:raise HTTPException(403,"Нет доступа")
    obj=db.get(CashCollection,cash_id)
    if not obj:raise HTTPException(404,"Инкассация не найдена")
    assert_store_access(db,user,obj.store_id)
    obj.handed_to=int(payload.get("handed_to") or user.id)
    obj.handed_at=datetime.utcnow();obj.status="handed"
    db.commit();return {"ok":True}
