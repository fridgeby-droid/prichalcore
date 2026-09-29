# Performance notes — v1.7.11

## Почему v1.7.10 могла ощущаться медленной

В v1.7.10 первый экран создавал несколько независимых сетевых запросов, а backend повторно читал одну и ту же матрицу permissions и часть связанных сущностей. Главная также рассчитывала значения всех доступных виджетов, даже если пользователь удалил их со своего рабочего стола. Отдельные блоки использовали N+1 SQL: один запрос на список + по одному запросу на каждую статью или магазин.

## Изменения v1.7.11

### Frontend

1. `index.html` разделён на versioned CSS/JS assets.
2. JS/CSS получают годовой immutable cache; HTML остаётся `no-store`, поэтому deploy новой версии не вызывает проблему «старого Telegram-кеша».
3. Включён gzip.
4. SortableJS загружается лениво только для настройки виджетов.
5. Главная получает профиль и бейджи из уже выполняемого `/api/dashboard/ui`.
6. Профиль не подгружается при каждом рендере верхней панели.
7. Повторный запрос бейджей в течение 5 секунд подавляется; мутации выполняют forced refresh.
8. Фото профиля кешируется как object URL до закрытия MiniApp.

### Backend / SQL

1. Session + User — один JOIN на каждом авторизованном API-запросе.
2. Все RolePermission текущей роли — один SELECT на request.
3. Assigned stores/current employee/active store ids — request-local cache.
4. Required Knowledge использует LEFT JOIN с KnowledgeAcknowledgement вместо SELECT на каждую статью.
5. Weekly inspections используют GROUP BY по всем магазинам вместо COUNT на каждый магазин.
6. Task/Test card titles загружаются JOIN'ами.
7. Home вычисляет values только для `layout` пользователя.
8. Добавлены hot-path composite indexes для PostgreSQL.

## Диагностика production

Каждый ответ содержит заголовок:

```text
X-Process-Time-ms: 42.7
```

API, которые заняли >=150 ms серверного времени, логируются:

```text
Slow API 311.2 ms GET /api/dashboard/ui
```

Это позволяет отделить задержку backend/PostgreSQL от Telegram WebView и пользовательской сети.

## Regression test

`scripts/performance_smoke_test.py` создаёт отдельную SQLite-базу, 12 магазинов и 24 обязательные статьи. Тест проверяет, что dashboard не возвращается к линейным N+1 запросам.
