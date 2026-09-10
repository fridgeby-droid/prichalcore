from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import get_current_user
from app.db.database import get_db
from app.db.models import Employee, EmployeeStore, Store, User, WorkAbsence, WorkShiftAssignment, WorkSubstitution

router = APIRouter(prefix="/api/employees", tags=["employees"])

POSITIONS = {"seller", "mentor", "manager", "operations_director", "leader", "admin"}
EMPLOYMENT_STATUSES = {"working", "trainee", "vacation", "fired"}


class EmployeeIn(BaseModel):
    full_name: str
    position: str = "seller"
    employment_status: str = "working"
    hire_date: date | None = None
    comment: str | None = None
    telegram_id: int | None = None
    user_id: int | None = None
    store_ids: list[int] = []
    active: bool = True


class EmployeePatch(BaseModel):
    full_name: str | None = None
    position: str | None = None
    employment_status: str | None = None
    hire_date: date | None = None
    comment: str | None = None
    telegram_id: int | None = None
    user_id: int | None = None
    store_ids: list[int] | None = None
    active: bool | None = None


def validate_position(value: str):
    if value not in POSITIONS:
        raise HTTPException(400, "Неизвестная должность")


def validate_status(value: str):
    if value not in EMPLOYMENT_STATUSES:
        raise HTTPException(400, "Неизвестный статус сотрудника")


def sync_linked_user(employee: Employee, db: Session):
    if employee.user_id:
        linked = db.get(User, employee.user_id)
        if not linked:
            raise HTTPException(400, "Telegram-пользователь не найден")
        if employee.telegram_id and employee.telegram_id != linked.telegram_id:
            raise HTTPException(400, "Telegram ID не совпадает с выбранным аккаунтом")
        employee.telegram_id = linked.telegram_id


def replace_stores(db: Session, employee_id: int, store_ids: list[int]):
    db.execute(delete(EmployeeStore).where(EmployeeStore.employee_id == employee_id))
    for sid in sorted(set(store_ids)):
        store = db.get(Store, sid)
        if not store:
            raise HTTPException(400, f"Магазин #{sid} не найден")
        db.add(EmployeeStore(employee_id=employee_id, store_id=sid))


def employee_dict(db: Session, e: Employee) -> dict[str, Any]:
    stores = list(db.execute(
        select(Store.id, Store.name)
        .join(EmployeeStore, EmployeeStore.store_id == Store.id)
        .where(EmployeeStore.employee_id == e.id)
        .order_by(Store.name)
    ).all())
    user = db.get(User, e.user_id) if e.user_id else None
    return {
        "id": e.id,
        "full_name": e.full_name,
        "position": e.position,
        "employment_status": e.employment_status,
        "hire_date": e.hire_date.isoformat() if e.hire_date else None,
        "comment": e.comment,
        "telegram_id": e.telegram_id,
        "user_id": e.user_id,
        "telegram_name": (user.full_name or user.username or str(user.telegram_id)) if user else None,
        "store_ids": [sid for sid, _ in stores],
        "stores": [{"id": sid, "name": name} for sid, name in stores],
        "active": e.active,
        "created_at": e.created_at.isoformat() if e.created_at else None,
        "updated_at": e.updated_at.isoformat() if e.updated_at else None,
    }


@router.get("")
def list_employees(
    status: str | None = None,
    position: str | None = None,
    store_id: int | None = None,
    q: str | None = None,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    stmt = select(Employee).order_by(Employee.full_name, Employee.id)
    if status:
        stmt = stmt.where(Employee.employment_status == status)
    if position:
        stmt = stmt.where(Employee.position == position)
    if store_id:
        stmt = stmt.join(EmployeeStore, EmployeeStore.employee_id == Employee.id).where(EmployeeStore.store_id == store_id)
    if q:
        stmt = stmt.where(Employee.full_name.ilike(f"%{q.strip()}%"))
    return [employee_dict(db, e) for e in db.scalars(stmt).unique().all()]


@router.get("/meta")
def employee_meta(user=Depends(get_current_user), db: Session = Depends(get_db)):
    stores = list(db.scalars(select(Store).where(Store.active.is_(True)).order_by(Store.name)).all())
    users = list(db.scalars(select(User).where(User.active.is_(True)).order_by(User.full_name, User.id)).all())
    linked_user_ids = set(db.scalars(select(Employee.user_id).where(Employee.user_id.is_not(None))).all())
    return {
        "positions": sorted(POSITIONS),
        "statuses": sorted(EMPLOYMENT_STATUSES),
        "stores": [{"id": s.id, "name": s.name} for s in stores],
        "users": [
            {
                "id": u.id,
                "telegram_id": u.telegram_id,
                "name": u.full_name or u.username or str(u.telegram_id),
                "linked": u.id in linked_user_ids,
            }
            for u in users
        ],
    }


@router.post("")
def create_employee(payload: EmployeeIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    name = payload.full_name.strip()
    if not name:
        raise HTTPException(400, "Укажите ФИО")
    validate_position(payload.position)
    validate_status(payload.employment_status)
    e = Employee(
        full_name=name,
        position=payload.position,
        employment_status=payload.employment_status,
        hire_date=payload.hire_date,
        comment=payload.comment,
        telegram_id=payload.telegram_id,
        user_id=payload.user_id,
        active=payload.active,
    )
    sync_linked_user(e, db)
    db.add(e)
    try:
        db.flush()
        replace_stores(db, e.id, payload.store_ids)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(400, "Этот Telegram-аккаунт или Telegram ID уже связан с другим сотрудником")
    db.refresh(e)
    return employee_dict(db, e)


@router.patch("/{employee_id}")
def update_employee(employee_id: int, payload: EmployeePatch, user=Depends(get_current_user), db: Session = Depends(get_db)):
    e = db.get(Employee, employee_id)
    if not e:
        raise HTTPException(404, "Сотрудник не найден")
    data = payload.model_dump(exclude_unset=True)
    if "full_name" in data:
        data["full_name"] = (data["full_name"] or "").strip()
        if not data["full_name"]:
            raise HTTPException(400, "Укажите ФИО")
    if "position" in data:
        validate_position(data["position"])
    if "employment_status" in data:
        validate_status(data["employment_status"])
    store_ids = data.pop("store_ids", None)
    for key, value in data.items():
        setattr(e, key, value)
    sync_linked_user(e, db)
    try:
        db.flush()
        if store_ids is not None:
            replace_stores(db, e.id, store_ids)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(400, "Этот Telegram-аккаунт или Telegram ID уже связан с другим сотрудником")
    db.refresh(e)
    return employee_dict(db, e)


@router.delete("/{employee_id}")
def delete_employee(employee_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    e = db.get(Employee, employee_id)
    if not e:
        raise HTTPException(404, "Сотрудник не найден")
    used = any([
        db.scalar(select(WorkShiftAssignment.id).where(WorkShiftAssignment.employee_id == employee_id).limit(1)),
        db.scalar(select(WorkAbsence.id).where(WorkAbsence.employee_id == employee_id).limit(1)),
        db.scalar(select(WorkSubstitution.id).where(
            (WorkSubstitution.absent_employee_id == employee_id) | (WorkSubstitution.replacement_employee_id == employee_id)
        ).limit(1)),
    ])
    if used:
        e.employment_status = "fired"
        e.active = False
        db.commit()
        return {"ok": True, "soft_deleted": True, "detail": "Сотрудник используется в истории и переведён в статус «Уволен»"}
    db.delete(e)
    db.commit()
    return {"ok": True, "soft_deleted": False}
