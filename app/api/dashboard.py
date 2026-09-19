from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.security import get_current_user, user_store_ids
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
    RolePermission,
    ShiftReport,
    Store,
    TaskAssignee,
    TaskV2,
    TelegramDeliveryLog,
    TrainingAssignment,
    TrainingTest,
    Violation,
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

MODULE_PERMISSION = {
    "orders": "orders.list",
    "work_schedule": "schedule.my",
    "shifts": "shifts.history",
    "tasks": "tasks.my",
    "inspections": "inspections.history",
    "employees": "employees.view",
    "knowledge": "knowledge.read",
    "testing": "testing.take",
    "telegram_delivery": "telegram.logs",
}


def _employee(db: Session, user):
    return db.scalar(select(Employee).where(Employee.user_id == user.id, Employee.active.is_(True)))


def _perm(db: Session, user, key: str) -> dict[str, str]:
    if user.role == "admin":
        return {"access_level":"edit","data_scope":"network"}
    rk = getattr(user, "role_key", None) or user.role
    p = db.get(RolePermission, {"role_key":rk, "permission_key":key})
    if not p:
        return {"access_level":"hidden","data_scope":"own"}
    return {"access_level":p.access_level, "data_scope":p.data_scope}


def _allowed(db: Session, user, key: str) -> bool:
    return _perm(db, user, key)["access_level"] != "hidden"


def _scope_stores(db: Session, user, key: str) -> list[int]:
    p = _perm(db, user, key)
    if p["data_scope"] == "network" or user.role in {"operations_director","leader","admin"}:
        return list(db.scalars(select(Store.id).where(Store.active.is_(True))).all())
    return user_store_ids(db, user)


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
    src = seller if user.role in {"seller","mentor"} else manager
    return [{"key":k,"size":sz,"order":i} for i,(k,sz) in enumerate(src) if k in allowed_keys]


def _visible_required_articles(db: Session, user, employee) -> list[KnowledgeArticle]:
    q = select(KnowledgeArticle).where(KnowledgeArticle.status=="published", KnowledgeArticle.required_ack.is_(True))
    rows = list(db.scalars(q).all())
    result=[]
    store_ids=set(user_store_ids(db,user))
    for a in rows:
        if a.store_id and a.store_id not in store_ids and user.role not in {"operations_director","leader","admin"}:
            continue
        tags = set(a.position_tags_json or [])
        if tags and employee and employee.position not in tags:
            continue
        ack=db.scalar(select(KnowledgeAcknowledgement.id).where(KnowledgeAcknowledgement.article_id==a.id,KnowledgeAcknowledgement.user_id==user.id,KnowledgeAcknowledgement.revision==a.revision))
        if not ack: result.append(a)
    return result


def _badge_data(db: Session, user) -> dict[str,int]:
    result={}
    employee=_employee(db,user)
    if _allowed(db,user,"orders.list"):
        p=_perm(db,user,"orders.list")
        q=select(func.count(Order.id)).where(Order.status.in_(["new","skipped"]))
        if p["data_scope"]=="own": q=q.where(Order.created_by==user.id)
        else:
            ids=_scope_stores(db,user,"orders.list"); q=q.where(Order.store_id.in_(ids or [-1]))
        result["orders"]=int(db.scalar(q) or 0)
    if _allowed(db,user,"tasks.my") and employee:
        result["tasks"]=int(db.scalar(select(func.count(TaskAssignee.id)).where(TaskAssignee.employee_id==employee.id,TaskAssignee.status=="new")) or 0)
        if _allowed(db,user,"tasks.control"):
            review=int(db.scalar(select(func.count(TaskAssignee.id)).join(TaskV2,TaskV2.id==TaskAssignee.task_id).where(TaskAssignee.status=="review",TaskV2.created_by==user.id)) or 0)
            result["tasks"] += review
    if _allowed(db,user,"shifts.control"):
        ids=_scope_stores(db,user,"shifts.control")
        result["shifts"]=int(db.scalar(select(func.count(ShiftReport.id)).where(ShiftReport.status=="review",ShiftReport.store_id.in_(ids or [-1]))) or 0)
    elif employee:
        result["shifts"]=int(db.scalar(select(func.count(ShiftReport.id)).where(ShiftReport.employee_id==employee.id,ShiftReport.status=="rejected")) or 0)
    if _allowed(db,user,"knowledge.read"):
        result["knowledge"]=len(_visible_required_articles(db,user,employee))
    if _allowed(db,user,"testing.take") and employee:
        result["testing"]=int(db.scalar(select(func.count(TrainingAssignment.id)).where(TrainingAssignment.employee_id==employee.id,TrainingAssignment.status=="assigned")) or 0)
    if _allowed(db,user,"inspections.control"):
        minrow=db.get(AppSetting,"min_inspections_per_week")
        target=int(minrow.value_json if minrow and isinstance(minrow.value_json,(int,float)) else 3)
        ids=_scope_stores(db,user,"inspections.control")
        today=date.today(); monday=today-timedelta(days=today.weekday()); start=datetime.combine(monday,datetime.min.time())
        missing=0
        for sid in ids:
            done=int(db.scalar(select(func.count(Inspection.id)).where(Inspection.store_id==sid,Inspection.status=="completed",Inspection.completed_at>=start)) or 0)
            if done<target: missing+=1
        result["inspections"]=missing
    if _allowed(db,user,"telegram.logs"):
        since=datetime.utcnow()-timedelta(days=7)
        result["telegram_delivery"]=int(db.scalar(select(func.count(TelegramDeliveryLog.id)).where(TelegramDeliveryLog.status=="error",TelegramDeliveryLog.created_at>=since)) or 0)
    return {k:v for k,v in result.items() if v>0}


def _widget_values(db: Session, user) -> dict[str,Any]:
    emp=_employee(db,user)
    today=date.today(); now=datetime.utcnow()
    result={}
    if emp:
        nxt=db.scalar(select(WorkShiftAssignment).where(WorkShiftAssignment.employee_id==emp.id,WorkShiftAssignment.work_date>=today).order_by(WorkShiftAssignment.work_date,WorkShiftAssignment.shift_type).limit(1))
        if nxt:
            st=db.get(Store,nxt.store_id)
            result["next_shift"]={"date":nxt.work_date.isoformat(),"shift_type":nxt.shift_type,"store_name":st.name if st else f"Точка #{nxt.store_id}"}
        else: result["next_shift"]={"empty":True}
        ass=list(db.scalars(select(TaskAssignee).where(TaskAssignee.employee_id==emp.id,TaskAssignee.status.in_(["new","in_progress","rejected","review"])).order_by(TaskAssignee.created_at.desc()).limit(5)).all())
        result["my_tasks"]={"count":len(ass),"items":[{"id":a.task_id,"title":db.get(TaskV2,a.task_id).title if db.get(TaskV2,a.task_id) else f"Задача #{a.task_id}","status":a.status} for a in ass[:3]]}
        overdue=int(db.scalar(select(func.count(TaskAssignee.id)).join(TaskV2,TaskV2.id==TaskAssignee.task_id).where(TaskAssignee.employee_id==emp.id,TaskAssignee.status.notin_(["done","cancelled"]),TaskV2.deadline.is_not(None),TaskV2.deadline<now)) or 0)
        result["overdue_tasks"]={"count":overdue}
        req=_visible_required_articles(db,user,emp); result["required_knowledge"]={"count":len(req),"items":[{"id":a.id,"title":a.title} for a in req[:3]]}
        tests=list(db.scalars(select(TrainingAssignment).where(TrainingAssignment.employee_id==emp.id,TrainingAssignment.status=="assigned").order_by(TrainingAssignment.created_at.desc()).limit(5)).all())
        result["assigned_tests"]={"count":len(tests),"items":[{"id":x.test_id,"title":db.get(TrainingTest,x.test_id).title if db.get(TrainingTest,x.test_id) else f"Тест #{x.test_id}","status":x.status} for x in tests[:3]]}
        today_assignment=db.scalar(select(WorkShiftAssignment).where(WorkShiftAssignment.employee_id==emp.id,WorkShiftAssignment.work_date==today).limit(1))
        if today_assignment:
            report=db.scalar(select(ShiftReport).where(ShiftReport.work_assignment_id==today_assignment.id).order_by(ShiftReport.id.desc()).limit(1))
            result["handover_due"]={"needed":not report or report.status in {"draft","rejected"},"status":report.status if report else "not_started"}
        else: result["handover_due"]={"needed":False,"status":"no_shift"}
    ids=_scope_stores(db,user,"dashboard.new_orders") if _allowed(db,user,"dashboard.new_orders") else []
    result["new_orders"]={"count":int(db.scalar(select(func.count(Order.id)).where(Order.status=="new",Order.store_id.in_(ids or [-1]))) or 0)}
    ids2=_scope_stores(db,user,"dashboard.handover_review") if _allowed(db,user,"dashboard.handover_review") else []
    result["handover_review"]={"count":int(db.scalar(select(func.count(ShiftReport.id)).where(ShiftReport.status=="review",ShiftReport.store_id.in_(ids2 or [-1]))) or 0)}
    result["schedule_errors"]={"count":0,"note":"Откройте раздел График для проверки конфликтов"}
    ids3=_scope_stores(db,user,"dashboard.inspections_week") if _allowed(db,user,"dashboard.inspections_week") else []
    minrow=db.get(AppSetting,"min_inspections_per_week"); target=int(minrow.value_json if minrow and isinstance(minrow.value_json,(int,float)) else 3)
    monday=today-timedelta(days=today.weekday()); start=datetime.combine(monday,datetime.min.time())
    store_progress=[]
    for sid in ids3:
        st=db.get(Store,sid); done=int(db.scalar(select(func.count(Inspection.id)).where(Inspection.store_id==sid,Inspection.status=="completed",Inspection.completed_at>=start)) or 0)
        store_progress.append({"store_id":sid,"name":st.name if st else str(sid),"done":done,"target":target})
    result["inspections_week"]={"stores":store_progress,"complete":sum(1 for x in store_progress if x["done"]>=x["target"]),"total":len(store_progress)}
    ids4=_scope_stores(db,user,"dashboard.my_stores") if _allowed(db,user,"dashboard.my_stores") else user_store_ids(db,user)
    stores=list(db.scalars(select(Store).where(Store.id.in_(ids4 or [-1])).order_by(Store.name)).all())
    result["my_stores"]={"items":[{"id":s.id,"name":s.name} for s in stores]}
    if _allowed(db,user,"dashboard.team_learning"):
        emp_ids=list(db.scalars(select(EmployeeStore.employee_id).where(EmployeeStore.store_id.in_(ids4 or [-1]))).all())
        emp_ids=list(set(emp_ids))
        total=int(db.scalar(select(func.count(TrainingAssignment.id)).where(TrainingAssignment.employee_id.in_(emp_ids or [-1]))) or 0)
        passed=int(db.scalar(select(func.count(TrainingAssignment.id)).where(TrainingAssignment.employee_id.in_(emp_ids or [-1]),TrainingAssignment.status=="passed")) or 0)
        result["team_learning"]={"total":total,"passed":passed,"percent":round(passed/total*100) if total else 100}
    for k in ["revenue_month","avg_check","plan_fact"]: result[k]={"coming_soon":True,"note":"Будет доступно после подключения Saby"}
    return result


@router.get("")
def dashboard(user=Depends(get_current_user),db:Session=Depends(get_db)):
    # Legacy response kept for existing UI consumers.
    start=datetime.combine(date.today(),datetime.min.time())
    allowed=user_store_ids(db,user)
    def count(model,field):
        q=select(func.count(model.id)).where(field>=start)
        if user.role not in {"operations_director","leader","admin"} and hasattr(model,"store_id"): q=q.where(model.store_id.in_(allowed or [-1]))
        return db.scalar(q) or 0
    stores=[]
    sq=select(Store).where(Store.active.is_(True))
    if user.role not in {"operations_director","leader","admin"}:sq=sq.where(Store.id.in_(allowed or [-1]))
    for s in db.scalars(sq.order_by(Store.name)).all(): stores.append({"id":s.id,"name":s.name,"score":0,"components":{}})
    return {"today":{"orders":count(Order,Order.created_at),"shifts":db.scalar(select(func.count(ShiftReport.id)).where(ShiftReport.submitted_at>=start,ShiftReport.status!="draft")) or 0,"inspections":count(Inspection,Inspection.completed_at),"cash":0},"my_overdue_tasks":_widget_values(db,user).get("overdue_tasks",{}).get("count",0),"stores":stores}


@router.get("/ui")
def dashboard_ui(user=Depends(get_current_user),db:Session=Depends(get_db)):
    allowed=[]
    for w in WIDGET_CATALOG:
        if _allowed(db,user,w["permission"]): allowed.append(w)
    keys={x["key"] for x in allowed}
    pref=db.get(DashboardPreference,user.id)
    layout=(pref.layout_json if pref and isinstance(pref.layout_json,list) else None) or _default_layout(user,keys)
    # Strip widgets no longer allowed and append nothing automatically: user keeps personal choice.
    layout=[x for x in layout if isinstance(x,dict) and x.get("key") in keys]
    if not layout: layout=_default_layout(user,keys)
    return {"catalog":allowed,"layout":layout,"values":_widget_values(db,user),"badges":_badge_data(db,user)}


class LayoutIn(BaseModel):
    layout: list[dict[str,Any]]

@router.put("/layout")
def save_layout(payload:LayoutIn,user=Depends(get_current_user),db:Session=Depends(get_db)):
    allowed={w["key"] for w in WIDGET_CATALOG if _allowed(db,user,w["permission"])}
    clean=[]
    seen=set()
    for i,x in enumerate(payload.layout[:30]):
        key=str(x.get("key") or "")
        if key not in allowed or key in seen: continue
        size=str(x.get("size") or "M").upper()
        if size not in {"S","M","L"}: size="M"
        seen.add(key); clean.append({"key":key,"size":size,"order":i})
    pref=db.get(DashboardPreference,user.id)
    if not pref: pref=DashboardPreference(user_id=user.id,layout_json=clean);db.add(pref)
    else: pref.layout_json=clean
    db.commit();return {"ok":True,"layout":clean}

@router.get("/badges")
def badges(user=Depends(get_current_user),db:Session=Depends(get_db)):
    return _badge_data(db,user)
