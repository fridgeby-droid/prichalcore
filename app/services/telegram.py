from __future__ import annotations

import httpx
from app.core.config import BOT_TOKEN, APP_URL, TELEGRAM_WEBHOOK_SECRET

API = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else ""


def _post(method: str, payload: dict):
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not configured")
    with httpx.Client(timeout=20) as client:
        response = client.post(f"{API}/{method}", json=payload)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram error: {data}")
        return data


def send_message(chat_id: int, text: str, reply_markup: dict | None = None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return _post("sendMessage", payload)


def set_webhook():
    if not APP_URL:
        raise RuntimeError("APP_URL/DOMAIN is not configured")
    payload = {"url": f"{APP_URL.rstrip('/')}/telegram/webhook", "allowed_updates": ["message"]}
    if TELEGRAM_WEBHOOK_SECRET:
        payload["secret_token"] = TELEGRAM_WEBHOOK_SECRET
    return _post("setWebhook", payload)


def get_file_path(file_id: str) -> str:
    data = _post("getFile", {"file_id": file_id})
    return data["result"]["file_path"]


def file_download_url(file_path: str) -> str:
    return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"


def miniapp_keyboard():
    return {
        "keyboard": [[{"text": "⚓ Открыть Причал Core", "web_app": {"url": f"{APP_URL.rstrip('/')}/miniapp"}}]],
        "resize_keyboard": True,
    }
