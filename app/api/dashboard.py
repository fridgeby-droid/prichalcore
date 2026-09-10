from datetime import datetime,date,timedelta
from fastapi import APIRouter,Depends
from sqlalchemy import select,func
from sqlalchemy.orm import Session
from app.core.security import get_current_user,user_store_ids
from app.db.database import get_db
from app.db.models import Order,ShiftReport,Task,Inspection,CashCollection,PlanFact,Store
from app.api.ratings import compute_store_rating

router=APIRouter(prefix="/api/dashboard",tags=["dashboard"])

@router.get("")
def dashboard(user=Depends(get_current_user),db:Session=Depends(get_db)):
    allowed=user_store_ids(db,user)
    start=datetime.combine(date.today(),datetime.min.time())
    def count(model,field):
        q=select(func.count(model.id)).where(field>=start)
        if user.role not in {"operations_director","leader","admin"} and hasattr(model,"store_id"):q=q.where(model.store_id.in_(allowed or [-1]))
        return db.scalar(q) or 0
    overdue_q=select(func.count(Task.id)).where(Task.assigned_to==user.id,Task.status!="done",Task.deadline.is_not(None),Task.deadline<datetime.utcnow())
    stores=[]
    sq=select(Store).where(Store.active.is_(True))
    if user.role not in {"operations_director","leader","admin"}:sq=sq.where(Store.id.in_(allowed or [-1]))
    for s in db.scalars(sq.order_by(Store.name)).all():
        stores.append({"id":s.id,"name":s.name,**compute_store_rating(db,s.id)})
    return {
        "today":{"orders":count(Order,Order.created_at),"shifts":count(ShiftReport,ShiftReport.submitted_at),"inspections":count(Inspection,Inspection.completed_at),"cash":count(CashCollection,CashCollection.collected_at)},
        "my_overdue_tasks":db.scalar(overdue_q) or 0,
        "stores":stores,
    }
