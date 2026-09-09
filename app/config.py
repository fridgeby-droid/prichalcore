from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    bot_token: str = os.getenv("BOT_TOKEN", "")
    webhook_secret: str = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    bootstrap_admin_telegram_id: int = _int("BOOTSTRAP_ADMIN_TELEGRAM_ID", 0)

    app_url: str = os.getenv("APP_URL", "").rstrip("/")
    port: int = _int("PORT", 5000)
    timezone: str = os.getenv("APP_TIMEZONE", "Asia/Yekaterinburg")
    app_env: str = os.getenv("APP_ENV", "development")
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./prichal_core_dev.db")
    database_url_direct: str = os.getenv("DATABASE_URL_DIRECT", "")

    auto_set_webhook: bool = _bool("AUTO_SET_WEBHOOK", True)
    enable_scheduler: bool = _bool("ENABLE_SCHEDULER", False)
    session_ttl_hours: int = _int("SESSION_TTL_HOURS", 24)
    telegram_auth_max_age_seconds: int = _int("TELEGRAM_AUTH_MAX_AGE_SECONDS", 86400)

    internal_api_secret: str = os.getenv("INTERNAL_API_SECRET", "")
    ai_agent_url: str = os.getenv("AI_AGENT_URL", "").rstrip("/")
    ai_agent_secret: str = os.getenv("AI_AGENT_SECRET", "")


settings = Settings()
