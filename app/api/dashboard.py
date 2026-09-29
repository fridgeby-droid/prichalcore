from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from app.core.security import get_current_user
from app.core.permissions import (
    assigned_store_ids,
    current_employee,
    effective_permission,
    has_access,
    scope_store_ids,
)
from app.db.database import get_db
from app.db.models import (
    AppSetting,
    DashboardPreference,
    Employee,
    EmployeeStore,
    Inspection,
    KnowledgeAcknowledgement,
    KnowledgeArticle,
    Order,
    Photo,
    ShiftReport,
    Store,
    TaskAssignee,
    TaskV2,
    TelegramDeliveryLog,
    TrainingAssignment,
    TrainingTest,
    WorkShiftAssignment,
)

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

WIDGET_CATALOG = [
    {"key":"next_shift","permission":"dashboard.next_shift","title":"Следующая смена","default_size":"M","kind":"work"},
    {"key":"my_tasks","permission":"dashboard.my_tasks","title":"Мои задачи","default_size":"M","kind":"tasks"},
    {"key":"overdue_tasks","permission":"dashboard.overdue_tasks","title":"Просроченные задачи","default_size":"S","kind":"tasks"},
    {"key":"required_knowledge","permission":"dashboard.required_knowledge","title":"Обязательные материалы","default_size":"M","kind":"learning"},
    {"key":"assigned_tests","permission":"dashboard.assigned_tests","title":"Тестирование","default_size":"M","kind":"learning"},
    {"key":"handover_due","permission":"dashboard.handover_due","title":"Пересменка","default_size":"M","kind":"handover"},
    {"key":"new_orders","permission":"dashboard.new_orders","title":"Новые заявки","default_size":"S","kind":"orders"},
    {"key":"handover_review","permission":"dashboard.handover_review","title":"Пересменки на проверке","default_size":"S","kind":"handover"},
    {"key":"schedule_errors","permission":"dashboard.schedule_errors","title":"Ошибки графика","default_size":"S","kind":"schedule"},
    {"key":"inspections_week","permission":"dashboard.inspections_week","title":"Проверки недели","default_size":"M","kind":"inspection"},
    {"key":"my_stores","permission":"dashboard.my_stores","title":"Мои магазины","default_size":"L","kind":"stores"},
    {"key":"team_learning","permission":"dashboard.team_learning","title":"Обучение команды","default_size":"L","kind":"learning"},
    {"key":"revenue_month","permission":"dashboard.revenue_month","title":"Выручка за месяц","default_size":"M","kind":"saby","coming_soon":True},
    {"key":"avg_check","permission":"dashboard.avg_check","title":"Средний чек","default_size":"S","kind":"saby","coming_soon":True},
    {"key":"plan_fact","permission":"dashboard.plan_fact","title":"План / факт","default_size":"M","kind":"saby","coming_soon":True},
]


def _employee(db: Session, user):
    return current_employee(db, user)


def _perm(db: Session, user, key: str) -> dict[str, str]:
    return effective_permission(db, user, key)


def _allowed(db: Session, user, key: str) -> bool:
    return has_access(db, user, key, "view")


def _scope_stores(db: Session, user, key: str) -> list[int]:
    return scope_store_ids(db, user, key, own_as_assigned=True)


def _default_layout(user, allowed_keys: set[str]):
    seller = [
        ("next_shift","M"),("my_tasks","M"),("overdue_tasks","S"),
        ("required_knowledge","M"),("assigned_tests","M"),("handover_due","M"),
    ]
    manager = [
        ("new_orders","S"),("handover_review","S"),("schedule_errors","S"),
        ("inspections_week","M"),("my_stores","L"),("overdue_tasks","S"),
        ("team_learning","L"),("next_shift","M"),("my_tasks","M"),
    ]
    manager_markers={"new_orders","handover_review","schedule_errors","inspections_week","my_stores","team_learning"}
    src = manager if (allowed_keys & manager_markers) else seller
    return [{"key":k,"size":sz,"order":i} for i,(k,sz) in enumerate(src) if k in allowed_keys]


def _visible_required_articles(db: Session, user, employee) -> list[KnowledgeArticle]:
    """Required, visible and still-unacknowledged articles in one SQL query.

    v1.7.10 queried acknowledgements once per article. This outer join removes
    that N+1 pattern; position-tag filtering remains in Python because tags are
    stored as JSON and we support both PostgreSQL and SQLite smoke tests.
    """
    p = effective_permission(db, user, "knowledge.read")
    store_ids = set(scope_store_ids(db, user, "knowledge.read", own_as_assigned=True))

    join_on = and_(
        KnowledgeAcknowledgement.article_id == KnowledgeArticle.id,
        KnowledgeAcknowledgement.user_id == user.id,
        KnowledgeAcknowledgement.revision == KnowledgeArticle.revision,
    )
    q = (
        select(KnowledgeArticle)
        .outerjoin(KnowledgeAcknowledgement, join_on)
        .where(
            KnowledgeArticle.status == "published",
            KnowledgeArticle.required_ack.is_(True),
            KnowledgeAcknowledgement.id.is_(None),
        )
        .order_by(KnowledgeArticle.updated_at.desc())
    )
    if p["data_scope"] != "network":
        if store_ids:
            q = q.where(or_(KnowledgeArticle.store_id.is_(None), KnowledgeArticle.store_id.in_(store_ids)))
        else:
            q = q.where(KnowledgeArticle.store_id.is_(None))

    rows = list(db.scalars(q).all())
    if employee:
        rows = [
            a for a in rows
            if not set(a.position_tags_json or []) or employee.position in set(a.position_tags_json or [])
        ]
    return rows


def _inspection_progress(db: Session, store_ids: list[int], start: datetime, target: int) -> list[dict[str, Any]]:
    """Return weekly progress for all stores using one grouped query."""
    if not store_ids:
        return []
    rows = db.execute(
        select(Store.id, Store.name, func.count(Inspection.id))
        .outerjoin(
            Inspection,
            and_(
                Inspection.store_id == Store.id,
                Inspection.status == "completed",
                Inspection.completed_at >= start,
            ),
        )
        .where(Store.id.in_(store_ids))
        .group_by(Store.id, Store.name)
        .order_by(Store.name)
    ).all()
    return [
        {"store_id": sid, "name": name, "done": int(done or 0), "target": target}
        for sid, name, done in rows
    ]


def _profile_summary(db: Session, user, employee) -> dict[str, Any]:
    if not employee:
        return {"full_name": user.full_name, "avatar_photo_id": None}
    avatar_id = db.scalar(
        select(Photo.id)
        .where(Photo.entity_type == "profile_avatar", Photo.entity_id == employee.id)
        .order_by(Photo.created_at.desc())
        .limit(1)
    )
    return {
        "id": employee.id,
        "full_name": employee.full_name,
        "avatar_photo_id": avatar_id,
        "app_theme": employee.app_theme or "light",
    }


def _badge_data(db: Session, user) -> dict[str,int]:
    result={}
    employee=_employee(db,user)

    if _allowed(db,user,"orders.list"):
        p=_perm(db,user,"orders.list")
        q=select(func.count(Order.id)).where(Order.status.in_(["new","skipped"]))
        if p["data_scope"]=="own":
            q=q.where(Order.created_by==user.id)
        else:
            ids=_scope_stores(db,user,"orders.list")
            q=q.where(Order.store_id.in_(ids or [-1]))
        result["orders"]=int(db.scalar(q) or 0)

    if _allowed(db,user,"tasks.my") and employee:
        result["tasks"]=int(
            db.scalar(
                select(func.count(TaskAssignee.id)).where(
                    TaskAssignee.employee_id==employee.id,
                    TaskAssignee.status=="new",
                )
            ) or 0
        )
        if _allowed(db,user,"tasks.control"):
            review=int(
                db.scalar(
                    select(func.count(TaskAssignee.id))
                    .join(TaskV2,TaskV2.id==TaskAssignee.task_id)
                    .where(TaskAssignee.status=="review",TaskV2.created_by==user.id)
                ) or 0
            )
            result["tasks"] += review

    if _allowed(db,user,"shifts.control"):
        ids=_scope_stores(db,user,"shifts.control")
        result["shifts"]=int(
            db.scalar(
                select(func.count(ShiftReport.id)).where(
                    ShiftReport.status=="review",
                    ShiftReport.store_id.in_(ids or [-1]),
                )
            ) or 0
        )
    elif employee:
        result["shifts"]=int(
            db.scalar(
                select(func.count(ShiftReport.id)).where(
                    ShiftReport.employee_id==employee.id,
                    ShiftReport.status=="rejected",
                )
            ) or 0
        )

    if _allowed(db,user,"knowledge.read"):
        result["knowledge"]=len(_visible_required_articles(db,user,employee))

    if _allowed(db,user,"testing.take") and employee:
        result["testing"]=int(
            db.scalar(
                select(func.count(TrainingAssignment.id)).where(
                    TrainingAssignment.employee_id==employee.id,
                    TrainingAssignment.status=="assigned",
                )
            ) or 0
        )

    if _allowed(db,user,"inspections.control"):
        minrow=db.get(AppSetting,"min_inspections_per_week")
        target=int(minrow.value_json if minrow and isinstance(minrow.value_json,(int,float)) else 3)
        ids=_scope_stores(db,user,"inspections.control")
        today=date.today()
        monday=today-timedelta(days=today.weekday())
        start=datetime.combine(monday,datetime.min.time())
        progress=_inspection_progress(db,ids,start,target)
        result["inspections"]=sum(1 for x in progress if x["done"]<target)

    if _allowed(db,user,"telegram.logs"):
        since=datetime.utcnow()-timedelta(days=7)
        result["telegram_delivery"]=int(
            db.scalar(
                select(func.count(TelegramDeliveryLog.id)).where(
                    TelegramDeliveryLog.status=="error",
                    TelegramDeliveryLog.created_at>=since,
                )
            ) or 0
        )

    return {k:v for k,v in result.items() if v>0}


def _widget_values(db: Session, user, requested_keys: set[str] | None = None) -> dict[str,Any]:
    """Build only the widget values that are actually present on the user's Home.

    v1.7.10 calculated every possible widget on every Home open, including widgets
    the user had removed. Limiting work to the current layout cuts both SQL and
    serialization cost while keeping the catalog fully available for editing.
    """
    requested = requested_keys or {w["key"] for w in WIDGET_CATALOG}
    today=date.today()
    now=datetime.utcnow()
    result={}

    employee_keys={
        "next_shift","my_tasks","overdue_tasks",
        "required_knowledge","assigned_tests","handover_due",
    }
    emp=_employee(db,user) if requested & employee_keys else None

    if emp and "next_shift" in requested:
        nxt = db.execute(
            select(
                WorkShiftAssignment.work_date,
                WorkShiftAssignment.shift_type,
                WorkShiftAssignment.store_id,
                Store.name,
            )
            .join(Store, Store.id == WorkShiftAssignment.store_id)
            .where(
                WorkShiftAssignment.employee_id==emp.id,
                WorkShiftAssignment.work_date>=today,
            )
            .order_by(WorkShiftAssignment.work_date,WorkShiftAssignment.shift_type)
            .limit(1)
        ).first()
        if nxt:
            result["next_shift"]={
                "date":nxt.work_date.isoformat(),
                "shift_type":nxt.shift_type,
                "store_name":nxt.name,
            }
        else:
            result["next_shift"]={"empty":True}

    if emp and "my_tasks" in requested:
        task_rows = db.execute(
            select(TaskAssignee.task_id, TaskAssignee.status, TaskV2.title)
            .join(TaskV2, TaskV2.id == TaskAssignee.task_id)
            .where(
                TaskAssignee.employee_id==emp.id,
                TaskAssignee.status.in_(["new","in_progress","rejected","review"]),
            )
            .order_by(TaskAssignee.created_at.desc())
            .limit(5)
        ).all()
        result["my_tasks"]={
            "count":len(task_rows),
            "items":[{"id":tid,"title":title,"status":status} for tid,status,title in task_rows[:3]],
        }

    if emp and "overdue_tasks" in requested:
        overdue=int(
            db.scalar(
                select(func.count(TaskAssignee.id))
                .join(TaskV2,TaskV2.id==TaskAssignee.task_id)
                .where(
                    TaskAssignee.employee_id==emp.id,
                    TaskAssignee.status.notin_(["done","cancelled"]),
                    TaskV2.deadline.is_not(None),
                    TaskV2.deadline<now,
                )
            ) or 0
        )
        result["overdue_tasks"]={"count":overdue}

    if emp and "required_knowledge" in requested:
        req=_visible_required_articles(db,user,emp)
        result["required_knowledge"]={
            "count":len(req),
            "items":[{"id":a.id,"title":a.title} for a in req[:3]],
        }

    if emp and "assigned_tests" in requested:
        test_rows = db.execute(
            select(TrainingAssignment.test_id, TrainingAssignment.status, TrainingTest.title)
            .join(TrainingTest, TrainingTest.id == TrainingAssignment.test_id)
            .where(
                TrainingAssignment.employee_id==emp.id,
                TrainingAssignment.status=="assigned",
            )
            .order_by(TrainingAssignment.created_at.desc())
            .limit(5)
        ).all()
        result["assigned_tests"]={
            "count":len(test_rows),
            "items":[{"id":tid,"title":title,"status":status} for tid,status,title in test_rows[:3]],
        }

    if emp and "handover_due" in requested:
        today_assignment=db.scalar(
            select(WorkShiftAssignment)
            .where(
                WorkShiftAssignment.employee_id==emp.id,
                WorkShiftAssignment.work_date==today,
            )
            .limit(1)
        )
        if today_assignment:
            report=db.scalar(
                select(ShiftReport)
                .where(ShiftReport.work_assignment_id==today_assignment.id)
                .order_by(ShiftReport.id.desc())
                .limit(1)
            )
            result["handover_due"]={
                "needed":not report or report.status in {"draft","rejected"},
                "status":report.status if report else "not_started",
            }
        else:
            result["handover_due"]={"needed":False,"status":"no_shift"}

    if "new_orders" in requested:
        ids=_scope_stores(db,user,"dashboard.new_orders") if _allowed(db,user,"dashboard.new_orders") else []
        result["new_orders"]={
            "count":int(
                db.scalar(
                    select(func.count(Order.id)).where(
                        Order.status=="new",
                        Order.store_id.in_(ids or [-1]),
                    )
                ) or 0
            )
        }

    if "handover_review" in requested:
        ids2=_scope_stores(db,user,"dashboard.handover_review") if _allowed(db,user,"dashboard.handover_review") else []
        result["handover_review"]={
            "count":int(
                db.scalar(
                    select(func.count(ShiftReport.id)).where(
                        ShiftReport.status=="review",
                        ShiftReport.store_id.in_(ids2 or [-1]),
                    )
                ) or 0
            )
        }

    if "schedule_errors" in requested:
        result["schedule_errors"]={"count":0,"note":"Откройте раздел График для проверки конфликтов"}

    if "inspections_week" in requested:
        ids3=_scope_stores(db,user,"dashboard.inspections_week") if _allowed(db,user,"dashboard.inspections_week") else []
        minrow=db.get(AppSetting,"min_inspections_per_week")
        target=int(minrow.value_json if minrow and isinstance(minrow.value_json,(int,float)) else 3)
        monday=today-timedelta(days=today.weekday())
        start=datetime.combine(monday,datetime.min.time())
        store_progress=_inspection_progress(db,ids3,start,target)
        result["inspections_week"]={
            "stores":store_progress,
            "complete":sum(1 for x in store_progress if x["done"]>=x["target"]),
            "total":len(store_progress),
        }

    if requested & {"my_stores","team_learning"}:
        ids4=_scope_stores(db,user,"dashboard.my_stores") if _allowed(db,user,"dashboard.my_stores") else assigned_store_ids(db,user)

        if "my_stores" in requested:
            stores=list(
                db.scalars(
                    select(Store).where(Store.id.in_(ids4 or [-1])).order_by(Store.name)
                ).all()
            )
            result["my_stores"]={"items":[{"id":s.id,"name":s.name} for s in stores]}

        if "team_learning" in requested and _allowed(db,user,"dashboard.team_learning"):
            emp_ids=list(
                set(
                    db.scalars(
                        select(EmployeeStore.employee_id).where(EmployeeStore.store_id.in_(ids4 or [-1]))
                    ).all()
                )
            )
            if emp_ids:
                total, passed = db.execute(
                    select(
                        func.count(TrainingAssignment.id),
                        func.sum(case((TrainingAssignment.status=="passed",1), else_=0)),
                    ).where(TrainingAssignment.employee_id.in_(emp_ids))
                ).one()
                total=int(total or 0)
                passed=int(passed or 0)
            else:
                total=passed=0
            result["team_learning"]={
                "total":total,
                "passed":passed,
                "percent":round(passed/total*100) if total else 100,
            }

    for k in ["revenue_month","avg_check","plan_fact"]:
        if k in requested:
            result[k]={"coming_soon":True,"note":"Будет доступно после подключения Saby"}
    return result


@router.get("")
def dashboard(user=Depends(get_current_user),db:Session=Depends(get_db)):
    # Legacy response kept for existing UI consumers.
    start=datetime.combine(date.today(),datetime.min.time())
    allowed=scope_store_ids(db,user,"dashboard.my_stores",own_as_assigned=True) if has_access(db,user,"dashboard.my_stores") else assigned_store_ids(db,user)

    def count(model,field):
        q=select(func.count(model.id)).where(field>=start)
        if hasattr(model,"store_id"):
            q=q.where(model.store_id.in_(allowed or [-1]))
        return db.scalar(q) or 0

    stores=[
        {"id":sid,"name":name,"score":0,"components":{}}
        for sid,name in db.execute(
            select(Store.id,Store.name)
            .where(Store.active.is_(True),Store.id.in_(allowed or [-1]))
            .order_by(Store.name)
        ).all()
    ]
    overdue = _widget_values(db,user,{"overdue_tasks"}).get("overdue_tasks",{}).get("count",0)
    return {
        "today":{
            "orders":count(Order,Order.created_at),
            "shifts":db.scalar(
                select(func.count(ShiftReport.id)).where(
                    ShiftReport.submitted_at>=start,
                    ShiftReport.status!="draft",
                )
            ) or 0,
            "inspections":count(Inspection,Inspection.completed_at),
            "cash":0,
        },
        "my_overdue_tasks":overdue,
        "stores":stores,
    }


@router.get("/ui")
def dashboard_ui(user=Depends(get_current_user),db:Session=Depends(get_db)):
    allowed=[w for w in WIDGET_CATALOG if _allowed(db,user,w["permission"])]
    keys={x["key"] for x in allowed}
    pref=db.get(DashboardPreference,user.id)
    layout=(pref.layout_json if pref and isinstance(pref.layout_json,list) else None) or _default_layout(user,keys)
    layout=[x for x in layout if isinstance(x,dict) and x.get("key") in keys]
    if not layout:
        layout=_default_layout(user,keys)

    employee=_employee(db,user)
    requested_keys={x.get("key") for x in layout if isinstance(x,dict) and x.get("key")}
    values=_widget_values(db,user,requested_keys)
    badges=_badge_data(db,user)
    return {
        "catalog":allowed,
        "layout":layout,
        "values":values,
        "badges":badges,
        "profile":_profile_summary(db,user,employee),
        "generated_at":datetime.utcnow().isoformat(),
    }


class LayoutIn(BaseModel):
    layout: list[dict[str,Any]]


@router.put("/layout")
def save_layout(payload:LayoutIn,user=Depends(get_current_user),db:Session=Depends(get_db)):
    allowed={w["key"] for w in WIDGET_CATALOG if _allowed(db,user,w["permission"])}
    clean=[]
    seen=set()
    for i,x in enumerate(payload.layout[:30]):
        key=str(x.get("key") or "")
        if key not in allowed or key in seen:
            continue
        size=str(x.get("size") or "M").upper()
        if size not in {"S","M","L"}:
            size="M"
        seen.add(key)
        clean.append({"key":key,"size":size,"order":i})
    pref=db.get(DashboardPreference,user.id)
    if not pref:
        pref=DashboardPreference(user_id=user.id,layout_json=clean)
        db.add(pref)
    else:
        pref.layout_json=clean
    db.commit()
    return {"ok":True,"layout":clean}


@router.get("/badges")
def badges(user=Depends(get_current_user),db:Session=Depends(get_db)):
    return _badge_data(db,user)
