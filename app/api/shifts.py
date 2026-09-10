from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import APP_TIMEZONE
from app.core.security import get_current_user, user_store_ids
from app.db.database import get_db
from app.db.models import (
    Employee,
    EmployeeStore,
    Photo,
    ShiftReport,
    ShiftReportValue,
    ShiftReviewEvent,
    ShiftReviewRemark,
    ShiftTemplate,
    ShiftTemplateField,
    Store,
    User,
    WorkShiftAssignment,
)

router = APIRouter(prefix="/api/shifts", tags=["shifts"])

FINAL_STATUSES = {"accepted", "accepted_with_remarks"}
EDITABLE_STATUSES = {"draft", "rejected"}
REVIEW_DECISIONS = {"accepted", "accepted_with_remarks", "rejected"}
STATUS_NAMES = {
    "draft": "Черновик",
    "review": "На проверке",
    "accepted": "Принята",
    "accepted_with_remarks": "Принята с замечаниями",
    "rejected": "Не принята",
}


def _today_local() -> date:
    try:
        return datetime.now(ZoneInfo(APP_TIMEZONE)).date()
    except Exception:
        return date.today()


def _now_local() -> datetime:
    try:
        return datetime.now(ZoneInfo(APP_TIMEZONE))
    except Exception:
        return datetime.now().astimezone()


def _linked_employee(db: Session, user: User) -> Employee | None:
    return db.scalar(select(Employee).where(Employee.user_id == user.id, Employee.active.is_(True)))


def _scope_store_ids(db: Session, user: User) -> list[int]:
    ids = set(user_store_ids(db, user))
    employee = _linked_employee(db, user)
    if employee:
        ids.update(db.scalars(select(EmployeeStore.store_id).where(EmployeeStore.employee_id == employee.id)).all())
    return sorted(ids)


def _can_access_store(db: Session, user: User, store_id: int) -> bool:
    return store_id in _scope_store_ids(db, user)


def _assert_store(db: Session, user: User, store_id: int):
    if not _can_access_store(db, user, store_id):
        raise HTTPException(403, "Нет доступа к магазину")


def _kind_aliases(shift_type: str) -> list[str]:
    return ["day", "evening"] if shift_type == "day" else ["night", "morning"]


def _template_for_assignment(db: Session, assignment: WorkShiftAssignment) -> ShiftTemplate | None:
    aliases = _kind_aliases(assignment.shift_type)
    templates = list(db.scalars(
        select(ShiftTemplate)
        .where(
            ShiftTemplate.store_id == assignment.store_id,
            ShiftTemplate.active.is_(True),
            ShiftTemplate.shift_kind.in_(aliases),
        )
        .order_by(ShiftTemplate.id.desc())
    ).all())
    if not templates:
        return None
    exact = [t for t in templates if t.shift_kind == assignment.shift_type]
    return exact[0] if exact else templates[0]


def _field_options(field: ShiftTemplateField) -> dict[str, Any]:
    raw = field.options_json
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        return {"options": raw}
    return {}


def _report_values(db: Session, report_id: int):
    rows = db.execute(
        select(ShiftReportValue, ShiftTemplateField)
        .join(ShiftTemplateField, ShiftTemplateField.id == ShiftReportValue.field_id)
        .where(ShiftReportValue.report_id == report_id)
        .order_by(ShiftTemplateField.sort_order, ShiftTemplateField.id)
    ).all()
    return rows


def _photo_count(db: Session, value_id: int) -> int:
    return len(list(db.scalars(select(Photo.id).where(Photo.entity_type == "shift_report_field", Photo.entity_id == value_id)).all()))


def _issues_for_report(db: Session, report: ShiftReport) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for value_row, field in _report_values(db, report.id):
        opts = _field_options(field)
        value = value_row.value_json
        if field.field_type == "photo":
            count = _photo_count(db, value_row.id)
            if field.required and count == 0:
                issues.append({"field_id": field.id, "label": field.label, "detail": "Нет обязательного фото"})
            continue
        if field.required and value in (None, "", []):
            issues.append({"field_id": field.id, "label": field.label, "detail": "Поле не заполнено"})
            continue
        if field.field_type == "boolean" and opts.get("deviation_on_false") and value is False:
            issues.append({"field_id": field.id, "label": field.label, "detail": "Отмечено отклонение"})
        if field.field_type == "number" and value not in (None, ""):
            try:
                num = float(value)
                if opts.get("min") is not None and num < float(opts["min"]):
                    issues.append({"field_id": field.id, "label": field.label, "detail": f"Значение ниже нормы ({opts['min']})"})
                if opts.get("max") is not None and num > float(opts["max"]):
                    issues.append({"field_id": field.id, "label": field.label, "detail": f"Значение выше нормы ({opts['max']})"})
            except (TypeError, ValueError):
                pass
        if field.field_type == "select" and value in (opts.get("deviation_values") or []):
            issues.append({"field_id": field.id, "label": field.label, "detail": f"Выбрано: {value}"})
    return issues


def _snapshot(db: Session, report: ShiftReport) -> dict[str, Any]:
    values = []
    for value_row, field in _report_values(db, report.id):
        values.append({
            "field_id": field.id,
            "label": field.label,
            "field_type": field.field_type,
            "value": value_row.value_json,
            "photo_count": _photo_count(db, value_row.id) if field.field_type == "photo" else 0,
        })
    active_remarks = list(db.scalars(
        select(ShiftReviewRemark)
        .where(ShiftReviewRemark.report_id == report.id, ShiftReviewRemark.active.is_(True))
        .order_by(ShiftReviewRemark.created_at)
    ).all())
    return {
        "report_id": report.id,
        "revision": report.revision,
        "status": report.status,
        "values": values,
        "review_comment": report.review_comment,
        "remarks": [{"field_id": r.field_id, "remark": r.remark} for r in active_remarks],
    }


def _due_state(assignment: WorkShiftAssignment) -> str:
    now = _now_local()
    if assignment.shift_type == "day":
        d = assignment.work_date
        start = datetime.combine(d, time(19, 30), tzinfo=now.tzinfo)
        end = datetime.combine(d, time(20, 10), tzinfo=now.tzinfo)
    else:
        d = assignment.work_date + timedelta(days=1)
        start = datetime.combine(d, time(7, 30), tzinfo=now.tzinfo)
        end = datetime.combine(d, time(8, 10), tzinfo=now.tzinfo)
    if now < start:
        return "upcoming"
    if now <= end:
        return "due"
    return "late"


def _report_brief(db: Session, report: ShiftReport) -> dict[str, Any]:
    store = db.get(Store, report.store_id)
    employee = db.get(Employee, report.employee_id) if report.employee_id else None
    author = db.get(User, report.submitted_by)
    reviewer = db.get(User, report.reviewed_by) if report.reviewed_by else None
    return {
        "id": report.id,
        "store_id": report.store_id,
        "store_name": store.name if store else f"Магазин #{report.store_id}",
        "employee_id": report.employee_id,
        "employee_name": employee.full_name if employee else (author.full_name if author else "—"),
        "shift_kind": report.shift_kind,
        "work_date": report.work_date.isoformat() if report.work_date else None,
        "status": report.status,
        "status_name": STATUS_NAMES.get(report.status, report.status),
        "revision": report.revision,
        "submitted_at": report.submitted_at.isoformat() if report.submitted_at else None,
        "reviewed_at": report.reviewed_at.isoformat() if report.reviewed_at else None,
        "reviewed_by_name": reviewer.full_name if reviewer else None,
        "review_comment": report.review_comment,
        "issues_count": len(_issues_for_report(db, report)),
    }


def _report_detail(db: Session, report: ShiftReport) -> dict[str, Any]:
    result = _report_brief(db, report)
    values = []
    remarks = list(db.scalars(
        select(ShiftReviewRemark)
        .where(ShiftReviewRemark.report_id == report.id, ShiftReviewRemark.active.is_(True))
        .order_by(ShiftReviewRemark.created_at)
    ).all())
    remarks_by_field: dict[int | None, list[str]] = {}
    for remark in remarks:
        remarks_by_field.setdefault(remark.field_id, []).append(remark.remark)
    for value_row, field in _report_values(db, report.id):
        opts = _field_options(field)
        photos = list(db.scalars(
            select(Photo)
            .where(Photo.entity_type == "shift_report_field", Photo.entity_id == value_row.id)
            .order_by(Photo.created_at)
        ).all())
        values.append({
            "value_id": value_row.id,
            "field_id": field.id,
            "key": field.key,
            "label": field.label,
            "field_type": field.field_type,
            "required": field.required,
            "options": opts,
            "value": value_row.value_json,
            "photo_count": len(photos),
            "photo_ids": [p.id for p in photos],
            "remarks": remarks_by_field.get(field.id, []),
        })
    events = list(db.scalars(
        select(ShiftReviewEvent)
        .where(ShiftReviewEvent.report_id == report.id)
        .order_by(ShiftReviewEvent.created_at.desc())
    ).all())
    result.update({
        "values": values,
        "general_remarks": remarks_by_field.get(None, []),
        "issues": _issues_for_report(db, report),
        "events": [
            {
                "id": e.id,
                "action": e.action,
                "revision": e.revision,
                "comment": e.comment,
                "created_at": e.created_at.isoformat() if e.created_at else None,
                "reviewer_id": e.reviewer_id,
                "snapshot": e.snapshot_json,
            }
            for e in events
        ],
    })
    return result


@router.get("/mine")
def mine(
    date_from: date | None = None,
    date_to: date | None = None,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    employee = _linked_employee(db, user)
    if not employee:
        return {"employee": None, "items": []}
    today = _today_local()
    start = date_from or (today - timedelta(days=7))
    end = date_to or (today + timedelta(days=7))
    assignments = list(db.scalars(
        select(WorkShiftAssignment)
        .where(
            WorkShiftAssignment.employee_id == employee.id,
            WorkShiftAssignment.work_date >= start,
            WorkShiftAssignment.work_date <= end,
        )
        .order_by(WorkShiftAssignment.work_date.desc(), WorkShiftAssignment.shift_type)
    ).all())
    items = []
    for assignment in assignments:
        store = db.get(Store, assignment.store_id)
        report = db.scalar(select(ShiftReport).where(ShiftReport.work_assignment_id == assignment.id).order_by(ShiftReport.id.desc()))
        template = _template_for_assignment(db, assignment)
        items.append({
            "assignment_id": assignment.id,
            "work_date": assignment.work_date.isoformat(),
            "shift_type": assignment.shift_type,
            "store_id": assignment.store_id,
            "store_name": store.name if store else f"Магазин #{assignment.store_id}",
            "due_state": _due_state(assignment),
            "template_id": template.id if template else None,
            "template_name": template.name if template else None,
            "report": _report_brief(db, report) if report else None,
        })
    return {"employee": {"id": employee.id, "name": employee.full_name}, "items": items}


@router.post("/drafts")
def create_or_open_draft(payload: dict[str, Any], user=Depends(get_current_user), db: Session = Depends(get_db)):
    assignment_id = int(payload.get("assignment_id") or 0)
    assignment = db.get(WorkShiftAssignment, assignment_id)
    if not assignment:
        raise HTTPException(404, "Смена в графике не найдена")
    employee = _linked_employee(db, user)
    if not employee or assignment.employee_id != employee.id:
        raise HTTPException(403, "Эта смена назначена другому сотруднику")
    existing = db.scalar(select(ShiftReport).where(ShiftReport.work_assignment_id == assignment.id).order_by(ShiftReport.id.desc()))
    if existing:
        if existing.status in {"review", "accepted", "accepted_with_remarks"}:
            return _report_detail(db, existing)
        return _report_detail(db, existing)
    template = _template_for_assignment(db, assignment)
    if not template:
        raise HTTPException(400, "Для этой точки и смены ещё не настроена форма пересменки")
    report = ShiftReport(
        store_id=assignment.store_id,
        template_id=template.id,
        submitted_by=user.id,
        employee_id=employee.id,
        work_assignment_id=assignment.id,
        work_date=assignment.work_date,
        shift_kind=assignment.shift_type,
        status="draft",
        revision=1,
    )
    db.add(report)
    db.flush()
    fields = list(db.scalars(
        select(ShiftTemplateField)
        .where(ShiftTemplateField.template_id == template.id)
        .order_by(ShiftTemplateField.sort_order, ShiftTemplateField.id)
    ).all())
    for field in fields:
        db.add(ShiftReportValue(report_id=report.id, field_id=field.id, value_json=None))
    db.commit()
    db.refresh(report)
    return _report_detail(db, report)


@router.get("/reports/{report_id}")
def report_detail(report_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    report = db.get(ShiftReport, report_id)
    if not report:
        raise HTTPException(404, "Пересменка не найдена")
    if report.submitted_by != user.id:
        _assert_store(db, user, report.store_id)
    return _report_detail(db, report)


@router.patch("/reports/{report_id}/draft")
def save_draft(report_id: int, payload: dict[str, Any], user=Depends(get_current_user), db: Session = Depends(get_db)):
    report = db.get(ShiftReport, report_id)
    if not report:
        raise HTTPException(404, "Пересменка не найдена")
    if report.submitted_by != user.id:
        raise HTTPException(403, "Редактировать пересменку может только сотрудник, который её сдаёт")
    if report.status not in EDITABLE_STATUSES:
        raise HTTPException(400, "Эта пересменка уже отправлена на проверку")
    values = payload.get("values") or {}
    allowed = {f.id for f in db.scalars(select(ShiftTemplateField).where(ShiftTemplateField.template_id == report.template_id)).all()}
    for value_row in db.scalars(select(ShiftReportValue).where(ShiftReportValue.report_id == report.id)).all():
        if value_row.field_id not in allowed:
            continue
        key = str(value_row.field_id)
        if key in values:
            value_row.value_json = values[key]
    db.commit()
    return _report_detail(db, report)


@router.post("/reports/{report_id}/submit")
def submit_report(report_id: int, payload: dict[str, Any] | None = None, user=Depends(get_current_user), db: Session = Depends(get_db)):
    report = db.get(ShiftReport, report_id)
    if not report:
        raise HTTPException(404, "Пересменка не найдена")
    if report.submitted_by != user.id:
        raise HTTPException(403, "Отправить пересменку может только сотрудник, который её сдаёт")
    if report.status not in EDITABLE_STATUSES:
        raise HTTPException(400, "Пересменка уже отправлена на проверку")
    if payload and payload.get("values"):
        save_draft(report_id, payload, user, db)
        report = db.get(ShiftReport, report_id)
    issues = _issues_for_report(db, report)
    missing = [x for x in issues if x["detail"] in {"Поле не заполнено", "Нет обязательного фото"}]
    if missing:
        raise HTTPException(400, "Заполните обязательные поля: " + ", ".join(x["label"] for x in missing))
    was_rejected = report.status == "rejected"
    if was_rejected:
        report.revision = int(report.revision or 1) + 1
        for remark in db.scalars(select(ShiftReviewRemark).where(ShiftReviewRemark.report_id == report.id, ShiftReviewRemark.active.is_(True))).all():
            remark.active = False
    report.status = "review"
    report.submitted_at = datetime.utcnow()
    report.reviewed_by = None
    report.reviewed_at = None
    report.review_comment = None
    db.flush()
    db.add(ShiftReviewEvent(
        report_id=report.id,
        reviewer_id=None,
        action="resubmitted" if was_rejected else "submitted",
        revision=report.revision,
        snapshot_json=_snapshot(db, report),
    ))
    db.commit()
    return _report_detail(db, report)


@router.get("/control")
def control(
    control_date: date | None = None,
    store_id: int | None = None,
    status: str | None = None,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    target = control_date or _today_local()
    store_ids = _scope_store_ids(db, user)
    if store_id:
        _assert_store(db, user, store_id)
        store_ids = [store_id]
    if not store_ids:
        return {"date": target.isoformat(), "summary": {"total": 0, "review": 0, "accepted": 0, "remarks": 0, "rejected": 0, "missing": 0}, "items": []}

    # Day shifts hand over on their work date. Night shifts hand over the next morning.
    candidate_dates = [target, target - timedelta(days=1)]
    assignments = list(db.scalars(
        select(WorkShiftAssignment)
        .where(
            WorkShiftAssignment.store_id.in_(store_ids),
            WorkShiftAssignment.work_date.in_(candidate_dates),
        )
        .order_by(WorkShiftAssignment.store_id, WorkShiftAssignment.work_date, WorkShiftAssignment.shift_type, WorkShiftAssignment.slot)
    ).all())
    items = []
    for assignment in assignments:
        is_due_date = (assignment.shift_type == "day" and assignment.work_date == target) or (assignment.shift_type == "night" and assignment.work_date + timedelta(days=1) == target)
        if not is_due_date:
            continue
        store = db.get(Store, assignment.store_id)
        employee = db.get(Employee, assignment.employee_id) if assignment.employee_id else None
        report = db.scalar(select(ShiftReport).where(ShiftReport.work_assignment_id == assignment.id).order_by(ShiftReport.id.desc()))
        effective_status = report.status if report else ("missing" if _due_state(assignment) == "late" else "expected")
        if status and effective_status != status:
            continue
        items.append({
            "assignment_id": assignment.id,
            "store_id": assignment.store_id,
            "store_name": store.name if store else f"Магазин #{assignment.store_id}",
            "employee_id": assignment.employee_id,
            "employee_name": employee.full_name if employee else "—",
            "shift_type": assignment.shift_type,
            "work_date": assignment.work_date.isoformat(),
            "due_state": _due_state(assignment),
            "status": effective_status,
            "report": _report_brief(db, report) if report else None,
        })
    summary = {
        "total": len(items),
        "review": sum(1 for x in items if x["status"] == "review"),
        "accepted": sum(1 for x in items if x["status"] == "accepted"),
        "remarks": sum(1 for x in items if x["status"] == "accepted_with_remarks"),
        "rejected": sum(1 for x in items if x["status"] == "rejected"),
        "missing": sum(1 for x in items if x["status"] == "missing"),
        "expected": sum(1 for x in items if x["status"] == "expected"),
    }
    return {"date": target.isoformat(), "summary": summary, "items": items}


class ReviewRemarkIn(BaseModel):
    field_id: int | None = None
    remark: str


class ReviewIn(BaseModel):
    decision: str
    general_comment: str | None = None
    remarks: list[ReviewRemarkIn] = Field(default_factory=list)


@router.post("/reports/{report_id}/review")
def review_report(report_id: int, payload: ReviewIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    if payload.decision not in REVIEW_DECISIONS:
        raise HTTPException(400, "Неизвестное решение")
    report = db.get(ShiftReport, report_id)
    if not report:
        raise HTTPException(404, "Пересменка не найдена")
    _assert_store(db, user, report.store_id)
    if report.status != "review":
        raise HTTPException(400, "На проверку можно обработать только пересменку со статусом «На проверке»")
    clean_general = (payload.general_comment or "").strip() or None
    clean_remarks = [r for r in payload.remarks if r.remark.strip()]
    if payload.decision in {"accepted_with_remarks", "rejected"} and not (clean_general or clean_remarks):
        raise HTTPException(400, "Для этого решения добавьте хотя бы одно замечание")
    valid_fields = {f.id for f in db.scalars(select(ShiftTemplateField).where(ShiftTemplateField.template_id == report.template_id)).all()}
    for old in db.scalars(select(ShiftReviewRemark).where(ShiftReviewRemark.report_id == report.id, ShiftReviewRemark.active.is_(True))).all():
        old.active = False
    for item in clean_remarks:
        if item.field_id is not None and item.field_id not in valid_fields:
            raise HTTPException(400, "Замечание относится к другому полю")
        db.add(ShiftReviewRemark(
            report_id=report.id,
            field_id=item.field_id,
            reviewer_id=user.id,
            remark=item.remark.strip(),
            active=True,
        ))
    if clean_general:
        db.add(ShiftReviewRemark(
            report_id=report.id,
            field_id=None,
            reviewer_id=user.id,
            remark=clean_general,
            active=True,
        ))
    report.status = payload.decision
    report.review_comment = clean_general
    report.reviewed_by = user.id
    report.reviewed_at = datetime.utcnow()
    db.flush()
    db.add(ShiftReviewEvent(
        report_id=report.id,
        reviewer_id=user.id,
        action=payload.decision,
        revision=report.revision,
        comment=clean_general,
        snapshot_json=_snapshot(db, report),
    ))
    db.commit()
    return _report_detail(db, report)


@router.get("/history")
def history(
    date_from: date | None = None,
    date_to: date | None = None,
    store_id: int | None = None,
    status: str | None = None,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    today = _today_local()
    start = date_from or today.replace(day=1)
    end = date_to or today
    store_ids = _scope_store_ids(db, user)
    if store_id:
        _assert_store(db, user, store_id)
        store_ids = [store_id]
    if not store_ids:
        return []
    stmt = select(ShiftReport).where(
        ShiftReport.store_id.in_(store_ids),
        ShiftReport.work_date.is_not(None),
        ShiftReport.work_date >= start,
        ShiftReport.work_date <= end,
        ShiftReport.status != "draft",
    ).order_by(ShiftReport.work_date.desc(), ShiftReport.submitted_at.desc())
    if status:
        stmt = stmt.where(ShiftReport.status == status)
    return [_report_brief(db, r) for r in db.scalars(stmt).all()]


@router.get("/forms")
def forms(store_id: int | None = None, user=Depends(get_current_user), db: Session = Depends(get_db)):
    store_ids = _scope_store_ids(db, user)
    if store_id:
        _assert_store(db, user, store_id)
        store_ids = [store_id]
    if not store_ids:
        return []
    stmt = select(ShiftTemplate).where(ShiftTemplate.store_id.in_(store_ids)).order_by(ShiftTemplate.store_id, ShiftTemplate.name)
    result = []
    for template in db.scalars(stmt).all():
        store = db.get(Store, template.store_id)
        fields = list(db.scalars(
            select(ShiftTemplateField)
            .where(ShiftTemplateField.template_id == template.id)
            .order_by(ShiftTemplateField.sort_order, ShiftTemplateField.id)
        ).all())
        result.append({
            "id": template.id,
            "store_id": template.store_id,
            "store_name": store.name if store else f"Магазин #{template.store_id}",
            "name": template.name,
            "shift_kind": template.shift_kind,
            "active": template.active,
            "fields": [
                {
                    "id": f.id,
                    "key": f.key,
                    "label": f.label,
                    "field_type": f.field_type,
                    "required": f.required,
                    "sort_order": f.sort_order,
                    "options_json": f.options_json,
                }
                for f in fields
            ],
        })
    return result


class FormFieldIn(BaseModel):
    label: str
    field_type: str = "text"
    required: bool = False
    options_json: dict | list | None = None


class FormIn(BaseModel):
    store_id: int
    name: str
    shift_kind: str
    active: bool = True
    fields: list[FormFieldIn] = Field(default_factory=list)


def _validate_form(payload: FormIn):
    if payload.shift_kind not in {"day", "night"}:
        raise HTTPException(400, "Тип формы: day или night")
    if not payload.name.strip():
        raise HTTPException(400, "Укажите название формы")
    allowed_types = {"text", "textarea", "number", "boolean", "select", "photo"}
    for field in payload.fields:
        if field.field_type not in allowed_types:
            raise HTTPException(400, f"Неизвестный тип поля: {field.field_type}")
        if not field.label.strip():
            raise HTTPException(400, "У каждого поля должно быть название")


@router.post("/forms")
def create_form(payload: FormIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    _assert_store(db, user, payload.store_id)
    _validate_form(payload)
    if payload.active:
        for old in db.scalars(select(ShiftTemplate).where(
            ShiftTemplate.store_id == payload.store_id,
            ShiftTemplate.shift_kind == payload.shift_kind,
            ShiftTemplate.active.is_(True),
        )).all():
            old.active = False
    template = ShiftTemplate(
        store_id=payload.store_id,
        name=payload.name.strip(),
        shift_kind=payload.shift_kind,
        active=payload.active,
    )
    db.add(template)
    db.flush()
    for i, field in enumerate(payload.fields):
        db.add(ShiftTemplateField(
            template_id=template.id,
            key=f"f_{template.id}_{i}_{int(datetime.utcnow().timestamp())}",
            label=field.label.strip(),
            field_type=field.field_type,
            required=field.required,
            sort_order=i,
            options_json=field.options_json,
        ))
    db.commit()
    return {"id": template.id}


@router.put("/forms/{template_id}")
def update_form(template_id: int, payload: FormIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    template = db.get(ShiftTemplate, template_id)
    if not template:
        raise HTTPException(404, "Форма не найдена")
    _assert_store(db, user, template.store_id)
    _assert_store(db, user, payload.store_id)
    _validate_form(payload)
    used = db.scalar(select(ShiftReport.id).where(ShiftReport.template_id == template.id).limit(1))
    if used:
        # Version the form instead of rewriting fields referenced by historical reports.
        template.active = False
        if payload.active:
            for old in db.scalars(select(ShiftTemplate).where(
                ShiftTemplate.store_id == payload.store_id,
                ShiftTemplate.shift_kind == payload.shift_kind,
                ShiftTemplate.active.is_(True),
                ShiftTemplate.id != template.id,
            )).all():
                old.active = False
        replacement = ShiftTemplate(
            store_id=payload.store_id,
            name=payload.name.strip(),
            shift_kind=payload.shift_kind,
            active=payload.active,
        )
        db.add(replacement)
        db.flush()
        for i, field in enumerate(payload.fields):
            db.add(ShiftTemplateField(
                template_id=replacement.id,
                key=f"f_{replacement.id}_{i}_{int(datetime.utcnow().timestamp())}",
                label=field.label.strip(),
                field_type=field.field_type,
                required=field.required,
                sort_order=i,
                options_json=field.options_json,
            ))
        db.commit()
        return {"ok": True, "versioned": True, "id": replacement.id}
    if payload.active:
        for old in db.scalars(select(ShiftTemplate).where(
            ShiftTemplate.store_id == payload.store_id,
            ShiftTemplate.shift_kind == payload.shift_kind,
            ShiftTemplate.active.is_(True),
            ShiftTemplate.id != template.id,
        )).all():
            old.active = False
    template.store_id = payload.store_id
    template.name = payload.name.strip()
    template.shift_kind = payload.shift_kind
    template.active = payload.active
    db.execute(delete(ShiftTemplateField).where(ShiftTemplateField.template_id == template.id))
    for i, field in enumerate(payload.fields):
        db.add(ShiftTemplateField(
            template_id=template.id,
            key=f"f_{template.id}_{i}_{int(datetime.utcnow().timestamp())}",
            label=field.label.strip(),
            field_type=field.field_type,
            required=field.required,
            sort_order=i,
            options_json=field.options_json,
        ))
    db.commit()
    return {"ok": True, "history_locked": False}


@router.delete("/forms/{template_id}")
def delete_form(template_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    template = db.get(ShiftTemplate, template_id)
    if not template:
        raise HTTPException(404, "Форма не найдена")
    _assert_store(db, user, template.store_id)
    used = db.scalar(select(ShiftReport.id).where(ShiftReport.template_id == template.id).limit(1))
    if used:
        template.active = False
        db.commit()
        return {"ok": True, "soft_deleted": True}
    db.delete(template)
    db.commit()
    return {"ok": True, "soft_deleted": False}


# Backward-compatible endpoints used by older UI code.
@router.get("/templates")
def templates(store_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    return forms(store_id=store_id, user=user, db=db)


@router.get("/reports")
def reports(store_id: int | None = None, user=Depends(get_current_user), db: Session = Depends(get_db)):
    return history(store_id=store_id, user=user, db=db)
