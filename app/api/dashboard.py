from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import current_user, internal_secret
from ..config import settings
from ..db import get_db
from ..models import CashCollection, Inspection, Order, OrderStatus, ShiftKind, ShiftReport, StoreMetric, Task, TaskStatus, User, UserRole
from ..permissions import store_ids_for_user

router = APIRouter(prefix="/api", tags=["dashboard"])


@router.get("/dashboard")
def dashboard(user: User = Depends(current_user), db: Session = Depends(get_db)):
    ids = store_ids_for_user(db, user)
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    now = datetime.now(timezone.utc)

    def filtered_count(model, extra=None):
        q = select(func.count()).select_from(model)
        if hasattr(model, "store_id") and user.role not in {UserRole.admin, UserRole.operations_director, UserRole.executive}:
            q = q.where(model.store_id.in_(ids or [-1]))
        if extra is not None:
            q = q.where(extra)
        return db.scalar(q) or 0

    pending_orders = filtered_count(Order, Order.status.in_([OrderStatus.submitted, OrderStatus.approved]))
    overdue_tasks = filtered_count(Task, (Task.status.in_([TaskStatus.open, TaskStatus.in_progress])) & (Task.due_at.is_not(None)) & (Task.due_at < now))
    inspections_week = filtered_count(Inspection, Inspection.created_at >= datetime.combine(week_start, datetime.min.time()))
    cash_week = filtered_count(CashCollection, CashCollection.collected_at >= datetime.combine(week_start, datetime.min.time()))

    morning_reports = filtered_count(ShiftReport, (ShiftReport.report_date == today) & (ShiftReport.kind == ShiftKind.morning) & (ShiftReport.finalized.is_(True)))
    evening_reports = filtered_count(ShiftReport, (ShiftReport.report_date == today) & (ShiftReport.kind == ShiftKind.evening) & (ShiftReport.finalized.is_(True)))

    metrics_q = select(StoreMetric).where(StoreMetric.metric_date >= week_start).order_by(StoreMetric.metric_date.desc())
    if user.role not in {UserRole.admin, UserRole.operations_director, UserRole.executive}:
        metrics_q = metrics_q.where(StoreMetric.store_id.in_(ids or [-1]))
    metrics = db.scalars(metrics_q).all()
    revenue = sum(float(x.revenue or 0) for x in metrics)
    plan = sum(float(x.plan or 0) for x in metrics)

    return {
        "stores": len(ids),
        "pending_orders": pending_orders,
        "overdue_tasks": overdue_tasks,
        "inspections_week": inspections_week,
        "cash_collections_week": cash_week,
        "shift_reports_today": {"morning": morning_reports, "evening": evening_reports},
        "revenue_week": revenue,
        "plan_week": plan,
        "plan_percent": round((revenue / plan * 100), 1) if plan else None,
    }


class MetricIn(BaseModel):
    store_id: int
    metric_date: date
    source: str = "saby"
    revenue: float | None = None
    plan: float | None = None
    avg_check: float | None = None
    writeoffs: float | None = None
    payload: dict = {}


@router.post("/integrations/store-metrics", dependencies=[Depends(internal_secret)])
def ingest_metric(payload: MetricIn, db: Session = Depends(get_db)):
    row = db.scalar(select(StoreMetric).where(StoreMetric.store_id == payload.store_id, StoreMetric.metric_date == payload.metric_date, StoreMetric.source == payload.source))
    if not row:
        row = StoreMetric(store_id=payload.store_id, metric_date=payload.metric_date, source=payload.source)
        db.add(row)
    row.revenue = payload.revenue
    row.plan = payload.plan
    row.avg_check = payload.avg_check
    row.writeoffs = payload.writeoffs
    row.payload = payload.payload
    db.commit()
    return {"ok": True}


@router.post("/ai/store/{store_id}")
async def ai_store(store_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if store_id not in store_ids_for_user(db, user):
        raise HTTPException(403, "No access")
    if not settings.ai_agent_url:
        return {"configured": False, "message": "AI-агент ещё не подключён. API-контракт уже предусмотрен."}
    context = {
        "store_id": store_id,
        "requested_by": user.id,
        "task": "analyze_store",
    }
    headers = {"X-Internal-Secret": settings.ai_agent_secret} if settings.ai_agent_secret else {}
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(f"{settings.ai_agent_url}/analyze", json=context, headers=headers)
    if r.status_code >= 400:
        raise HTTPException(502, f"AI agent error: {r.status_code}")
    return {"configured": True, "result": r.json()}
