from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
import httpx
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import allow_roles, current_user
from ..db import get_db
from ..models import (
    CashCollection,
    Incident,
    Inspection,
    Order,
    OrderItem,
    OrderStatus,
    Product,
    ShiftKind,
    ShiftReport,
    Store,
    SubstitutionRequest,
    Task,
    TaskStatus,
    User,
    UserRole,
)
from ..permissions import can_access_store, store_ids_for_user
from ..services import create_photo_request

router = APIRouter(prefix="/api", tags=["operations"])
MANAGEMENT_ROLES = (UserRole.manager, UserRole.operations_director, UserRole.admin)


def require_store(db: Session, user: User, store_id: int):
    if not can_access_store(db, user, store_id):
        raise HTTPException(403, "No access to this store")


class OrderItemIn(BaseModel):
    product_id: int
    quantity: Decimal = Field(gt=0)


class OrderIn(BaseModel):
    store_id: int
    supplier_id: int
    items: list[OrderItemIn]
    comment: str | None = None


@router.post("/orders")
def create_order(payload: OrderIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require_store(db, user, payload.store_id)
    if not payload.items:
        raise HTTPException(400, "Order must contain items")
    product_ids = [x.product_id for x in payload.items]
    products = {p.id: p for p in db.scalars(select(Product).where(Product.id.in_(product_ids))).all()}
    for item in payload.items:
        p = products.get(item.product_id)
        if not p or p.supplier_id != payload.supplier_id or not p.is_active:
            raise HTTPException(400, f"Invalid product {item.product_id}")
    order = Order(store_id=payload.store_id, supplier_id=payload.supplier_id, created_by=user.id, comment=payload.comment, status=OrderStatus.submitted)
    db.add(order); db.flush()
    for item in payload.items:
        db.add(OrderItem(order_id=order.id, product_id=item.product_id, quantity=item.quantity))
    db.commit(); db.refresh(order)
    return {"id": order.id, "status": order.status.value}


@router.get("/orders")
def list_orders(store_id: int | None = None, status: OrderStatus | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ids = store_ids_for_user(db, user)
    q = select(Order).order_by(Order.created_at.desc()).limit(300)
    if user.role not in {UserRole.admin, UserRole.operations_director, UserRole.executive}:
        q = q.where(Order.store_id.in_(ids or [-1]))
    if store_id:
        require_store(db, user, store_id); q = q.where(Order.store_id == store_id)
    if status: q = q.where(Order.status == status)
    rows = db.scalars(q).all()
    return [{"id": o.id, "store_id": o.store_id, "supplier_id": o.supplier_id, "created_by": o.created_by, "status": o.status.value, "comment": o.comment, "created_at": o.created_at.isoformat()} for o in rows]


class OrderStatusIn(BaseModel):
    status: OrderStatus


@router.patch("/orders/{order_id}/status")
def change_order_status(order_id: int, payload: OrderStatusIn, user: User = Depends(allow_roles(*MANAGEMENT_ROLES)), db: Session = Depends(get_db)):
    o = db.get(Order, order_id)
    if not o: raise HTTPException(404, "Order not found")
    require_store(db, user, o.store_id)
    o.status = payload.status; o.processed_by = user.id; o.processed_at = datetime.now(timezone.utc)
    db.commit(); return {"ok": True, "status": o.status.value}


class ShiftReportIn(BaseModel):
    store_id: int
    kind: ShiftKind
    cash_amount: Decimal | None = None
    cash_difference: Decimal | None = None
    issues: str | None = None
    payload: dict = {}
    finalized: bool = True


@router.post("/shift-reports")
def create_shift_report(payload: ShiftReportIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require_store(db, user, payload.store_id)
    today = date.today()
    report = db.scalar(select(ShiftReport).where(ShiftReport.store_id == payload.store_id, ShiftReport.report_date == today, ShiftReport.kind == payload.kind))
    if not report:
        report = ShiftReport(store_id=payload.store_id, user_id=user.id, report_date=today, kind=payload.kind)
        db.add(report)
    report.cash_amount = payload.cash_amount; report.cash_difference = payload.cash_difference; report.issues = payload.issues; report.payload = payload.payload; report.finalized = payload.finalized; report.user_id = user.id
    db.commit(); db.refresh(report)
    return {"id": report.id, "finalized": report.finalized}


@router.get("/shift-reports")
def list_shift_reports(store_id: int | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ids = store_ids_for_user(db, user)
    q = select(ShiftReport).order_by(ShiftReport.created_at.desc()).limit(300)
    if user.role not in {UserRole.admin, UserRole.operations_director, UserRole.executive}:
        q = q.where(ShiftReport.store_id.in_(ids or [-1]))
    if store_id: require_store(db, user, store_id); q = q.where(ShiftReport.store_id == store_id)
    rows = db.scalars(q).all()
    return [{"id": r.id, "store_id": r.store_id, "user_id": r.user_id, "date": r.report_date.isoformat(), "kind": r.kind.value, "cash_amount": float(r.cash_amount) if r.cash_amount is not None else None, "cash_difference": float(r.cash_difference) if r.cash_difference is not None else None, "issues": r.issues, "finalized": r.finalized, "created_at": r.created_at.isoformat()} for r in rows]


class InspectionIn(BaseModel):
    store_id: int
    checklist: dict
    score: Decimal | None = None
    summary: str | None = None


@router.post("/inspections")
def create_inspection(payload: InspectionIn, user: User = Depends(allow_roles(*MANAGEMENT_ROLES)), db: Session = Depends(get_db)):
    require_store(db, user, payload.store_id)
    row = Inspection(store_id=payload.store_id, manager_id=user.id, checklist=payload.checklist, score=payload.score, summary=payload.summary)
    db.add(row); db.commit(); db.refresh(row)
    return {"id": row.id}


@router.get("/inspections")
def list_inspections(store_id: int | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ids = store_ids_for_user(db, user)
    q = select(Inspection).order_by(Inspection.created_at.desc()).limit(300)
    if user.role not in {UserRole.admin, UserRole.operations_director, UserRole.executive}:
        q = q.where(Inspection.store_id.in_(ids or [-1]))
    if store_id: require_store(db, user, store_id); q = q.where(Inspection.store_id == store_id)
    rows = db.scalars(q).all()
    return [{"id": x.id, "store_id": x.store_id, "manager_id": x.manager_id, "score": float(x.score) if x.score is not None else None, "summary": x.summary, "checklist": x.checklist, "created_at": x.created_at.isoformat()} for x in rows]


class TaskIn(BaseModel):
    store_id: int | None = None
    assigned_to: int
    title: str
    description: str | None = None
    category: str = "general"
    priority: str = "normal"
    due_at: datetime | None = None
    requires_photo: bool = False


@router.post("/tasks")
def create_task(payload: TaskIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if payload.store_id is not None: require_store(db, user, payload.store_id)
    task = Task(created_by=user.id, **payload.model_dump())
    db.add(task); db.commit(); db.refresh(task); return {"id": task.id, "status": task.status.value}


@router.get("/tasks")
def list_tasks(mine: bool = False, user: User = Depends(current_user), db: Session = Depends(get_db)):
    q = select(Task).order_by(Task.created_at.desc()).limit(500)
    if mine or user.role in {UserRole.seller, UserRole.mentor}:
        q = q.where(Task.assigned_to == user.id)
    elif user.role == UserRole.manager:
        ids = store_ids_for_user(db, user); q = q.where((Task.store_id.in_(ids or [-1])) | (Task.assigned_to == user.id))
    rows = db.scalars(q).all()
    return [{"id": x.id, "store_id": x.store_id, "assigned_to": x.assigned_to, "created_by": x.created_by, "title": x.title, "description": x.description, "category": x.category, "priority": x.priority, "status": x.status.value, "due_at": x.due_at.isoformat() if x.due_at else None, "requires_photo": x.requires_photo} for x in rows]


class TaskStatusIn(BaseModel):
    status: TaskStatus


@router.patch("/tasks/{task_id}/status")
def task_status(task_id: int, payload: TaskStatusIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task: raise HTTPException(404, "Task not found")
    if task.assigned_to != user.id and user.role not in {UserRole.manager, UserRole.operations_director, UserRole.admin}:
        raise HTTPException(403, "No access")
    task.status = payload.status
    if payload.status == TaskStatus.done: task.completed_at = datetime.now(timezone.utc)
    db.commit(); return {"ok": True}


class CashIn(BaseModel):
    store_id: int
    amount: Decimal = Field(gt=0)
    note: str | None = None


@router.post("/cash-collections")
def cash_collection(payload: CashIn, user: User = Depends(allow_roles(*MANAGEMENT_ROLES)), db: Session = Depends(get_db)):
    require_store(db, user, payload.store_id)
    row = CashCollection(store_id=payload.store_id, manager_id=user.id, amount=payload.amount, note=payload.note)
    db.add(row); db.commit(); db.refresh(row); return {"id": row.id}


@router.get("/cash-collections")
def cash_collections(user: User = Depends(current_user), db: Session = Depends(get_db)):
    ids = store_ids_for_user(db, user)
    q = select(CashCollection).order_by(CashCollection.collected_at.desc()).limit(300)
    if user.role not in {UserRole.admin, UserRole.operations_director, UserRole.executive}: q = q.where(CashCollection.store_id.in_(ids or [-1]))
    rows = db.scalars(q).all()
    return [{"id": x.id, "store_id": x.store_id, "manager_id": x.manager_id, "amount": float(x.amount), "collected_at": x.collected_at.isoformat(), "delivered_at": x.delivered_at.isoformat() if x.delivered_at else None, "note": x.note} for x in rows]


class IncidentIn(BaseModel):
    store_id: int
    category: str
    severity: str = "yellow"
    description: str


@router.post("/incidents")
def incident(payload: IncidentIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require_store(db, user, payload.store_id)
    row = Incident(store_id=payload.store_id, created_by=user.id, category=payload.category, severity=payload.severity, description=payload.description)
    db.add(row); db.commit(); db.refresh(row); return {"id": row.id}


class SubstitutionIn(BaseModel):
    store_id: int
    shift_date: date
    shift_label: str
    reason: str | None = None


@router.post("/substitutions")
def substitution(payload: SubstitutionIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require_store(db, user, payload.store_id)
    row = SubstitutionRequest(store_id=payload.store_id, requested_by=user.id, shift_date=payload.shift_date, shift_label=payload.shift_label, reason=payload.reason)
    db.add(row); db.commit(); db.refresh(row); return {"id": row.id}


class PhotoRequestIn(BaseModel):
    entity_type: str
    entity_id: int
    field_key: str | None = None


@router.post("/photos/request")
async def request_photo(payload: PhotoRequestIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    req = await create_photo_request(db, user, payload.entity_type, payload.entity_id, payload.field_key)
    return {"request_id": req.id, "expires_at": req.expires_at.isoformat(), "message": "Отправьте фото в чат с ботом"}

@router.get("/photos")
def list_photos(entity_type: str, entity_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    from ..models import Photo
    rows = db.scalars(select(Photo).where(Photo.entity_type == entity_type, Photo.entity_id == entity_id).order_by(Photo.created_at)).all()
    result = []
    for p in rows:
        if p.store_id is not None and not can_access_store(db, user, p.store_id):
            continue
        result.append({"id": p.id, "field_key": p.field_key, "width": p.width, "height": p.height, "file_size": p.file_size, "created_at": p.created_at.isoformat(), "content_url": f"/api/photos/{p.id}/content"})
    return result


@router.get("/photos/{photo_id}/content")
async def photo_content(photo_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    from ..models import Photo
    from ..config import settings
    p = db.get(Photo, photo_id)
    if not p:
        raise HTTPException(404, "Photo not found")
    if p.store_id is not None and not can_access_store(db, user, p.store_id):
        raise HTTPException(403, "No access")
    async with httpx.AsyncClient(timeout=30) as client:
        meta = (await client.post(f"https://api.telegram.org/bot{settings.bot_token}/getFile", json={"file_id": p.telegram_file_id})).json()
        if not meta.get("ok"):
            raise HTTPException(502, "Telegram file unavailable")
        file_path = meta["result"]["file_path"]
        r = await client.get(f"https://api.telegram.org/file/bot{settings.bot_token}/{file_path}")
        if r.status_code >= 400:
            raise HTTPException(502, "Telegram file download failed")
        return StreamingResponse(iter([r.content]), media_type=r.headers.get("content-type", "image/jpeg"), headers={"Cache-Control":"private, max-age=300"})
