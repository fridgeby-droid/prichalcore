from typing import Any
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, delete
from sqlalchemy.orm import Session

from app.core.security import require_roles, ALL_ROLES
from app.core.serializers import row
from app.db.database import get_db
from app.db.models import (
    Store, User, UserStore, Supplier, Category, Product, StoreProduct,
    StoreSupplierSchedule, ShiftTemplate, ShiftTemplateField,
    InspectionTemplate, InspectionTemplateField, AppSetting
)

router = APIRouter(prefix="/api/admin", tags=["admin"])
admin_dep = require_roles("admin")


class StoreIn(BaseModel):
    name: str
    code: str | None = None


@router.get("/stores")
def list_stores(user=Depends(admin_dep), db: Session = Depends(get_db)):
    return [row(x, "id", "name", "code", "active") for x in db.scalars(select(Store).order_by(Store.name)).all()]


@router.post("/stores")
def create_store(payload: StoreIn, user=Depends(admin_dep), db: Session = Depends(get_db)):
    obj = Store(name=payload.name.strip(), code=(payload.code or None))
    db.add(obj); db.commit(); db.refresh(obj)
    return row(obj, "id", "name", "code", "active")


@router.patch("/stores/{store_id}")
def update_store(store_id: int, payload: dict[str, Any], user=Depends(admin_dep), db: Session = Depends(get_db)):
    obj = db.get(Store, store_id)
    if not obj: raise HTTPException(404, "Магазин не найден")
    for k in ("name", "code", "active"):
        if k in payload: setattr(obj, k, payload[k])
    db.commit(); db.refresh(obj)
    return row(obj, "id", "name", "code", "active")


class SupplierIn(BaseModel):
    name: str
    deadline_time: str = "10:00"


@router.get("/suppliers")
def list_suppliers(user=Depends(admin_dep), db: Session = Depends(get_db)):
    return [row(x, "id", "name", "deadline_time", "active") for x in db.scalars(select(Supplier).order_by(Supplier.name)).all()]


@router.post("/suppliers")
def create_supplier(payload: SupplierIn, user=Depends(admin_dep), db: Session = Depends(get_db)):
    obj = Supplier(name=payload.name.strip(), deadline_time=payload.deadline_time)
    db.add(obj); db.commit(); db.refresh(obj)
    return row(obj, "id", "name", "deadline_time", "active")


@router.patch("/suppliers/{supplier_id}")
def update_supplier(supplier_id: int, payload: dict[str, Any], user=Depends(admin_dep), db: Session = Depends(get_db)):
    obj = db.get(Supplier, supplier_id)
    if not obj: raise HTTPException(404, "Поставщик не найден")
    for k in ("name", "deadline_time", "active"):
        if k in payload: setattr(obj, k, payload[k])
    db.commit(); db.refresh(obj)
    return row(obj, "id", "name", "deadline_time", "active")


class CategoryIn(BaseModel):
    name: str
    supplier_id: int | None = None


@router.get("/categories")
def categories(user=Depends(admin_dep), db: Session = Depends(get_db)):
    return [row(x, "id", "name", "supplier_id", "active") for x in db.scalars(select(Category).order_by(Category.name)).all()]


@router.post("/categories")
def create_category(payload: CategoryIn, user=Depends(admin_dep), db: Session = Depends(get_db)):
    obj = Category(name=payload.name.strip(), supplier_id=payload.supplier_id)
    db.add(obj); db.commit(); db.refresh(obj)
    return row(obj, "id", "name", "supplier_id", "active")


class ProductIn(BaseModel):
    name: str
    supplier_id: int
    category_id: int | None = None
    unit: str = "шт"


@router.get("/products")
def list_products(user=Depends(admin_dep), db: Session = Depends(get_db)):
    return [row(x, "id", "name", "supplier_id", "category_id", "unit", "active") for x in db.scalars(select(Product).order_by(Product.name)).all()]


@router.post("/products")
def create_product(payload: ProductIn, user=Depends(admin_dep), db: Session = Depends(get_db)):
    obj = Product(name=payload.name.strip(), supplier_id=payload.supplier_id, category_id=payload.category_id, unit=payload.unit)
    db.add(obj); db.commit(); db.refresh(obj)
    return row(obj, "id", "name", "supplier_id", "category_id", "unit", "active")


@router.patch("/products/{product_id}")
def update_product(product_id: int, payload: dict[str, Any], user=Depends(admin_dep), db: Session = Depends(get_db)):
    obj = db.get(Product, product_id)
    if not obj: raise HTTPException(404, "Товар не найден")
    for k in ("name", "supplier_id", "category_id", "unit", "active"):
        if k in payload: setattr(obj, k, payload[k])
    db.commit(); db.refresh(obj)
    return row(obj, "id", "name", "supplier_id", "category_id", "unit", "active")


class UserUpdate(BaseModel):
    role: str | None = None
    status: str | None = None
    active: bool | None = None
    store_ids: list[int] | None = None


@router.get("/users")
def list_users(user=Depends(admin_dep), db: Session = Depends(get_db)):
    users = db.scalars(select(User).order_by(User.full_name)).all()
    result = []
    for u in users:
        store_ids = list(db.scalars(select(UserStore.store_id).where(UserStore.user_id == u.id)).all())
        result.append({**row(u, "id", "telegram_id", "full_name", "username", "role", "status", "active"), "store_ids": store_ids})
    return result


@router.patch("/users/{user_id}")
def update_user(user_id: int, payload: UserUpdate, user=Depends(admin_dep), db: Session = Depends(get_db)):
    obj = db.get(User, user_id)
    if not obj: raise HTTPException(404, "Пользователь не найден")
    if payload.role is not None:
        if payload.role not in ALL_ROLES: raise HTTPException(400, "Неизвестная роль")
        obj.role = payload.role
    if payload.status is not None: obj.status = payload.status
    if payload.active is not None: obj.active = payload.active
    if payload.store_ids is not None:
        db.execute(delete(UserStore).where(UserStore.user_id == obj.id))
        for sid in sorted(set(payload.store_ids)):
            db.add(UserStore(user_id=obj.id, store_id=sid))
    db.commit()
    return {"ok": True}


class StoreProductIn(BaseModel):
    store_id: int
    product_id: int
    enabled: bool = True


@router.post("/store-products")
def set_store_product(payload: StoreProductIn, user=Depends(admin_dep), db: Session = Depends(get_db)):
    obj = db.get(StoreProduct, {"store_id": payload.store_id, "product_id": payload.product_id})
    if not obj:
        obj = StoreProduct(store_id=payload.store_id, product_id=payload.product_id, enabled=payload.enabled); db.add(obj)
    else: obj.enabled = payload.enabled
    db.commit(); return {"ok": True}


class ScheduleIn(BaseModel):
    store_id: int
    supplier_id: int
    weekday: int = Field(ge=0, le=6)
    active: bool = True


@router.get("/schedules")
def schedules(user=Depends(admin_dep), db: Session = Depends(get_db)):
    return [row(x, "id", "store_id", "supplier_id", "weekday", "active") for x in db.scalars(select(StoreSupplierSchedule).order_by(StoreSupplierSchedule.store_id, StoreSupplierSchedule.weekday)).all()]


@router.post("/schedules")
def create_schedule(payload: ScheduleIn, user=Depends(admin_dep), db: Session = Depends(get_db)):
    q = select(StoreSupplierSchedule).where(StoreSupplierSchedule.store_id == payload.store_id, StoreSupplierSchedule.supplier_id == payload.supplier_id, StoreSupplierSchedule.weekday == payload.weekday)
    obj = db.scalar(q)
    if obj: obj.active = payload.active
    else:
        obj = StoreSupplierSchedule(**payload.model_dump()); db.add(obj)
    db.commit(); return {"ok": True}


class TemplateFieldIn(BaseModel):
    key: str
    label: str
    field_type: str = "text"
    required: bool = False
    sort_order: int = 0
    options_json: dict | list | None = None
    creates_violation_on_false: bool = False


class ShiftTemplateIn(BaseModel):
    store_id: int
    name: str
    shift_kind: str
    fields: list[TemplateFieldIn] = []


@router.get("/shift-templates")
def list_shift_templates(user=Depends(admin_dep), db: Session = Depends(get_db)):
    result=[]
    for t in db.scalars(select(ShiftTemplate).order_by(ShiftTemplate.store_id, ShiftTemplate.name)).all():
        fs=db.scalars(select(ShiftTemplateField).where(ShiftTemplateField.template_id==t.id).order_by(ShiftTemplateField.sort_order)).all()
        result.append({**row(t,"id","store_id","name","shift_kind","active"),"fields":[row(f,"id","key","label","field_type","required","sort_order","options_json") for f in fs]})
    return result


@router.post("/shift-templates")
def create_shift_template(payload: ShiftTemplateIn, user=Depends(admin_dep), db: Session = Depends(get_db)):
    t=ShiftTemplate(store_id=payload.store_id,name=payload.name,shift_kind=payload.shift_kind); db.add(t); db.flush()
    for f in payload.fields:
        db.add(ShiftTemplateField(template_id=t.id, **f.model_dump(exclude={"creates_violation_on_false"})))
    db.commit(); return {"id":t.id}


class InspectionTemplateIn(BaseModel):
    name: str
    fields: list[TemplateFieldIn] = []


@router.get("/inspection-templates")
def list_inspection_templates(user=Depends(admin_dep), db: Session = Depends(get_db)):
    result=[]
    for t in db.scalars(select(InspectionTemplate).order_by(InspectionTemplate.name)).all():
        fs=db.scalars(select(InspectionTemplateField).where(InspectionTemplateField.template_id==t.id).order_by(InspectionTemplateField.sort_order)).all()
        result.append({**row(t,"id","name","active"),"fields":[row(f,"id","key","label","field_type","required","sort_order","options_json","creates_violation_on_false") for f in fs]})
    return result


@router.post("/inspection-templates")
def create_inspection_template(payload: InspectionTemplateIn, user=Depends(admin_dep), db: Session = Depends(get_db)):
    t=InspectionTemplate(name=payload.name); db.add(t); db.flush()
    for f in payload.fields:
        db.add(InspectionTemplateField(template_id=t.id, **f.model_dump()))
    db.commit(); return {"id":t.id}


@router.get("/settings/{key}")
def get_setting(key: str, user=Depends(admin_dep), db: Session = Depends(get_db)):
    obj=db.get(AppSetting,key); return {"key":key,"value":obj.value_json if obj else None}


@router.put("/settings/{key}")
def set_setting(key: str, payload: dict[str, Any], user=Depends(admin_dep), db: Session = Depends(get_db)):
    obj=db.get(AppSetting,key)
    if not obj: obj=AppSetting(key=key,value_json=payload.get("value")); db.add(obj)
    else: obj.value_json=payload.get("value")
    db.commit(); return {"ok":True}

@router.patch("/shift-templates/{template_id}")
def update_shift_template(template_id:int,payload:ShiftTemplateIn,user=Depends(admin_dep),db:Session=Depends(get_db)):
    t=db.get(ShiftTemplate,template_id)
    if not t: raise HTTPException(404,"Шаблон не найден")
    t.store_id=payload.store_id;t.name=payload.name;t.shift_kind=payload.shift_kind
    db.execute(delete(ShiftTemplateField).where(ShiftTemplateField.template_id==t.id))
    for f in payload.fields:
        db.add(ShiftTemplateField(template_id=t.id,**f.model_dump(exclude={"creates_violation_on_false"})))
    db.commit();return {"ok":True}

@router.patch("/inspection-templates/{template_id}")
def update_inspection_template(template_id:int,payload:InspectionTemplateIn,user=Depends(admin_dep),db:Session=Depends(get_db)):
    t=db.get(InspectionTemplate,template_id)
    if not t: raise HTTPException(404,"Шаблон не найден")
    t.name=payload.name
    db.execute(delete(InspectionTemplateField).where(InspectionTemplateField.template_id==t.id))
    for f in payload.fields:
        db.add(InspectionTemplateField(template_id=t.id,**f.model_dump()))
    db.commit();return {"ok":True}

@router.patch("/categories/{category_id}")
def update_category(category_id:int,payload:dict[str,Any],user=Depends(admin_dep),db:Session=Depends(get_db)):
    obj=db.get(Category,category_id)
    if not obj: raise HTTPException(404,"Категория не найдена")
    for k in ("name","supplier_id","active"):
        if k in payload:setattr(obj,k,payload[k])
    db.commit();return {"ok":True}
