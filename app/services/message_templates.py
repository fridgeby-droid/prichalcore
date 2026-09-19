from __future__ import annotations

import re
from dataclasses import dataclass
from html import escape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import APP_URL
from app.db.models import TelegramMessageTemplate


@dataclass(frozen=True)
class VariableSpec:
    label: str
    sample: str
    raw_html: bool = False


@dataclass(frozen=True)
class EventTemplateSpec:
    event_type: str
    name: str
    category: str
    audience: str  # group / personal
    body: str
    variables: dict[str, VariableSpec]
    button_enabled: bool = False
    button_text: str = "Открыть Причал Core"
    button_target: str = "miniapp"
    send_media: bool = False
    media_order: str = "text_first"
    photo_caption: str = ""
    supports_media: bool = False


EVENT_TEMPLATE_SPECS: dict[str, EventTemplateSpec] = {
    "order_accepted": EventTemplateSpec(
        event_type="order_accepted", name="Принятая заявка поставщику", category="Заявки", audience="group",
        body=(
            "🏪 <b>{store_name}</b>\n"
            "🚚 Поставщик: <b>{supplier_name}</b>\n"
            "📅 {date}\n\n"
            "📦 <b>Заявка:</b>\n{order_items}\n\n"
            "💬 {comment}\n\n"
            "👤 Создал: {created_by}\n"
            "✅ Проверил: {accepted_by}"
        ),
        variables={
            "store_name": VariableSpec("Название магазина", "Батумская, 5"),
            "supplier_name": VariableSpec("Поставщик", "Кредо"),
            "date": VariableSpec("Дата заявки", "19.09.2026"),
            "time": VariableSpec("Время заявки", "10:24"),
            "order_id": VariableSpec("Номер заявки", "184"),
            "order_items": VariableSpec("Список товаров", "• Krusovice — <b>2 кег</b>\n• Шихан — <b>1 кег</b>\n• Maisel's — <b>3 шт</b>", True),
            "comment": VariableSpec("Комментарий", "Привезти к открытию"),
            "created_by": VariableSpec("Кто создал", "Анна Иванова"),
            "accepted_by": VariableSpec("Кто принял", "Иван Петров"),
            "total_items": VariableSpec("Количество позиций", "3"),
        },
    ),
    "handover_accepted": EventTemplateSpec(
        event_type="handover_accepted", name="Принятая пересменка магазина", category="Пересменки", audience="group",
        body=(
            "{status_label}\n"
            "🏪 <b>{store_name}</b>\n"
            "🕒 {date} · {shift_name}\n"
            "👤 Сдал: {employee_name}\n"
            "👔 Проверил: {reviewer_name}\n\n"
            "📋 <b>Отчёт:</b>\n{handover_answers}\n\n"
            "💬 {remarks}"
        ),
        variables={
            "status_label": VariableSpec("Результат проверки", "⚠️ <b>ПРИНЯТА С ЗАМЕЧАНИЯМИ</b>", True),
            "store_name": VariableSpec("Название магазина", "Батумская, 5"),
            "date": VariableSpec("Дата смены", "19.09.2026"),
            "shift_name": VariableSpec("Тип смены", "Ночная"),
            "employee_name": VariableSpec("Кто сдал", "Анна Иванова"),
            "reviewer_name": VariableSpec("Кто проверил", "Иван Петров"),
            "handover_answers": VariableSpec("Ответы пересменки", "• Торговый зал: <b>Всё в порядке</b>\n• Касса: <b>Расхождений нет</b>\n• Фото: <b>Приложено</b>", True),
            "remarks": VariableSpec("Замечания", "Заменить ценник на витрине"),
            "report_id": VariableSpec("Номер пересменки", "421"),
        },
        send_media=True, supports_media=True,
    ),
    "schedule_created": EventTemplateSpec(
        event_type="schedule_created", name="Назначена смена", category="График", audience="personal",
        body="📅 <b>Вам назначена смена</b>\n🏪 {store_name}\n🗓 {date}\n🕒 {shift_name}",
        variables={
            "store_name": VariableSpec("Магазин", "Батумская, 5"),
            "date": VariableSpec("Дата", "21.09.2026"),
            "shift_name": VariableSpec("Смена", "Дневная"),
            "employee_name": VariableSpec("Сотрудник", "Анна Иванова"),
        },
        button_enabled=True,
    ),
    "schedule_deleted": EventTemplateSpec(
        event_type="schedule_deleted", name="Смена удалена", category="График", audience="personal",
        body="📅 <b>Изменение графика</b>\nСмена удалена из вашего графика.\n🏪 {store_name}\n🗓 {date}\n🕒 {shift_name}",
        variables={
            "store_name": VariableSpec("Магазин", "Батумская, 5"),
            "date": VariableSpec("Дата", "21.09.2026"),
            "shift_name": VariableSpec("Смена", "Дневная"),
            "employee_name": VariableSpec("Сотрудник", "Анна Иванова"),
        },
        button_enabled=True,
    ),
    "schedule_substitution": EventTemplateSpec(
        event_type="schedule_substitution", name="Изменение графика / подмена", category="График", audience="personal",
        body="🔄 <b>Изменение графика</b>\n{change_type}\n🏪 {store_name}\n🗓 {date}{date_to}\n🕒 {shift_name}",
        variables={
            "change_type": VariableSpec("Тип изменения", "Подмена"),
            "store_name": VariableSpec("Магазин", "Батумская, 5"),
            "date": VariableSpec("Дата начала", "21.09.2026"),
            "date_to": VariableSpec("Дата окончания", " — 28.09.2026"),
            "shift_name": VariableSpec("Смена", "Ночная"),
            "employee_name": VariableSpec("Сотрудник", "Анна Иванова"),
        },
        button_enabled=True,
    ),
    "task_assigned": EventTemplateSpec(
        event_type="task_assigned", name="Назначена задача", category="Задачи", audience="personal",
        body="✅ <b>Вам назначена задача</b>\n{task_title}\n⏰ {deadline}\n⚑ Приоритет: {priority}",
        variables={
            "task_title": VariableSpec("Название задачи", "Проверить ценники"),
            "deadline": VariableSpec("Срок", "20.09.2026 18:00"),
            "priority": VariableSpec("Приоритет", "Высокий"),
            "task_id": VariableSpec("Номер задачи", "927"),
        },
        button_enabled=True,
    ),
    "knowledge_published": EventTemplateSpec(
        event_type="knowledge_published", name="Новый материал в Базе знаний", category="База знаний", audience="personal",
        body="📚 <b>Новый материал в Базе знаний</b>\n{article_title}\n\n{required_note}",
        variables={
            "article_title": VariableSpec("Название статьи", "Регламент открытия магазина"),
            "required_note": VariableSpec("Требование ознакомления", "⚠️ Материал обязательный — после прочтения подтвердите ознакомление в Core."),
            "revision": VariableSpec("Редакция статьи", "2"),
            "article_id": VariableSpec("Номер статьи", "51"),
        },
        button_enabled=True,
    ),
    "test_assigned": EventTemplateSpec(
        event_type="test_assigned", name="Назначен тест", category="Тестирование", audience="personal",
        body="🎓 <b>Вам назначен тест</b>\n{test_title}\n⏰ Срок: {due_at}\n🎯 Проходной балл: {pass_percent}%\n🔁 Попыток: {attempts_allowed}",
        variables={
            "test_title": VariableSpec("Название теста", "Техника безопасности"),
            "due_at": VariableSpec("Срок", "25.09.2026 18:00"),
            "pass_percent": VariableSpec("Проходной балл", "80"),
            "attempts_allowed": VariableSpec("Количество попыток", "2"),
            "test_id": VariableSpec("Номер теста", "12"),
        },
        button_enabled=True,
    ),
}


ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a", "code", "pre", "blockquote"}
VARIABLE_RE = re.compile(r"\{([a-zA-Z0-9_]+)\}")


class _TelegramHTMLValidator(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs):
        if tag not in ALLOWED_TAGS:
            self.errors.append(f"Неподдерживаемый HTML-тег <{tag}>")
            return
        for key, value in attrs:
            if tag == "a" and key == "href":
                if not value or urlparse(value).scheme not in {"http", "https", "tg"}:
                    self.errors.append("Ссылка в <a> должна начинаться с http://, https:// или tg://")
            else:
                self.errors.append(f"Атрибут {key} для <{tag}> не поддерживается")

    def handle_startendtag(self, tag: str, attrs):
        self.handle_starttag(tag, attrs)


def get_spec(event_type: str) -> EventTemplateSpec:
    try:
        return EVENT_TEMPLATE_SPECS[event_type]
    except KeyError:
        raise ValueError("Неизвестный тип события")


def validate_template(event_type: str, body_html: str, *, button_enabled: bool = False, button_target: str = "miniapp", button_url: str | None = None, media_order: str = "text_first") -> list[str]:
    spec = get_spec(event_type)
    errors: list[str] = []
    if not body_html or not body_html.strip():
        errors.append("Текст сообщения не может быть пустым")
    unknown = sorted(set(VARIABLE_RE.findall(body_html)) - set(spec.variables))
    if unknown:
        errors.append("Неизвестные переменные: " + ", ".join("{" + x + "}" for x in unknown))
    parser = _TelegramHTMLValidator()
    try:
        parser.feed(body_html)
        parser.close()
    except Exception:
        errors.append("Некорректная HTML-разметка")
    errors.extend(parser.errors)
    if button_enabled:
        if button_target not in {"miniapp", "custom"}:
            errors.append("Некорректный тип кнопки")
        if spec.audience == "group" and button_target == "miniapp":
            errors.append("Кнопка MiniApp доступна только в личных чатах; для группы выберите собственную ссылку")
        if button_target == "custom":
            parsed = urlparse((button_url or "").strip())
            if parsed.scheme not in {"http", "https", "tg"}:
                errors.append("Ссылка кнопки должна начинаться с http://, https:// или tg://")
    if media_order not in {"text_first", "media_first"}:
        errors.append("Некорректный порядок отправки медиа")
    return errors


def render_body(event_type: str, body_html: str, values: dict[str, Any]) -> str:
    spec = get_spec(event_type)

    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in spec.variables:
            return match.group(0)
        value = values.get(key, "")
        text = "" if value is None else str(value)
        return text if spec.variables[key].raw_html else escape(text)

    return VARIABLE_RE.sub(repl, body_html)


def sample_values(event_type: str) -> dict[str, str]:
    spec = get_spec(event_type)
    return {key: value.sample for key, value in spec.variables.items()}


def template_row_or_default(db: Session, event_type: str) -> dict[str, Any]:
    spec = get_spec(event_type)
    row = db.scalar(select(TelegramMessageTemplate).where(TelegramMessageTemplate.event_type == event_type))
    if row and row.active:
        return {
            "event_type": event_type,
            "name": row.name,
            "body_html": row.body_html,
            "active": row.active,
            "button_enabled": row.button_enabled,
            "button_text": row.button_text or "Открыть Причал Core",
            "button_target": row.button_target or "miniapp",
            "button_url": row.button_url,
            "send_media": row.send_media,
            "media_order": row.media_order or "text_first",
            "photo_caption": row.photo_caption or "",
            "custom": True,
        }
    return {
        "event_type": event_type,
        "name": spec.name,
        "body_html": spec.body,
        "active": True,
        "button_enabled": spec.button_enabled,
        "button_text": spec.button_text,
        "button_target": spec.button_target,
        "button_url": None,
        "send_media": spec.send_media,
        "media_order": spec.media_order,
        "photo_caption": spec.photo_caption,
        "custom": False,
    }


def build_reply_markup(config: dict[str, Any]) -> dict | None:
    if not config.get("button_enabled"):
        return None
    text = (config.get("button_text") or "Открыть Причал Core").strip()[:64]
    target = config.get("button_target") or "miniapp"
    if target == "miniapp":
        if not APP_URL:
            return None
        return {"inline_keyboard": [[{"text": text, "web_app": {"url": f"{APP_URL.rstrip('/')}/miniapp"}}]]}
    if target == "custom":
        url = (config.get("button_url") or "").strip()
        if not url:
            return None
        return {"inline_keyboard": [[{"text": text, "url": url}]]}
    return None


def render_event(db: Session, event_type: str, values: dict[str, Any]) -> dict[str, Any]:
    config = template_row_or_default(db, event_type)
    text = render_body(event_type, config["body_html"], values)
    caption = render_body(event_type, config.get("photo_caption") or "", values) if config.get("photo_caption") else None
    return {**config, "text": text, "photo_caption_rendered": caption, "reply_markup": build_reply_markup(config)}


def serialize_template(db: Session, event_type: str) -> dict[str, Any]:
    spec = get_spec(event_type)
    row = db.scalar(select(TelegramMessageTemplate).where(TelegramMessageTemplate.event_type == event_type))
    if row:
        config = {
            "event_type": event_type, "name": row.name, "body_html": row.body_html,
            "active": row.active, "button_enabled": row.button_enabled,
            "button_text": row.button_text or "Открыть Причал Core",
            "button_target": row.button_target or "miniapp", "button_url": row.button_url,
            "send_media": row.send_media, "media_order": row.media_order or "text_first",
            "photo_caption": row.photo_caption or "", "custom": True,
        }
    else:
        config = template_row_or_default(db, event_type)
    return {
        **config,
        "category": spec.category,
        "audience": spec.audience,
        "supports_media": spec.supports_media,
        "variables": [
            {"key": key, "label": value.label, "sample": value.sample, "raw_html": value.raw_html}
            for key, value in spec.variables.items()
        ],
        "updated_at": row.updated_at if row else None,
        "updated_by": row.updated_by if row else None,
    }


def reset_template(db: Session, event_type: str, user_id: int | None = None) -> TelegramMessageTemplate:
    spec = get_spec(event_type)
    row = db.scalar(select(TelegramMessageTemplate).where(TelegramMessageTemplate.event_type == event_type))
    if not row:
        row = TelegramMessageTemplate(event_type=event_type, name=spec.name, body_html=spec.body)
        db.add(row)
    row.name = spec.name
    row.body_html = spec.body
    row.active = True
    row.button_enabled = spec.button_enabled
    row.button_text = spec.button_text
    row.button_target = spec.button_target
    row.button_url = None
    row.send_media = spec.send_media
    row.media_order = spec.media_order
    row.photo_caption = spec.photo_caption or None
    row.updated_by = user_id
    return row


def seed_default_templates(db: Session) -> None:
    for event_type, spec in EVENT_TEMPLATE_SPECS.items():
        row = db.scalar(select(TelegramMessageTemplate).where(TelegramMessageTemplate.event_type == event_type))
        if row:
            continue
        db.add(TelegramMessageTemplate(
            event_type=event_type,
            name=spec.name,
            body_html=spec.body,
            active=True,
            button_enabled=spec.button_enabled,
            button_text=spec.button_text,
            button_target=spec.button_target,
            send_media=spec.send_media,
            media_order=spec.media_order,
            photo_caption=spec.photo_caption or None,
        ))
