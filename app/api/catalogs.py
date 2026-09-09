from __future__ import annotations

from datetime import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..auth import allow_roles, current_user
from ..db import get_db
from ..models import Product, Store, StoreProduct, StoreSupplier, Supplier, User, UserRole, UserStatus, UserStore
from ..permissions import store_ids_for_user

router = APIRouter(prefix="/api", tags=["catalogs"])
ADMIN_ROLES = (UserRole.admin, UserRole.operations_director)


class StoreIn(BaseModel):
    name: str = Field(min_length=2, max_length=255)
    code: str | None = None
    is_active: bool = True


@router.get("/stores")
def stores(user: User = Depends(current_user), db: Session = Depends(get_db)):
    ids = store_ids_for_user(db, user)
    q = select(Store).order_by(Store.name)
    if user.role not in {UserRole.admin, UserRole.operations_director, UserRole.executive}:
        q = q.where(Store.id.in_(ids or [-1]))
    return [{"id": s.id, "name": s.name, "code": s.code, "is_active": s.is_active} for s in db.scalars(q).all()]


@router.post("/admin/stores")
def create_store(payload: StoreIn, _: User = Depends(allow_roles(*ADMIN_ROLES)), db: Session = Depends(get_db)):
    s = Store(**payload.model_dump())
    db.add(s); db.commit(); db.refresh(s)
    return {"id": s.id, "name": s.name, "is_active": s.is_active}


@router.patch("/admin/stores/{store_id}")
def update_store(store_id: int, payload: StoreIn, _: User = Depends(allow_roles(*ADMIN_ROLES)), db: Session = Depends(get_db)):
    s = db.get(Store, store_id)
    if not s: raise HTTPException(404, "Store not found")
    for k, v in payload.model_dump().items(): setattr(s, k, v)
    db.commit(); return {"ok": True}


class SupplierIn(BaseModel):
    name: str
    default_deadline: str = "10:00"
    is_active: bool = True


def parse_hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


@router.get("/suppliers")
def suppliers(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(Supplier).where(Supplier.is_active.is_(True)).order_by(Supplier.name)).all()
    return [{"id": x.id, "name": x.name, "default_deadline": x.default_deadline.strftime("%H:%M"), "is_active": x.is_active} for x in rows]


@router.post("/admin/suppliers")
def create_supplier(payload: SupplierIn, _: User = Depends(allow_roles(*ADMIN_ROLES)), db: Session = Depends(get_db)):
    x = Supplier(name=payload.name, default_deadline=parse_hhmm(payload.default_deadline), is_active=payload.is_active)
    db.add(x); db.commit(); db.refresh(x)
    return {"id": x.id, "name": x.name}


@router.patch("/admin/suppliers/{supplier_id}")
def update_supplier(supplier_id: int, payload: SupplierIn, _: User = Depends(allow_roles(*ADMIN_ROLES)), db: Session = Depends(get_db)):
    x = db.get(Supplier, supplier_id)
    if not x: raise HTTPException(404, "Supplier not found")
    x.name = payload.name; x.default_deadline = parse_hhmm(payload.default_deadline); x.is_active = payload.is_active
    db.commit(); return {"ok": True}


class ProductIn(BaseModel):
    supplier_id: int
    name: str
    category: str | None = None
    unit: str = "шт"
    sku: str | None = None
    is_active: bool = True
    store_ids: list[int] = []


@router.get("/products")
def products(supplier_id: int | None = None, store_id: int | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    q = select(Product).where(Product.is_active.is_(True)).order_by(Product.name)
    if supplier_id: q = q.where(Product.supplier_id == supplier_id)
    rows = list(db.scalars(q).all())
    if store_id:
        allowed = set(db.scalars(select(StoreProduct.product_id).where(StoreProduct.store_id == store_id, StoreProduct.is_active.is_(True))).all())
        if allowed: rows = [p for p in rows if p.id in allowed]
    return [{"id": p.id, "supplier_id": p.supplier_id, "name": p.name, "category": p.category, "unit": p.unit, "sku": p.sku, "is_active": p.is_active} for p in rows]


@router.post("/admin/products")
def create_product(payload: ProductIn, _: User = Depends(allow_roles(*ADMIN_ROLES)), db: Session = Depends(get_db)):
    data = payload.model_dump(exclude={"store_ids"})
    p = Product(**data); db.add(p); db.flush()
    for sid in payload.store_ids: db.add(StoreProduct(store_id=sid, product_id=p.id))
    db.commit(); db.refresh(p); return {"id": p.id, "name": p.name}


@router.patch("/admin/products/{product_id}")
def update_product(product_id: int, payload: ProductIn, _: User = Depends(allow_roles(*ADMIN_ROLES)), db: Session = Depends(get_db)):
    p = db.get(Product, product_id)
    if not p: raise HTTPException(404, "Product not found")
    for k, v in payload.model_dump(exclude={"store_ids"}).items(): setattr(p, k, v)
    db.execute(delete(StoreProduct).where(StoreProduct.product_id == p.id))
    for sid in payload.store_ids: db.add(StoreProduct(store_id=sid, product_id=p.id))
    db.commit(); return {"ok": True}


class StoreSupplierIn(BaseModel):
    store_id: int
    supplier_id: int
    weekdays: list[int] = []
    deadline: str | None = None
    is_active: bool = True


@router.get("/store-suppliers")
def store_suppliers(store_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(StoreSupplier).where(StoreSupplier.store_id == store_id, StoreSupplier.is_active.is_(True))).all()
    return [{"id": x.id, "store_id": x.store_id, "supplier_id": x.supplier_id, "weekdays": x.weekdays, "deadline": x.deadline.strftime("%H:%M") if x.deadline else None} for x in rows]


@router.post("/admin/store-suppliers")
def upsert_store_supplier(payload: StoreSupplierIn, _: User = Depends(allow_roles(*ADMIN_ROLES)), db: Session = Depends(get_db)):
    x = db.scalar(select(StoreSupplier).where(StoreSupplier.store_id == payload.store_id, StoreSupplier.supplier_id == payload.supplier_id))
    if not x:
        x = StoreSupplier(store_id=payload.store_id, supplier_id=payload.supplier_id); db.add(x)
    x.weekdays = payload.weekdays; x.deadline = parse_hhmm(payload.deadline) if payload.deadline else None; x.is_active = payload.is_active
    db.commit(); return {"ok": True}


class UserAdminIn(BaseModel):
    role: UserRole
    status: UserStatus
    store_ids: list[int] = []


@router.get("/admin/users")
def admin_users(_: User = Depends(allow_roles(*ADMIN_ROLES)), db: Session = Depends(get_db)):
    rows = db.scalars(select(User).order_by(User.created_at.desc())).all()
    return [{"id": u.id, "telegram_id": u.telegram_id, "full_name": u.full_name, "username": u.username, "role": u.role.value, "status": u.status.value, "store_ids": [x.store_id for x in u.stores]} for u in rows]


@router.patch("/admin/users/{user_id}")
def admin_update_user(user_id: int, payload: UserAdminIn, _: User = Depends(allow_roles(*ADMIN_ROLES)), db: Session = Depends(get_db)):
    u = db.get(User, user_id)
    if not u: raise HTTPException(404, "User not found")
    u.role = payload.role; u.status = payload.status
    db.execute(delete(UserStore).where(UserStore.user_id == u.id))
    for idx, sid in enumerate(payload.store_ids): db.add(UserStore(user_id=u.id, store_id=sid, is_primary=(idx == 0)))
    db.commit(); return {"ok": True}
