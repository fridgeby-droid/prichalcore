from fastapi import APIRouter,Depends,HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.core.security import get_current_user
from app.core.permissions import require_any_access,assigned_store_ids
from app.core.serializers import row
from app.db.database import get_db
from app.db.models import Store,Supplier,StoreSupplierSchedule,User,UserStore

router=APIRouter(prefix="/api/reference",tags=["reference"])
REFERENCE_KEYS=("orders.list","orders.create","orders.schedule_view","schedule.my","schedule.stores","schedule.edit","shifts.submit","shifts.control","tasks.my","tasks.create","inspections.conduct","employees.view","knowledge.read","testing.take","testing.control")

@router.get("/stores")
def stores(user=Depends(get_current_user),db:Session=Depends(get_db)):
    p=require_any_access(db,user,REFERENCE_KEYS,"view");q=select(Store).where(Store.active.is_(True))
    if p["data_scope"]!="network":q=q.where(Store.id.in_(assigned_store_ids(db,user) or [-1]))
    return [row(x,"id","name","code") for x in db.scalars(q.order_by(Store.name)).all()]

@router.get("/suppliers")
def suppliers(store_id:int|None=None,user=Depends(get_current_user),db:Session=Depends(get_db)):
    p=require_any_access(db,user,("orders.create","orders.list","orders.schedule_view","orders.schedule_edit"),"view");q=select(Supplier).where(Supplier.active.is_(True)).order_by(Supplier.name)
    if store_id:
        if p["data_scope"]!="network" and store_id not in assigned_store_ids(db,user):raise HTTPException(403,"Нет доступа к магазину")
        ids=list(db.scalars(select(StoreSupplierSchedule.supplier_id).where(StoreSupplierSchedule.store_id==store_id,StoreSupplierSchedule.active.is_(True))).all())
        if ids:q=q.where(Supplier.id.in_(ids))
    return [row(x,"id","name","deadline_time") for x in db.scalars(q).all()]

@router.get("/users")
def users(store_id:int|None=None,user=Depends(get_current_user),db:Session=Depends(get_db)):
    p=require_any_access(db,user,("employees.view","tasks.create","schedule.edit","shifts.control","testing.assign"),"view");q=select(User).where(User.active.is_(True),User.status=="active").order_by(User.full_name)
    if p["data_scope"]=="own":q=q.where(User.id==user.id)
    elif p["data_scope"]=="stores":
        ids=assigned_store_ids(db,user)
        if store_id is not None and store_id not in ids:raise HTTPException(403,"Нет доступа к магазину")
        target=[store_id] if store_id is not None else ids
        uids=list(db.scalars(select(UserStore.user_id).where(UserStore.store_id.in_(target or [-1]))).all());uids=list(set(uids+[user.id]));q=q.where(User.id.in_(uids or [-1]))
    elif store_id is not None:
        uids=list(db.scalars(select(UserStore.user_id).where(UserStore.store_id==store_id)).all());q=q.where(User.id.in_(uids or [-1]))
    return [row(x,"id","full_name","role","role_key","telegram_id") for x in db.scalars(q).all()]
