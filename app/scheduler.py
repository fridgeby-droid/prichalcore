from __future__ import annotations

import logging
from datetime import date
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from .config import settings
from .db import SessionLocal
from .models import ShiftKind, ShiftReport, Store, User, UserRole, UserStatus, UserStore
from .telegram import send_message

log = logging.getLogger(__name__)
_scheduler: AsyncIOScheduler | None = None


async def _notify_missing_shift(kind: ShiftKind):
    db = SessionLocal()
    try:
        today = date.today()
        active_stores = list(db.scalars(select(Store).where(Store.is_active.is_(True))).all())
        for store in active_stores:
            exists = db.scalar(select(ShiftReport.id).where(
                ShiftReport.store_id == store.id,
                ShiftReport.report_date == today,
                ShiftReport.kind == kind,
                ShiftReport.finalized.is_(True),
            ))
            if exists:
                continue
            manager_ids = list(db.scalars(select(UserStore.user_id).where(UserStore.store_id == store.id)).all())
            managers = list(db.scalars(select(User).where(
                User.id.in_(manager_ids or [-1]),
                User.status == UserStatus.active,
                User.role.in_([UserRole.manager, UserRole.operations_director, UserRole.admin]),
            )).all())
            for manager in managers:
                await send_message(manager.telegram_id, f"⚠️ {store.name}: пересменка {kind.value} не сдана вовремя.")
    finally:
        db.close()


def start_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler:
        return _scheduler
    tz = ZoneInfo(settings.timezone)
    scheduler = AsyncIOScheduler(timezone=tz)
    scheduler.add_job(_notify_missing_shift, "cron", hour=8, minute=10, args=[ShiftKind.morning], id="morning_shift_missing")
    scheduler.add_job(_notify_missing_shift, "cron", hour=20, minute=10, args=[ShiftKind.evening], id="evening_shift_missing")
    scheduler.start()
    _scheduler = scheduler
    log.info("Scheduler started for %s", settings.timezone)
    return scheduler
