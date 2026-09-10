from fastapi import APIRouter,Depends
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.core.security import get_current_user,assert_store_access,user_store_ids
from app.core.serializers import row
from app.db.database import get_db
from app.db.models import Store,Supplier,StoreSupplierSchedule,User,UserStore

router=APIRouter(prefix="/api/reference",tags=["reference"])

@router.get("/stores")
def stores(user=Depends(get_current_user),db:Session=Depends(get_db)):
    allowed=user_store_ids(db,user)
    q=select(Store).where(Store.active.is_(True)).order_by(Store.name)
    if user.role not in {"operations_director","leader","admin"}:q=q.where(Store.id.in_(allowed or [-1]))
    return [row(x,"id","name","code") for x in db.scalars(q).all()]

@router.get("/suppliers")
def suppliers(store_id:int|None=None,user=Depends(get_current_user),db:Session=Depends(get_db)):
    q=select(Supplier).where(Supplier.active.is_(True)).order_by(Supplier.name)
    if store_id:
        assert_store_access(db,user,store_id)
        ids=list(db.scalars(select(StoreSupplierSchedule.supplier_id).where(StoreSupplierSchedule.store_id==store_id,StoreSupplierSchedule.active.is_(True))).all())
        if ids:q=q.where(Supplier.id.in_(ids))
    return [row(x,"id","name","deadline_time") for x in db.scalars(q).all()]

@router.get("/users")
def users(store_id:int|None=None,user=Depends(get_current_user),db:Session=Depends(get_db)):
    q=select(User).where(User.active.is_(True),User.status=="active").order_by(User.full_name)
    rows=db.scalars(q).all()
    if store_id:
        assert_store_access(db,user,store_id)
        out=[]
        for u in rows:
            if u.role in {"operations_director","leader","admin"}:out.append(u);continue
            linked=db.scalar(select(UserStore).where(UserStore.user_id==u.id,UserStore.store_id==store_id))
            if linked:out.append(u)
        rows=out
    return [row(x,"id","full_name","role","telegram_id") for x in rows]
