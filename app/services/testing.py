from __future__ import annotations

from datetime import datetime, timedelta
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Employee, KnowledgeAcknowledgement, KnowledgeArticle, TrainingAssignment, TrainingTest


def ensure_article_ack_assignments(db: Session, article_id: int, user_id: int) -> list[int]:
    """Create missing assignments for published tests linked to an acknowledged article."""
    employee = db.scalar(select(Employee).where(Employee.user_id == user_id, Employee.active.is_(True)))
    if not employee:
        return []
    tests = list(db.scalars(select(TrainingTest).where(
        TrainingTest.linked_article_id == article_id,
        TrainingTest.auto_assign_on_ack.is_(True),
        TrainingTest.status == "published",
    )).all())
    ids: list[int] = []
    for test in tests:
        assignment = _ensure_assignment(db, test, employee.id, source="article_ack")
        ids.append(assignment.id)
    return ids


def _ensure_assignment(db: Session, test: TrainingTest, employee_id: int, source: str = "article_ack") -> TrainingAssignment:
    assignment = db.scalar(select(TrainingAssignment).where(
        TrainingAssignment.test_id == test.id,
        TrainingAssignment.employee_id == employee_id,
    ))
    if assignment:
        return assignment
    due_at = datetime.utcnow() + timedelta(days=test.auto_due_days) if test.auto_due_days else None
    assignment = TrainingAssignment(
        test_id=test.id,
        employee_id=employee_id,
        assigned_by=None,
        source=source,
        status="assigned",
        due_at=due_at,
        retry_unlocked=True,
    )
    db.add(assignment)
    db.flush()
    return assignment


def backfill_test_for_acknowledged(db: Session, test: TrainingTest) -> int:
    """When an auto-linked test is published, assign it to employees who already acknowledged the current article revision."""
    if test.status != "published" or not test.auto_assign_on_ack or not test.linked_article_id:
        return 0
    article = db.get(KnowledgeArticle, test.linked_article_id)
    if not article:
        return 0
    user_ids = list(db.scalars(select(KnowledgeAcknowledgement.user_id).where(
        KnowledgeAcknowledgement.article_id == article.id,
        KnowledgeAcknowledgement.revision == article.revision,
    )).all())
    if not user_ids:
        return 0
    employees = list(db.scalars(select(Employee).where(Employee.user_id.in_(user_ids), Employee.active.is_(True))).all())
    created = 0
    for e in employees:
        existing = db.scalar(select(TrainingAssignment.id).where(TrainingAssignment.test_id == test.id, TrainingAssignment.employee_id == e.id))
        if existing:
            continue
        _ensure_assignment(db, test, e.id)
        created += 1
    return created
