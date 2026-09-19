from __future__ import annotations

import random
import re
from datetime import datetime, timedelta
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.security import MANAGEMENT_ROLES, get_current_user
from app.db.database import get_db
from app.db.models import (
    Employee,
    EmployeeStore,
    KnowledgeArticle,
    Store,
    TrainingAssignment,
    TrainingAttempt,
    TrainingAttemptAnswer,
    TrainingQuestion,
    TrainingQuestionMediaRequest,
    TrainingQuestionOption,
    TrainingTest,
    User,
)
from app.services.telegram import file_download_url, get_file_path

router = APIRouter(prefix="/api/testing", tags=["testing"])
EDITOR_ROLES = set(MANAGEMENT_ROLES)
TEST_STATUSES = {"draft", "published", "archived"}
QUESTION_TYPES = {"single", "multiple", "text"}


def _require_editor(user: User):
    if user.role not in EDITOR_ROLES:
        raise HTTPException(403, "Создавать и управлять тестами могут управляющий, ОД, руководитель и администратор")


def _current_employee(db: Session, user: User) -> Employee | None:
    return db.scalar(select(Employee).where(Employee.user_id == user.id, Employee.active.is_(True)))


def _norm_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).casefold()


def _option_dict(o: TrainingQuestionOption, include_key: bool = False) -> dict:
    d = {"id": o.id, "text": o.text, "sort_order": o.sort_order}
    if include_key:
        d["is_correct"] = bool(o.is_correct)
    return d


def _question_dict(db: Session, q: TrainingQuestion, include_key: bool = False) -> dict:
    opts = list(db.scalars(select(TrainingQuestionOption).where(TrainingQuestionOption.question_id == q.id).order_by(TrainingQuestionOption.sort_order, TrainingQuestionOption.id)).all())
    d = {
        "id": q.id,
        "test_id": q.test_id,
        "text": q.text,
        "question_type": q.question_type,
        "sort_order": q.sort_order,
        "active": q.active,
        "has_image": bool(q.image_telegram_file_id),
        "options": [_option_dict(x, include_key) for x in opts],
    }
    if include_key:
        d["accepted_text"] = q.accepted_text_json or []
        d["explanation_internal"] = q.explanation_internal
    return d


def _test_dict(db: Session, test: TrainingTest, *, include_questions: bool = False, include_keys: bool = False) -> dict:
    article = db.get(KnowledgeArticle, test.linked_article_id) if test.linked_article_id else None
    q_count = db.scalar(select(func.count(TrainingQuestion.id)).where(TrainingQuestion.test_id == test.id, TrainingQuestion.active.is_(True))) or 0
    d = {
        "id": test.id,
        "title": test.title,
        "description": test.description,
        "status": test.status,
        "linked_article_id": test.linked_article_id,
        "linked_article_title": article.title if article else None,
        "auto_assign_on_ack": bool(test.auto_assign_on_ack),
        "auto_due_days": test.auto_due_days,
        "pass_percent": test.pass_percent,
        "time_limit_minutes": test.time_limit_minutes,
        "attempts_allowed": test.attempts_allowed,
        "questions_to_draw": test.questions_to_draw,
        "shuffle_questions": bool(test.shuffle_questions),
        "shuffle_options": bool(test.shuffle_options),
        "question_count": int(q_count),
        "created_by": test.created_by,
        "created_at": test.created_at,
        "updated_at": test.updated_at,
    }
    if include_questions:
        qs = list(db.scalars(select(TrainingQuestion).where(TrainingQuestion.test_id == test.id).order_by(TrainingQuestion.sort_order, TrainingQuestion.id)).all())
        d["questions"] = [_question_dict(db, q, include_keys) for q in qs]
    return d


def _attempt_count(db: Session, assignment_id: int) -> int:
    return int(db.scalar(select(func.count(TrainingAttempt.id)).where(TrainingAttempt.assignment_id == assignment_id)) or 0)


def _active_attempt_for_employee(db: Session, employee_id: int) -> TrainingAttempt | None:
    a = db.scalar(select(TrainingAttempt).where(TrainingAttempt.employee_id == employee_id, TrainingAttempt.status == "active").order_by(TrainingAttempt.started_at.desc()))
    if a and a.expires_at <= datetime.utcnow():
        _finalize_attempt(db, a, timed_out=True)
        db.flush()
        return None
    return a


def _refresh_assignment(db: Session, assignment: TrainingAssignment):
    if assignment.status == "passed":
        return
    active = db.scalar(select(TrainingAttempt).where(TrainingAttempt.assignment_id == assignment.id, TrainingAttempt.status == "active").order_by(TrainingAttempt.started_at.desc()))
    if active:
        if active.expires_at <= datetime.utcnow():
            _finalize_attempt(db, active, timed_out=True)
        else:
            assignment.status = "in_progress"
            return
    if assignment.due_at and assignment.due_at <= datetime.utcnow() and assignment.status not in {"passed", "failed"}:
        assignment.status = "overdue"


def _grade_answer(db: Session, question: TrainingQuestion, answer: TrainingAttemptAnswer | None) -> bool:
    if not answer:
        return False
    if question.question_type == "text":
        accepted = {_norm_text(x) for x in (question.accepted_text_json or []) if _norm_text(x)}
        return bool(accepted) and _norm_text(answer.text_answer) in accepted
    correct = set(db.scalars(select(TrainingQuestionOption.id).where(TrainingQuestionOption.question_id == question.id, TrainingQuestionOption.is_correct.is_(True))).all())
    selected = {int(x) for x in (answer.selected_option_ids_json or [])}
    if question.question_type == "single":
        return len(selected) == 1 and selected == correct
    return selected == correct and bool(correct)


def _finalize_attempt(db: Session, attempt: TrainingAttempt, *, timed_out: bool = False) -> TrainingAttempt:
    if attempt.status != "active":
        return attempt
    question_ids = [int(x) for x in (attempt.question_order_json or [])]
    questions = {q.id: q for q in db.scalars(select(TrainingQuestion).where(TrainingQuestion.id.in_(question_ids or [-1]))).all()}
    answers = {a.question_id: a for a in db.scalars(select(TrainingAttemptAnswer).where(TrainingAttemptAnswer.attempt_id == attempt.id)).all()}
    correct = 0
    for qid in question_ids:
        q = questions.get(qid)
        if not q:
            continue
        ans = answers.get(qid)
        ok = _grade_answer(db, q, ans)
        if ans:
            ans.is_correct = ok
        if ok:
            correct += 1
    total = len(question_ids)
    score = round((correct / total * 100.0), 2) if total else 0.0
    test = db.get(TrainingTest, attempt.test_id)
    passed = bool(test and score >= test.pass_percent)
    attempt.status = "timeout" if timed_out else "submitted"
    attempt.completed_at = datetime.utcnow()
    attempt.score_percent = score
    attempt.passed = passed
    assignment = db.get(TrainingAssignment, attempt.assignment_id)
    if assignment:
        assignment.status = "passed" if passed else "failed"
        assignment.retry_unlocked = False
        assignment.updated_at = datetime.utcnow()
    return attempt


def _attempt_payload(db: Session, attempt: TrainingAttempt, include_answers: bool = True) -> dict:
    if attempt.status == "active" and attempt.expires_at <= datetime.utcnow():
        _finalize_attempt(db, attempt, timed_out=True)
        db.commit()
    test = db.get(TrainingTest, attempt.test_id)
    assignment = db.get(TrainingAssignment, attempt.assignment_id)
    qids = [int(x) for x in (attempt.question_order_json or [])]
    qmap = {q.id: q for q in db.scalars(select(TrainingQuestion).where(TrainingQuestion.id.in_(qids or [-1]))).all()}
    answers = {a.question_id: a for a in db.scalars(select(TrainingAttemptAnswer).where(TrainingAttemptAnswer.attempt_id == attempt.id)).all()} if include_answers else {}
    option_order = attempt.option_order_json or {}
    questions = []
    for qid in qids:
        q = qmap.get(qid)
        if not q:
            continue
        opts = list(db.scalars(select(TrainingQuestionOption).where(TrainingQuestionOption.question_id == q.id)).all())
        by_id = {x.id: x for x in opts}
        ordered_ids = [int(x) for x in option_order.get(str(qid), [])]
        ordered = [by_id[x] for x in ordered_ids if x in by_id] or sorted(opts, key=lambda x: (x.sort_order, x.id))
        ans = answers.get(qid)
        questions.append({
            "id": q.id,
            "text": q.text,
            "question_type": q.question_type,
            "has_image": bool(q.image_telegram_file_id),
            "options": [_option_dict(x, False) for x in ordered],
            "answer": ({"selected_option_ids": ans.selected_option_ids_json or [], "text_answer": ans.text_answer or ""} if ans else {"selected_option_ids": [], "text_answer": ""}) if include_answers else None,
        })
    return {
        "id": attempt.id,
        "assignment_id": attempt.assignment_id,
        "test_id": attempt.test_id,
        "test_title": test.title if test else "Тест",
        "attempt_no": attempt.attempt_no,
        "status": attempt.status,
        "started_at": attempt.started_at,
        "expires_at": attempt.expires_at,
        "completed_at": attempt.completed_at,
        "score_percent": float(attempt.score_percent) if attempt.score_percent is not None else None,
        "passed": attempt.passed,
        "pass_percent": test.pass_percent if test else None,
        "remaining_seconds": max(0, int((attempt.expires_at - datetime.utcnow()).total_seconds())) if attempt.status == "active" else 0,
        "due_at": assignment.due_at if assignment else None,
        "questions": questions,
    }


def _assignment_dict(db: Session, a: TrainingAssignment) -> dict:
    _refresh_assignment(db, a)
    test = db.get(TrainingTest, a.test_id)
    employee = db.get(Employee, a.employee_id)
    count = _attempt_count(db, a.id)
    last = db.scalar(select(TrainingAttempt).where(TrainingAttempt.assignment_id == a.id).order_by(TrainingAttempt.attempt_no.desc()))
    return {
        "id": a.id,
        "test_id": a.test_id,
        "test_title": test.title if test else "Тест удалён",
        "employee_id": a.employee_id,
        "employee_name": employee.full_name if employee else "Сотрудник",
        "employee_position": employee.position if employee else None,
        "source": a.source,
        "status": a.status,
        "due_at": a.due_at,
        "retry_unlocked": bool(a.retry_unlocked),
        "attempts_used": count,
        "attempts_allowed": test.attempts_allowed if test else 0,
        "pass_percent": test.pass_percent if test else None,
        "time_limit_minutes": test.time_limit_minutes if test else None,
        "linked_article_id": test.linked_article_id if test else None,
        "last_score": float(last.score_percent) if last and last.score_percent is not None else None,
        "last_passed": last.passed if last else None,
        "created_at": a.created_at,
    }


class TestIn(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    description: str | None = None
    status: str = "draft"
    linked_article_id: int | None = None
    auto_assign_on_ack: bool = False
    auto_due_days: int | None = Field(default=None, ge=1, le=365)
    pass_percent: int = Field(default=80, ge=1, le=100)
    time_limit_minutes: int = Field(default=20, ge=1, le=300)
    attempts_allowed: int = Field(default=1, ge=1, le=20)
    questions_to_draw: int | None = Field(default=None, ge=1, le=200)
    shuffle_questions: bool = True
    shuffle_options: bool = True


class OptionIn(BaseModel):
    id: int | None = None
    text: str = Field(min_length=1)
    is_correct: bool = False


class QuestionIn(BaseModel):
    text: str = Field(min_length=1)
    question_type: Literal["single", "multiple", "text"]
    options: list[OptionIn] = []
    accepted_text: list[str] = []
    explanation_internal: str | None = None
    active: bool = True
    sort_order: int = 0


class AssignmentIn(BaseModel):
    employee_ids: list[int] = []
    roles: list[str] = []
    store_ids: list[int] = []
    due_at: datetime | None = None


class AnswerIn(BaseModel):
    selected_option_ids: list[int] = []
    text_answer: str | None = None


@router.get("/meta")
def meta(user=Depends(get_current_user), db: Session = Depends(get_db)):
    employees = list(db.scalars(select(Employee).where(Employee.active.is_(True)).order_by(Employee.full_name)).all())
    stores = list(db.scalars(select(Store).where(Store.active.is_(True)).order_by(Store.name)).all())
    articles = list(db.scalars(select(KnowledgeArticle).where(KnowledgeArticle.status == "published").order_by(KnowledgeArticle.title)).all())
    return {
        "can_edit": user.role in EDITOR_ROLES,
        "employees": [{"id": e.id, "full_name": e.full_name, "position": e.position} for e in employees],
        "stores": [{"id": s.id, "name": s.name} for s in stores],
        "roles": ["seller", "mentor", "manager", "operations_director", "leader", "admin"],
        "articles": [{"id": a.id, "title": a.title, "required_ack": a.required_ack} for a in articles],
    }


@router.get("/active-attempt")
def active_attempt(user=Depends(get_current_user), db: Session = Depends(get_db)):
    employee = _current_employee(db, user)
    if not employee:
        return None
    attempt = _active_attempt_for_employee(db, employee.id)
    if not attempt:
        db.commit()
        return None
    return _attempt_payload(db, attempt)


@router.get("/my")
def my_tests(user=Depends(get_current_user), db: Session = Depends(get_db)):
    employee = _current_employee(db, user)
    if not employee:
        return []
    rows = list(db.scalars(select(TrainingAssignment).where(TrainingAssignment.employee_id == employee.id).order_by(TrainingAssignment.created_at.desc())).all())
    result = [_assignment_dict(db, x) for x in rows]
    db.commit()
    return result


@router.get("/catalog")
def catalog(user=Depends(get_current_user), db: Session = Depends(get_db)):
    rows = list(db.scalars(select(TrainingTest).where(TrainingTest.status == "published").order_by(TrainingTest.updated_at.desc())).all())
    employee = _current_employee(db, user)
    assigned = {}
    if employee:
        for a in db.scalars(select(TrainingAssignment).where(TrainingAssignment.employee_id == employee.id)).all():
            assigned[a.test_id] = a.id
    return [{**_test_dict(db, t), "assignment_id": assigned.get(t.id)} for t in rows]


@router.get("/tests")
def tests(user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_editor(user)
    return [_test_dict(db, x) for x in db.scalars(select(TrainingTest).order_by(TrainingTest.updated_at.desc())).all()]


@router.post("/tests")
def create_test(payload: TestIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_editor(user)
    if payload.status not in TEST_STATUSES:
        raise HTTPException(400, "Некорректный статус")
    if payload.linked_article_id and not db.get(KnowledgeArticle, payload.linked_article_id):
        raise HTTPException(404, "Статья не найдена")
    test = TrainingTest(created_by=user.id, **payload.model_dump())
    db.add(test); db.flush()
    from app.services.testing import backfill_test_for_acknowledged
    backfill_test_for_acknowledged(db, test)
    db.commit(); db.refresh(test)
    return _test_dict(db, test, include_questions=True, include_keys=True)


@router.get("/tests/{test_id}")
def get_test(test_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    test = db.get(TrainingTest, test_id)
    if not test:
        raise HTTPException(404, "Тест не найден")
    if user.role not in EDITOR_ROLES and test.status != "published":
        raise HTTPException(404, "Тест недоступен")
    return _test_dict(db, test, include_questions=user.role in EDITOR_ROLES, include_keys=user.role in EDITOR_ROLES)


@router.patch("/tests/{test_id}")
def update_test(test_id: int, payload: TestIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_editor(user)
    test = db.get(TrainingTest, test_id)
    if not test:
        raise HTTPException(404, "Тест не найден")
    if payload.status not in TEST_STATUSES:
        raise HTTPException(400, "Некорректный статус")
    for k, v in payload.model_dump().items():
        setattr(test, k, v)
    db.flush()
    from app.services.testing import backfill_test_for_acknowledged
    backfill_test_for_acknowledged(db, test)
    db.commit(); db.refresh(test)
    return _test_dict(db, test, include_questions=True, include_keys=True)


@router.delete("/tests/{test_id}")
def archive_test(test_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_editor(user)
    test = db.get(TrainingTest, test_id)
    if not test:
        raise HTTPException(404, "Тест не найден")
    test.status = "archived"; db.commit()
    return {"ok": True}


@router.post("/tests/{test_id}/questions")
def add_question(test_id: int, payload: QuestionIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_editor(user)
    test = db.get(TrainingTest, test_id)
    if not test:
        raise HTTPException(404, "Тест не найден")
    _validate_question(payload)
    q = TrainingQuestion(test_id=test_id, text=payload.text, question_type=payload.question_type, accepted_text_json=payload.accepted_text or None, explanation_internal=payload.explanation_internal, active=payload.active, sort_order=payload.sort_order)
    db.add(q); db.flush()
    for i, o in enumerate(payload.options):
        db.add(TrainingQuestionOption(question_id=q.id, text=o.text, is_correct=o.is_correct, sort_order=i))
    db.commit(); db.refresh(q)
    return _question_dict(db, q, True)


def _validate_question(payload: QuestionIn):
    if payload.question_type == "text":
        if not [x for x in payload.accepted_text if _norm_text(x)]:
            raise HTTPException(400, "Для текстового вопроса укажите хотя бы один принимаемый ответ")
        return
    if len(payload.options) < 2:
        raise HTTPException(400, "Добавьте минимум два варианта ответа")
    correct = sum(1 for x in payload.options if x.is_correct)
    if payload.question_type == "single" and correct != 1:
        raise HTTPException(400, "Для вопроса с одним ответом отметьте ровно один правильный вариант")
    if payload.question_type == "multiple" and correct < 1:
        raise HTTPException(400, "Отметьте хотя бы один правильный вариант")


@router.patch("/questions/{question_id}")
def update_question(question_id: int, payload: QuestionIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_editor(user)
    q = db.get(TrainingQuestion, question_id)
    if not q:
        raise HTTPException(404, "Вопрос не найден")
    _validate_question(payload)
    q.text = payload.text; q.question_type = payload.question_type; q.accepted_text_json = payload.accepted_text or None; q.explanation_internal = payload.explanation_internal; q.active = payload.active; q.sort_order = payload.sort_order
    for old in db.scalars(select(TrainingQuestionOption).where(TrainingQuestionOption.question_id == q.id)).all():
        db.delete(old)
    db.flush()
    for i, o in enumerate(payload.options):
        db.add(TrainingQuestionOption(question_id=q.id, text=o.text, is_correct=o.is_correct, sort_order=i))
    db.commit(); db.refresh(q)
    return _question_dict(db, q, True)


@router.delete("/questions/{question_id}")
def delete_question(question_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_editor(user)
    q = db.get(TrainingQuestion, question_id)
    if not q:
        raise HTTPException(404, "Вопрос не найден")
    used = db.scalar(select(TrainingAttemptAnswer.id).where(TrainingAttemptAnswer.question_id == q.id).limit(1))
    if used:
        q.active = False
    else:
        db.delete(q)
    db.commit()
    return {"ok": True}


@router.post("/questions/{question_id}/photo-request")
def question_photo_request(question_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_editor(user)
    q = db.get(TrainingQuestion, question_id)
    if not q:
        raise HTTPException(404, "Вопрос не найден")
    req = TrainingQuestionMediaRequest(question_id=q.id, user_id=user.id, expires_at=datetime.utcnow() + timedelta(minutes=30))
    db.add(req); db.commit(); db.refresh(req)
    return {"ok": True, "request_id": req.id, "message": "Отправьте фото в чат с ботом"}


@router.delete("/questions/{question_id}/photo")
def delete_question_photo(question_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_editor(user)
    q = db.get(TrainingQuestion, question_id)
    if not q:
        raise HTTPException(404, "Вопрос не найден")
    q.image_telegram_file_id = None; q.image_telegram_file_unique_id = None; q.image_mime_type = None
    db.commit(); return {"ok": True}


@router.get("/questions/{question_id}/image")
def question_image(question_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    q = db.get(TrainingQuestion, question_id)
    if not q or not q.image_telegram_file_id:
        raise HTTPException(404, "Изображение не найдено")
    if user.role not in EDITOR_ROLES:
        employee = _current_employee(db, user)
        active = _active_attempt_for_employee(db, employee.id) if employee else None
        if not active or question_id not in {int(x) for x in (active.question_order_json or [])}:
            raise HTTPException(403, "Изображение доступно только в активном тесте")
    try:
        path = get_file_path(q.image_telegram_file_id)
        url = file_download_url(path)
        with httpx.Client(timeout=60) as client:
            r = client.get(url); r.raise_for_status()
            return Response(content=r.content, media_type=q.image_mime_type or "image/jpeg", headers={"Cache-Control": "private, max-age=300"})
    except Exception as e:
        raise HTTPException(502, f"Не удалось получить фото из Telegram: {e}")


@router.post("/tests/{test_id}/assign")
def assign_test(test_id: int, payload: AssignmentIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_editor(user)
    test = db.get(TrainingTest, test_id)
    if not test:
        raise HTTPException(404, "Тест не найден")
    ids = set(payload.employee_ids)
    if payload.roles:
        ids.update(db.scalars(select(Employee.id).where(Employee.active.is_(True), Employee.position.in_(payload.roles))).all())
    if payload.store_ids:
        ids.update(db.scalars(select(EmployeeStore.employee_id).join(Employee, Employee.id == EmployeeStore.employee_id).where(EmployeeStore.store_id.in_(payload.store_ids), Employee.active.is_(True))).all())
    if not ids:
        raise HTTPException(400, "Выберите сотрудников, роль или магазины")
    created = 0; updated = 0
    for eid in ids:
        e = db.get(Employee, int(eid))
        if not e or not e.active:
            continue
        a = db.scalar(select(TrainingAssignment).where(TrainingAssignment.test_id == test.id, TrainingAssignment.employee_id == e.id))
        if a:
            a.due_at = payload.due_at
            if a.status in {"overdue"} and (not payload.due_at or payload.due_at > datetime.utcnow()):
                a.status = "assigned"
            updated += 1
        else:
            db.add(TrainingAssignment(test_id=test.id, employee_id=e.id, assigned_by=user.id, source="manual", status="assigned", due_at=payload.due_at, retry_unlocked=True)); created += 1
    db.commit()
    return {"ok": True, "created": created, "updated": updated, "total": created + updated}


@router.get("/control")
def control(test_id: int | None = None, status: str | None = None, employee_id: int | None = None, store_id: int | None = None, role: str | None = None, user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_editor(user)
    q = select(TrainingAssignment).order_by(TrainingAssignment.updated_at.desc())
    if test_id: q = q.where(TrainingAssignment.test_id == test_id)
    if employee_id: q = q.where(TrainingAssignment.employee_id == employee_id)
    if role: q = q.join(Employee, Employee.id == TrainingAssignment.employee_id).where(Employee.position == role)
    if store_id: q = q.join(EmployeeStore, EmployeeStore.employee_id == TrainingAssignment.employee_id).where(EmployeeStore.store_id == store_id)
    rows = list(db.scalars(q).unique().all())
    result = []
    for a in rows:
        item = _assignment_dict(db, a)
        if status and item["status"] != status:
            continue
        result.append(item)
    db.commit()
    return result


@router.post("/assignments/{assignment_id}/allow-retry")
def allow_retry(assignment_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_editor(user)
    a = db.get(TrainingAssignment, assignment_id)
    if not a:
        raise HTTPException(404, "Назначение не найдено")
    test = db.get(TrainingTest, a.test_id)
    used = _attempt_count(db, a.id)
    if not test or used >= test.attempts_allowed:
        raise HTTPException(400, "Лимит попыток исчерпан")
    if a.status not in {"failed", "overdue"}:
        raise HTTPException(400, "Разрешение повторной попытки сейчас не требуется")
    if a.due_at and a.due_at <= datetime.utcnow():
        raise HTTPException(400, "Срок теста истёк. Сначала измените срок назначения")
    a.retry_unlocked = True
    a.status = "assigned"
    db.commit()
    return {"ok": True}


@router.post("/assignments/{assignment_id}/start")
def start_attempt(assignment_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    employee = _current_employee(db, user)
    if not employee:
        raise HTTPException(400, "Telegram-аккаунт не связан с карточкой сотрудника")
    a = db.get(TrainingAssignment, assignment_id)
    if not a or a.employee_id != employee.id:
        raise HTTPException(404, "Тест не назначен")
    active = _active_attempt_for_employee(db, employee.id)
    if active:
        if active.assignment_id == a.id:
            return _attempt_payload(db, active)
        raise HTTPException(409, "Сначала завершите уже начатый тест")
    test = db.get(TrainingTest, a.test_id)
    if not test or test.status != "published":
        raise HTTPException(400, "Тест недоступен")
    _refresh_assignment(db, a)
    if a.due_at and a.due_at <= datetime.utcnow():
        a.status = "overdue"; db.commit(); raise HTTPException(400, "Срок прохождения теста истёк")
    used = _attempt_count(db, a.id)
    if used >= test.attempts_allowed:
        raise HTTPException(400, "Лимит попыток исчерпан")
    if used > 0 and not a.retry_unlocked:
        raise HTTPException(403, "Повторная попытка доступна только после разрешения руководителя")
    qs = list(db.scalars(select(TrainingQuestion).where(TrainingQuestion.test_id == test.id, TrainingQuestion.active.is_(True)).order_by(TrainingQuestion.sort_order, TrainingQuestion.id)).all())
    if not qs:
        raise HTTPException(400, "В тесте нет вопросов")
    draw = min(test.questions_to_draw or len(qs), len(qs))
    chosen = random.sample(qs, draw) if draw < len(qs) else list(qs)
    if test.shuffle_questions:
        random.shuffle(chosen)
    question_ids = [q.id for q in chosen]
    option_order: dict[str, list[int]] = {}
    for q in chosen:
        ids = list(db.scalars(select(TrainingQuestionOption.id).where(TrainingQuestionOption.question_id == q.id).order_by(TrainingQuestionOption.sort_order, TrainingQuestionOption.id)).all())
        if test.shuffle_options:
            random.shuffle(ids)
        option_order[str(q.id)] = ids
    now = datetime.utcnow()
    expires = now + timedelta(minutes=test.time_limit_minutes)
    if a.due_at and a.due_at < expires:
        expires = a.due_at
    attempt = TrainingAttempt(assignment_id=a.id, test_id=test.id, employee_id=employee.id, attempt_no=used + 1, status="active", started_at=now, expires_at=expires, question_order_json=question_ids, option_order_json=option_order)
    db.add(attempt); db.flush()
    a.status = "in_progress"; a.retry_unlocked = False
    db.commit(); db.refresh(attempt)
    return _attempt_payload(db, attempt)


@router.get("/attempts/{attempt_id}")
def get_attempt(attempt_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    attempt = db.get(TrainingAttempt, attempt_id)
    if not attempt:
        raise HTTPException(404, "Попытка не найдена")
    employee = _current_employee(db, user)
    if user.role not in EDITOR_ROLES and (not employee or attempt.employee_id != employee.id):
        raise HTTPException(403, "Нет доступа")
    return _attempt_payload(db, attempt, include_answers=True)


@router.put("/attempts/{attempt_id}/answers/{question_id}")
def save_answer(attempt_id: int, question_id: int, payload: AnswerIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    attempt = db.get(TrainingAttempt, attempt_id)
    employee = _current_employee(db, user)
    if not attempt or not employee or attempt.employee_id != employee.id:
        raise HTTPException(404, "Активная попытка не найдена")
    if attempt.status != "active":
        raise HTTPException(400, "Попытка уже завершена")
    if attempt.expires_at <= datetime.utcnow():
        _finalize_attempt(db, attempt, timed_out=True); db.commit(); raise HTTPException(409, "Время теста истекло")
    qids = {int(x) for x in (attempt.question_order_json or [])}
    if question_id not in qids:
        raise HTTPException(400, "Вопрос не входит в эту попытку")
    q = db.get(TrainingQuestion, question_id)
    if not q:
        raise HTTPException(404, "Вопрос не найден")
    selected = [int(x) for x in payload.selected_option_ids]
    if q.question_type in {"single", "multiple"}:
        allowed = set(db.scalars(select(TrainingQuestionOption.id).where(TrainingQuestionOption.question_id == q.id)).all())
        if not set(selected).issubset(allowed):
            raise HTTPException(400, "Некорректный вариант ответа")
        if q.question_type == "single" and len(selected) > 1:
            raise HTTPException(400, "Можно выбрать только один ответ")
    else:
        selected = []
    ans = db.scalar(select(TrainingAttemptAnswer).where(TrainingAttemptAnswer.attempt_id == attempt.id, TrainingAttemptAnswer.question_id == q.id))
    if not ans:
        ans = TrainingAttemptAnswer(attempt_id=attempt.id, question_id=q.id); db.add(ans)
    ans.selected_option_ids_json = selected or None
    ans.text_answer = payload.text_answer if q.question_type == "text" else None
    ans.is_correct = None
    ans.answered_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


@router.post("/attempts/{attempt_id}/submit")
def submit_attempt(attempt_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    attempt = db.get(TrainingAttempt, attempt_id)
    employee = _current_employee(db, user)
    if not attempt or not employee or attempt.employee_id != employee.id:
        raise HTTPException(404, "Активная попытка не найдена")
    if attempt.status == "active":
        _finalize_attempt(db, attempt, timed_out=attempt.expires_at <= datetime.utcnow())
        db.commit(); db.refresh(attempt)
    return _attempt_payload(db, attempt, include_answers=False)


@router.get("/assignments/{assignment_id}/attempts")
def assignment_attempts(assignment_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    a = db.get(TrainingAssignment, assignment_id)
    if not a:
        raise HTTPException(404, "Назначение не найдено")
    employee = _current_employee(db, user)
    if user.role not in EDITOR_ROLES and (not employee or a.employee_id != employee.id):
        raise HTTPException(403, "Нет доступа")
    rows = list(db.scalars(select(TrainingAttempt).where(TrainingAttempt.assignment_id == a.id).order_by(TrainingAttempt.attempt_no.desc())).all())
    return [{"id": x.id, "attempt_no": x.attempt_no, "status": x.status, "started_at": x.started_at, "completed_at": x.completed_at, "score_percent": float(x.score_percent) if x.score_percent is not None else None, "passed": x.passed} for x in rows]


@router.post("/ai-draft")
def ai_draft(user=Depends(get_current_user)):
    _require_editor(user)
    raise HTTPException(501, "Генерация теста через AI будет доступна после подключения AI-агента")
