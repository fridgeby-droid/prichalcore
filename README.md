# Причал Core

Чистый backend v0.1 для Telegram MiniApp + Flask + Neon PostgreSQL.

## Проверки после деплоя

- `/health`
- `/api/v1/db-health`
- `/setwebhook`
- затем отправить `/start` новому Telegram-боту.

## Миграция БД

После успешного db-health:

```bash
alembic upgrade head
```

После этого в Neon появятся таблицы Core.
