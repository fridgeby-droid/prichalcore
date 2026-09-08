import logging

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from sqlalchemy import text

from config import (
    APP_ENV,
    BOT_TOKEN,
    DATABASE_URL,
    LOG_LEVEL,
    MINIAPP_URL,
    PORT,
    TELEGRAM_AUTH_MAX_AGE_SECONDS,
    TELEGRAM_WEBHOOK_SECRET,
)
from core.telegram_api import send_start, set_webhook
from core.telegram_auth import validate_init_data
from database.db import get_engine

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
logger = logging.getLogger("prichal-core")

app = Flask(__name__)
CORS(app)


@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "prichal-core",
        "environment": APP_ENV,
    })


@app.get("/api/v1/db-health")
def db_health():
    if not DATABASE_URL:
        return jsonify({"status": "not_configured", "database": "postgresql"}), 503
    try:
        with get_engine().connect() as conn:
            probe = conn.execute(text("SELECT 1")).scalar_one()
            database_name = conn.execute(text("SELECT current_database()" )).scalar_one()
        return jsonify({
            "status": "ok",
            "database": "postgresql",
            "database_name": database_name,
            "probe": probe,
        })
    except Exception as exc:
        logger.exception("Database health check failed")
        return jsonify({
            "status": "error",
            "database": "postgresql",
            "error": exc.__class__.__name__,
        }), 503


@app.post("/webhook")
def webhook():
    if TELEGRAM_WEBHOOK_SECRET:
        received = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if received != TELEGRAM_WEBHOOK_SECRET:
            return jsonify({"ok": False}), 403

    update = request.get_json(silent=True) or {}
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    text_value = (message.get("text") or "").strip()
    chat_id = chat.get("id")

    if chat_id and text_value.startswith("/start"):
        try:
            send_start(chat_id)
        except Exception:
            logger.exception("Failed to send /start response")

    return jsonify({"ok": True})


@app.route("/setwebhook", methods=["GET", "POST"])
def configure_webhook():
    try:
        result = set_webhook()
        return jsonify(result)
    except Exception as exc:
        logger.exception("setWebhook failed")
        return jsonify({"ok": False, "error": exc.__class__.__name__}), 500


@app.get("/api/v1/me")
def me():
    init_data = request.headers.get("X-Telegram-Init-Data", "") or request.args.get("initData", "")
    user = validate_init_data(init_data, BOT_TOKEN, TELEGRAM_AUTH_MAX_AGE_SECONDS)
    if not user:
        return jsonify({"authenticated": False}), 401
    return jsonify({
        "authenticated": True,
        "telegram_user": {
            "id": user.get("id"),
            "first_name": user.get("first_name"),
            "last_name": user.get("last_name"),
            "username": user.get("username"),
        },
    })


@app.get("/miniapp")
@app.get("/miniapp/")
def miniapp_index():
    return send_from_directory("miniapp", "index.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
