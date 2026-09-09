from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text, select

from .api.auth_api import router as auth_router
from .api.catalogs import router as catalogs_router
from .api.dashboard import router as dashboard_router
from .api.operations import router as operations_router
from .config import settings
from .db import Base, SessionLocal, direct_engine, engine
from .models import PendingPhotoUpload, Photo, Store, User
from .services import active_photo_request
from .telegram import send_message, set_webhook

logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("prichal-core")

ROOT = Path(__file__).resolve().parent.parent
MINIAPP = ROOT / "miniapp"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initial clean build: create missing tables automatically. Later schema changes should use Alembic.
    Base.metadata.create_all(bind=direct_engine)
    if settings.auto_set_webhook and settings.bot_token and settings.app_url:
        result = await set_webhook()
        log.info("Telegram webhook: %s", result)
    if settings.enable_scheduler:
        from .scheduler import start_scheduler
        start_scheduler()
    yield


app = FastAPI(title="Prichal Core API", version="0.1.0", docs_url="/docs" if settings.app_env != "production" else None, lifespan=lifespan)
app.include_router(auth_router)
app.include_router(catalogs_router)
app.include_router(operations_router)
app.include_router(dashboard_router)
app.mount("/miniapp/assets", StaticFiles(directory=MINIAPP), name="miniapp-assets")


@app.get("/")
def root():
    return {"service": "prichal-core", "status": "ok", "miniapp": "/miniapp"}


@app.get("/health")
def health():
    return {"status": "ok", "service": "prichal-core", "version": "0.1.0", "time": datetime.now(timezone.utc).isoformat()}


@app.get("/db-health")
def db_health():
    with engine.connect() as conn:
        value = conn.execute(text("SELECT 1")).scalar_one()
    return {"status": "ok", "database": "postgresql" if settings.database_url.startswith("postgres") else "sqlite-dev", "probe": value}


@app.get("/miniapp")
def miniapp():
    return FileResponse(MINIAPP / "index.html")


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    if settings.webhook_secret and x_telegram_bot_api_secret_token != settings.webhook_secret:
        raise HTTPException(403, "Invalid Telegram webhook secret")
    update = await request.json()
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    from_user = message.get("from") or {}
    chat_id = chat.get("id")
    telegram_id = from_user.get("id")
    text_value = (message.get("text") or "").strip()

    if not chat_id or not telegram_id:
        return {"ok": True}

    if text_value.startswith("/start"):
        markup = {
            "inline_keyboard": [[{"text": "⚓ Открыть Причал Core", "web_app": {"url": f"{settings.app_url}/miniapp"}}]]
        }
        await send_message(chat_id, "⚓ Причал Core\nЕдиный рабочий центр сети.", markup)
        return {"ok": True}

    if text_value.startswith("/whoami"):
        await send_message(chat_id, f"Ваш Telegram ID: {telegram_id}")
        return {"ok": True}

    photos = message.get("photo") or []
    if photos:
        db = SessionLocal()
        try:
            user = db.scalar(select(User).where(User.telegram_id == telegram_id))
            if not user:
                await send_message(chat_id, "Сначала откройте MiniApp и авторизуйтесь.")
                return {"ok": True}
            pending = active_photo_request(db, user.id)
            if not pending:
                await send_message(chat_id, "Сейчас нет активного запроса фото. Откройте нужный раздел в MiniApp и нажмите «Добавить фото».")
                return {"ok": True}
            best = photos[-1]
            store_id = None
            from .models import ShiftReport, Inspection, Task, Incident, CashCollection
            entity_map = {
                "shift_report": ShiftReport,
                "inspection": Inspection,
                "task": Task,
                "incident": Incident,
                "cash_collection": CashCollection,
            }
            model = entity_map.get(pending.entity_type)
            if model:
                entity = db.get(model, pending.entity_id)
                store_id = getattr(entity, "store_id", None) if entity else None
            row = Photo(
                entity_type=pending.entity_type,
                entity_id=pending.entity_id,
                store_id=store_id,
                uploaded_by=user.id,
                field_key=pending.field_key,
                telegram_file_id=best["file_id"],
                telegram_file_unique_id=best["file_unique_id"],
                telegram_chat_id=chat_id,
                telegram_message_id=message["message_id"],
                width=best.get("width"),
                height=best.get("height"),
                file_size=best.get("file_size"),
            )
            db.add(row)
            pending.status = "completed"
            db.commit()
            await send_message(chat_id, "✅ Фото сохранено и привязано к записи в Причал Core.")
        finally:
            db.close()
        return {"ok": True}

    return {"ok": True}


@app.exception_handler(Exception)
async def unhandled_exception(_: Request, exc: Exception):
    log.exception("Unhandled error")
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})
