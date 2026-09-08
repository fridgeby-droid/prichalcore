import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
APP_URL = os.getenv("APP_URL", "https://prichalcore.bothost.tech").rstrip("/")
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DATABASE_URL_DIRECT = os.getenv("DATABASE_URL_DIRECT", "").strip()
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
TELEGRAM_AUTH_MAX_AGE_SECONDS = int(os.getenv("TELEGRAM_AUTH_MAX_AGE_SECONDS", "86400"))
APP_ENV = os.getenv("APP_ENV", "production")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
PORT = int(os.getenv("PORT", "3000"))
WEBHOOK_URL = f"{APP_URL}/webhook"
MINIAPP_URL = f"{APP_URL}/miniapp"
