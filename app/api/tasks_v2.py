from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session
from starlette.responses import StreamingResponse
import httpx

from app.core.security import get_current_user, TASK_CREATOR_ROLES
from app.db.database import get_db
from app.services.telegram import get_file_path, file_download_url, send_message
from app.db.models import (
    Employee, EmployeeStore, Store, TaskV2, TaskTarget, TaskAssignee,
    TaskChecklistItem, TaskChecklistProgress, TaskComment, TaskHistory,
    TaskAttachment, TaskAttachmentRequest, User,
)

router = APIRouter(prefix="/api/tasks-v2", tags=["tasks-v2"])

PRIORITIES = {"low", "medium", "high", "urgent"}
STATUSES = {"new", "in_progress", "review", "done", "rejected", "cancelled"}
RECURRENCES = {"none", "daily", "weekly", "monthly"}


def now():
    return datetime.utcnow()


def current_employee(db: Session, user: User) -> Employee | None:
    return db.scalar(select(Employee).where(Employee.user_id == user.id, Employee.active.is_(True)))


def name_user(db: Session, uid: int | None):
    if not uid:
        return None
    u = db.get(User, uid)
    return (u.full_name or u.username or str(u.telegram_id)) if u else None


def employee_name(db: Session, eid: int | None):
    e = db.get(Employee, eid) if eid else None
    return e.full_name if e else None


def store_name(db: Session, sid: int | None):
    s = db.get(Store, sid) if sid else None
    return s.name if s else None


class ChecklistIn(BaseModel):
    text: str
    require_photo: bool = False


class TaskCreateIn(BaseModel):
    title: str
    description: str | None = None
    priority: str = "medium"
    deadline: datetime | None = None
    employee_ids: list[int] = Field(default_factory=list)
    store_ids: list[int] = Field(default_factory=list)
    checklist: list[ChecklistIn] = Field(default_factory=list)
    require_photo: bool = False
    require_file: bool = False
    require_comment: bool = False
    recurrence: str = "none"
    recurrence_until: datetime | None = None


class ReviewIn(BaseModel):
    decision: str  # done/rejected
    comment: str | None = None


class CommentIn(BaseModel):
    text: str
    assignee_id: int | None = None


class AttachmentRequestIn(BaseModel):
    assignee_id: int
    checklist_item_id: int | None = None
    kind: str = "photo"  # photo/file
    label: str | None = None


def add_history(db: Session, task_id: int, user_id: int, action: str, details: dict | None = None, assignee_id: int | None = None):
    db.add(TaskHistory(task_id=task_id, assignee_id=assignee_id, user_id=user_id, action=action, details_json=details or {}))


def expand_store_employees(db: Session, store_ids: list[int]) -> set[int]:
    if not store_ids:
        return set()
    rows = db.scalars(
        select(Employee.id)
        .join(EmployeeStore, EmployeeStore.employee_id == Employee.id)
        .where(EmployeeStore.store_id.in_(store_ids), Employee.active.is_(True), Employee.employment_status.in_(["working", "trainee", "vacation"]))
    ).all()
    return set(rows)


def task_accessible(db: Session, task: TaskV2, user: User) -> bool:
    if user.role in {"operations_director", "leader", "admin"} or task.created_by == user.id:
        return True
    emp = current_employee(db, user)
    if not emp:
        return False
    return db.scalar(select(TaskAssignee.id).where(TaskAssignee.task_id == task.id, TaskAssignee.employee_id == emp.id).limit(1)) is not None


def assignee_payload(db: Session, a: TaskAssignee):
    checklist = []
    items = db.scalars(select(TaskChecklistItem).where(TaskChecklistItem.task_id == a.task_id).order_by(TaskChecklistItem.sort_order, TaskChecklistItem.id)).all()
    progress = {p.checklist_item_id: p for p in db.scalars(select(TaskChecklistProgress).where(TaskChecklistProgress.assignee_id == a.id)).all()}
    for item in items:
        p = progress.get(item.id)
        checklist.append({
            "id": item.id, "text": item.text, "require_photo": item.require_photo,
            "done": bool(p and p.done), "comment": p.comment if p else None,
        })
    attachments = db.scalars(select(TaskAttachment).where(TaskAttachment.assignee_id == a.id).order_by(TaskAttachment.created_at)).all()
    return {
        "id": a.id,
        "employee_id": a.employee_id,
        "employee_name": employee_name(db, a.employee_id),
        "source_store_id": a.source_store_id,
        "source_store_name": store_name(db, a.source_store_id),
        "status": a.status,
        "started_at": a.started_at.isoformat() if a.started_at else None,
        "submitted_at": a.submitted_at.isoformat() if a.submitted_at else None,
        "completed_at": a.completed_at.isoformat() if a.completed_at else None,
        "review_comment": a.review_comment,
        "checklist": checklist,
        "attachments": [{"id": x.id, "kind": x.kind, "file_name": x.file_name, "label": x.label, "created_at": x.created_at.isoformat()} for x in attachments],
    }


def task_payload(db: Session, t: TaskV2, include_detail: bool = False):
    targets = db.scalars(select(TaskTarget).where(TaskTarget.task_id == t.id)).all()
    assignees = db.scalars(select(TaskAssignee).where(TaskAssignee.task_id == t.id).order_by(TaskAssignee.id)).all()
    status_counts: dict[str, int] = {}
    for a in assignees:
        status_counts[a.status] = status_counts.get(a.status, 0) + 1
    payload = {
        "id": t.id,
        "title": t.title,
        "description": t.description,
        "priority": t.priority,
        "deadline": t.deadline.isoformat() if t.deadline else None,
        "require_photo": t.require_photo,
        "require_file": t.require_file,
        "require_comment": t.require_comment,
        "recurrence": t.recurrence,
        "recurrence_until": t.recurrence_until.isoformat() if t.recurrence_until else None,
        "created_by": t.created_by,
        "creator_name": name_user(db, t.created_by),
        "created_at": t.created_at.isoformat(),
        "cancelled": t.cancelled,
        "status_counts": status_counts,
        "assignee_count": len(assignees),
        "targets": [{"type": x.target_type, "employee_id": x.employee_id, "employee_name": employee_name(db,x.employee_id), "store_id": x.store_id, "store_name": store_name(db,x.store_id)} for x in targets],
    }
    if include_detail:
        payload["assignees"] = [assignee_payload(db, a) for a in assignees]
        payload["comments"] = [{
            "id": c.id, "user_id": c.user_id, "user_name": name_user(db,c.user_id), "assignee_id": c.assignee_id,
            "text": c.text, "created_at": c.created_at.isoformat()
        } for c in db.scalars(select(TaskComment).where(TaskComment.task_id == t.id).order_by(TaskComment.created_at)).all()]
        payload["history"] = [{
            "id": h.id, "user_name": name_user(db,h.user_id), "action": h.action, "details": h.details_json or {},
            "assignee_id": h.assignee_id, "created_at": h.created_at.isoformat()
        } for h in db.scalars(select(TaskHistory).where(TaskHistory.task_id == t.id).order_by(TaskHistory.created_at.desc())).all()]
    return payload


@router.get("/meta")
def meta(user=Depends(get_current_user), db: Session = Depends(get_db)):
    employees = db.scalars(select(Employee).where(Employee.active.is_(True), Employee.employment_status != "fired").order_by(Employee.full_name)).all()
    stores = db.scalars(select(Store).where(Store.active.is_(True)).order_by(Store.name)).all()
    return {
        "employees": [{"id": e.id, "name": e.full_name, "position": e.position} for e in employees],
        "stores": [{"id": s.id, "name": s.name} for s in stores],
        "priorities": ["low","medium","high","urgent"],
        "recurrences": ["none","daily","weekly","monthly"],
        "current_employee_id": current_employee(db,user).id if current_employee(db,user) else None,
        "creators": [{"id":u.id,"name":u.full_name or u.username or str(u.telegram_id)} for u in db.scalars(select(User).where(User.active.is_(True)).order_by(User.full_name,User.id)).all()],
    }


@router.post("")
def create_task(payload: TaskCreateIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role not in TASK_CREATOR_ROLES:
        raise HTTPException(403, "Только управляющий, ОД и руководитель могут ставить задачи")
    title = payload.title.strip()
    if not title:
        raise HTTPException(400, "Укажите название задачи")
    if payload.priority not in PRIORITIES:
        raise HTTPException(400, "Неизвестный приоритет")
    if payload.recurrence not in RECURRENCES:
        raise HTTPException(400, "Неизвестная повторяемость")
    employee_ids = set(payload.employee_ids)
    store_ids = set(payload.store_ids)
    employee_ids |= expand_store_employees(db, list(store_ids))
    if not employee_ids:
        raise HTTPException(400, "Выберите хотя бы одного сотрудника или магазин")
    for eid in employee_ids:
        e = db.get(Employee, eid)
        if not e or not e.active or e.employment_status == "fired":
            raise HTTPException(400, f"Сотрудник #{eid} недоступен")
    for sid in store_ids:
        if not db.get(Store, sid):
            raise HTTPException(400, f"Магазин #{sid} не найден")

    t = TaskV2(
        title=title, description=payload.description, priority=payload.priority, deadline=payload.deadline,
        require_photo=payload.require_photo, require_file=payload.require_file, require_comment=payload.require_comment,
        recurrence=payload.recurrence, recurrence_until=payload.recurrence_until, created_by=user.id,
    )
    db.add(t); db.flush()
    for eid in sorted(set(payload.employee_ids)):
        db.add(TaskTarget(task_id=t.id, target_type="employee", employee_id=eid))
    for sid in sorted(store_ids):
        db.add(TaskTarget(task_id=t.id, target_type="store", store_id=sid))
    for eid in sorted(employee_ids):
        source_store = db.scalar(select(EmployeeStore.store_id).where(EmployeeStore.employee_id == eid, EmployeeStore.store_id.in_(store_ids)).limit(1)) if store_ids else None
        db.add(TaskAssignee(task_id=t.id, employee_id=eid, source_store_id=source_store, status="new"))
    for i, item in enumerate(payload.checklist):
        text = item.text.strip()
        if text:
            db.add(TaskChecklistItem(task_id=t.id, text=text, require_photo=item.require_photo, sort_order=i))
    add_history(db, t.id, user.id, "created", {"employees": len(employee_ids), "stores": len(store_ids)})
    db.commit(); db.refresh(t)
    return task_payload(db, t, True)


@router.get("")
def list_tasks(
    scope: str = "my", status: str | None = None, priority: str | None = None,
    store_id: int | None = None, employee_id: int | None = None, creator_id: int | None = None,
    deadline_from: datetime | None = None, deadline_to: datetime | None = None,
    user=Depends(get_current_user), db: Session = Depends(get_db)
):
    emp = current_employee(db, user)
    q = select(TaskV2).where(TaskV2.cancelled.is_(False))
    if scope == "my":
        if not emp:
            return []
        q = q.join(TaskAssignee, TaskAssignee.task_id == TaskV2.id).where(TaskAssignee.employee_id == emp.id)
        if status:
            q = q.where(TaskAssignee.status == status)
    elif scope == "control":
        if user.role in {"operations_director","leader","admin"}:
            pass
        else:
            q = q.where(TaskV2.created_by == user.id)
        if status:
            q = q.join(TaskAssignee, TaskAssignee.task_id == TaskV2.id).where(TaskAssignee.status == status)
    elif scope == "history":
        if user.role not in {"operations_director","leader","admin"}:
            if emp:
                q = q.outerjoin(TaskAssignee, TaskAssignee.task_id == TaskV2.id).where(or_(TaskV2.created_by == user.id, TaskAssignee.employee_id == emp.id))
            else:
                q = q.where(TaskV2.created_by == user.id)
    else:
        raise HTTPException(400, "Неизвестный раздел")
    if priority: q = q.where(TaskV2.priority == priority)
    if creator_id: q = q.where(TaskV2.created_by == creator_id)
    if deadline_from: q = q.where(TaskV2.deadline >= deadline_from)
    if deadline_to: q = q.where(TaskV2.deadline <= deadline_to)
    if employee_id:
        q = q.join(TaskAssignee, TaskAssignee.task_id == TaskV2.id).where(TaskAssignee.employee_id == employee_id)
    if store_id:
        q = q.join(TaskTarget, TaskTarget.task_id == TaskV2.id).where(TaskTarget.store_id == store_id)
    q = q.distinct().order_by(TaskV2.deadline.asc().nullslast(), TaskV2.created_at.desc()).limit(500)
    return [task_payload(db, t, False) for t in db.scalars(q).unique().all()]


@router.patch("/{task_id}")
def edit_task(task_id:int,payload:dict,user=Depends(get_current_user),db:Session=Depends(get_db)):
    t=db.get(TaskV2,task_id)
    if not t: raise HTTPException(404,"Задача не найдена")
    if not (t.created_by==user.id or user.role in {"operations_director","leader","admin"}): raise HTTPException(403,"Нет доступа")
    changed={}
    for key in ("title","description","priority","deadline","require_photo","require_file","require_comment","recurrence","recurrence_until"):
        if key in payload:
            old=getattr(t,key); val=payload[key]
            if key in {"deadline","recurrence_until"} and isinstance(val,str): val=datetime.fromisoformat(val.replace("Z","+00:00")).replace(tzinfo=None) if val else None
            if key=="priority" and val not in PRIORITIES: raise HTTPException(400,"Неизвестный приоритет")
            if key=="recurrence" and val not in RECURRENCES: raise HTTPException(400,"Неизвестная повторяемость")
            setattr(t,key,val); changed[key]={"from":str(old) if old is not None else None,"to":str(val) if val is not None else None}
    if "employee_ids" in payload or "store_ids" in payload:
        explicit=set(int(x) for x in payload.get("employee_ids",[]))
        stores=set(int(x) for x in payload.get("store_ids",[]))
        desired=explicit|expand_store_employees(db,list(stores))
        if not desired: raise HTTPException(400,"Должен остаться хотя бы один исполнитель")
        old_targets=db.scalars(select(TaskTarget).where(TaskTarget.task_id==t.id)).all()
        for x in old_targets: db.delete(x)
        for eid in sorted(explicit): db.add(TaskTarget(task_id=t.id,target_type="employee",employee_id=eid))
        for sid in sorted(stores): db.add(TaskTarget(task_id=t.id,target_type="store",store_id=sid))
        existing={a.employee_id:a for a in db.scalars(select(TaskAssignee).where(TaskAssignee.task_id==t.id)).all()}
        for eid,a in existing.items():
            if eid not in desired and a.status not in {"done"}: a.status="cancelled"
        for eid in desired:
            if eid not in existing:
                source=db.scalar(select(EmployeeStore.store_id).where(EmployeeStore.employee_id==eid,EmployeeStore.store_id.in_(stores)).limit(1)) if stores else None
                db.add(TaskAssignee(task_id=t.id,employee_id=eid,source_store_id=source,status="new"))
            elif existing[eid].status=="cancelled": existing[eid].status="new"
        changed["targets"]={"employees":sorted(explicit),"stores":sorted(stores)}
    if changed: add_history(db,t.id,user.id,"edited",changed)
    db.commit();return task_payload(db,t,True)


@router.get("/{task_id}")
def get_task(task_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    t = db.get(TaskV2, task_id)
    if not t or not task_accessible(db, t, user):
        raise HTTPException(404, "Задача не найдена")
    return task_payload(db, t, True)


@router.patch("/{task_id}/assignees/{assignee_id}/status")
def change_assignee_status(task_id: int, assignee_id: int, payload: dict, user=Depends(get_current_user), db: Session = Depends(get_db)):
    t = db.get(TaskV2, task_id); a = db.get(TaskAssignee, assignee_id)
    if not t or not a or a.task_id != t.id: raise HTTPException(404, "Исполнение задачи не найдено")
    emp = current_employee(db, user)
    is_assignee = bool(emp and a.employee_id == emp.id)
    can_review = t.created_by == user.id or user.role in {"operations_director","leader","admin"}
    new_status = str(payload.get("status", ""))
    if new_status not in STATUSES: raise HTTPException(400, "Неизвестный статус")
    if is_assignee and new_status not in {"in_progress","review"}: raise HTTPException(403, "Исполнитель может только начать задачу или отправить её на проверку")
    if not is_assignee and not can_review: raise HTTPException(403, "Нет доступа")
    if new_status == "review":
        if t.require_photo and not db.scalar(select(TaskAttachment.id).where(TaskAttachment.assignee_id == a.id, TaskAttachment.kind == "photo").limit(1)):
            raise HTTPException(400, "Требуется приложить фото")
        if t.require_file and not db.scalar(select(TaskAttachment.id).where(TaskAttachment.assignee_id == a.id, TaskAttachment.kind == "file").limit(1)):
            raise HTTPException(400, "Требуется приложить файл")
        if t.require_comment and not db.scalar(select(TaskComment.id).where(TaskComment.assignee_id == a.id, TaskComment.user_id == user.id).limit(1)):
            raise HTTPException(400, "Требуется комментарий к выполнению")
        incomplete = db.scalar(select(TaskChecklistItem.id).where(TaskChecklistItem.task_id == t.id).limit(1)) and any(
            not p.done for p in db.scalars(select(TaskChecklistProgress).where(TaskChecklistProgress.assignee_id == a.id)).all()
        )
        # More exact: count total vs completed.
        total = len(db.scalars(select(TaskChecklistItem.id).where(TaskChecklistItem.task_id == t.id)).all())
        completed = len(db.scalars(select(TaskChecklistProgress.id).where(TaskChecklistProgress.assignee_id == a.id, TaskChecklistProgress.done.is_(True))).all())
        if total and completed < total: raise HTTPException(400, "Выполните все пункты чек-листа")
        a.submitted_at = now()
    if new_status == "in_progress" and not a.started_at: a.started_at = now()
    if new_status == "done": a.completed_at = now()
    a.status = new_status
    add_history(db, t.id, user.id, f"status:{new_status}", {}, a.id)
    db.commit(); return {"ok": True}


def maybe_create_next_recurrence(db: Session, t: TaskV2, user_id: int):
    if t.recurrence == "none" or t.cancelled:
        return None
    assignees = db.scalars(select(TaskAssignee).where(TaskAssignee.task_id == t.id)).all()
    if not assignees or any(a.status != "done" for a in assignees):
        return None
    if db.scalar(select(TaskHistory.id).where(TaskHistory.task_id == t.id, TaskHistory.action == "recurrence_created").limit(1)):
        return None
    base = t.deadline or now()
    if t.recurrence == "daily": nxt = base + timedelta(days=1)
    elif t.recurrence == "weekly": nxt = base + timedelta(days=7)
    else:
        # Same day/time in next month, clamped for short months.
        import calendar
        y, m = base.year, base.month + 1
        if m == 13: y, m = y + 1, 1
        d = min(base.day, calendar.monthrange(y, m)[1])
        nxt = base.replace(year=y, month=m, day=d)
    if t.recurrence_until and nxt > t.recurrence_until:
        add_history(db, t.id, user_id, "recurrence_finished", {"next": nxt.isoformat()})
        return None
    nt = TaskV2(title=t.title, description=t.description, priority=t.priority, deadline=nxt, require_photo=t.require_photo, require_file=t.require_file, require_comment=t.require_comment, recurrence=t.recurrence, recurrence_until=t.recurrence_until, created_by=t.created_by)
    db.add(nt); db.flush()
    targets = db.scalars(select(TaskTarget).where(TaskTarget.task_id == t.id)).all()
    for x in targets:
        db.add(TaskTarget(task_id=nt.id,target_type=x.target_type,employee_id=x.employee_id,store_id=x.store_id))
    for a in assignees:
        db.add(TaskAssignee(task_id=nt.id,employee_id=a.employee_id,source_store_id=a.source_store_id,status="new"))
    items = db.scalars(select(TaskChecklistItem).where(TaskChecklistItem.task_id == t.id).order_by(TaskChecklistItem.sort_order)).all()
    for i in items:
        db.add(TaskChecklistItem(task_id=nt.id,text=i.text,require_photo=i.require_photo,sort_order=i.sort_order))
    add_history(db,t.id,user_id,"recurrence_created",{"task_id":nt.id,"deadline":nxt.isoformat()})
    add_history(db,nt.id,user_id,"created_from_recurrence",{"source_task_id":t.id})
    return nt


@router.post("/{task_id}/assignees/{assignee_id}/review")
def review(task_id: int, assignee_id: int, payload: ReviewIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    t = db.get(TaskV2, task_id); a = db.get(TaskAssignee, assignee_id)
    if not t or not a or a.task_id != t.id: raise HTTPException(404, "Исполнение задачи не найдено")
    if not (t.created_by == user.id or user.role in {"operations_director","leader","admin"}): raise HTTPException(403,"Нет доступа")
    if a.status != "review": raise HTTPException(400,"Задача ещё не отправлена на проверку")
    if payload.decision not in {"done","rejected"}: raise HTTPException(400,"Решение должно быть done/rejected")
    if payload.decision == "rejected" and not (payload.comment or "").strip(): raise HTTPException(400,"При возврате укажите замечание")
    a.status = payload.decision
    a.review_comment = (payload.comment or "").strip() or None
    if payload.decision == "done": a.completed_at = now()
    add_history(db,t.id,user.id,"review:"+payload.decision,{"comment":a.review_comment},a.id)
    if a.review_comment:
        db.add(TaskComment(task_id=t.id, assignee_id=a.id, user_id=user.id, text=a.review_comment, kind="review"))
    maybe_create_next_recurrence(db,t,user.id)
    db.commit(); return {"ok":True}


@router.patch("/{task_id}/assignees/{assignee_id}/checklist/{item_id}")
def checklist(task_id:int, assignee_id:int, item_id:int, payload:dict, user=Depends(get_current_user), db:Session=Depends(get_db)):
    t=db.get(TaskV2,task_id); a=db.get(TaskAssignee,assignee_id); item=db.get(TaskChecklistItem,item_id)
    if not t or not a or not item or a.task_id!=t.id or item.task_id!=t.id: raise HTTPException(404,"Пункт не найден")
    emp=current_employee(db,user)
    if not emp or a.employee_id!=emp.id: raise HTTPException(403,"Только исполнитель может отмечать чек-лист")
    p=db.scalar(select(TaskChecklistProgress).where(TaskChecklistProgress.assignee_id==a.id,TaskChecklistProgress.checklist_item_id==item.id))
    if not p:
        p=TaskChecklistProgress(assignee_id=a.id,checklist_item_id=item.id);db.add(p)
    p.done=bool(payload.get("done"));p.comment=payload.get("comment");p.updated_at=now()
    if p.done and item.require_photo and not db.scalar(select(TaskAttachment.id).where(TaskAttachment.assignee_id==a.id,TaskAttachment.checklist_item_id==item.id,TaskAttachment.kind=="photo").limit(1)):
        p.done=False; raise HTTPException(400,"К этому пункту требуется фото")
    add_history(db,t.id,user.id,"checklist",{"item_id":item.id,"done":p.done},a.id)
    db.commit();return {"ok":True}


@router.post("/{task_id}/comments")
def comment(task_id:int,payload:CommentIn,user=Depends(get_current_user),db:Session=Depends(get_db)):
    t=db.get(TaskV2,task_id)
    if not t or not task_accessible(db,t,user):raise HTTPException(404,"Задача не найдена")
    text=payload.text.strip()
    if not text:raise HTTPException(400,"Введите комментарий")
    db.add(TaskComment(task_id=t.id,assignee_id=payload.assignee_id,user_id=user.id,text=text,kind="comment"))
    add_history(db,t.id,user.id,"comment",{},payload.assignee_id)
    db.commit();return {"ok":True}


@router.post("/{task_id}/attachments/request")
def attachment_request(task_id:int,payload:AttachmentRequestIn,user=Depends(get_current_user),db:Session=Depends(get_db)):
    t=db.get(TaskV2,task_id);a=db.get(TaskAssignee,payload.assignee_id)
    if not t or not a or a.task_id!=t.id:raise HTTPException(404,"Задача не найдена")
    emp=current_employee(db,user)
    if not emp or a.employee_id!=emp.id:raise HTTPException(403,"Прикреплять результат может исполнитель")
    if payload.kind not in {"photo","file"}:raise HTTPException(400,"kind: photo/file")
    for old in db.scalars(select(TaskAttachmentRequest).where(TaskAttachmentRequest.user_id==user.id,TaskAttachmentRequest.status=="waiting")).all():old.status="cancelled"
    if payload.checklist_item_id:
        item=db.get(TaskChecklistItem,payload.checklist_item_id)
        if not item or item.task_id!=t.id: raise HTTPException(400,"Пункт чек-листа не найден")
    req=TaskAttachmentRequest(task_id=t.id,assignee_id=a.id,checklist_item_id=payload.checklist_item_id,user_id=user.id,kind=payload.kind,label=payload.label,status="waiting",expires_at=now()+timedelta(minutes=20))
    db.add(req);db.commit();db.refresh(req)
    prompt = "📷 Отправьте фотографию результата задачи." if payload.kind=="photo" else "📎 Отправьте файл результата задачи."
    send_message(user.telegram_id, prompt)
    return {"id":req.id,"kind":req.kind,"status":"waiting"}


@router.get("/{task_id}/attachments/{attachment_id}/content")
def attachment_content(task_id:int,attachment_id:int,user=Depends(get_current_user),db:Session=Depends(get_db)):
    t=db.get(TaskV2,task_id);a=db.get(TaskAttachment,attachment_id)
    if not t or not a or a.task_id!=t.id or not task_accessible(db,t,user): raise HTTPException(404,"Файл не найден")
    path=get_file_path(a.telegram_file_id); url=file_download_url(path)
    client=httpx.Client(timeout=30); response=client.stream("GET",url); response.__enter__()
    if response.status_code!=200:
        response.__exit__(None,None,None);client.close();raise HTTPException(502,"Не удалось получить файл из Telegram")
    def gen():
        try:
            for chunk in response.iter_bytes(): yield chunk
        finally:
            response.__exit__(None,None,None);client.close()
    headers={}
    if a.file_name: headers["Content-Disposition"] = f'inline; filename="{a.file_name}"'
    return StreamingResponse(gen(),media_type=a.mime_type or ("image/jpeg" if a.kind=="photo" else "application/octet-stream"),headers=headers)


@router.post("/{task_id}/cancel")
def cancel(task_id:int,user=Depends(get_current_user),db:Session=Depends(get_db)):
    t=db.get(TaskV2,task_id)
    if not t:raise HTTPException(404,"Задача не найдена")
    if not (t.created_by==user.id or user.role in {"operations_director","leader","admin"}):raise HTTPException(403,"Нет доступа")
    t.cancelled=True
    for a in db.scalars(select(TaskAssignee).where(TaskAssignee.task_id==t.id)).all():a.status="cancelled"
    add_history(db,t.id,user.id,"cancelled",{})
    db.commit();return {"ok":True}
