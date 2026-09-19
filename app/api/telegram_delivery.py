from __future__ import annotations

from datetime import datetime
from typing import Any
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import get_current_user
from app.core.permissions import (require_access, require_any_access, role_key, assigned_store_ids, current_employee, assert_store_scope)
from app.db.database import get_db
from app.db.models import (
    Employee, EmployeeStore, EmployeeNotificationPreference, Store, Supplier, Order, ShiftReport, OrderScheduleRule,
    TelegramDeliveryLog, TelegramDestination, TelegramRoute, TelegramMessageTemplate,
)
from app.services.notifications import (
    GROUP_EVENT_NAMES, PERSONAL_EVENT_NAMES, send_handover_accepted,
    send_order_accepted,
)
from app.services.telegram import send_message
from app.services.message_templates import (
    EVENT_TEMPLATE_SPECS, build_reply_markup, render_body, reset_template,
    sample_values, serialize_template, validate_template,
)

router = APIRouter(prefix="/api/telegram-delivery", tags=["telegram-delivery"])
def _require_admin(user):
    if role_key(user)!="admin": raise HTTPException(403,"Конструктор шаблонов доступен только администратору")

def _employee_allowed(db:Session,user,employee_id:int,key:str,minimum:str="view") -> bool:
    p=require_access(db,user,key,minimum)
    if p["data_scope"]=="network": return True
    me=current_employee(db,user)
    if p["data_scope"]=="own": return bool(me and me.id==employee_id)
    stores=set(assigned_store_ids(db,user)); es=set(db.scalars(select(EmployeeStore.store_id).where(EmployeeStore.employee_id==employee_id)).all())
    return bool(stores & es)


def _supplier_ids_for_stores(db: Session, store_ids: set[int]) -> set[int]:
    if not store_ids:
        return set()
    ids=set(db.scalars(select(OrderScheduleRule.supplier_id).where(OrderScheduleRule.store_id.in_(store_ids))).all())
    ids.update(db.scalars(select(Order.supplier_id).where(Order.store_id.in_(store_ids))).all())
    return ids


def _route_matches_scope(db: Session, user, route: TelegramRoute, scope: str) -> bool:
    if scope=="network":
        return True
    if scope=="own":
        return route.created_by == user.id
    stores=set(assigned_store_ids(db,user))
    if route.target_type=="store":
        return route.target_id in stores
    if route.target_type=="supplier":
        return route.target_id in _supplier_ids_for_stores(db,stores)
    return False


def _route_allowed(db: Session, user, route: TelegramRoute, key: str="telegram.routes", minimum: str="view") -> bool:
    p=require_access(db,user,key,minimum)
    return _route_matches_scope(db,user,route,p["data_scope"])


def _assert_route_target_scope(db: Session, user, target_type: str, target_id: int, key: str="telegram.routes", minimum: str="edit"):
    p=require_access(db,user,key,minimum)
    if p["data_scope"]=="network":
        return
    stores=set(assigned_store_ids(db,user))
    if target_type=="store" and target_id in stores:
        return
    if target_type=="supplier" and target_id in _supplier_ids_for_stores(db,stores):
        return
    raise HTTPException(403,"Нет доступа к объекту маршрута")


def _destination_allowed(db: Session, user, dest: TelegramDestination, key: str="telegram.groups", minimum: str="view") -> bool:
    p=require_access(db,user,key,minimum)
    if p["data_scope"]=="network":
        return True
    if dest.registered_by_user_id==user.id:
        return True
    routes=list(db.scalars(select(TelegramRoute).where(TelegramRoute.destination_id==dest.id)).all())
    return any(_route_matches_scope(db,user,r,p["data_scope"]) for r in routes)


def _log_allowed(db: Session, user, log: TelegramDeliveryLog, key: str="telegram.logs") -> bool:
    p=require_access(db,user,key,"view")
    if p["data_scope"]=="network":
        return True
    me=current_employee(db,user)
    if p["data_scope"]=="own":
        return bool(me and log.recipient_employee_id==me.id)
    stores=set(assigned_store_ids(db,user))
    if log.recipient_employee_id:
        es=set(db.scalars(select(EmployeeStore.store_id).where(EmployeeStore.employee_id==log.recipient_employee_id)).all())
        return bool(stores & es)
    if log.entity_type=="order":
        obj=db.get(Order,log.entity_id); return bool(obj and obj.store_id in stores)
    if log.entity_type=="shift_report":
        obj=db.get(ShiftReport,log.entity_id); return bool(obj and obj.store_id in stores)
    return False


def _destination_dict(x: TelegramDestination) -> dict[str, Any]:
    return {
        "id": x.id, "chat_id": x.chat_id, "chat_type": x.chat_type,
        "title": x.title, "username": x.username, "active": x.active,
        "registered_at": x.registered_at,
    }


def _route_dict(db: Session, x: TelegramRoute) -> dict[str, Any]:
    d = db.get(TelegramDestination, x.destination_id)
    target_name = f"#{x.target_id}"
    if x.target_type == "supplier":
        obj = db.get(Supplier, x.target_id)
        if obj: target_name = obj.name
    elif x.target_type == "store":
        obj = db.get(Store, x.target_id)
        if obj: target_name = obj.name
    return {
        "id": x.id, "event_type": x.event_type,
        "event_name": GROUP_EVENT_NAMES.get(x.event_type, x.event_type),
        "target_type": x.target_type, "target_id": x.target_id,
        "target_name": target_name, "destination_id": x.destination_id,
        "destination_title": d.title if d else None,
        "destination_chat_id": d.chat_id if d else None,
        "active": x.active,
    }


def _scoped_supplier_rows(db: Session, user, permission: dict) -> list[dict]:
    q=select(Supplier).where(Supplier.active.is_(True))
    if permission["data_scope"]!="network":
        allowed=_supplier_ids_for_stores(db,set(assigned_store_ids(db,user)))
        q=q.where(Supplier.id.in_(allowed or {-1}))
    return [{"id":x.id,"name":x.name} for x in db.scalars(q.order_by(Supplier.name)).all()]


@router.get("/meta")
def meta(user=Depends(get_current_user), db: Session = Depends(get_db)):
    p=require_any_access(db,user,["telegram.groups","telegram.routes","telegram.logs","telegram.personal"])
    sq=select(Store).where(Store.active.is_(True)).order_by(Store.name)
    if p["data_scope"]!="network": sq=sq.where(Store.id.in_(assigned_store_ids(db,user) or [-1]))
    return {
        "group_events": GROUP_EVENT_NAMES,
        "personal_events": PERSONAL_EVENT_NAMES,
        "stores": [{"id": x.id, "name": x.name} for x in db.scalars(sq).all()],
        "suppliers": _scoped_supplier_rows(db,user,p),
    }


@router.get("/destinations")
def destinations(user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_access(db,user,"telegram.groups")
    rows=db.scalars(select(TelegramDestination).order_by(TelegramDestination.active.desc(), TelegramDestination.title)).all()
    return [_destination_dict(x) for x in rows if _destination_allowed(db,user,x)]


@router.patch("/destinations/{destination_id}")
def update_destination(destination_id: int, payload: dict, user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_access(db,user,"telegram.groups","edit")
    x = db.get(TelegramDestination, destination_id)
    if not x: raise HTTPException(404, "Telegram-группа не найдена")
    if not _destination_allowed(db,user,x,"telegram.groups","edit"): raise HTTPException(403,"Нет доступа к Telegram-группе")
    if "title" in payload: x.title = str(payload["title"]).strip() or x.title
    if "active" in payload: x.active = bool(payload["active"])
    db.commit(); db.refresh(x)
    return _destination_dict(x)


@router.post("/destinations/{destination_id}/test")
def test_destination(destination_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_access(db,user,"telegram.groups","edit")
    x = db.get(TelegramDestination, destination_id)
    if not x or not x.active: raise HTTPException(404, "Telegram-группа недоступна")
    if not _destination_allowed(db,user,x,"telegram.groups","edit"): raise HTTPException(403,"Нет доступа к Telegram-группе")
    try:
        data = send_message(int(x.chat_id), "⚓ <b>Причал Core</b>\nТестовое сообщение. Группа подключена корректно.")
        return {"ok": True, "message_id": (data.get("result") or {}).get("message_id")}
    except Exception as e:
        raise HTTPException(502, f"Не удалось отправить сообщение: {e}")


class RouteIn(BaseModel):
    event_type: str
    target_type: str
    target_id: int
    destination_id: int
    active: bool = True


@router.get("/routes")
def routes(user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_access(db,user,"telegram.routes")
    rows=db.scalars(select(TelegramRoute).order_by(TelegramRoute.event_type, TelegramRoute.target_type, TelegramRoute.target_id)).all()
    return [_route_dict(db,x) for x in rows if _route_allowed(db,user,x)]


@router.post("/routes")
def save_route(payload: RouteIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_access(db,user,"telegram.routes","edit")
    if payload.event_type not in GROUP_EVENT_NAMES:
        raise HTTPException(400, "Неизвестный тип события")
    expected = "supplier" if payload.event_type == "order_accepted" else "store"
    if payload.target_type != expected:
        raise HTTPException(400, f"Для этого события нужен объект типа {expected}")
    _assert_route_target_scope(db,user,payload.target_type,payload.target_id,"telegram.routes","edit")
    destination=db.get(TelegramDestination, payload.destination_id)
    if not destination:
        raise HTTPException(400, "Telegram-группа не найдена")
    if not _destination_allowed(db,user,destination,"telegram.groups","view"):
        raise HTTPException(403,"Нет доступа к Telegram-группе")
    if payload.target_type == "supplier" and not db.get(Supplier, payload.target_id):
        raise HTTPException(400, "Поставщик не найден")
    if payload.target_type == "store" and not db.get(Store, payload.target_id):
        raise HTTPException(400, "Магазин не найден")
    x = db.scalar(select(TelegramRoute).where(
        TelegramRoute.event_type == payload.event_type,
        TelegramRoute.target_type == payload.target_type,
        TelegramRoute.target_id == payload.target_id,
    ))
    if not x:
        x = TelegramRoute(event_type=payload.event_type, target_type=payload.target_type, target_id=payload.target_id, destination_id=payload.destination_id, active=payload.active, created_by=user.id)
        db.add(x)
    else:
        x.destination_id = payload.destination_id; x.active = payload.active
    db.commit(); db.refresh(x)
    return _route_dict(db, x)


@router.delete("/routes/{route_id}")
def delete_route(route_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_access(db,user,"telegram.routes","edit")
    x = db.get(TelegramRoute, route_id)
    if not x: raise HTTPException(404, "Маршрут не найден")
    if not _route_allowed(db,user,x,"telegram.routes","edit"): raise HTTPException(403,"Нет доступа к маршруту")
    db.delete(x); db.commit()
    return {"ok": True}


@router.get("/logs")
def logs(limit: int = 100, status: str | None = None, event_type: str | None = None, user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_access(db,user,"telegram.logs")
    q = select(TelegramDeliveryLog).order_by(TelegramDeliveryLog.created_at.desc()).limit(min(max(limit, 1), 500))
    if status: q = q.where(TelegramDeliveryLog.status == status)
    if event_type: q = q.where(TelegramDeliveryLog.event_type == event_type)
    rows = [x for x in db.scalars(q).all() if _log_allowed(db,user,x,"telegram.logs")]
    return [{
        "id": x.id, "event_type": x.event_type,
        "event_name": GROUP_EVENT_NAMES.get(x.event_type) or PERSONAL_EVENT_NAMES.get(x.event_type) or x.event_type,
        "entity_type": x.entity_type, "entity_id": x.entity_id,
        "recipient_employee_id": x.recipient_employee_id,
        "chat_id": x.chat_id, "status": x.status, "telegram_message_id": x.telegram_message_id,
        "error": x.error, "created_at": x.created_at, "sent_at": x.sent_at,
    } for x in rows]


@router.get("/entity-status/{entity_type}/{entity_id}")
def entity_status(entity_type: str, entity_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_access(db,user,"telegram.logs")
    q = select(TelegramDeliveryLog).where(TelegramDeliveryLog.entity_type == entity_type, TelegramDeliveryLog.entity_id == entity_id).order_by(TelegramDeliveryLog.created_at.desc())
    rows = [x for x in db.scalars(q).all() if _log_allowed(db,user,x,"telegram.logs")]
    return [{"event_type":x.event_type,"status":x.status,"sent_at":x.sent_at,"error":x.error,"message_id":x.telegram_message_id} for x in rows[:20]]


@router.post("/resend/order/{order_id}")
def resend_order(order_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_access(db,user,"orders.resend","edit")
    order=db.get(Order,order_id)
    if not order: raise HTTPException(404,"Заявка не найдена")
    assert_store_scope(db,user,"orders.resend",order.store_id,minimum="edit",own_as_assigned=True)
    result = send_order_accepted(db, order_id, force=True); db.commit(); return result


@router.post("/resend/handover/{report_id}")
def resend_handover(report_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_access(db,user,"telegram.routes","edit")
    require_access(db,user,"shifts.control","edit")
    report=db.get(ShiftReport,report_id)
    if not report: raise HTTPException(404,"Пересменка не найдена")
    assert_store_scope(db,user,"shifts.control",report.store_id,minimum="edit",own_as_assigned=True)
    result = send_handover_accepted(db, report_id, force=True); db.commit(); return result


@router.get("/personal-preferences/{employee_id}")
def get_personal_preferences(employee_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_access(db,user,"telegram.personal")
    if not _employee_allowed(db,user,employee_id,"telegram.personal"): raise HTTPException(403,"Нет доступа к сотруднику")
    if not db.get(Employee, employee_id): raise HTTPException(404, "Сотрудник не найден")
    rows = {x.event_type: x.enabled for x in db.scalars(select(EmployeeNotificationPreference).where(EmployeeNotificationPreference.employee_id == employee_id)).all()}
    return {k: rows.get(k, True) for k in PERSONAL_EVENT_NAMES}


@router.patch("/personal-preferences/{employee_id}")
def set_personal_preferences(employee_id: int, payload: dict[str, bool], user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_access(db,user,"telegram.personal","edit")
    if not _employee_allowed(db,user,employee_id,"telegram.personal","edit"): raise HTTPException(403,"Нет доступа к сотруднику")
    if not db.get(Employee, employee_id): raise HTTPException(404, "Сотрудник не найден")
    for event_type, enabled in payload.items():
        if event_type not in PERSONAL_EVENT_NAMES: continue
        row = db.get(EmployeeNotificationPreference, {"employee_id": employee_id, "event_type": event_type})
        if not row:
            row = EmployeeNotificationPreference(employee_id=employee_id, event_type=event_type, enabled=bool(enabled)); db.add(row)
        else:
            row.enabled = bool(enabled)
    db.commit()
    return {"ok": True}


class TemplateIn(BaseModel):
    name: str
    body_html: str
    active: bool = True
    button_enabled: bool = False
    button_text: str | None = "Открыть Причал Core"
    button_target: str = "miniapp"
    button_url: str | None = None
    send_media: bool = True
    media_order: str = "text_first"
    photo_caption: str | None = None


class TemplatePreviewIn(TemplateIn):
    event_type: str


class TemplateTestIn(TemplatePreviewIn):
    recipient: str = "me"  # me/destination
    destination_id: int | None = None


def _template_payload(payload: TemplateIn) -> dict[str, Any]:
    return {
        "name": payload.name.strip(),
        "body_html": payload.body_html,
        "active": bool(payload.active),
        "button_enabled": bool(payload.button_enabled),
        "button_text": (payload.button_text or "Открыть Причал Core").strip()[:64],
        "button_target": payload.button_target,
        "button_url": (payload.button_url or "").strip() or None,
        "send_media": bool(payload.send_media),
        "media_order": payload.media_order,
        "photo_caption": (payload.photo_caption or "").strip() or None,
    }


def _validate_template_or_400(event_type: str, payload: TemplateIn):
    if event_type not in EVENT_TEMPLATE_SPECS:
        raise HTTPException(404, "Тип события не найден")
    errors = validate_template(
        event_type, payload.body_html,
        button_enabled=payload.button_enabled,
        button_target=payload.button_target,
        button_url=payload.button_url,
        media_order=payload.media_order,
    )
    if payload.photo_caption:
        errors += validate_template(event_type, payload.photo_caption, button_enabled=False)
    if len(payload.body_html) > 12000:
        errors.append("Шаблон слишком длинный")
    if len(payload.photo_caption or "") > 1024:
        errors.append("Подпись к фото не должна превышать 1024 символа")
    if not payload.name.strip():
        errors.append("Укажите название шаблона")
    if errors:
        raise HTTPException(400, "; ".join(dict.fromkeys(errors)))


@router.get("/templates")
def list_templates(user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_admin(user)
    return [serialize_template(db, key) for key in EVENT_TEMPLATE_SPECS]


@router.get("/templates/{event_type}")
def get_template(event_type: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_admin(user)
    if event_type not in EVENT_TEMPLATE_SPECS:
        raise HTTPException(404, "Шаблон не найден")
    return serialize_template(db, event_type)


@router.put("/templates/{event_type}")
def save_template(event_type: str, payload: TemplateIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_admin(user)
    _validate_template_or_400(event_type, payload)
    data = _template_payload(payload)
    row = db.scalar(select(TelegramMessageTemplate).where(TelegramMessageTemplate.event_type == event_type))
    if not row:
        row = TelegramMessageTemplate(event_type=event_type, name=data["name"], body_html=data["body_html"])
        db.add(row)
    for key, value in data.items():
        setattr(row, key, value)
    row.updated_by = user.id
    db.commit(); db.refresh(row)
    return serialize_template(db, event_type)


@router.post("/templates/{event_type}/reset")
def restore_template(event_type: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_admin(user)
    if event_type not in EVENT_TEMPLATE_SPECS:
        raise HTTPException(404, "Шаблон не найден")
    reset_template(db, event_type, user.id)
    db.commit()
    return serialize_template(db, event_type)


@router.post("/templates/preview")
def preview_template(payload: TemplatePreviewIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_admin(user)
    _validate_template_or_400(payload.event_type, payload)
    data = _template_payload(payload)
    values = sample_values(payload.event_type)
    text = render_body(payload.event_type, data["body_html"], values)
    caption = render_body(payload.event_type, data["photo_caption"] or "", values) if data["photo_caption"] else None
    markup = build_reply_markup(data)
    return {"text": text, "html": text, "photo_caption": caption, "reply_markup": markup}


@router.post("/templates/test")
def test_template(payload: TemplateTestIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_admin(user)
    _validate_template_or_400(payload.event_type, payload)
    data = _template_payload(payload)
    text = render_body(payload.event_type, data["body_html"], sample_values(payload.event_type))
    markup = build_reply_markup(data)
    if payload.recipient == "me":
        if not user.telegram_id:
            raise HTTPException(400, "У администратора не указан Telegram ID")
        chat_id = int(user.telegram_id)
    elif payload.recipient == "destination":
        if not payload.destination_id:
            raise HTTPException(400, "Выберите Telegram-группу")
        destination = db.get(TelegramDestination, payload.destination_id)
        if not destination or not destination.active:
            raise HTTPException(404, "Telegram-группа недоступна")
        chat_id = int(destination.chat_id)
    else:
        raise HTTPException(400, "Некорректный получатель")
    try:
        result = send_message(chat_id, text, markup)
        return {"ok": True, "message_id": (result.get("result") or {}).get("message_id")}
    except Exception as exc:
        raise HTTPException(502, f"Не удалось отправить тестовое сообщение: {exc}")
