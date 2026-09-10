import os


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DOMAIN = os.getenv("DOMAIN", "").strip()
APP_URL = os.getenv("APP_URL", "").strip() or (f"https://{DOMAIN}" if DOMAIN else "")
PORT = _int("PORT", 5000)
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
BOOTSTRAP_ADMIN_TELEGRAM_ID = os.getenv("BOOTSTRAP_ADMIN_TELEGRAM_ID", "").strip()
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Asia/Yekaterinburg")
APP_ENV = os.getenv("APP_ENV", "production")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
AUTO_SET_WEBHOOK = _bool("AUTO_SET_WEBHOOK", True)
ENABLE_SCHEDULER = _bool("ENABLE_SCHEDULER", True)
SESSION_TTL_HOURS = _int("SESSION_TTL_HOURS", 24)
TELEGRAM_AUTH_MAX_AGE_SECONDS = _int("TELEGRAM_AUTH_MAX_AGE_SECONDS", 86400)
PHOTO_REQUEST_TTL_MINUTES = _int("PHOTO_REQUEST_TTL_MINUTES", 15)
MIN_INSPECTIONS_PER_STORE_PER_WEEK = _int("MIN_INSPECTIONS_PER_STORE_PER_WEEK", 3)
PLAN_ALERT_THRESHOLD = _float("PLAN_ALERT_THRESHOLD", 0.90)
INTERNAL_API_SECRET = os.getenv("INTERNAL_API_SECRET", "").strip()
AI_AGENT_URL = os.getenv("AI_AGENT_URL", "").strip()
AI_AGENT_SECRET = os.getenv("AI_AGENT_SECRET", "").strip()
