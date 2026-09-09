from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import settings

log = logging.getLogger(__name__)


def api_url(method: str) -> str:
    return f"https://api.telegram.org/bot{settings.bot_token}/{method}"


async def tg_call(method: str, payload: dict[str, Any] | None = None) -> dict:
    if not settings.bot_token:
        return {"ok": False, "description": "BOT_TOKEN missing"}
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(api_url(method), json=payload or {})
        data = response.json()
        if not data.get("ok"):
            log.warning("Telegram %s failed: %s", method, data)
        return data


async def send_message(chat_id: int, text: str, reply_markup: dict | None = None) -> dict:
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return await tg_call("sendMessage", payload)


async def set_webhook() -> dict:
    if not settings.app_url:
        return {"ok": False, "description": "APP_URL missing"}
    payload = {
        "url": f"{settings.app_url}/telegram/webhook",
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": False,
    }
    if settings.webhook_secret:
        payload["secret_token"] = settings.webhook_secret
    return await tg_call("setWebhook", payload)
