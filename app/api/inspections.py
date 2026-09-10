from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import APP_TIMEZONE, MIN_INSPECTIONS_PER_STORE_PER_WEEK
from app.core.security import get_current_user, assert_store_access, MANAGEMENT_ROLES, user_store_ids
from app.db.database import get_db
from app.db.models import (
    InspectionTemplate, InspectionTemplateStore, InspectionTemplateField,
    Inspection, InspectionValue, Violation, Photo, Store, User,
    TaskV2, TaskAssignee, TaskAttachment,
)

router = APIRouter(prefix="/api/inspections", tags=["inspections"])

SEVERITIES = {"low", "medium", "high", "critical"}
FIELD_TYPES = {"boolean", "select", "number", "text", "textarea", "photo"}


def _require_management(user):
    if user.role not in MANAGEMENT_ROLES:
        raise HTTPException(403, "Проверки доступны управляющим и руководству")


def _local_today():
    try:
        return datetime.now(ZoneInfo(APP_TIMEZONE)).date()
    except Exception:
        return datetime.now().date()


def _week_bounds_utc():
    today = _local_today()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    try:
        tz = ZoneInfo(APP_TIMEZONE)
        start = datetime.combine(monday, datetime.min.time(), tzinfo=tz).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        end = datetime.combine(sunday + timedelta(days=1), datetime.min.time(), tzinfo=tz).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    except Exception:
        start = datetime.combine(monday, datetime.min.time())
        end = datetime.combine(sunday + timedelta(days=1), datetime.min.time())
    return monday, sunday, start, end


def _name_user(db: Session, uid: int | None):
    u = db.get(User, uid) if uid else None
    return (u.full_name or u.username or str(u.telegram_id)) if u else None


def _store_name(db: Session, sid: int | None):
    s = db.get(Store, sid) if sid else None
    return s.name if s else None


def _field_payload(f: InspectionTemplateField):
    return {
        "id": f.id, "key": f.key, "label": f.label, "field_type": f.field_type,
        "required": f.required, "sort_order": f.sort_order, "options": f.options_json or [],
        "creates_violation_on_false": f.creates_violation_on_false,
        "help_text": f.help_text, "allow_comment": f.allow_comment,
        "require_photo": f.require_photo, "default_severity": f.default_severity or "medium",
        "violation_rule": f.violation_rule_json,
    }


def _template_store_ids(db: Session, template_id: int):
    return list(db.scalars(select(InspectionTemplateStore.store_id).where(InspectionTemplateStore.template_id == template_id)).all())


def _template_allowed_for_store(db: Session, template_id: int, store_id: int):
    assigned = _template_store_ids(db, template_id)
    return not assigned or store_id in assigned


def _photo_payload(db: Session, entity_type: str, entity_id: int):
    rows = db.scalars(select(Photo).where(Photo.entity_type == entity_type, Photo.entity_id == entity_id).order_by(Photo.created_at)).all()
    return [{"id": p.id, "label": p.label, "created_at": p.created_at.isoformat()} for p in rows]


def _is_blank(v: Any):
    return v is None or v == "" or (isinstance(v, list) and not v)


def _violates(field: InspectionTemplateField, value: Any, manual: bool):
    if manual:
        return True
    if field.field_type == "boolean" and field.creates_violation_on_false and value is False:
        return True
    rule = field.violation_rule_json or {}
    op = rule.get("operator") if isinstance(rule, dict) else None
    if not op or _is_blank(value):
        return False
    try:
        if op == "false": return value is False
        if op == "equals": return str(value) == str(rule.get("value"))
        if op == "not_equals": return str(value) != str(rule.get("value"))
        if op == "lt": return float(value) < float(rule.get("value"))
        if op == "gt": return float(value) > float(rule.get("value"))
        if op == "outside":
            n = float(value)
            lo, hi = rule.get("min"), rule.get("max")
            return (lo is not None and n < float(lo)) or (hi is not None and n > float(hi))
    except (TypeError, ValueError):
        return False
    return False


def _result_key(score: float | None):
    if score is None: return "neutral"
    if score >= 90: return "good"
    if score >= 70: return "attention"
    return "poor"


class FormFieldIn(BaseModel):
    label: str
    field_type: str = "boolean"
    required: bool = False
    sort_order: int = 0
    options: list[str] = Field(default_factory=list)
    help_text: str | None = None
    allow_comment: bool = True
    require_photo: bool = False
    default_severity: str = "medium"
    creates_violation_on_false: bool = False
    violation_rule: dict | None = None


class FormIn(BaseModel):
    name: str
    description: str | None = None
    store_ids: list[int] = Field(default_factory=list)  # empty = all stores
    fields: list[FormFieldIn] = Field(default_factory=list)


@router.get("/meta")
def meta(user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_management(user)
    allowed = user_store_ids(db, user)
    q = select(Store).where(Store.active.is_(True)).order_by(Store.name)
    if user.role not in {"operations_director","leader","admin"}: q = q.where(Store.id.in_(allowed or [-1]))
    stores = db.scalars(q).all()
    manager_ids = set(db.scalars(select(Inspection.manager_id).distinct()).all())
    manager_ids.add(user.id)
    managers = []
    for uid in sorted(manager_ids):
        u = db.get(User, uid)
        if u: managers.append({"id":u.id,"name":u.full_name or u.username or str(u.telegram_id)})
    return {"stores":[{"id":s.id,"name":s.name} for s in stores],"managers":managers,"required_per_week":MIN_INSPECTIONS_PER_STORE_PER_WEEK,"severities":["low","medium","high","critical"]}


@router.get("/forms")
def forms(user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_management(user)
    allowed = set(user_store_ids(db, user))
    result = []
    for t in db.scalars(select(InspectionTemplate).where(InspectionTemplate.active.is_(True)).order_by(InspectionTemplate.name)).all():
        store_ids = _template_store_ids(db, t.id)
        if user.role == "manager" and store_ids and not (set(store_ids) & allowed):
            continue
        fs = db.scalars(select(InspectionTemplateField).where(InspectionTemplateField.template_id == t.id).order_by(InspectionTemplateField.sort_order, InspectionTemplateField.id)).all()
        used = db.scalar(select(func.count(Inspection.id)).where(Inspection.template_id == t.id)) or 0
        result.append({
            "id": t.id, "name": t.name, "description": t.description, "store_ids": store_ids,
            "fields": [_field_payload(f) for f in fs], "used_count": int(used),
        })
    return result


@router.post("/forms")
def create_form(payload: FormIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_management(user)
    name = payload.name.strip()
    if not name: raise HTTPException(400, "Укажите название формы")
    if not payload.fields: raise HTTPException(400, "Добавьте хотя бы один пункт")
    allowed = set(user_store_ids(db, user))
    for sid in payload.store_ids:
        if user.role == "manager" and sid not in allowed: raise HTTPException(403, "Нет доступа к магазину")
        if not db.get(Store, sid): raise HTTPException(400, f"Магазин #{sid} не найден")
    t = InspectionTemplate(name=name, description=payload.description, active=True)
    db.add(t)
    try:
        db.flush()
    except IntegrityError:
        db.rollback(); raise HTTPException(400, "Форма с таким названием уже существует")
    for sid in sorted(set(payload.store_ids)):
        db.add(InspectionTemplateStore(template_id=t.id, store_id=sid))
    for i, f in enumerate(payload.fields):
        if f.field_type not in FIELD_TYPES: raise HTTPException(400, f"Неизвестный тип поля: {f.field_type}")
        if f.default_severity not in SEVERITIES: raise HTTPException(400, "Неизвестная критичность")
        db.add(InspectionTemplateField(
            template_id=t.id, key=f"f_{t.id}_{i}_{int(datetime.utcnow().timestamp())}", label=f.label.strip(),
            field_type=f.field_type, required=f.required, sort_order=i, options_json=f.options or None,
            creates_violation_on_false=f.creates_violation_on_false, help_text=f.help_text,
            allow_comment=f.allow_comment, require_photo=f.require_photo,
            default_severity=f.default_severity, violation_rule_json=f.violation_rule,
        ))
    db.commit(); return {"id": t.id}


@router.patch("/forms/{template_id}")
def update_form(template_id: int, payload: FormIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_management(user)
    t = db.get(InspectionTemplate, template_id)
    if not t or not t.active: raise HTTPException(404, "Форма не найдена")
    used = db.scalar(select(func.count(Inspection.id)).where(Inspection.template_id == t.id)) or 0
    # Preserve historical answers: once a form has been used, archive it and create a fresh version.
    if used:
        original_name = t.name
        t.active = False
        t.name = f"{original_name} [архив {t.id}]"
        db.flush()
        clone = FormIn(name=payload.name, description=payload.description, store_ids=payload.store_ids, fields=payload.fields)
        # Inline create to keep one transaction.
        nt = InspectionTemplate(name=clone.name.strip(), description=clone.description, active=True)
        db.add(nt); db.flush()
        for sid in sorted(set(clone.store_ids)): db.add(InspectionTemplateStore(template_id=nt.id, store_id=sid))
        for i, f in enumerate(clone.fields):
            db.add(InspectionTemplateField(template_id=nt.id,key=f"f_{nt.id}_{i}_{int(datetime.utcnow().timestamp())}",label=f.label.strip(),field_type=f.field_type,required=f.required,sort_order=i,options_json=f.options or None,creates_violation_on_false=f.creates_violation_on_false,help_text=f.help_text,allow_comment=f.allow_comment,require_photo=f.require_photo,default_severity=f.default_severity,violation_rule_json=f.violation_rule))
        db.commit(); return {"id": nt.id, "versioned": True}
    t.name = payload.name.strip(); t.description = payload.description; t.updated_at = datetime.utcnow()
    db.execute(delete(InspectionTemplateStore).where(InspectionTemplateStore.template_id == t.id))
    db.execute(delete(InspectionTemplateField).where(InspectionTemplateField.template_id == t.id))
    for sid in sorted(set(payload.store_ids)): db.add(InspectionTemplateStore(template_id=t.id, store_id=sid))
    for i, f in enumerate(payload.fields):
        db.add(InspectionTemplateField(template_id=t.id,key=f"f_{t.id}_{i}_{int(datetime.utcnow().timestamp())}",label=f.label.strip(),field_type=f.field_type,required=f.required,sort_order=i,options_json=f.options or None,creates_violation_on_false=f.creates_violation_on_false,help_text=f.help_text,allow_comment=f.allow_comment,require_photo=f.require_photo,default_severity=f.default_severity,violation_rule_json=f.violation_rule))
    db.commit(); return {"id": t.id, "versioned": False}


@router.delete("/forms/{template_id}")
def disable_form(template_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_management(user)
    t = db.get(InspectionTemplate, template_id)
    if not t: raise HTTPException(404, "Форма не найдена")
    t.active = False; t.updated_at = datetime.utcnow(); db.commit(); return {"ok": True}


@router.get("/templates")
def templates(store_id: int | None = None, user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_management(user)
    if store_id is not None: assert_store_access(db, user, store_id)
    rows = forms(user, db)
    if store_id is not None:
        rows = [x for x in rows if not x["store_ids"] or store_id in x["store_ids"]]
    return rows


class DraftIn(BaseModel):
    store_id: int
    template_id: int


@router.post("/drafts")
def start_draft(payload: DraftIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_management(user); assert_store_access(db, user, payload.store_id)
    t = db.get(InspectionTemplate, payload.template_id)
    if not t or not t.active: raise HTTPException(404, "Форма не найдена")
    if not _template_allowed_for_store(db, t.id, payload.store_id): raise HTTPException(400, "Эта форма не назначена выбранному магазину")
    obj = Inspection(store_id=payload.store_id, template_id=t.id, manager_id=user.id, status="draft", started_at=datetime.utcnow(), completed_at=None)
    db.add(obj); db.flush()
    fields = db.scalars(select(InspectionTemplateField).where(InspectionTemplateField.template_id == t.id).order_by(InspectionTemplateField.sort_order)).all()
    for f in fields: db.add(InspectionValue(inspection_id=obj.id, field_id=f.id))
    db.commit(); return {"id": obj.id}


@router.get("/drafts")
def drafts(user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_management(user)
    q = select(Inspection).where(Inspection.status == "draft").order_by(Inspection.started_at.desc())
    allowed = user_store_ids(db, user)
    if user.role not in {"operations_director", "leader", "admin"}: q = q.where(Inspection.store_id.in_(allowed or [-1]))
    return [{"id": x.id, "store_id": x.store_id, "store_name": _store_name(db,x.store_id), "template_id": x.template_id, "template_name": db.get(InspectionTemplate,x.template_id).name if db.get(InspectionTemplate,x.template_id) else None, "started_at": x.started_at.isoformat() if x.started_at else None} for x in db.scalars(q).all()]


def _detail(db: Session, obj: Inspection):
    t = db.get(InspectionTemplate, obj.template_id)
    fields = {f.id: f for f in db.scalars(select(InspectionTemplateField).where(InspectionTemplateField.template_id == obj.template_id)).all()}
    vals = db.scalars(select(InspectionValue).where(InspectionValue.inspection_id == obj.id).order_by(InspectionValue.id)).all()
    violations = db.scalars(select(Violation).where(Violation.inspection_id == obj.id).order_by(Violation.created_at)).all()
    return {
        "id": obj.id, "store_id": obj.store_id, "store_name": _store_name(db,obj.store_id),
        "template_id": obj.template_id, "template_name": t.name if t else None,
        "manager_id": obj.manager_id, "manager_name": _name_user(db,obj.manager_id), "status": obj.status,
        "score": float(obj.score) if obj.score is not None else None, "result": _result_key(float(obj.score) if obj.score is not None else None),
        "comment": obj.comment, "started_at": obj.started_at.isoformat() if obj.started_at else None,
        "completed_at": obj.completed_at.isoformat() if obj.completed_at else None,
        "photos": _photo_payload(db,"inspection",obj.id),
        "values": [{
            "id": v.id, "field": _field_payload(fields[v.field_id]) if v.field_id in fields else {"id":v.field_id,"label":"Удалённый пункт","field_type":"text"},
            "value": v.value_json, "comment": v.comment, "manual_violation": v.manual_violation,
            "severity": v.severity, "photos": _photo_payload(db,"inspection_value",v.id),
        } for v in vals],
        "violations": [{
            "id": z.id, "field_id": z.field_id, "inspection_value_id": z.inspection_value_id,
            "title": z.title, "comment": z.comment, "severity": z.severity, "status": z.status,
            "task_v2_id": z.task_v2_id, "created_at": z.created_at.isoformat(),
        } for z in violations],
    }


@router.get("/{inspection_id}")
def detail(inspection_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_management(user)
    obj = db.get(Inspection, inspection_id)
    if not obj: raise HTTPException(404, "Проверка не найдена")
    assert_store_access(db, user, obj.store_id)
    return _detail(db, obj)


class FinishValueIn(BaseModel):
    value_id: int
    value: Any = None
    comment: str | None = None
    manual_violation: bool = False
    severity: str | None = None


class FinishIn(BaseModel):
    comment: str | None = None
    values: list[FinishValueIn] = Field(default_factory=list)


@router.post("/{inspection_id}/finish")
def finish(inspection_id: int, payload: FinishIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_management(user)
    obj = db.get(Inspection, inspection_id)
    if not obj or obj.status != "draft": raise HTTPException(404, "Черновик проверки не найден")
    assert_store_access(db, user, obj.store_id)
    if obj.manager_id != user.id and user.role not in {"operations_director","leader","admin"}: raise HTTPException(403,"Завершить проверку может её автор")
    values = {v.id: v for v in db.scalars(select(InspectionValue).where(InspectionValue.inspection_id == obj.id)).all()}
    fields = {f.id: f for f in db.scalars(select(InspectionTemplateField).where(InspectionTemplateField.template_id == obj.template_id)).all()}
    incoming = {x.value_id: x for x in payload.values}
    db.execute(delete(Violation).where(Violation.inspection_id == obj.id))
    scored = []
    for vid, v in values.items():
        f = fields.get(v.field_id)
        if not f: continue
        x = incoming.get(vid)
        if x:
            v.value_json = x.value; v.comment = (x.comment or "").strip() or None
            v.manual_violation = bool(x.manual_violation); v.severity = x.severity or f.default_severity or "medium"
        if f.field_type == "photo":
            has_photo = bool(_photo_payload(db,"inspection_value",v.id))
            if f.required and not has_photo: raise HTTPException(400, f"Добавьте фото: {f.label}")
        elif f.required and _is_blank(v.value_json):
            raise HTTPException(400, f"Заполните обязательное поле: {f.label}")
        if f.require_photo and not _photo_payload(db,"inspection_value",v.id):
            raise HTTPException(400, f"К пункту «{f.label}» требуется фото")
        violated = _violates(f, v.value_json, v.manual_violation)
        evaluable = f.field_type == "boolean" or bool(f.violation_rule_json) or v.manual_violation
        if evaluable and not _is_blank(v.value_json): scored.append(0 if violated else 100)
        if violated:
            sev = v.severity or f.default_severity or "medium"
            if sev not in SEVERITIES: sev = "medium"
            db.add(Violation(store_id=obj.store_id, inspection_id=obj.id, field_id=f.id, inspection_value_id=v.id, title=f.label, comment=v.comment, severity=sev, status="open"))
    obj.comment = (payload.comment or "").strip() or None
    obj.score = sum(scored)/len(scored) if scored else None
    obj.status = "completed"; obj.completed_at = datetime.utcnow()
    db.commit(); return {"id":obj.id,"score":float(obj.score) if obj.score is not None else None}


@router.delete("/{inspection_id}/draft")
def delete_draft(inspection_id:int,user=Depends(get_current_user),db:Session=Depends(get_db)):
    _require_management(user)
    obj=db.get(Inspection,inspection_id)
    if not obj or obj.status!="draft": raise HTTPException(404,"Черновик не найден")
    assert_store_access(db,user,obj.store_id)
    db.delete(obj);db.commit();return {"ok":True}


@router.get("")
def list_all(store_id:int|None=None, manager_id:int|None=None, date_from:str|None=None, date_to:str|None=None, has_violations:bool|None=None, result:str|None=None, user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_management(user)
    q=select(Inspection).where(Inspection.status=="completed").order_by(Inspection.completed_at.desc()).limit(300)
    allowed=user_store_ids(db,user)
    if user.role not in {"operations_director","leader","admin"}: q=q.where(Inspection.store_id.in_(allowed or [-1]))
    if store_id:q=q.where(Inspection.store_id==store_id)
    if manager_id:q=q.where(Inspection.manager_id==manager_id)
    if date_from:
        try:q=q.where(Inspection.completed_at>=datetime.fromisoformat(date_from))
        except ValueError:pass
    if date_to:
        try:q=q.where(Inspection.completed_at<datetime.fromisoformat(date_to)+timedelta(days=1))
        except ValueError:pass
    rows=list(db.scalars(q).all())
    out=[]
    for x in rows:
        vc=db.scalar(select(func.count(Violation.id)).where(Violation.inspection_id==x.id)) or 0
        score=float(x.score) if x.score is not None else None
        rk=_result_key(score)
        if has_violations is True and not vc:continue
        if has_violations is False and vc:continue
        if result and result!=rk:continue
        out.append({"id":x.id,"store_id":x.store_id,"store_name":_store_name(db,x.store_id),"template_id":x.template_id,"template_name":db.get(InspectionTemplate,x.template_id).name if db.get(InspectionTemplate,x.template_id) else None,"manager_id":x.manager_id,"manager_name":_name_user(db,x.manager_id),"score":score,"result":rk,"comment":x.comment,"violation_count":int(vc),"completed_at":x.completed_at.isoformat() if x.completed_at else None})
    return out


@router.get("/control/weekly")
def weekly_control(user=Depends(get_current_user),db:Session=Depends(get_db)):
    _require_management(user)
    monday,sunday,start,end=_week_bounds_utc()
    allowed=user_store_ids(db,user)
    q=select(Store).where(Store.active.is_(True)).order_by(Store.name)
    if user.role not in {"operations_director","leader","admin"}: q=q.where(Store.id.in_(allowed or [-1]))
    stores=db.scalars(q).all();result=[]
    for s in stores:
        count=db.scalar(select(func.count(Inspection.id)).where(Inspection.store_id==s.id,Inspection.status=="completed",Inspection.completed_at>=start,Inspection.completed_at<end)) or 0
        open_v=db.scalar(select(func.count(Violation.id)).where(Violation.store_id==s.id,Violation.status=="open")) or 0
        critical=db.scalar(select(func.count(Violation.id)).where(Violation.store_id==s.id,Violation.status=="open",Violation.severity=="critical")) or 0
        result.append({"store_id":s.id,"store_name":s.name,"count":int(count),"required":MIN_INSPECTIONS_PER_STORE_PER_WEEK,"complete":count>=MIN_INSPECTIONS_PER_STORE_PER_WEEK,"remaining":max(0,MIN_INSPECTIONS_PER_STORE_PER_WEEK-int(count)),"open_violations":int(open_v),"critical_violations":int(critical)})
    return {"week_from":monday.isoformat(),"week_to":sunday.isoformat(),"required":MIN_INSPECTIONS_PER_STORE_PER_WEEK,"stores":result}


@router.get("/violations/list")
def violations(store_id:int|None=None,status:str|None="open",severity:str|None=None,user=Depends(get_current_user),db:Session=Depends(get_db)):
    _require_management(user)
    q=select(Violation).order_by(Violation.created_at.desc()).limit(300)
    allowed=user_store_ids(db,user)
    if user.role not in {"operations_director","leader","admin"}:q=q.where(Violation.store_id.in_(allowed or [-1]))
    if store_id:q=q.where(Violation.store_id==store_id)
    if status:q=q.where(Violation.status==status)
    if severity:q=q.where(Violation.severity==severity)
    return [{"id":x.id,"store_id":x.store_id,"store_name":_store_name(db,x.store_id),"inspection_id":x.inspection_id,"title":x.title,"comment":x.comment,"severity":x.severity,"status":x.status,"task_v2_id":x.task_v2_id,"created_at":x.created_at.isoformat()} for x in db.scalars(q).all()]


@router.patch("/violations/{violation_id}")
def update_violation(violation_id:int,payload:dict,user=Depends(get_current_user),db:Session=Depends(get_db)):
    _require_management(user)
    v=db.get(Violation,violation_id)
    if not v:raise HTTPException(404,"Нарушение не найдено")
    assert_store_access(db,user,v.store_id)
    if "status" in payload:
        if payload["status"] not in {"open","resolved"}:raise HTTPException(400,"Неизвестный статус")
        v.status=payload["status"];v.resolved_at=datetime.utcnow() if v.status=="resolved" else None
    if "severity" in payload:
        if payload["severity"] not in SEVERITIES:raise HTTPException(400,"Неизвестная критичность")
        v.severity=payload["severity"]
    if "comment" in payload:v.comment=payload["comment"]
    db.commit();return {"ok":True}


@router.get("/violations/{violation_id}/task-prefill")
def violation_task_prefill(violation_id:int,user=Depends(get_current_user),db:Session=Depends(get_db)):
    _require_management(user)
    v=db.get(Violation,violation_id)
    if not v:raise HTTPException(404,"Нарушение не найдено")
    assert_store_access(db,user,v.store_id)
    priority={"low":"low","medium":"medium","high":"high","critical":"urgent"}.get(v.severity,"medium")
    return {"violation_id":v.id,"title":f"Устранить нарушение: {v.title}","description":f"Нарушение по результатам проверки #{v.inspection_id}."+(f"\nКомментарий: {v.comment}" if v.comment else ""),"priority":priority,"store_ids":[v.store_id]}


@router.post("/violations/{violation_id}/link-task/{task_id}")
def link_task(violation_id:int,task_id:int,user=Depends(get_current_user),db:Session=Depends(get_db)):
    _require_management(user)
    v=db.get(Violation,violation_id)
    if not v:raise HTTPException(404,"Нарушение не найдено")
    assert_store_access(db,user,v.store_id)
    task=db.get(TaskV2,task_id)
    if not task:raise HTTPException(400,"Задача не найдена")
    v.task_v2_id=task_id

    # Copy inspection evidence into the created task. Telegram file_id is reusable,
    # so no binary file is downloaded or duplicated in Neon.
    source_photos=[]
    if v.inspection_value_id:
        source_photos += list(db.scalars(select(Photo).where(Photo.entity_type=="inspection_value",Photo.entity_id==v.inspection_value_id)).all())
    if v.inspection_id:
        source_photos += list(db.scalars(select(Photo).where(Photo.entity_type=="inspection",Photo.entity_id==v.inspection_id)).all())
    assignees=list(db.scalars(select(TaskAssignee).where(TaskAssignee.task_id==task.id)).all())
    copied=0
    for a in assignees:
        existing=set(db.scalars(select(TaskAttachment.telegram_file_unique_id).where(TaskAttachment.task_id==task.id,TaskAttachment.assignee_id==a.id)).all())
        for p in source_photos:
            if p.telegram_file_unique_id and p.telegram_file_unique_id in existing:
                continue
            db.add(TaskAttachment(
                task_id=task.id,assignee_id=a.id,checklist_item_id=None,uploaded_by=p.uploaded_by,
                kind="photo",telegram_file_id=p.telegram_file_id,telegram_file_unique_id=p.telegram_file_unique_id,
                telegram_chat_id=p.telegram_chat_id,telegram_message_id=p.telegram_message_id,
                file_name=None,mime_type="image/jpeg",file_size=None,label="Фото нарушения из проверки",
            ))
            if p.telegram_file_unique_id: existing.add(p.telegram_file_unique_id)
            copied+=1
    db.commit()
    return {"ok":True,"copied_photos":copied}
