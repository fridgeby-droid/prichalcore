from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import APP_URL
from app.core.permissions import has_access, role_key
from app.db.models import (
    Employee, EmployeeNotificationPreference, EmployeeStore, KnowledgeArticle,
    KnowledgeSection, Order, OrderItem, Photo, Product, ShiftReport, ShiftReportValue,
    ShiftTemplateField, Store, Supplier, TaskV2, TelegramDeliveryLog,
    TelegramDestination, TelegramRoute, TrainingAssignment, TrainingTest, User,
)
from app.services.message_templates import EVENT_TEMPLATE_SPECS, render_event
from app.services.telegram import send_message, send_photo

PERSONAL_EVENT_NAMES = {
    "schedule_created": "Новая смена",
    "schedule_deleted": "Смена удалена",
    "schedule_substitution": "Подмена / изменение смены",
    "knowledge_published": "Новый материал в Базе знаний",
    "task_assigned": "Новая задача",
    "test_assigned": "Назначен тест",
}

GROUP_EVENT_NAMES = {
    "order_accepted": "Принятая заявка поставщику",
    "handover_accepted": "Принятая пересменка магазина",
}


def _message_id(data: dict | None) -> int | None:
    try:
        return int((data or {}).get("result", {}).get("message_id"))
    except Exception:
        return None


def _pref_enabled(db: Session, employee_id: int, event_type: str) -> bool:
    pref = db.get(EmployeeNotificationPreference, {"employee_id": employee_id, "event_type": event_type})
    return True if pref is None else bool(pref.enabled)


def _existing(db: Session, dedupe_key: str) -> TelegramDeliveryLog | None:
    return db.scalar(select(TelegramDeliveryLog).where(TelegramDeliveryLog.dedupe_key == dedupe_key))


def _save_log(
    db: Session,
    *,
    event_type: str,
    entity_type: str,
    entity_id: int,
    chat_id: int,
    dedupe_key: str,
    status: str,
    destination_id: int | None = None,
    recipient_employee_id: int | None = None,
    telegram_message_id: int | None = None,
    error: str | None = None,
    payload: dict | None = None,
) -> TelegramDeliveryLog:
    log = TelegramDeliveryLog(
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        destination_id=destination_id,
        recipient_employee_id=recipient_employee_id,
        chat_id=chat_id,
        status=status,
        dedupe_key=dedupe_key,
        telegram_message_id=telegram_message_id,
        error=error,
        payload_json=payload,
        sent_at=datetime.utcnow() if status == "sent" else None,
    )
    db.add(log)
    db.flush()
    return log


def _default_markup() -> dict | None:
    if not APP_URL:
        return None
    return {"inline_keyboard": [[{"text": "Открыть Причал Core", "web_app": {"url": f"{APP_URL.rstrip('/')}/miniapp"}}]]}


def notify_employee(
    db: Session,
    *,
    employee_id: int,
    event_type: str,
    entity_type: str,
    entity_id: int,
    text: str,
    dedupe_suffix: str = "",
    reply_markup: dict | None = None,
    use_default_button: bool = True,
) -> TelegramDeliveryLog | None:
    employee = db.get(Employee, employee_id)
    if not employee or not employee.active or not employee.telegram_id:
        return None
    if not _pref_enabled(db, employee.id, event_type):
        return None
    dedupe_key = f"personal:{event_type}:{entity_type}:{entity_id}:{employee.id}{dedupe_suffix}"
    if _existing(db, dedupe_key):
        return None
    try:
        if reply_markup is None and use_default_button:
            reply_markup = _default_markup()
        data = send_message(int(employee.telegram_id), text, reply_markup)
        return _save_log(
            db, event_type=event_type, entity_type=entity_type, entity_id=entity_id,
            chat_id=int(employee.telegram_id), dedupe_key=dedupe_key, status="sent",
            recipient_employee_id=employee.id, telegram_message_id=_message_id(data),
            payload={"text": text, "reply_markup": reply_markup},
        )
    except Exception as exc:
        return _save_log(
            db, event_type=event_type, entity_type=entity_type, entity_id=entity_id,
            chat_id=int(employee.telegram_id), dedupe_key=dedupe_key, status="error",
            recipient_employee_id=employee.id, error=str(exc)[:2000],
            payload={"text": text, "reply_markup": reply_markup},
        )


def queue_employee(
    db: Session,
    *,
    employee_id: int,
    event_type: str,
    entity_type: str,
    entity_id: int,
    text: str,
    dedupe_suffix: str = "",
    reply_markup: dict | None = None,
    use_default_button: bool = True,
) -> TelegramDeliveryLog | None:
    employee = db.get(Employee, employee_id)
    if not employee or not employee.active or not employee.telegram_id:
        return None
    if not _pref_enabled(db, employee.id, event_type):
        return None
    dedupe_key = f"personal:{event_type}:{entity_type}:{entity_id}:{employee.id}{dedupe_suffix}"
    if _existing(db, dedupe_key):
        return None
    if reply_markup is None and use_default_button:
        reply_markup = _default_markup()
    return _save_log(
        db, event_type=event_type, entity_type=entity_type, entity_id=entity_id,
        chat_id=int(employee.telegram_id), dedupe_key=dedupe_key, status="pending",
        recipient_employee_id=employee.id, payload={"text": text, "reply_markup": reply_markup},
    )


def process_pending_personal_deliveries(db: Session, limit: int = 25) -> int:
    rows = list(db.scalars(
        select(TelegramDeliveryLog)
        .where(TelegramDeliveryLog.status == "pending", TelegramDeliveryLog.recipient_employee_id.is_not(None))
        .order_by(TelegramDeliveryLog.created_at)
        .limit(limit)
    ).all())
    processed = 0
    for log in rows:
        payload = log.payload_json or {}
        try:
            data = send_message(int(log.chat_id), str(payload.get("text") or ""), payload.get("reply_markup"))
            log.status = "sent"
            log.telegram_message_id = _message_id(data)
            log.sent_at = datetime.utcnow()
            log.error = None
        except Exception as exc:
            log.status = "error"
            log.error = str(exc)[:2000]
        processed += 1
    return processed


def _route(db: Session, event_type: str, target_type: str, target_id: int):
    route = db.scalar(select(TelegramRoute).where(
        TelegramRoute.event_type == event_type,
        TelegramRoute.target_type == target_type,
        TelegramRoute.target_id == target_id,
        TelegramRoute.active.is_(True),
    ))
    if not route:
        return None, None
    destination = db.get(TelegramDestination, route.destination_id)
    if not destination or not destination.active:
        return route, None
    return route, destination


def _send_group(
    db: Session,
    *,
    event_type: str,
    target_type: str,
    target_id: int,
    entity_type: str,
    entity_id: int,
    text: str,
    photos: Iterable[str] = (),
    force: bool = False,
    reply_markup: dict | None = None,
    send_media: bool = True,
    media_order: str = "text_first",
    photo_caption: str | None = None,
) -> dict:
    route, destination = _route(db, event_type, target_type, target_id)
    if not destination:
        return {"status": "not_configured", "message": "Telegram-группа не привязана"}
    suffix = f":retry:{int(datetime.utcnow().timestamp()*1000)}" if force else ""
    dedupe_key = f"group:{event_type}:{entity_type}:{entity_id}{suffix}"
    existing = _existing(db, dedupe_key)
    if existing and not force:
        return {"status": existing.status, "log_id": existing.id, "message_id": existing.telegram_message_id, "duplicate": True}

    photo_files = list(photos) if send_media else []
    photo_ids: list[int | None] = []
    main_message_id: int | None = None
    try:
        def deliver_photos():
            for file_id in photo_files:
                try:
                    pdata = send_photo(int(destination.chat_id), file_id, photo_caption or None)
                    photo_ids.append(_message_id(pdata))
                except Exception:
                    photo_ids.append(None)

        if media_order == "media_first":
            deliver_photos()
            data = send_message(int(destination.chat_id), text, reply_markup)
            main_message_id = _message_id(data)
        else:
            data = send_message(int(destination.chat_id), text, reply_markup)
            main_message_id = _message_id(data)
            deliver_photos()

        log = _save_log(
            db, event_type=event_type, entity_type=entity_type, entity_id=entity_id,
            destination_id=destination.id, chat_id=int(destination.chat_id), dedupe_key=dedupe_key,
            status="sent", telegram_message_id=main_message_id,
            payload={
                "text": text, "reply_markup": reply_markup, "photo_message_ids": photo_ids,
                "target_type": target_type, "target_id": target_id,
                "media_order": media_order, "photo_caption": photo_caption,
            },
        )
        return {"status": "sent", "log_id": log.id, "message_id": main_message_id}
    except Exception as exc:
        log = _save_log(
            db, event_type=event_type, entity_type=entity_type, entity_id=entity_id,
            destination_id=destination.id, chat_id=int(destination.chat_id), dedupe_key=dedupe_key,
            status="error", error=str(exc)[:2000],
            payload={"text": text, "reply_markup": reply_markup, "target_type": target_type, "target_id": target_id},
        )
        return {"status": "error", "log_id": log.id, "error": str(exc)}


def _user_name(user: User | None) -> str:
    if not user:
        return "—"
    return user.full_name or user.username or str(user.telegram_id)


def _employee_name(employee: Employee | None) -> str:
    return employee.full_name if employee else "—"


def order_template_values(db: Session, order: Order) -> dict[str, str]:
    store = db.get(Store, order.store_id)
    supplier = db.get(Supplier, order.supplier_id)
    creator = db.get(User, order.created_by)
    accepted_by = db.get(User, order.accepted_by) if getattr(order, "accepted_by", None) else None
    rows = db.execute(select(OrderItem, Product).join(Product, Product.id == OrderItem.product_id).where(OrderItem.order_id == order.id)).all()
    item_lines: list[str] = []
    for item, product in rows:
        qty = f"{float(item.quantity):g}"
        item_lines.append(f"• {escape(product.name)} — <b>{escape(qty)} {escape(product.unit)}</b>")
    if not item_lines:
        item_lines.append("• Пропуск — товары не требуются")
    return {
        "store_name": store.name if store else f"Магазин #{order.store_id}",
        "supplier_name": supplier.name if supplier else f"#{order.supplier_id}",
        "date": order.created_at.strftime("%d.%m.%Y") if order.created_at else "",
        "time": order.created_at.strftime("%H:%M") if order.created_at else "",
        "order_id": str(order.id),
        "order_items": "\n".join(item_lines),
        "comment": order.comment or "Без комментария",
        "created_by": _user_name(creator),
        "accepted_by": _user_name(accepted_by),
        "total_items": str(len(rows)),
    }


def order_delivery_text(db: Session, order: Order) -> str:
    return render_event(db, "order_accepted", order_template_values(db, order))["text"]


def send_order_accepted(db: Session, order_id: int, force: bool = False) -> dict:
    order = db.get(Order, order_id)
    if not order:
        return {"status": "not_found"}
    rendered = render_event(db, "order_accepted", order_template_values(db, order))
    return _send_group(
        db, event_type="order_accepted", target_type="supplier", target_id=order.supplier_id,
        entity_type="order", entity_id=order.id, text=rendered["text"], force=force,
        reply_markup=rendered.get("reply_markup"), send_media=bool(rendered.get("send_media")),
        media_order=rendered.get("media_order") or "text_first",
        photo_caption=rendered.get("photo_caption_rendered"),
    )


def _handover_answers_html(db: Session, report: ShiftReport) -> str:
    values = db.execute(
        select(ShiftReportValue, ShiftTemplateField)
        .join(ShiftTemplateField, ShiftTemplateField.id == ShiftReportValue.field_id)
        .where(ShiftReportValue.report_id == report.id)
        .order_by(ShiftTemplateField.sort_order, ShiftTemplateField.id)
    ).all()
    lines: list[str] = []
    for value, field in values:
        if field.field_type == "photo":
            count = db.scalar(select(Photo.id).where(Photo.entity_type == "shift_report_field", Photo.entity_id == value.id).limit(1))
            rendered = "Фото приложено" if count else "—"
        else:
            raw = value.value_json
            if isinstance(raw, bool):
                rendered = "Да" if raw else "Нет"
            elif raw is None or raw == "":
                rendered = "—"
            elif isinstance(raw, (dict, list)):
                rendered = str(raw)
            else:
                rendered = str(raw)
        lines.append(f"• {escape(field.label)}: <b>{escape(rendered)}</b>")
    return "\n".join(lines) if lines else "• Нет заполненных пунктов"


def handover_template_values(db: Session, report: ShiftReport) -> dict[str, str]:
    store = db.get(Store, report.store_id)
    employee = db.get(Employee, report.employee_id) if report.employee_id else None
    reviewer = db.get(User, report.reviewed_by) if report.reviewed_by else None
    status_label = "✅ <b>ПЕРЕСМЕНКА ПРИНЯТА</b>" if report.status == "accepted" else "⚠️ <b>ПРИНЯТА С ЗАМЕЧАНИЯМИ</b>"
    shift_name = "Дневная" if report.shift_kind in {"day", "morning"} else "Ночная"
    return {
        "status_label": status_label,
        "store_name": store.name if store else f"Магазин #{report.store_id}",
        "date": report.work_date.strftime("%d.%m.%Y") if report.work_date else "",
        "shift_name": shift_name,
        "employee_name": _employee_name(employee),
        "reviewer_name": _user_name(reviewer),
        "handover_answers": _handover_answers_html(db, report),
        "remarks": report.review_comment or "Без замечаний",
        "report_id": str(report.id),
    }


def handover_delivery_text(db: Session, report: ShiftReport) -> str:
    return render_event(db, "handover_accepted", handover_template_values(db, report))["text"]


def _handover_photo_file_ids(db: Session, report_id: int) -> list[str]:
    value_ids = list(db.scalars(select(ShiftReportValue.id).where(ShiftReportValue.report_id == report_id)).all())
    if not value_ids:
        return []
    return list(db.scalars(select(Photo.telegram_file_id).where(Photo.entity_type == "shift_report_field", Photo.entity_id.in_(value_ids)).order_by(Photo.created_at)).all())


def send_handover_accepted(db: Session, report_id: int, force: bool = False) -> dict:
    report = db.get(ShiftReport, report_id)
    if not report:
        return {"status": "not_found"}
    if report.status not in {"accepted", "accepted_with_remarks"}:
        return {"status": "skipped", "message": "Пересменка ещё не принята"}
    rendered = render_event(db, "handover_accepted", handover_template_values(db, report))
    return _send_group(
        db, event_type="handover_accepted", target_type="store", target_id=report.store_id,
        entity_type="shift_report", entity_id=report.id, text=rendered["text"],
        photos=_handover_photo_file_ids(db, report.id), force=force,
        reply_markup=rendered.get("reply_markup"), send_media=bool(rendered.get("send_media")),
        media_order=rendered.get("media_order") or "text_first",
        photo_caption=rendered.get("photo_caption_rendered"),
    )


def notify_schedule_change(db: Session, employee_id: int, entity_id: int, action: str, store_id: int | None, work_date, shift_type: str | None = None):
    store = db.get(Store, store_id) if store_id else None
    employee = db.get(Employee, employee_id)
    label = "Дневная" if shift_type == "day" else "Ночная" if shift_type == "night" else "Смена"
    event = "schedule_created" if action == "created" else "schedule_deleted" if action == "deleted" else "schedule_substitution"
    values = {
        "store_name": store.name if store else "Магазин",
        "date": work_date.strftime("%d.%m.%Y") if work_date else "",
        "date_to": "",
        "shift_name": label,
        "employee_name": _employee_name(employee),
        "change_type": "Подмена / изменение смены",
    }
    rendered = render_event(db, event, values)
    return notify_employee(
        db, employee_id=employee_id, event_type=event, entity_type="work_schedule", entity_id=entity_id,
        text=rendered["text"], reply_markup=rendered.get("reply_markup"), use_default_button=False,
    )


def notify_schedule_absence(db: Session, employee_id: int, entity_id: int, status_name: str, date_from, date_to):
    employee = db.get(Employee, employee_id)
    values = {
        "change_type": status_name,
        "store_name": "—",
        "date": date_from.strftime("%d.%m.%Y") if date_from else "",
        "date_to": f" — {date_to.strftime('%d.%m.%Y')}" if date_to and date_to != date_from else "",
        "shift_name": "—",
        "employee_name": _employee_name(employee),
    }
    rendered = render_event(db, "schedule_substitution", values)
    return notify_employee(
        db, employee_id=employee_id, event_type="schedule_substitution", entity_type="work_absence", entity_id=entity_id,
        text=rendered["text"], reply_markup=rendered.get("reply_markup"), use_default_button=False,
    )


def _employee_can_see_article(db: Session, employee: Employee, article: KnowledgeArticle) -> bool:
    if not employee.user_id:
        return False
    user = db.get(User, employee.user_id)
    section = db.get(KnowledgeSection, article.section_id)
    if not user or not section or not section.active:
        return False
    if not has_access(db,user,"knowledge.read"):
        return False
    if role_key(user)!="admin":
        cur=section; seen=set(); rk=role_key(user)
        while cur and cur.id not in seen:
            seen.add(cur.id)
            roles=set(cur.role_access or [])
            if roles and rk not in roles: return False
            cur=db.get(KnowledgeSection,cur.parent_id) if cur.parent_id else None
    if article.store_id:
        store_ids = set(db.scalars(select(EmployeeStore.store_id).where(EmployeeStore.employee_id == employee.id)).all())
        if article.store_id not in store_ids:
            return False
    positions = set(article.position_tags_json or [])
    if positions and employee.position not in positions:
        return False
    return True


def notify_knowledge_published(db: Session, article_id: int):
    article = db.get(KnowledgeArticle, article_id)
    if not article or article.status != "published":
        return 0
    count = 0
    values = {
        "article_title": article.title,
        "required_note": "⚠️ Материал обязательный — после прочтения подтвердите ознакомление в Core." if article.required_ack else "Материал доступен для чтения в Базе знаний.",
        "revision": str(article.revision),
        "article_id": str(article.id),
    }
    rendered = render_event(db, "knowledge_published", values)
    for employee in db.scalars(select(Employee).where(Employee.active.is_(True), Employee.telegram_id.is_not(None))).all():
        if not _employee_can_see_article(db, employee, article):
            continue
        if queue_employee(
            db, employee_id=employee.id, event_type="knowledge_published", entity_type="knowledge_article",
            entity_id=article.id, text=rendered["text"], dedupe_suffix=f":r{article.revision}",
            reply_markup=rendered.get("reply_markup"), use_default_button=False,
        ):
            count += 1
    return count


def notify_task_assignees(db: Session, task_id: int, employee_ids: Iterable[int]):
    task = db.get(TaskV2, task_id)
    if not task:
        return 0
    priority_names = {"low": "Низкий", "medium": "Обычный", "high": "Высокий", "urgent": "Срочный"}
    values = {
        "task_title": task.title,
        "deadline": task.deadline.strftime("%d.%m.%Y %H:%M") if task.deadline else "Без срока",
        "priority": priority_names.get(task.priority, task.priority),
        "task_id": str(task.id),
    }
    rendered = render_event(db, "task_assigned", values)
    count = 0
    for eid in set(int(x) for x in employee_ids):
        if notify_employee(
            db, employee_id=eid, event_type="task_assigned", entity_type="task", entity_id=task.id,
            text=rendered["text"], reply_markup=rendered.get("reply_markup"), use_default_button=False,
        ):
            count += 1
    return count


def notify_test_assignment(db: Session, assignment_id: int):
    assignment = db.get(TrainingAssignment, assignment_id)
    if not assignment:
        return None
    test = db.get(TrainingTest, assignment.test_id)
    if not test:
        return None
    values = {
        "test_title": test.title,
        "due_at": assignment.due_at.strftime("%d.%m.%Y %H:%M") if assignment.due_at else "Без срока",
        "pass_percent": str(test.pass_percent),
        "attempts_allowed": str(test.attempts_allowed),
        "test_id": str(test.id),
    }
    rendered = render_event(db, "test_assigned", values)
    return notify_employee(
        db, employee_id=assignment.employee_id, event_type="test_assigned", entity_type="training_assignment",
        entity_id=assignment.id, text=rendered["text"], reply_markup=rendered.get("reply_markup"), use_default_button=False,
    )
