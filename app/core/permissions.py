from __future__ import annotations

ACCESS_LEVELS=["hidden","view","edit"]
DATA_SCOPES=["own","stores","network"]

PERMISSION_GROUPS={
"Главная / Виджеты":[
("dashboard.next_shift","Следующая смена"),("dashboard.my_tasks","Мои задачи"),("dashboard.overdue_tasks","Просроченные задачи"),("dashboard.required_knowledge","Обязательные материалы"),("dashboard.assigned_tests","Назначенные тесты"),("dashboard.handover_due","Текущая пересменка"),("dashboard.new_orders","Новые заявки"),("dashboard.handover_review","Пересменки на проверке"),("dashboard.schedule_errors","Ошибки графика"),("dashboard.inspections_week","Проверки недели"),("dashboard.my_stores","Мои магазины"),("dashboard.team_learning","Обучение команды"),("dashboard.revenue_month","Выручка за месяц — после Saby"),("dashboard.avg_check","Средний чек — после Saby"),("dashboard.plan_fact","План / факт — после Saby")],
"Заявки":[
("orders.list","Просмотр списка заявок"),("orders.create","Создание заявки"),("orders.edit_new","Редактирование новой заявки"),("orders.accept","Принятие заявки"),("orders.cancel","Отмена заявки"),("orders.delete","Удаление заявки"),("orders.resend","Повторная Telegram-отправка"),("orders.schedule_view","Просмотр графика заявок"),("orders.schedule_edit","Редактирование графика заявок"),
("orders.stores_create","Магазины — создание"),("orders.stores_edit","Магазины — изменение"),("orders.stores_disable","Магазины — отключение"),("orders.stores_delete","Магазины — удаление"),("orders.suppliers_create","Поставщики — создание"),("orders.suppliers_edit","Поставщики — изменение"),("orders.suppliers_disable","Поставщики — отключение"),("orders.suppliers_delete","Поставщики — удаление"),("orders.products_create","Товары — создание"),("orders.products_edit","Товары — изменение"),("orders.products_disable","Товары — отключение"),("orders.products_delete","Товары — удаление"),("orders.categories_manage","Категории"),("orders.units_manage","Единицы измерения"),("orders.limits_manage","Лимиты"),("orders.prices_manage","Прайс-листы"),("orders.import","Массовый импорт товаров")],
"График и подмены":[("schedule.my","Мой график"),("schedule.stores","График магазинов"),("schedule.edit","Редактор смен"),("schedule.past_edit","Изменение прошлых смен"),("schedule.absences","Отпуска / больничные / обучение"),("schedule.substitutions","Подмены"),("schedule.errors","Ошибки графика"),("schedule.history","История графика")],
"Пересменки":[("shifts.submit","Сдать пересменку"),("shifts.history","История"),("shifts.control","Контроль"),("shifts.accept","Принять"),("shifts.accept_remarks","Принять с замечаниями"),("shifts.reject","Не принять"),("shifts.forms","Редактор форм")],
"Задачи":[("tasks.my","Мои задачи"),("tasks.create","Создание задачи"),("tasks.assign_one","Назначить одному"),("tasks.assign_many","Назначить нескольким"),("tasks.assign_store","Назначить магазину"),("tasks.edit_before_start","Изменить до начала"),("tasks.control","Контроль"),("tasks.accept","Принять выполнение"),("tasks.reject","Вернуть на доработку"),("tasks.cancel","Отменить"),("tasks.history","История")],
"Проверки":[("inspections.conduct","Провести проверку"),("inspections.control","Контроль"),("inspections.history","История"),("inspections.violation_close","Закрытие нарушения"),("inspections.task_from_violation","Создание задачи из нарушения"),("inspections.forms","Редактор форм")],
"Сотрудники":[("employees.view","Просмотр сотрудников"),("employees.create","Создание"),("employees.edit","Редактирование"),("employees.deactivate","Увольнение / деактивация"),("employees.assign_stores","Закрепление магазинов"),("employees.primary_store","Основной магазин"),("employees.telegram_link","Связь Telegram"),("employees.contacts","Контактные данные"),("employees.other_profiles","Личные страницы других сотрудников")],
"База знаний":[("knowledge.read","Чтение"),("knowledge.create","Создание статьи"),("knowledge.edit","Редактирование статьи"),("knowledge.publish","Публикация"),("knowledge.archive","Архив"),("knowledge.sections","Разделы / подразделы"),("knowledge.access","Настройка доступа"),("knowledge.required","Обязательное ознакомление")],
"Тестирование":[("testing.take","Прохождение тестов"),("testing.catalog","Каталог"),("testing.create","Создание теста"),("testing.questions","Вопросы"),("testing.publish","Публикация"),("testing.assign","Назначение"),("testing.control","Контроль результатов"),("testing.retry","Разрешить повторную попытку")],
"Telegram":[("telegram.groups","Группы"),("telegram.routes","Маршруты"),("telegram.templates","Шаблоны сообщений"),("telegram.logs","Журнал доставки"),("telegram.personal","Личные уведомления")],
"Система":[("system.settings","Глобальные настройки"),("system.modules","Отключённые модули"),("system.audit","Системный журнал")],
}

def catalog():
    return [{"group":g,"items":[{"key":k,"label":l} for k,l in items]} for g,items in PERMISSION_GROUPS.items()]

def all_keys():
    return [k for items in PERMISSION_GROUPS.values() for k,_ in items]
