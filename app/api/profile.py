from __future__ import annotations

from datetime import date, datetime, timedelta

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from starlette.responses import StreamingResponse

from app.core.config import PHOTO_REQUEST_TTL_MINUTES
from app.core.security import get_current_user
from app.db.database import get_db
from app.db.models import (
    Employee, EmployeeStore, KnowledgeAcknowledgement, KnowledgeArticle, Mentorship,
    Photo, PhotoRequest, ShiftReport, Store, TaskAssignee, TaskHistory, TaskV2,
    TrainingAssignment, TrainingAttempt, TrainingTest, User, UserStore,
    WorkAbsence, WorkScheduleChange, WorkShiftAssignment,
)
from app.services.telegram import file_download_url, get_file_path, send_message

router = APIRouter(prefix="/api/profile", tags=["profile"])

POSITION_NAMES = {
    "seller": "Продавец", "mentor": "Наставник", "manager": "Управляющий",
    "operations_director": "Операционный директор", "leader": "Руководитель", "admin": "Администратор",
}
STATUS_NAMES = {"working": "Работает", "trainee": "Стажировка", "vacation": "Отпуск", "fired": "Уволен"}
TASK_STATUS_NAMES = {"new":"Новая","in_progress":"В работе","review":"На проверке","done":"Выполнена","rejected":"Не принята","cancelled":"Отменена"}
TEST_STATUS_NAMES = {"assigned":"Назначен","in_progress":"В процессе","passed":"Сдан","failed":"Не сдан","overdue":"Просрочен"}


class ProfilePatch(BaseModel):
    phone: str | None = None
    email: str | None = None
    primary_store_id: int | None = None
    app_theme: str | None = None


def _employee_for_user(db: Session, user: User) -> Employee:
    employee = db.scalar(select(Employee).where(Employee.user_id == user.id))
    if employee:
        return employee
    employee = db.scalar(select(Employee).where(Employee.telegram_id == user.telegram_id))
    if employee:
        employee.user_id = user.id
        db.commit(); db.refresh(employee)
        return employee
    employee = Employee(
        full_name=user.full_name or user.username or str(user.telegram_id),
        position=user.role if user.role in POSITION_NAMES else "seller",
        employment_status="working" if user.status == "active" and user.active else "trainee",
        telegram_id=user.telegram_id,
        user_id=user.id,
        active=True,
    )
    db.add(employee); db.flush()
    for sid in db.scalars(select(UserStore.store_id).where(UserStore.user_id == user.id)).all():
        db.add(EmployeeStore(employee_id=employee.id, store_id=sid))
    db.commit(); db.refresh(employee)
    return employee


def _stores(db: Session, employee_id: int, primary_store_id: int | None):
    rows = list(db.execute(
        select(Store.id, Store.name)
        .join(EmployeeStore, EmployeeStore.store_id == Store.id)
        .where(EmployeeStore.employee_id == employee_id)
        .order_by(Store.name)
    ).all())
    return [{"id": sid, "name": name, "primary": sid == primary_store_id} for sid, name in rows]


def _tenure(hire_date: date | None):
    if not hire_date:
        return None
    today = date.today()
    months = (today.year - hire_date.year) * 12 + today.month - hire_date.month - (1 if today.day < hire_date.day else 0)
    years, rem = divmod(max(0, months), 12)
    if years and rem:
        return f"{years} г. {rem} мес."
    if years:
        return f"{years} г."
    return f"{rem} мес."


def _avatar_photo(db: Session, employee: Employee):
    return db.scalar(select(Photo).where(Photo.entity_type == "profile_avatar", Photo.entity_id == employee.id).order_by(Photo.created_at.desc()))


def _task_item(db: Session, assignee: TaskAssignee):
    task = db.get(TaskV2, assignee.task_id)
    if not task:
        return None
    store = db.get(Store, assignee.source_store_id) if assignee.source_store_id else None
    now = datetime.utcnow()
    overdue = bool(task.deadline and task.deadline < now and assignee.status not in {"done", "cancelled"})
    return {
        "task_id": task.id, "assignee_id": assignee.id, "title": task.title,
        "priority": task.priority, "status": assignee.status, "status_name": TASK_STATUS_NAMES.get(assignee.status, assignee.status),
        "deadline": task.deadline, "store_id": assignee.source_store_id, "store_name": store.name if store else None,
        "overdue": overdue, "completed_at": assignee.completed_at, "created_at": assignee.created_at,
    }


def _article_visible_for_user(db: Session, user: User, article: KnowledgeArticle) -> bool:
    try:
        from app.api.knowledge import _article_visible
        return _article_visible(db, user, article)
    except Exception:
        return article.status == "published"


@router.get("/me")
def profile_me(user=Depends(get_current_user), db: Session = Depends(get_db)):
    employee = _employee_for_user(db, user)
    db.commit(); db.refresh(employee)
    stores = _stores(db, employee.id, employee.primary_store_id)
    avatar = _avatar_photo(db, employee)
    mentorship = None
    if employee.employment_status == "trainee" and employee.user_id:
        m = db.scalar(select(Mentorship).where(Mentorship.trainee_id == employee.user_id, Mentorship.status == "active").order_by(Mentorship.created_at.desc()))
        if m:
            mentor = db.get(User, m.mentor_id)
            mentorship = {"mentor_name": (mentor.full_name or mentor.username or str(mentor.telegram_id)) if mentor else "Наставник", "stage": m.stage, "start_date": m.start_date}
    return {
        "id": employee.id, "full_name": employee.full_name, "position": employee.position,
        "position_name": POSITION_NAMES.get(employee.position, employee.position),
        "employment_status": employee.employment_status, "status_name": STATUS_NAMES.get(employee.employment_status, employee.employment_status),
        "hire_date": employee.hire_date, "tenure": _tenure(employee.hire_date), "phone": employee.phone,
        "email": employee.email, "telegram_id": employee.telegram_id, "stores": stores,
        "primary_store_id": employee.primary_store_id, "app_theme": employee.app_theme or "light",
        "avatar_photo_id": avatar.id if avatar else None, "mentorship": mentorship,
        "statistics": {"available": False, "source": "saby", "message": "Личная статистика появится после подключения Saby"},
        "achievements": {"available": False, "message": "Достижения добавим в следующем этапе"},
    }


@router.patch("/me")
def update_profile(payload: ProfilePatch, user=Depends(get_current_user), db: Session = Depends(get_db)):
    employee = _employee_for_user(db, user)
    data = payload.model_dump(exclude_unset=True)
    if "phone" in data:
        employee.phone = (data["phone"] or "").strip() or None
    if "email" in data:
        email = (data["email"] or "").strip()
        if email and ("@" not in email or "." not in email.split("@")[-1]):
            raise HTTPException(400, "Проверьте адрес электронной почты")
        employee.email = email or None
    if "app_theme" in data:
        if data["app_theme"] not in {"system", "mint", "ocean", "sand", "lavender", "graphite", "light", "dark"}:
            raise HTTPException(400, "Неизвестная тема оформления")
        employee.app_theme = data["app_theme"]
    if "primary_store_id" in data:
        sid = data["primary_store_id"]
        if sid is not None:
            allowed = db.scalar(select(EmployeeStore).where(EmployeeStore.employee_id == employee.id, EmployeeStore.store_id == sid))
            if not allowed:
                raise HTTPException(400, "Основным можно выбрать только закреплённый магазин")
        employee.primary_store_id = sid
    db.commit()
    return {"ok": True}


@router.post("/avatar-request")
def avatar_request(user=Depends(get_current_user), db: Session = Depends(get_db)):
    employee = _employee_for_user(db, user)
    for old in db.scalars(select(PhotoRequest).where(PhotoRequest.user_id == user.id, PhotoRequest.status == "waiting")).all():
        old.status = "cancelled"
    req = PhotoRequest(user_id=user.id, entity_type="profile_avatar", entity_id=employee.id, label="Фото профиля", status="waiting", expires_at=datetime.utcnow() + timedelta(minutes=PHOTO_REQUEST_TTL_MINUTES))
    db.add(req); db.commit(); db.refresh(req)
    send_message(user.telegram_id, "📷 Отправьте фотографию для личной страницы. Core автоматически установит последнее отправленное фото как аватар.")
    return {"ok": True, "request_id": req.id}


@router.get("/avatar")
def avatar(user=Depends(get_current_user), db: Session = Depends(get_db)):
    employee = _employee_for_user(db, user)
    photo = _avatar_photo(db, employee)
    if not photo:
        raise HTTPException(404, "Фото профиля не загружено")
    path = get_file_path(photo.telegram_file_id)
    url = file_download_url(path)
    client = httpx.Client(timeout=30)
    response = client.stream("GET", url)
    response.__enter__()
    if response.status_code != 200:
        response.__exit__(None, None, None); client.close()
        raise HTTPException(502, "Не удалось получить фото из Telegram")
    ctype = response.headers.get("content-type", "image/jpeg")
    def gen():
        try:
            for chunk in response.iter_bytes():
                yield chunk
        finally:
            response.__exit__(None, None, None); client.close()
    return StreamingResponse(gen(), media_type=ctype, headers={"Cache-Control":"private, max-age=60"})


@router.get("/work")
def profile_work(user=Depends(get_current_user), db: Session = Depends(get_db)):
    employee = _employee_for_user(db, user)
    today = date.today(); past_from = today - timedelta(days=90); future_to = today + timedelta(days=90)
    stores = {s.id: s.name for s in db.scalars(select(Store)).all()}
    shifts = list(db.scalars(select(WorkShiftAssignment).where(
        WorkShiftAssignment.employee_id == employee.id,
        WorkShiftAssignment.work_date.between(past_from, future_to),
    ).order_by(WorkShiftAssignment.work_date)).all())
    shift_rows = [{"id": x.id, "work_date": x.work_date, "shift_type": x.shift_type, "store_id": x.store_id, "store_name": stores.get(x.store_id, str(x.store_id))} for x in shifts]
    absences = list(db.scalars(select(WorkAbsence).where(WorkAbsence.employee_id == employee.id, WorkAbsence.date_from <= future_to, WorkAbsence.date_to >= past_from).order_by(WorkAbsence.date_from)).all())
    assignees = list(db.scalars(select(TaskAssignee).where(TaskAssignee.employee_id == employee.id).order_by(TaskAssignee.created_at.desc()).limit(200)).all())
    tasks = [x for x in (_task_item(db, a) for a in assignees) if x]
    handovers = list(db.scalars(select(ShiftReport).where(ShiftReport.employee_id == employee.id).order_by(ShiftReport.submitted_at.desc()).limit(30)).all())
    handover_rows = []
    for r in handovers:
        handover_rows.append({"id":r.id,"work_date":r.work_date,"shift_kind":r.shift_kind,"status":r.status,"revision":r.revision,"submitted_at":r.submitted_at,"store_name":stores.get(r.store_id,str(r.store_id))})
    return {
        "upcoming_shifts": [x for x in shift_rows if x["work_date"] >= today][:20],
        "shift_history": [x for x in reversed(shift_rows) if x["work_date"] < today][:30],
        "absences": [{"date_from":x.date_from,"date_to":x.date_to,"status":x.status,"comment":x.comment} for x in absences],
        "active_tasks": [x for x in tasks if x["status"] not in {"done","cancelled"}][:30],
        "task_history": [x for x in tasks if x["status"] in {"done","cancelled"}][:30],
        "overdue_tasks": sum(1 for x in tasks if x["overdue"]),
        "handovers": handover_rows,
    }


@router.get("/learning")
def profile_learning(user=Depends(get_current_user), db: Session = Depends(get_db)):
    employee = _employee_for_user(db, user)
    required = []
    for a in db.scalars(select(KnowledgeArticle).where(KnowledgeArticle.status == "published", KnowledgeArticle.required_ack.is_(True)).order_by(KnowledgeArticle.updated_at.desc())).all():
        if not _article_visible_for_user(db, user, a):
            continue
        ack = db.scalar(select(KnowledgeAcknowledgement).where(KnowledgeAcknowledgement.article_id == a.id, KnowledgeAcknowledgement.user_id == user.id, KnowledgeAcknowledgement.revision == a.revision))
        required.append({"id":a.id,"title":a.title,"acknowledged":bool(ack),"acknowledged_at":ack.acknowledged_at if ack else None})
    assignments = list(db.scalars(select(TrainingAssignment).where(TrainingAssignment.employee_id == employee.id).order_by(TrainingAssignment.created_at.desc())).all())
    test_rows = []
    for a in assignments:
        test = db.get(TrainingTest, a.test_id)
        attempts = list(db.scalars(select(TrainingAttempt).where(TrainingAttempt.assignment_id == a.id).order_by(TrainingAttempt.attempt_no.desc())).all())
        test_rows.append({
            "assignment_id":a.id,"test_id":a.test_id,"title":test.title if test else "Тест",
            "status":a.status,"status_name":TEST_STATUS_NAMES.get(a.status,a.status),"due_at":a.due_at,
            "pass_percent":test.pass_percent if test else None,"attempts_allowed":test.attempts_allowed if test else 0,
            "attempts":[{"id":x.id,"attempt_no":x.attempt_no,"status":x.status,"score_percent":float(x.score_percent) if x.score_percent is not None else None,"passed":x.passed,"started_at":x.started_at,"completed_at":x.completed_at} for x in attempts],
        })
    return {
        "knowledge": {"total":len(required),"completed":sum(1 for x in required if x["acknowledged"]),"pending":sum(1 for x in required if not x["acknowledged"]),"items":required},
        "tests": {"total":len(test_rows),"passed":sum(1 for x in test_rows if x["status"]=="passed"),"in_progress":sum(1 for x in test_rows if x["status"] in {"assigned","in_progress"}),"failed":sum(1 for x in test_rows if x["status"] in {"failed","overdue"}),"items":test_rows},
    }


@router.get("/activity")
def profile_activity(limit: int = 100, user=Depends(get_current_user), db: Session = Depends(get_db)):
    employee = _employee_for_user(db, user)
    events = []
    for a in db.scalars(select(KnowledgeAcknowledgement).where(KnowledgeAcknowledgement.user_id == user.id).order_by(KnowledgeAcknowledgement.acknowledged_at.desc()).limit(limit)).all():
        article = db.get(KnowledgeArticle, a.article_id)
        events.append({"at":a.acknowledged_at,"kind":"knowledge","title":f"Ознакомился(ась) со статьёй «{article.title if article else 'Материал'}»"})
    for a in db.scalars(select(TrainingAttempt).where(TrainingAttempt.employee_id == employee.id, TrainingAttempt.completed_at.is_not(None)).order_by(TrainingAttempt.completed_at.desc()).limit(limit)).all():
        test = db.get(TrainingTest, a.test_id)
        score = f" — {float(a.score_percent):.0f}%" if a.score_percent is not None else ""
        events.append({"at":a.completed_at,"kind":"test","title":f"Прошёл(а) тест «{test.title if test else 'Тест'}»{score}"})
    for r in db.scalars(select(ShiftReport).where(ShiftReport.employee_id == employee.id).order_by(ShiftReport.submitted_at.desc()).limit(limit)).all():
        events.append({"at":r.submitted_at,"kind":"handover","title":f"Отправил(а) пересменку · {r.status}"})
    for h in db.scalars(select(TaskHistory).where(TaskHistory.user_id == user.id).order_by(TaskHistory.created_at.desc()).limit(limit)).all():
        task = db.get(TaskV2, h.task_id)
        events.append({"at":h.created_at,"kind":"task","title":f"Задача «{task.title if task else 'Задача'}»: {h.action}"})
    for c in db.scalars(select(WorkScheduleChange).where(or_(WorkScheduleChange.employee_id == employee.id, WorkScheduleChange.changed_by == user.id)).order_by(WorkScheduleChange.created_at.desc()).limit(limit)).all():
        events.append({"at":c.created_at,"kind":"schedule","title":f"Изменение графика: {c.action}"})
    events = [x for x in events if x["at"]]
    events.sort(key=lambda x: x["at"], reverse=True)
    return events[:max(1, min(limit, 200))]
