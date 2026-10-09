from __future__ import annotations

from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import select, func
from sqlalchemy.orm import Session
import threading
import logging
import time

from app.core.config import APP_TIMEZONE, MIN_INSPECTIONS_PER_STORE_PER_WEEK, PLAN_ALERT_THRESHOLD
from app.db.database import session_scope
from app.db.models import (
    Broadcast, User, UserStore, NotificationLog, Store, StoreSupplierSchedule, Supplier,
    Order, ShiftTemplate, ShiftReport, Task, Inspection, PlanFact
)
from app.services.telegram import send_message

_scheduler_thread = None
_stop_event = threading.Event()


def _local_now():
    return datetime.now(ZoneInfo(APP_TIMEZONE))


def _sent(db: Session, key: str) -> bool:
    if "scheduler_sent" not in db.info:
        db.info["scheduler_sent"]=set(db.scalars(select(NotificationLog.dedupe_key).where(NotificationLog.created_at>=datetime.utcnow()-timedelta(days=8))).all())
    return key in db.info["scheduler_sent"]


def _mark(db: Session, key: str, user_id: int | None, kind: str):
    if not _sent(db,key):
        db.add(NotificationLog(dedupe_key=key,user_id=user_id,kind=kind))
        db.info["scheduler_sent"].add(key)


def _users_for(db: Session, roles: list[str] | None, store_ids: list[int] | None):
    if 'scheduler_users' not in db.info:
        db.info['scheduler_users']=list(db.scalars(select(User).where(User.active.is_(True),User.status=="active")).all())
        links={}
        for uid,sid in db.execute(select(UserStore.user_id,UserStore.store_id)).all():links.setdefault(uid,set()).add(sid)
        db.info['scheduler_stores']=links
    stores=set(store_ids or [])
    return [u for u in db.info['scheduler_users'] if (not roles or u.role in roles) and (not stores or u.role in {"operations_director","leader","admin"} or stores.intersection(db.info['scheduler_stores'].get(u.id,set())))]


def _queue_alert(db,key,chat_id,text,markup=None):
    from app.db.models import TelegramDeliveryLog
    if key not in db.info.setdefault("queued_alerts",set()):
        db.info["queued_alerts"].add(key)
        db.add(TelegramDeliveryLog(event_type="scheduled_alert",entity_type="scheduler",entity_id=0,chat_id=chat_id,dedupe_key=key,status="pending",payload_json={"text":text,"reply_markup":markup}))


def process_broadcasts():
    now=_local_now(); hhmm=now.strftime("%H:%M"); wd=now.weekday(); today=now.date().isoformat()
    with session_scope() as db:
        for b in db.scalars(select(Broadcast).where(Broadcast.active.is_(True),Broadcast.send_time==hhmm)).all():
            if b.weekdays and wd not in b.weekdays:continue
            for u in _users_for(db,b.role_targets,b.store_ids):
                key=f"broadcast:{b.id}:{today}:{u.id}"
                if _sent(db,key):continue
                markup=None
                if b.button_text and b.button_url:markup={"inline_keyboard":[[{"text":b.button_text,"url":b.button_url}]]}
                try:
                    _queue_alert(db,key,u.telegram_id,b.message,markup);_mark(db,key,u.id,"broadcast")
                except Exception:
                    pass


def process_task_alerts():
    now=datetime.utcnow(); local=_local_now(); day=local.date().isoformat()
    with session_scope() as db:
        tasks=db.scalars(select(Task).where(Task.status!="done",Task.deadline.is_not(None),Task.deadline<=now)).all()
        for t in tasks:
            u=db.get(User,t.assigned_to)
            if not u or not u.active or u.status!="active":continue
            key=f"task-overdue:{t.id}:{day}:{u.id}"
            if _sent(db,key):continue
            try:
                _queue_alert(db,key,u.telegram_id,f"🔴 <b>Просрочена задача</b>\n{t.title}")
                _mark(db,key,u.id,"task_overdue")
            except Exception:pass


def process_order_deadlines():
    now=_local_now(); wd=now.weekday(); hhmm=now.strftime("%H:%M"); day=now.date()
    with session_scope() as db:
        schedules=db.execute(select(StoreSupplierSchedule,Supplier,Store).join(Supplier,Supplier.id==StoreSupplierSchedule.supplier_id).join(Store,Store.id==StoreSupplierSchedule.store_id).where(StoreSupplierSchedule.active.is_(True),StoreSupplierSchedule.weekday==wd,Supplier.active.is_(True),Store.active.is_(True))).all()
        for sch,sup,store in schedules:
            if sup.deadline_time!=hhmm:continue
            start=datetime.combine(day,datetime.min.time());end=start+timedelta(days=1)
            exists=db.scalar(select(Order.id).where(Order.store_id==store.id,Order.supplier_id==sup.id,Order.created_at>=start,Order.created_at<end))
            if exists:continue
            users=_users_for(db,["seller","mentor","manager","operations_director"],[store.id])
            for u in users:
                key=f"order-missing:{store.id}:{sup.id}:{day}:{u.id}"
                if _sent(db,key):continue
                try:
                    _queue_alert(db,key,u.telegram_id,f"⚠️ <b>Нет заявки поставщику</b>\n{store.name} — {sup.name}\nСрок: {sup.deadline_time}")
                    _mark(db,key,u.id,"order_missing")
                except Exception:pass


def process_shift_deadlines():
    now=_local_now(); hhmm=now.strftime("%H:%M"); day=now.date()
    kind = "morning" if hhmm=="08:10" else ("evening" if hhmm=="20:10" else None)
    if not kind:return
    with session_scope() as db:
        templates=db.execute(select(ShiftTemplate,Store).join(Store,Store.id==ShiftTemplate.store_id).where(ShiftTemplate.active.is_(True),ShiftTemplate.shift_kind==kind,Store.active.is_(True))).all()
        start=datetime.combine(day,datetime.min.time());end=start+timedelta(days=1)
        for t,store in templates:
            exists=db.scalar(select(ShiftReport.id).where(ShiftReport.store_id==store.id,ShiftReport.template_id==t.id,ShiftReport.submitted_at>=start,ShiftReport.submitted_at<end,ShiftReport.status!="draft"))
            if exists:continue
            users=_users_for(db,["manager","operations_director","leader","admin"],[store.id])
            for u in users:
                key=f"shift-missing:{kind}:{store.id}:{day}:{u.id}"
                if _sent(db,key):continue
                try:
                    _queue_alert(db,key,u.telegram_id,f"🔴 <b>Не сдана пересменка</b>\n{store.name} — {t.name}")
                    _mark(db,key,u.id,"shift_missing")
                except Exception:pass


def process_inspection_control():
    now=_local_now()
    if now.weekday()!=4 or now.strftime("%H:%M")!="18:00":return
    ws=now.date()-timedelta(days=now.weekday()); start=datetime.combine(ws,datetime.min.time())
    with session_scope() as db:
        counts=dict(db.execute(select(Inspection.store_id,func.count(Inspection.id)).where(Inspection.completed_at>=start).group_by(Inspection.store_id)).all())
        for store in db.scalars(select(Store).where(Store.active.is_(True))).all():
            count=counts.get(store.id,0)
            if count>=MIN_INSPECTIONS_PER_STORE_PER_WEEK:continue
            for u in _users_for(db,["manager","operations_director","leader","admin"],[store.id]):
                key=f"inspection-shortage:{store.id}:{ws}:{u.id}"
                if _sent(db,key):continue
                try:
                    _queue_alert(db,key,u.telegram_id,f"⚠️ <b>Недостаточно проверок точки</b>\n{store.name}: {count}/{MIN_INSPECTIONS_PER_STORE_PER_WEEK} за неделю")
                    _mark(db,key,u.id,"inspection_shortage")
                except Exception:pass


def process_plan_alerts():
    now=_local_now()
    if now.strftime("%H:%M")!="18:30":return
    ws=now.date()-timedelta(days=now.weekday())
    with session_scope() as db:
        rows=db.execute(select(PlanFact,Store).join(Store,Store.id==PlanFact.store_id).where(PlanFact.period_type=="weekly",PlanFact.period_date==ws,Store.active.is_(True))).all()
        for pf,store in rows:
            if float(pf.plan or 0)<=0:continue
            ratio=float(pf.fact or 0)/float(pf.plan)
            if ratio>=PLAN_ALERT_THRESHOLD:continue
            for u in _users_for(db,["manager","operations_director","leader","admin"],[store.id]):
                key=f"plan-low:{store.id}:{ws}:{now.date()}:{u.id}"
                if _sent(db,key):continue
                try:
                    _queue_alert(db,key,u.telegram_id,f"📉 <b>План ниже контрольного уровня</b>\n{store.name}: {ratio*100:.1f}%")
                    _mark(db,key,u.id,"plan_low")
                except Exception:pass




def scheduler_tick():
    process_broadcasts();process_task_alerts();process_order_deadlines();process_shift_deadlines();process_inspection_control();process_plan_alerts()


def _loop():
    last_minute=None
    while not _stop_event.is_set():
        try:
            # Scheduled rules run once per minute; outbound queue has its own worker.
            minute=_local_now().strftime("%Y-%m-%d %H:%M")
            if minute!=last_minute:
                from app.db.database import get_engine
                from sqlalchemy import text
                with get_engine().connect() as leader:
                    acquired=True
                    if leader.dialect.name=="postgresql":acquired=leader.execute(text("SELECT pg_try_advisory_lock(714119)")).scalar()
                    try:
                        if acquired:scheduler_tick();last_minute=minute
                    finally:
                        if acquired and leader.dialect.name=="postgresql":leader.execute(text("SELECT pg_advisory_unlock(714119)"))
        except Exception:
            logging.getLogger(__name__).exception("Scheduler tick failed")
        _stop_event.wait(30)


def start_scheduler():
    global _scheduler_thread
    if _scheduler_thread and _scheduler_thread.is_alive():
        return _scheduler_thread
    _stop_event.clear()
    _scheduler_thread = threading.Thread(target=_loop, name="core-scheduler", daemon=True)
    _scheduler_thread.start()
    return _scheduler_thread


def stop_scheduler():
    global _scheduler_thread
    _stop_event.set()
    _scheduler_thread = None
