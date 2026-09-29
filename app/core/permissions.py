from __future__ import annotations

from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

ACCESS_LEVELS = ["hidden", "view", "edit"]
DATA_SCOPES = ["own", "stores", "network"]
ACCESS_RANK = {"hidden": 0, "view": 1, "edit": 2}
SCOPE_RANK = {"own": 0, "stores": 1, "network": 2}

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


def role_key(user) -> str:
    return getattr(user, "role_key", None) or getattr(user, "role", "seller")


def _request_cache(db: Session) -> dict:
    """Per-SQLAlchemy-session cache.

    FastAPI creates one Session per request, so this cache is request-scoped:
    it removes duplicate SQL inside a request without making role changes stale
    across requests.
    """
    return db.info.setdefault("core_request_cache", {})


def permission_map(db: Session, user) -> dict[str, dict[str, str]]:
    """Load the full permission matrix for the current role once per request."""
    from app.db.models import RolePermission

    rk = role_key(user)
    if rk == "admin":
        return {k: {"access_level": "edit", "data_scope": "network"} for k in all_keys()}

    cache = _request_cache(db)
    cache_key = ("permissions", rk)
    if cache_key not in cache:
        rows = db.scalars(select(RolePermission).where(RolePermission.role_key == rk)).all()
        cache[cache_key] = {
            row.permission_key: {
                "access_level": row.access_level if row.access_level in ACCESS_RANK else "hidden",
                "data_scope": row.data_scope if row.data_scope in DATA_SCOPES else "own",
            }
            for row in rows
        }
    return cache[cache_key]


def effective_permission(db: Session, user, key: str) -> dict[str, str]:
    if role_key(user) == "admin":
        return {"access_level": "edit", "data_scope": "network"}
    return permission_map(db, user).get(key, {"access_level": "hidden", "data_scope": "own"})


def has_access(db: Session, user, key: str, minimum: str = "view") -> bool:
    return ACCESS_RANK[effective_permission(db, user, key)["access_level"]] >= ACCESS_RANK[minimum]


def require_access(db: Session, user, key: str, minimum: str = "view") -> dict[str, str]:
    p = effective_permission(db, user, key)
    if ACCESS_RANK[p["access_level"]] < ACCESS_RANK[minimum]:
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    return p


def require_any_access(db: Session, user, keys, minimum: str = "view") -> dict[str, str]:
    candidates=[]
    for key in keys:
        p=effective_permission(db,user,key)
        if ACCESS_RANK[p["access_level"]] >= ACCESS_RANK[minimum]: candidates.append(p)
    if not candidates: raise HTTPException(403,"Недостаточно прав")
    return max(candidates,key=lambda p:(ACCESS_RANK[p["access_level"]],SCOPE_RANK[p["data_scope"]]))


def permission_required(key: str, minimum: str = "view"):
    from app.core.security import get_current_user
    from app.db.database import get_db
    def dependency(user=Depends(get_current_user), db: Session = Depends(get_db)):
        require_access(db,user,key,minimum); return user
    return dependency


def current_employee(db: Session, user):
    from app.db.models import Employee
    cache = _request_cache(db)
    key = ("employee", user.id)
    if key not in cache:
        cache[key] = db.scalar(
            select(Employee).where(Employee.user_id == user.id, Employee.active.is_(True))
        )
    return cache[key]


def assigned_store_ids(db: Session, user) -> list[int]:
    from app.db.models import UserStore, EmployeeStore

    cache = _request_cache(db)
    key = ("assigned_store_ids", user.id)
    if key in cache:
        return cache[key]

    ids = set(db.scalars(select(UserStore.store_id).where(UserStore.user_id == user.id)).all())
    emp = current_employee(db, user)
    if emp:
        ids.update(
            db.scalars(
                select(EmployeeStore.store_id).where(EmployeeStore.employee_id == emp.id)
            ).all()
        )
    value = sorted(ids)
    cache[key] = value
    return value


def all_active_store_ids(db: Session) -> list[int]:
    from app.db.models import Store
    cache = _request_cache(db)
    key = ("active_store_ids",)
    if key not in cache:
        cache[key] = list(db.scalars(select(Store.id).where(Store.active.is_(True))).all())
    return cache[key]


def scope_store_ids(db: Session,user,key:str,*,own_as_assigned:bool=False) -> list[int]:
    p=require_access(db,user,key,"view")
    if p["data_scope"]=="network": return all_active_store_ids(db)
    if p["data_scope"]=="stores" or own_as_assigned: return assigned_store_ids(db,user)
    return []


def assert_store_scope(db:Session,user,key:str,store_id:int,*,minimum:str="view",own_as_assigned:bool=False):
    p=require_access(db,user,key,minimum)
    if p["data_scope"]=="network": return
    if (p["data_scope"]=="stores" or own_as_assigned) and store_id in assigned_store_ids(db,user): return
    raise HTTPException(403,"Нет доступа к магазину")


def assert_own_or_scope(db:Session,user,key:str,*,owner_user_id:int|None=None,employee_id:int|None=None,store_id:int|None=None,minimum:str="view"):
    p=require_access(db,user,key,minimum)
    if p["data_scope"]=="network": return
    if p["data_scope"]=="stores":
        if store_id is not None and store_id in assigned_store_ids(db,user): return
        raise HTTPException(403,"Нет доступа к данным этого магазина")
    if owner_user_id is not None and owner_user_id==user.id:return
    if employee_id is not None:
        emp=current_employee(db,user)
        if emp and emp.id==employee_id:return
    raise HTTPException(403,"Доступны только собственные данные")


def module_guard(module_key: str):
    """FastAPI dependency that keeps frozen modules unreachable even via direct API calls."""
    from app.db.database import get_db
    from app.db.models import AppSetting
    def dependency(db: Session = Depends(get_db)):
        obj=db.get(AppSetting,"module_states")
        states=(obj.value_json or {}) if obj else {}
        if not bool(states.get(module_key,False)):
            raise HTTPException(status_code=404,detail="Модуль отключён")
        return True
    return dependency
