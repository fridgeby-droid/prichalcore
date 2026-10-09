# Обновление 1.7.15

Связь магазинов и продавцов с Saby через Причал AI: [RELEASE_1.7.15.md](RELEASE_1.7.15.md).

# Обновление 1.7.14

Быстрые переходы и подготовка экранов: [RELEASE_1.7.14.md](RELEASE_1.7.14.md). Полная сборка включает все предыдущие изменения.

# Обновление 1.7.13

Кэш данных Mini App: [RELEASE_1.7.13.md](RELEASE_1.7.13.md). Полная сборка включает 1.7.12.

# Обновление 1.7.12

Описание изменений, развёртывания и проверок: [RELEASE_1.7.12.md](RELEASE_1.7.12.md).

# Причал Core v1.7.11 — Performance Optimization

v1.7.11 построена поверх стабильной v1.7.10. Новых бизнес-модулей нет: версия ускоряет старт MiniApp, Главную, бейджи и типовые обращения к PostgreSQL, сохраняя матрицу прав и текущую бизнес-логику.

## Главное

- MiniApp больше не отдаётся одним HTML-файлом ~340 КБ.
  - `index.html` — только каркас (~0.6 КБ) и всегда без кеша;
  - `app.1.7.11.js` и `app.1.7.11.css` — versioned static assets с `Cache-Control: immutable`;
  - включено gzip-сжатие.
- SortableJS для редактора виджетов больше не блокирует первый запуск: библиотека грузится только при открытии «Настройка Главной».
- При старте Главной убран отдельный запрос `/api/profile/me`: компактный профиль возвращается вместе с `/api/dashboard/ui`.
- Верхняя навигация больше не запрашивает профиль заново при каждом переходе между разделами.
- Аватар кешируется в рамках текущей сессии MiniApp и не скачивается повторно при каждом рендере шапки.
- Повторные запросы бейджей подавлены: результат Главной сразу используется нижним меню; в течение 5 секунд повторный GET не выполняется. После изменений данных бейджи по-прежнему принудительно обновляются сразу.
- Авторизация API оптимизирована: `AuthSession + User` загружаются одним SQL-запросом вместо двух.
- Матрица permissions загружается одним запросом на роль в рамках request и затем используется из request-cache.
- `Employee`, закреплённые магазины и список активных магазинов также кешируются внутри request.
- Главная рассчитывает только те виджеты, которые реально находятся в персональной раскладке пользователя, а не весь каталог.
- Убраны N+1 запросы:
  - обязательные статьи + ознакомления;
  - названия задач на Главной;
  - названия назначенных тестов;
  - недельный прогресс проверок по магазинам.
- Добавлены составные PostgreSQL-индексы для hot-path запросов заявок, задач, пересменок, тестов, проверок, графика, фото и Telegram-журнала.
- Каждый HTTP-ответ содержит `X-Process-Time-ms`. API-запросы дольше 150 мс пишутся в лог как `Slow API`.

## Что не изменилось

- роли и права `hidden / view / edit`;
- scope `own / stores / network`;
- структура бизнес-модулей;
- персональные виджеты и темы;
- логика живых бейджей;
- Telegram-доставка;
- данные существующей PostgreSQL.

## База данных

Сбрасывать PostgreSQL Bothost **не нужно**. При первом запуске v1.7.11 автоматически выполняются `CREATE INDEX IF NOT EXISTS` для новых performance-индексов. Таблицы и пользовательские данные сохраняются.

## Проверки сборки

Перед упаковкой выполнены:

- Python compilation — OK;
- JavaScript syntax — OK;
- SQLite schema smoke-test — OK;
- 74 таблицы — OK;
- Security regression test — OK;
- Performance regression test — OK;
- `/dashboard/ui` на тестовой базе с 12 магазинами и 24 обязательными статьями — 19 SQL statements, без линейного роста по магазинам/статьям;
- versioned static cache headers — OK;
- gzip для JS/CSS — OK.

Ориентировочный размер первой передачи локальных assets при gzip:

- JS: ~66 КБ вместо ~285 КБ;
- CSS: ~12 КБ вместо ~55 КБ;
- `index.html`: ~0.6 КБ.

После первого открытия JS/CSS должны браться из кеша до смены номера версии файла.

## После deploy

Проверьте:

```text
/health
```

Ожидаемая версия:

```json
{
  "status": "ok",
  "service": "prichal-core",
  "version": "1.7.11"
}
```

Затем несколько раз откройте Главную и основные разделы. В логах Bothost строки вида

```text
Slow API 243.7 ms GET /api/...
```

покажут конкретные endpoint'ы, которые ещё требуют оптимизации на реальной PostgreSQL.

Подробности: `PERFORMANCE.md` и `SECURITY.md`.


### v1.7.11.7 — S3 avatar reading
Profile avatar is streamed from private S3 directly by the backend; legacy Telegram images remain supported.


## v1.7.11.8 — private S3 photo read paths
All image endpoints (avatar, shifts, inspections, task attachments, training questions, knowledge photos) read S3 directly after existing authorization checks. No Telegram read fallback for images. No local photos migration. Telegram remains only the upload transport and source identifier for the `stored_objects` mapping. Other media (videos and uploaded documents) are unchanged.
