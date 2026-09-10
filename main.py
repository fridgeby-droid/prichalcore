from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import APP_ENV, AUTO_SET_WEBHOOK, ENABLE_SCHEDULER, LOG_LEVEL, PORT, TELEGRAM_WEBHOOK_SECRET
from app.db.database import init_db, db_health
from app.main_app import register_routers
from app.services.telegram import set_webhook
from app.services.telegram_webhook import handle_update
from app.services.scheduler import start_scheduler, stop_scheduler

logging.basicConfig(level=getattr(logging, LOG_LEVEL.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log=logging.getLogger("prichal-core")
BASE=Path(__file__).resolve().parent

@asynccontextmanager
async def lifespan(app:FastAPI):
    init_db()
    if AUTO_SET_WEBHOOK:
        try:log.info("Telegram webhook: %s",set_webhook())
        except Exception as e:log.exception("Webhook setup failed: %s",e)
    if ENABLE_SCHEDULER:
        try:start_scheduler();log.info("Scheduler started")
        except Exception as e:log.exception("Scheduler start failed: %s",e)
    yield
    stop_scheduler()

app=FastAPI(title="Причал Core",version="1.4.0",lifespan=lifespan)
register_routers(app)
app.mount("/static",StaticFiles(directory=BASE/"miniapp"),name="static")

@app.get("/")
def root():return {"service":"prichal-core","version":"1.4.0","miniapp":"/miniapp"}

@app.get("/health")
def health():return {"status":"ok","service":"prichal-core","version":"1.4.0","environment":APP_ENV}

@app.get("/db-health")
def database_health():
    try:return {"status":"ok","database":"postgresql",**db_health()}
    except Exception as e:return JSONResponse(status_code=503,content={"status":"error","detail":str(e)})

@app.get("/miniapp")
def miniapp():return FileResponse(BASE/"miniapp"/"index.html")

@app.get("/setwebhook")
def webhook_setup():
    try:return set_webhook()
    except Exception as e:raise HTTPException(500,str(e))

@app.post("/telegram/webhook")
async def telegram_webhook(request:Request,x_telegram_bot_api_secret_token:str|None=Header(default=None)):
    if TELEGRAM_WEBHOOK_SECRET and x_telegram_bot_api_secret_token!=TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(403,"Invalid webhook secret")
    update=await request.json()
    try:handle_update(update)
    except Exception:log.exception("Telegram update failed")
    return {"ok":True}

if __name__=="__main__":
    import uvicorn
    uvicorn.run("main:app",host="0.0.0.0",port=PORT,reload=False)
