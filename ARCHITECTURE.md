# Архитектура Причал Core

```text
Telegram users
      │
      ├── Telegram Bot (/start, фото, уведомления)
      │
      └── Telegram MiniApp
               │
               ▼
         FastAPI / Python
               │
        ┌──────┴────────┐
        ▼               ▼
 Neon PostgreSQL    Telegram files
        │
        ├── справочники
        ├── пользователи/роли
        ├── заявки
        ├── пересменки
        ├── проверки/нарушения
        ├── задачи
        ├── инкассации
        ├── наставничество
        ├── план/факт
        ├── рейтинги
        └── рассылки
               │
               ▼
       внешний AI Agent (позже)
```

## Роли

- `seller` — заявки, пересменки, свои задачи
- `mentor` — функции продавца + наставничество
- `manager` — свои точки, проверки, задачи, инкассации, команда
- `operations_director` — сеть, управление и контроль
- `leader` — сеть, управление и контроль
- `admin` — полный доступ + настройка справочников и форм

## Модель доступа к магазинам

`users` ↔ `user_stores` ↔ `stores`

Продавец может быть привязан к нескольким точкам. Управляющий — к нескольким. Операционный директор, руководитель и администратор видят всю активную сеть.

## Конструкторы форм

Пересменка:

`shift_templates` → `shift_template_fields` → `shift_reports` → `shift_report_values`

Каждый магазин может иметь собственные утренние/вечерние формы.

Проверка:

`inspection_templates` → `inspection_template_fields` → `inspections` → `inspection_values`

Булево поле может автоматически создавать `violation`, если управляющий ответил «Нет».

## Рейтинг

Стандартные веса:

- план/факт — 40%
- проверки — 20%
- нарушения — 15%
- задачи — 15%
- пересменки — 10%

Веса хранятся в `app_settings` (`rating_weights`) и могут быть изменены позднее без изменения схемы.

## Следующие интеграции

### Saby

`PlanFact.source = saby`, плюс отдельный сервис импорта продаж, продавцов, чеков и KPI.

### AI Agent

Core остаётся системой учёта и действий. AI — отдельный сервис анализа. Интеграция идёт через API, а не через внедрение AI-логики в каждую форму MiniApp.

### Подмены

Позже добавляются `employee_shifts`, `substitution_requests`, `substitution_responses` и алгоритм поиска свободных сотрудников.

## Work schedule module (v1.3)

`WorkShiftAssignment` stores one employee assignment per date. Store/date/shift/slot is unique. Day shifts have slot 1; night shifts have slots 1 and 2.

`WorkAbsence` stores date ranges for day off, vacation, sick leave and training. Existing shifts are not silently removed when an absence is entered; conflicts surface through the errors endpoint.

`WorkScheduleChange` is an audit log for schedule/substitution changes.

`WorkSubstitution` stores replacement workflow records. Candidate selection uses store binding, current schedule, absence data and back-to-back shift checks.


## Employee model (v1.4)

Telegram authentication and HR records are separated:

`users` — access/login account.

`employees` — real employee card used by schedules and substitutions.

`employee_stores` — many-to-many binding between employees and stores.

`employees.user_id` is nullable and unique. An employee can exist before they open the bot. `employees.telegram_id` can be filled in advance; when that Telegram account logs in, Core links it automatically.

Work schedule relations now use `employee_id`:

- `work_shift_assignments.employee_id`
- `work_absences.employee_id`
- `work_schedule_changes.employee_id`
- `work_substitutions.absent_employee_id`
- `work_substitutions.replacement_employee_id`

Legacy `user_id` columns from v1.3 remain nullable for backward compatibility and migration only.


## Handover review workflow (v1.5)

Пересменка теперь связана с фактической записью `work_shift_assignments`.

```text
WorkShiftAssignment
        │
        ▼
ShiftReport (draft)
        │ продавец отправляет
        ▼
ShiftReport (review)
        │ управляющий проверяет
        ├── accepted
        ├── accepted_with_remarks
        └── rejected ──► исправление продавцом ──► новая revision ──► review
```

`shift_review_remarks` хранит активные замечания к конкретному `shift_template_field` или общее замечание.

`shift_review_events` хранит историю решений и JSON-снимок каждой отправленной/проверенной ревизии, поэтому исправления не уничтожают предыдущую историю.

Фото поля формы хранятся в Telegram. В таблице `photos` они связываются через `entity_type = shift_report_field` и `entity_id = shift_report_values.id`.

Использованная форма не переписывается: при редактировании Core создаёт новую версию `shift_template`, а старую деактивирует. Исторические отчёты продолжают ссылаться на старые поля.

## Tasks v2

Новые таблицы:
- `tasks_v2`
- `task_targets`
- `task_assignees`
- `task_checklist_items`
- `task_checklist_progress`
- `task_comments`
- `task_history_v2`
- `task_attachment_requests`
- `task_attachments`

Старая таблица `tasks` оставлена для совместимости существующих данных и больше не используется новым интерфейсом блока «Задачи».
