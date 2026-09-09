# Причал Core — целевая структура

## Роли

- **seller** — заявки, пересменки, свои задачи, инциденты, подмена.
- **mentor** — seller + наставничество/стажёры.
- **manager** — закреплённые точки, заявки, проверки, задачи, инкассации, сотрудники, аналитика.
- **operations_director** — вся сеть, управление справочниками и управляющими, недельные отчёты.
- **executive** — сеть и аналитика, преимущественно read-only.
- **admin** — полный доступ и настройки.

## Основные домены

1. Identity & Access
2. Stores & Catalogs
3. Supplier Orders
4. Shift Handover
5. Store Inspections
6. Tasks / Execution Control
7. Cash Collection
8. Incidents
9. Employee Substitutions
10. Mentorship & Training
11. Secret Guest / Store Rating
12. Focus Task of Month
13. Store Metrics (Saby)
14. AI Integration
15. Telegram Photo Storage
16. Audit Trail

## Принцип интеграции с AI

Core владеет операционными данными и правами доступа. AI-агент получает только нужный контекст через backend/API или read-only integration. Любые изменения состояния (создание задачи, изменение статуса и т.п.) должны выполняться через контролируемые методы Core, а не прямой произвольной записью AI в PostgreSQL.
