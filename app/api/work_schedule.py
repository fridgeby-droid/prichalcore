from __future__ import annotations

import calendar
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.security import get_current_user
from app.db.database import get_db
from app.db.models import (
    Employee,
    EmployeeStore,
    Store,
    User,
    WorkAbsence,
    WorkScheduleChange,
    WorkShiftAssignment,
    WorkSubstitution,
)

router = APIRouter(prefix="/api/work-schedule", tags=["work-schedule"])

SHIFT_CAPACITY = {"day": 1, "night": 2}
ABSENCE_STATUSES = {"day_off", "vacation", "sick", "training"}
SUB_STATUSES = {"new", "searching", "assigned", "closed", "cancelled"}
SCHEDULABLE_EMPLOYEE_STATUSES = {"working", "trainee"}


class AssignmentIn(BaseModel):
    store_id: int
    employee_id: int | None = None
    user_id: int | None = None  # legacy client compatibility
    work_date: date
    shift_type: str
    slot: int | None = None


class AbsenceIn(BaseModel):
    employee_id: int | None = None
    user_id: int | None = None  # legacy client compatibility
    date_from: date
    date_to: date
    status: str
    comment: str | None = None


class SubstitutionIn(BaseModel):
    store_id: int
    work_date: date
    shift_type: str
    absent_employee_id: int | None = None
    absent_user_id: int | None = None  # legacy client compatibility
    reason: str | None = None
    comment: str | None = None


class ReplacementIn(BaseModel):
    replacement_employee_id: int | None = None
    replacement_user_id: int | None = None  # legacy client compatibility


class StatusIn(BaseModel):
    status: str


def month_bounds(year: int, month: int) -> tuple[date, date]:
    if month < 1 or month > 12:
        raise HTTPException(400, "Некорректный месяц")
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def user_name(u: User | None) -> str:
    if not u:
        return "—"
    return u.full_name or u.username or str(u.telegram_id)


def employee_name(e: Employee | None) -> str:
    return e.full_name if e else "—"


def employee_from_legacy_user(db: Session, user_id: int | None) -> Employee | None:
    if not user_id:
        return None
    return db.scalar(select(Employee).where(Employee.user_id == user_id))


def resolve_employee_id(db: Session, employee_id: int | None, legacy_user_id: int | None = None) -> int:
    if employee_id:
        if not db.get(Employee, employee_id):
            raise HTTPException(404, "Сотрудник не найден")
        return employee_id
    # v1.4 MiniApp may still use the old JSON key user_id while already passing
    # an employee id. Prefer an Employee with that id, then fall back to the
    # true legacy relation Employee.user_id -> users.id.
    if legacy_user_id:
        by_employee_id = db.get(Employee, legacy_user_id)
        if by_employee_id:
            return by_employee_id.id
    e = employee_from_legacy_user(db, legacy_user_id)
    if e:
        return e.id
    raise HTTPException(400, "Выберите сотрудника")


def assignment_dict(a: WorkShiftAssignment, employees: dict[int, Employee], stores: dict[int, Store]) -> dict[str, Any]:
    e = employees.get(a.employee_id) if a.employee_id else None
    return {
        "id": a.id,
        "store_id": a.store_id,
        "store_name": stores.get(a.store_id).name if stores.get(a.store_id) else str(a.store_id),
        "employee_id": a.employee_id,
        "employee_name": employee_name(e),
        # compatibility keys for old MiniApp builds
        "user_id": a.employee_id,
        "user_name": employee_name(e),
        "work_date": a.work_date.isoformat(),
        "shift_type": a.shift_type,
        "slot": a.slot,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


def absence_dict(a: WorkAbsence, employees: dict[int, Employee]) -> dict[str, Any]:
    e = employees.get(a.employee_id) if a.employee_id else None
    return {
        "id": a.id,
        "employee_id": a.employee_id,
        "employee_name": employee_name(e),
        "user_id": a.employee_id,
        "user_name": employee_name(e),
        "date_from": a.date_from.isoformat(),
        "date_to": a.date_to.isoformat(),
        "status": a.status,
        "comment": a.comment,
    }


def log_change(
    db: Session,
    *,
    action: str,
    entity_type: str,
    entity_id: int | None,
    changed_by: int | None,
    store_id: int | None = None,
    employee_id: int | None = None,
    change_date: date | None = None,
    old: Any = None,
    new: Any = None,
):
    db.add(
        WorkScheduleChange(
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            store_id=store_id,
            employee_id=employee_id,
            changed_by=changed_by,
            change_date=change_date,
            old_json=old,
            new_json=new,
        )
    )


def assigned_store_ids(db: Session, employee_id: int) -> set[int]:
    return set(db.scalars(select(EmployeeStore.store_id).where(EmployeeStore.employee_id == employee_id)).all())


def get_absence_for_date(db: Session, employee_id: int, work_date: date) -> WorkAbsence | None:
    return db.scalar(
        select(WorkAbsence).where(
            WorkAbsence.employee_id == employee_id,
            WorkAbsence.date_from <= work_date,
            WorkAbsence.date_to >= work_date,
        ).order_by(WorkAbsence.id.desc())
    )


def assignment_conflicts(db: Session, payload: AssignmentIn, ignore_assignment_id: int | None = None) -> list[str]:
    errors: list[str] = []
    if payload.shift_type not in SHIFT_CAPACITY:
        return ["Неизвестный тип смены"]
    employee_id = resolve_employee_id(db, payload.employee_id, payload.user_id)
    employee = db.get(Employee, employee_id)
    store = db.get(Store, payload.store_id)

    if not employee or not employee.active or employee.employment_status not in SCHEDULABLE_EMPLOYEE_STATUSES:
        errors.append("Сотрудник сейчас недоступен для графика")
    if not store or not store.active:
        errors.append("Магазин неактивен")
    if payload.store_id not in assigned_store_ids(db, employee_id):
        errors.append("Сотрудник не закреплён за этой точкой")

    q = select(WorkShiftAssignment).where(
        WorkShiftAssignment.employee_id == employee_id,
        WorkShiftAssignment.work_date == payload.work_date,
    )
    if ignore_assignment_id:
        q = q.where(WorkShiftAssignment.id != ignore_assignment_id)
    if db.scalar(q):
        errors.append("У сотрудника уже есть смена в этот день")

    if get_absence_for_date(db, employee_id, payload.work_date):
        errors.append("На эту дату у сотрудника установлен другой статус")

    if payload.shift_type == "day":
        prev_night = db.scalar(select(WorkShiftAssignment).where(
            WorkShiftAssignment.employee_id == employee_id,
            WorkShiftAssignment.work_date == payload.work_date - timedelta(days=1),
            WorkShiftAssignment.shift_type == "night",
        ))
        if prev_night:
            errors.append("Две смены подряд: после ночной нельзя сразу ставить дневную")
    if payload.shift_type == "night":
        next_day = db.scalar(select(WorkShiftAssignment).where(
            WorkShiftAssignment.employee_id == employee_id,
            WorkShiftAssignment.work_date == payload.work_date + timedelta(days=1),
            WorkShiftAssignment.shift_type == "day",
        ))
        if next_day:
            errors.append("Две смены подряд: после ночной уже стоит дневная")
    return errors


def first_free_slot(db: Session, store_id: int, work_date: date, shift_type: str) -> int | None:
    capacity = SHIFT_CAPACITY[shift_type]
    used = set(db.scalars(select(WorkShiftAssignment.slot).where(
        WorkShiftAssignment.store_id == store_id,
        WorkShiftAssignment.work_date == work_date,
        WorkShiftAssignment.shift_type == shift_type,
    )).all())
    for slot in range(1, capacity + 1):
        if slot not in used:
            return slot
    return None


def current_employee(db: Session, user: User) -> Employee | None:
    return db.scalar(select(Employee).where(Employee.user_id == user.id))


@router.get("/context")
def context(year: int, month: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    start, end = month_bounds(year, month)
    stores = list(db.scalars(select(Store).where(Store.active.is_(True)).order_by(Store.name)).all())
    employees = list(db.scalars(select(Employee).where(Employee.active.is_(True)).order_by(Employee.full_name, Employee.id)).all())
    employee_ids = [e.id for e in employees]
    bindings = list(db.execute(select(EmployeeStore.employee_id, EmployeeStore.store_id).where(EmployeeStore.employee_id.in_(employee_ids or [-1]))).all())
    employee_map = {e.id: e for e in employees}
    store_map = {s.id: s for s in stores}
    assignments = list(db.scalars(select(WorkShiftAssignment).where(
        WorkShiftAssignment.work_date.between(start, end),
        WorkShiftAssignment.employee_id.is_not(None),
    ).order_by(WorkShiftAssignment.work_date, WorkShiftAssignment.store_id, WorkShiftAssignment.shift_type, WorkShiftAssignment.slot)).all())
    absences = list(db.scalars(select(WorkAbsence).where(
        WorkAbsence.employee_id.is_not(None),
        WorkAbsence.date_from <= end,
        WorkAbsence.date_to >= start,
    ).order_by(WorkAbsence.date_from)).all())
    me = current_employee(db, user)
    return {
        "year": year,
        "month": month,
        "stores": [{"id": s.id, "name": s.name} for s in stores],
        "employees": [
            {
                "id": e.id,
                "name": e.full_name,
                "position": e.position,
                "employment_status": e.employment_status,
                "telegram_linked": bool(e.user_id),
                "store_ids": [sid for eid, sid in bindings if eid == e.id],
            }
            for e in employees
        ],
        # compatibility for cached old JS
        "users": [
            {
                "id": e.id,
                "name": e.full_name,
                "role": e.position,
                "employment_status": e.employment_status,
                "store_ids": [sid for eid, sid in bindings if eid == e.id],
            }
            for e in employees
        ],
        "assignments": [assignment_dict(a, employee_map, store_map) for a in assignments],
        "absences": [absence_dict(a, employee_map) for a in absences],
        "me_employee_id": me.id if me else None,
        "me_id": me.id if me else None,
        "days_in_month": calendar.monthrange(year, month)[1],
    }


@router.get("/my")
def my_schedule(year: int, month: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    start, end = month_bounds(year, month)
    me = current_employee(db, user)
    if not me:
        return {"linked": False, "assignments": [], "absences": []}
    employees = {me.id: me}
    stores = {s.id: s for s in db.scalars(select(Store)).all()}
    assignments = list(db.scalars(select(WorkShiftAssignment).where(
        WorkShiftAssignment.employee_id == me.id,
        WorkShiftAssignment.work_date.between(start, end),
    ).order_by(WorkShiftAssignment.work_date)).all())
    absences = list(db.scalars(select(WorkAbsence).where(
        WorkAbsence.employee_id == me.id,
        WorkAbsence.date_from <= end,
        WorkAbsence.date_to >= start,
    ).order_by(WorkAbsence.date_from)).all())
    return {
        "linked": True,
        "employee_id": me.id,
        "employee_name": me.full_name,
        "assignments": [assignment_dict(a, employees, stores) for a in assignments],
        "absences": [absence_dict(a, employees) for a in absences],
    }


@router.post("/assignments")
def create_assignment(payload: AssignmentIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    payload.shift_type = payload.shift_type.lower().strip()
    payload.employee_id = resolve_employee_id(db, payload.employee_id, payload.user_id)
    if payload.shift_type not in SHIFT_CAPACITY:
        raise HTTPException(400, "Смена должна быть day или night")
    conflicts = assignment_conflicts(db, payload)
    if conflicts:
        raise HTTPException(400, "; ".join(conflicts))
    slot = payload.slot or first_free_slot(db, payload.store_id, payload.work_date, payload.shift_type)
    if slot is None or slot < 1 or slot > SHIFT_CAPACITY[payload.shift_type]:
        raise HTTPException(400, "В этой смене все места уже заняты")
    occupied = db.scalar(select(WorkShiftAssignment).where(
        WorkShiftAssignment.store_id == payload.store_id,
        WorkShiftAssignment.work_date == payload.work_date,
        WorkShiftAssignment.shift_type == payload.shift_type,
        WorkShiftAssignment.slot == slot,
    ))
    if occupied:
        raise HTTPException(400, "Это место в смене уже занято")
    a = WorkShiftAssignment(
        store_id=payload.store_id,
        employee_id=payload.employee_id,
        user_id=None,
        work_date=payload.work_date,
        shift_type=payload.shift_type,
        slot=slot,
        created_by=user.id,
    )
    db.add(a); db.flush()
    log_change(db, action="created", entity_type="assignment", entity_id=a.id, changed_by=user.id, store_id=a.store_id, employee_id=a.employee_id, change_date=a.work_date, new={"shift_type": a.shift_type, "slot": a.slot})
    db.commit()
    return {"ok": True, "id": a.id, "slot": slot}


@router.delete("/assignments/{assignment_id}")
def delete_assignment(assignment_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    a = db.get(WorkShiftAssignment, assignment_id)
    if not a:
        raise HTTPException(404, "Смена не найдена")
    old = {"shift_type": a.shift_type, "slot": a.slot, "store_id": a.store_id, "employee_id": a.employee_id, "work_date": a.work_date.isoformat()}
    log_change(db, action="deleted", entity_type="assignment", entity_id=a.id, changed_by=user.id, store_id=a.store_id, employee_id=a.employee_id, change_date=a.work_date, old=old)
    db.delete(a); db.commit()
    return {"ok": True}


@router.post("/absences")
def create_absence(payload: AbsenceIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    employee_id = resolve_employee_id(db, payload.employee_id, payload.user_id)
    if payload.status not in ABSENCE_STATUSES:
        raise HTTPException(400, "Некорректный статус")
    if payload.date_to < payload.date_from:
        raise HTTPException(400, "Дата окончания раньше даты начала")
    a = WorkAbsence(
        employee_id=employee_id,
        user_id=None,
        date_from=payload.date_from,
        date_to=payload.date_to,
        status=payload.status,
        comment=payload.comment,
        created_by=user.id,
    )
    db.add(a); db.flush()
    log_change(db, action="created", entity_type="absence", entity_id=a.id, changed_by=user.id, employee_id=a.employee_id, change_date=a.date_from, new={"date_from": a.date_from.isoformat(), "date_to": a.date_to.isoformat(), "status": a.status, "comment": a.comment})
    db.commit()
    return {"ok": True, "id": a.id}


@router.delete("/absences/{absence_id}")
def delete_absence(absence_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    a = db.get(WorkAbsence, absence_id)
    if not a:
        raise HTTPException(404, "Статус не найден")
    old = {"date_from": a.date_from.isoformat(), "date_to": a.date_to.isoformat(), "status": a.status, "comment": a.comment}
    log_change(db, action="deleted", entity_type="absence", entity_id=a.id, changed_by=user.id, employee_id=a.employee_id, change_date=a.date_from, old=old)
    db.delete(a); db.commit()
    return {"ok": True}


def scan_errors(db: Session, start: date, end: date, store_id: int | None = None) -> list[dict[str, Any]]:
    stores_q = select(Store).where(Store.active.is_(True)).order_by(Store.name)
    if store_id:
        stores_q = stores_q.where(Store.id == store_id)
    stores = list(db.scalars(stores_q).all())
    store_map = {s.id: s for s in stores}
    employees = {e.id: e for e in db.scalars(select(Employee)).all()}
    assignments = list(db.scalars(select(WorkShiftAssignment).where(
        WorkShiftAssignment.work_date.between(start, end),
        WorkShiftAssignment.store_id.in_(list(store_map) or [-1]),
        WorkShiftAssignment.employee_id.is_not(None),
    )).all())
    absences = list(db.scalars(select(WorkAbsence).where(
        WorkAbsence.date_from <= end,
        WorkAbsence.date_to >= start,
        WorkAbsence.employee_id.is_not(None),
    )).all())
    bindings = set(db.execute(select(EmployeeStore.employee_id, EmployeeStore.store_id)).all())
    errors: list[dict[str, Any]] = []

    by_store_date_shift: dict[tuple[int, date, str], list[WorkShiftAssignment]] = {}
    by_employee_date: dict[tuple[int, date], list[WorkShiftAssignment]] = {}
    for a in assignments:
        by_store_date_shift.setdefault((a.store_id, a.work_date, a.shift_type), []).append(a)
        if a.employee_id:
            by_employee_date.setdefault((a.employee_id, a.work_date), []).append(a)

    d = start
    while d <= end:
        for s in stores:
            for shift_type, needed in SHIFT_CAPACITY.items():
                rows = by_store_date_shift.get((s.id, d, shift_type), [])
                if len(rows) < needed:
                    errors.append({
                        "type": "understaffed", "severity": "error", "store_id": s.id, "store_name": s.name,
                        "work_date": d.isoformat(), "shift_type": shift_type, "title": "Не заполнена смена",
                        "detail": f"Нужно {needed}, назначено {len(rows)}", "missing": needed - len(rows),
                        "can_create_substitution": True,
                    })
        d += timedelta(days=1)

    for (eid, d), rows in by_employee_date.items():
        if len(rows) > 1:
            errors.append({"type": "duplicate", "severity": "error", "employee_id": eid, "employee_name": employee_name(employees.get(eid)), "work_date": d.isoformat(), "title": "Пересечение смен", "detail": "Сотрудник назначен более чем на одну смену в один день", "can_create_substitution": False})

    for a in assignments:
        eid = a.employee_id
        if not eid:
            continue
        if (eid, a.store_id) not in bindings:
            errors.append({"type": "wrong_store", "severity": "warning", "store_id": a.store_id, "store_name": store_map.get(a.store_id).name if store_map.get(a.store_id) else str(a.store_id), "employee_id": eid, "employee_name": employee_name(employees.get(eid)), "work_date": a.work_date.isoformat(), "shift_type": a.shift_type, "title": "Сотрудник не закреплён за точкой", "detail": "Проверьте закрепление сотрудника", "can_create_substitution": True})
        emp = employees.get(eid)
        if emp and (not emp.active or emp.employment_status not in SCHEDULABLE_EMPLOYEE_STATUSES):
            errors.append({"type": "employee_status", "severity": "error", "store_id": a.store_id, "store_name": store_map.get(a.store_id).name if store_map.get(a.store_id) else str(a.store_id), "employee_id": eid, "employee_name": employee_name(emp), "work_date": a.work_date.isoformat(), "shift_type": a.shift_type, "title": "Сотрудник недоступен", "detail": f"Статус: {emp.employment_status}", "can_create_substitution": True})
        for ab in absences:
            if ab.employee_id == eid and ab.date_from <= a.work_date <= ab.date_to:
                errors.append({"type": "absence_conflict", "severity": "error", "store_id": a.store_id, "store_name": store_map.get(a.store_id).name if store_map.get(a.store_id) else str(a.store_id), "employee_id": eid, "employee_name": employee_name(employees.get(eid)), "work_date": a.work_date.isoformat(), "shift_type": a.shift_type, "title": "Смена пересекается со статусом сотрудника", "detail": ab.status, "can_create_substitution": True})
                break
        if a.shift_type == "night":
            next_day = by_employee_date.get((eid, a.work_date + timedelta(days=1)), [])
            if any(x.shift_type == "day" for x in next_day):
                errors.append({"type": "back_to_back", "severity": "error", "store_id": a.store_id, "store_name": store_map.get(a.store_id).name if store_map.get(a.store_id) else str(a.store_id), "employee_id": eid, "employee_name": employee_name(employees.get(eid)), "work_date": (a.work_date + timedelta(days=1)).isoformat(), "shift_type": "day", "title": "Две смены подряд", "detail": "После ночной смены назначена дневная без перерыва", "can_create_substitution": True})

    errors.sort(key=lambda x: (x.get("work_date", ""), x.get("store_name", ""), x.get("shift_type", "")))
    return errors


@router.get("/errors")
def errors(year: int, month: int, store_id: int | None = None, user=Depends(get_current_user), db: Session = Depends(get_db)):
    start, end = month_bounds(year, month)
    rows = scan_errors(db, start, end, store_id)
    return {"count": len(rows), "items": rows}


@router.get("/history")
def history(year: int, month: int, store_id: int | None = None, user=Depends(get_current_user), db: Session = Depends(get_db)):
    start, end = month_bounds(year, month)
    q = select(WorkScheduleChange).where(or_(WorkScheduleChange.change_date.is_(None), WorkScheduleChange.change_date.between(start, end))).order_by(WorkScheduleChange.created_at.desc()).limit(500)
    if store_id:
        q = q.where(WorkScheduleChange.store_id == store_id)
    changes = list(db.scalars(q).all())
    users = {u.id: u for u in db.scalars(select(User)).all()}
    employees = {e.id: e for e in db.scalars(select(Employee)).all()}
    stores = {s.id: s for s in db.scalars(select(Store)).all()}
    return [{
        "id": c.id, "action": c.action, "entity_type": c.entity_type, "entity_id": c.entity_id,
        "store_id": c.store_id,
        "store_name": stores.get(c.store_id).name if c.store_id and stores.get(c.store_id) else None,
        "employee_id": c.employee_id,
        "employee_name": employee_name(employees.get(c.employee_id)) if c.employee_id else None,
        "user_id": c.employee_id,
        "user_name": employee_name(employees.get(c.employee_id)) if c.employee_id else None,
        "changed_by": c.changed_by,
        "changed_by_name": user_name(users.get(c.changed_by)) if c.changed_by else "Система",
        "change_date": c.change_date.isoformat() if c.change_date else None,
        "old": c.old_json, "new": c.new_json,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    } for c in changes]


@router.get("/substitutions")
def list_substitutions(year: int, month: int, status: str | None = None, user=Depends(get_current_user), db: Session = Depends(get_db)):
    start, end = month_bounds(year, month)
    q = select(WorkSubstitution).where(WorkSubstitution.work_date.between(start, end)).order_by(WorkSubstitution.work_date.desc(), WorkSubstitution.id.desc())
    if status:
        q = q.where(WorkSubstitution.status == status)
    rows = list(db.scalars(q).all())
    employees = {e.id: e for e in db.scalars(select(Employee)).all()}
    stores = {s.id: s for s in db.scalars(select(Store)).all()}
    return [{
        "id": x.id, "store_id": x.store_id,
        "store_name": stores.get(x.store_id).name if stores.get(x.store_id) else str(x.store_id),
        "work_date": x.work_date.isoformat(), "shift_type": x.shift_type,
        "absent_employee_id": x.absent_employee_id,
        "absent_employee_name": employee_name(employees.get(x.absent_employee_id)) if x.absent_employee_id else None,
        "replacement_employee_id": x.replacement_employee_id,
        "replacement_employee_name": employee_name(employees.get(x.replacement_employee_id)) if x.replacement_employee_id else None,
        # compatibility keys
        "absent_user_id": x.absent_employee_id,
        "absent_user_name": employee_name(employees.get(x.absent_employee_id)) if x.absent_employee_id else None,
        "replacement_user_id": x.replacement_employee_id,
        "replacement_user_name": employee_name(employees.get(x.replacement_employee_id)) if x.replacement_employee_id else None,
        "reason": x.reason, "comment": x.comment, "status": x.status,
        "created_at": x.created_at.isoformat() if x.created_at else None,
    } for x in rows]


@router.post("/substitutions")
def create_substitution(payload: SubstitutionIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    if payload.shift_type not in SHIFT_CAPACITY:
        raise HTTPException(400, "Некорректная смена")
    if not db.get(Store, payload.store_id):
        raise HTTPException(404, "Магазин не найден")
    absent_employee_id = None
    if payload.absent_employee_id or payload.absent_user_id:
        absent_employee_id = resolve_employee_id(db, payload.absent_employee_id, payload.absent_user_id)
    x = WorkSubstitution(
        store_id=payload.store_id, work_date=payload.work_date, shift_type=payload.shift_type,
        absent_employee_id=absent_employee_id, absent_user_id=None,
        reason=payload.reason, comment=payload.comment, status="new", created_by=user.id,
    )
    db.add(x); db.flush()
    log_change(db, action="created", entity_type="substitution", entity_id=x.id, changed_by=user.id, store_id=x.store_id, employee_id=x.absent_employee_id, change_date=x.work_date, new={"shift_type": x.shift_type, "reason": x.reason})
    db.commit()
    return {"ok": True, "id": x.id}


@router.get("/substitutions/{substitution_id}/candidates")
def substitution_candidates(substitution_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    x = db.get(WorkSubstitution, substitution_id)
    if not x:
        raise HTTPException(404, "Подмена не найдена")
    employees = list(db.scalars(select(Employee).where(Employee.active.is_(True)).order_by(Employee.full_name, Employee.id)).all())
    rows = []
    for e in employees:
        reasons = []
        if e.id == x.absent_employee_id:
            reasons.append("Это отсутствующий сотрудник")
        if e.employment_status not in SCHEDULABLE_EMPLOYEE_STATUSES:
            reasons.append("Статус сотрудника не позволяет назначить смену")
        if x.store_id not in assigned_store_ids(db, e.id):
            reasons.append("Не закреплён за точкой")
        test = AssignmentIn(store_id=x.store_id, employee_id=e.id, work_date=x.work_date, shift_type=x.shift_type)
        for r in assignment_conflicts(db, test):
            if r not in reasons:
                reasons.append(r)
        rows.append({"employee_id": e.id, "employee_name": e.full_name, "user_id": e.id, "user_name": e.full_name, "eligible": not reasons, "reasons": reasons})
    rows.sort(key=lambda r: (not r["eligible"], r["employee_name"]))
    return rows


@router.post("/substitutions/{substitution_id}/assign")
def assign_substitution(substitution_id: int, payload: ReplacementIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    x = db.get(WorkSubstitution, substitution_id)
    if not x:
        raise HTTPException(404, "Подмена не найдена")
    replacement_employee_id = resolve_employee_id(db, payload.replacement_employee_id, payload.replacement_user_id)

    old_assignment = None
    slot = None
    if x.absent_employee_id:
        old_assignment = db.scalar(select(WorkShiftAssignment).where(
            WorkShiftAssignment.store_id == x.store_id,
            WorkShiftAssignment.work_date == x.work_date,
            WorkShiftAssignment.shift_type == x.shift_type,
            WorkShiftAssignment.employee_id == x.absent_employee_id,
        ))
        if old_assignment:
            slot = old_assignment.slot

    test = AssignmentIn(store_id=x.store_id, employee_id=replacement_employee_id, work_date=x.work_date, shift_type=x.shift_type, slot=slot)
    conflicts = assignment_conflicts(db, test)
    if conflicts:
        raise HTTPException(400, "; ".join(conflicts))

    if old_assignment:
        old_data = {"employee_id": old_assignment.employee_id, "shift_type": old_assignment.shift_type, "slot": old_assignment.slot}
        old_id = old_assignment.id
        db.delete(old_assignment); db.flush()
        log_change(db, action="deleted_for_substitution", entity_type="assignment", entity_id=old_id, changed_by=user.id, store_id=x.store_id, employee_id=x.absent_employee_id, change_date=x.work_date, old=old_data)
    if slot is None:
        slot = first_free_slot(db, x.store_id, x.work_date, x.shift_type)
    if slot is None:
        raise HTTPException(400, "В смене нет свободного места")

    a = WorkShiftAssignment(store_id=x.store_id, employee_id=replacement_employee_id, user_id=None, work_date=x.work_date, shift_type=x.shift_type, slot=slot, created_by=user.id)
    db.add(a); db.flush()
    x.replacement_employee_id = replacement_employee_id
    x.replacement_user_id = None
    x.status = "assigned"
    log_change(db, action="assigned", entity_type="substitution", entity_id=x.id, changed_by=user.id, store_id=x.store_id, employee_id=replacement_employee_id, change_date=x.work_date, new={"replacement_employee_id": replacement_employee_id, "assignment_id": a.id})
    db.commit()
    return {"ok": True, "assignment_id": a.id}


@router.patch("/substitutions/{substitution_id}/status")
def update_substitution_status(substitution_id: int, payload: StatusIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    if payload.status not in SUB_STATUSES:
        raise HTTPException(400, "Некорректный статус")
    x = db.get(WorkSubstitution, substitution_id)
    if not x:
        raise HTTPException(404, "Подмена не найдена")
    old = x.status
    x.status = payload.status
    log_change(db, action="status_changed", entity_type="substitution", entity_id=x.id, changed_by=user.id, store_id=x.store_id, employee_id=x.replacement_employee_id or x.absent_employee_id, change_date=x.work_date, old={"status": old}, new={"status": x.status})
    db.commit()
    return {"ok": True}
