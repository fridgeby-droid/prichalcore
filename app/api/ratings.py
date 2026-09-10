from datetime import date, datetime, timedelta
from fastapi import APIRouter, Depends
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.core.security import get_current_user, user_store_ids
from app.db.database import get_db
from app.db.models import Store, PlanFact, Inspection, Violation, Task, ShiftTemplate, ShiftReport, AppSetting

router = APIRouter(prefix="/api/ratings", tags=["ratings"])

DEFAULT_WEIGHTS = {"plan": 0.40, "inspections": 0.20, "violations": 0.15, "tasks": 0.15, "shifts": 0.10}


def _week_start(d: date):
    return d - timedelta(days=d.weekday())


def compute_store_rating(db: Session, store_id: int):
    today = date.today()
    ws = _week_start(today)
    start_dt = datetime.combine(ws, datetime.min.time())
    end_dt = datetime.utcnow()
    weights_obj = db.get(AppSetting, "rating_weights")
    weights = weights_obj.value_json if weights_obj and isinstance(weights_obj.value_json, dict) else DEFAULT_WEIGHTS

    pf = db.scalar(select(PlanFact).where(PlanFact.store_id == store_id, PlanFact.period_type == "weekly", PlanFact.period_date == ws))
    plan_score = None
    if pf and float(pf.plan or 0) > 0:
        plan_score = min(120.0, float(pf.fact or 0) / float(pf.plan) * 100.0)

    insp_avg = db.scalar(select(func.avg(Inspection.score)).where(Inspection.store_id == store_id, Inspection.completed_at >= start_dt, Inspection.completed_at <= end_dt))
    inspection_score = float(insp_avg) if insp_avg is not None else None

    open_violations = db.scalar(select(func.count(Violation.id)).where(Violation.store_id == store_id, Violation.status == "open")) or 0
    violation_score = max(0.0, 100.0 - min(100.0, open_violations * 10.0))

    task_total = db.scalar(select(func.count(Task.id)).where(Task.store_id == store_id, Task.created_at >= start_dt, Task.created_at <= end_dt)) or 0
    task_done = db.scalar(select(func.count(Task.id)).where(Task.store_id == store_id, Task.created_at >= start_dt, Task.created_at <= end_dt, Task.status == "done")) or 0
    task_score = (task_done / task_total * 100.0) if task_total else None

    templates = db.scalars(select(ShiftTemplate).where(ShiftTemplate.store_id == store_id, ShiftTemplate.active.is_(True))).all()
    elapsed_days = (today - ws).days + 1
    expected = len(templates) * elapsed_days
    submitted = db.scalar(select(func.count(ShiftReport.id)).where(ShiftReport.store_id == store_id, ShiftReport.submitted_at >= start_dt, ShiftReport.submitted_at <= end_dt, ShiftReport.status != "draft")) or 0
    shift_score = min(100.0, submitted / expected * 100.0) if expected else None

    components = {
        "plan": plan_score,
        "inspections": inspection_score,
        "violations": violation_score,
        "tasks": task_score,
        "shifts": shift_score,
    }
    available = {k: v for k, v in components.items() if v is not None}
    denom = sum(float(weights.get(k, 0)) for k in available) or 1.0
    total = sum(v * float(weights.get(k, 0)) for k, v in available.items()) / denom
    return {"score": round(total, 1), "components": {k: (round(v,1) if v is not None else None) for k,v in components.items()}}


@router.get("")
def ratings(user=Depends(get_current_user), db: Session = Depends(get_db)):
    allowed = user_store_ids(db, user)
    q = select(Store).where(Store.active.is_(True))
    if user.role not in {"operations_director", "leader", "admin"}:
        q = q.where(Store.id.in_(allowed or [-1]))
    rows=[]
    for s in db.scalars(q.order_by(Store.name)).all():
        rows.append({"store_id":s.id,"store_name":s.name,**compute_store_rating(db,s.id)})
    return sorted(rows,key=lambda x:x["score"],reverse=True)
