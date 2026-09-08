import requests

from config import BOT_TOKEN, MINIAPP_URL, WEBHOOK_URL, TELEGRAM_WEBHOOK_SECRET

API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else ""


def _post(method: str, payload: dict):
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not configured")
    response = requests.post(f"{API_BASE}/{method}", json=payload, timeout=15)
    response.raise_for_status()
    return response.json()


def send_start(chat_id: int):
    return _post("sendMessage", {
        "chat_id": chat_id,
        "text": "⚓ Причал Core\n\nЕдиный рабочий центр сети.",
        "reply_markup": {
            "inline_keyboard": [[{
                "text": "Открыть Причал Core",
                "web_app": {"url": MINIAPP_URL},
            }]]
        },
    })


def set_webhook():
    payload = {
        "url": WEBHOOK_URL,
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": False,
    }
    if TELEGRAM_WEBHOOK_SECRET:
        payload["secret_token"] = TELEGRAM_WEBHOOK_SECRET
    return _post("setWebhook", payload)
