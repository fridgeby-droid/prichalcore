from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import APP_TIMEZONE
from app.core.security import get_current_user
from app.db.database import get_db
from app.db.models import (
    Category,
    Order,
    OrderItem,
    OrderScheduleRule,
    Product,
    ProductOrderLimit,
    Store,
    StoreProduct,
    Supplier,
    User,
)

router = APIRouter(prefix="/api/orders", tags=["orders"])
ORDER_STATUSES = {"new", "accepted", "cancelled", "skipped"}
DONE_FOR_PROGRESS = {"new", "accepted", "skipped"}


class OrderItemIn(BaseModel):
    product_id: int
    quantity: float = Field(gt=0)


class OrderIn(BaseModel):
    store_id: int
    supplier_id: int
    comment: str | None = None
    items: list[OrderItemIn] = []
    skip: bool = False


def _local_now() -> datetime:
    return datetime.now(ZoneInfo(APP_TIMEZONE))


def _local_day_bounds(local_date=None):
    tz = ZoneInfo(APP_TIMEZONE)
    d = local_date or _local_now().date()
    start = datetime.combine(d, datetime.min.time(), tzinfo=tz).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    return start, start + timedelta(days=1)


def _scheduled_supplier_ids(db: Session, store_id: int, weekday: int) -> list[int]:
    q = (
        select(OrderScheduleRule.supplier_id)
        .join(Supplier, Supplier.id == OrderScheduleRule.supplier_id)
        .where(
            OrderScheduleRule.store_id == store_id,
            OrderScheduleRule.schedule_type == "order",
            OrderScheduleRule.weekday == weekday,
            OrderScheduleRule.active.is_(True),
            Supplier.active.is_(True),
        )
        .distinct()
    )
    return list(db.scalars(q).all())


def _limit_for(db: Session, product_id: int, store_id: int):
    rows = list(
        db.scalars(
            select(ProductOrderLimit).where(
                ProductOrderLimit.product_id == product_id,
                ProductOrderLimit.active.is_(True),
                ProductOrderLimit.store_id.in_([store_id, None]),
            )
        ).all()
    )
    specific = next((x for x in rows if x.store_id == store_id), None)
    return specific or next((x for x in rows if x.store_id is None), None)


def _validate_items(db: Session, store_id: int, supplier_id: int, items: list[OrderItemIn]):
    seen = set()
    for item in items:
        if item.product_id in seen:
            raise HTTPException(400, "Один товар нельзя добавлять в заявку дважды")
        seen.add(item.product_id)
        p = db.get(Product, item.product_id)
        if not p or not p.active or p.supplier_id != supplier_id:
            raise HTTPException(400, "Товар не относится к выбранному поставщику")
        lim = _limit_for(db, p.id, store_id)
        qty = float(item.quantity)
        if lim:
            if lim.min_qty is not None and qty < float(lim.min_qty):
                raise HTTPException(400, f"{p.name}: минимум {float(lim.min_qty):g} {p.unit}")
            if lim.max_qty is not None and qty > float(lim.max_qty):
                raise HTTPException(400, f"{p.name}: максимум {float(lim.max_qty):g} {p.unit}")
            if lim.step_qty is not None and float(lim.step_qty) > 0:
                step = float(lim.step_qty)
                base = float(lim.min_qty or 0)
                ratio = (qty - base) / step
                if abs(ratio - round(ratio)) > 1e-6:
                    raise HTTPException(400, f"{p.name}: количество должно изменяться с шагом {step:g} {p.unit}")


def _order_dict(db: Session, o: Order, include_items: bool = True):
    store = db.get(Store, o.store_id)
    supplier = db.get(Supplier, o.supplier_id)
    creator = db.get(User, o.created_by)
    data = {
        "id": o.id,
        "store_id": o.store_id,
        "store_name": store.name if store else f"Точка #{o.store_id}",
        "supplier_id": o.supplier_id,
        "supplier_name": supplier.name if supplier else f"Поставщик #{o.supplier_id}",
        "created_by": o.created_by,
        "creator_name": (creator.full_name or creator.username or str(creator.telegram_id)) if creator else "—",
        "status": o.status,
        "comment": o.comment,
        "created_at": o.created_at.isoformat(),
        "updated_at": o.updated_at.isoformat(),
    }
    if include_items:
        rows = db.execute(
            select(OrderItem, Product)
            .join(Product, Product.id == OrderItem.product_id)
            .where(OrderItem.order_id == o.id)
            .order_by(Product.name)
        ).all()
        data["items"] = [
            {
                "id": oi.id,
                "product_id": p.id,
                "name": p.name,
                "unit": p.unit,
                "quantity": float(oi.quantity),
            }
            for oi, p in rows
        ]
    return data


def _period_bounds(period: str | None, date_value: str | None):
    if not period:
        return None, None
    now = _local_now()
    if period == "today":
        d = now.date()
    elif period == "yesterday":
        d = (now - timedelta(days=1)).date()
    elif period == "date":
        if not date_value:
            raise HTTPException(400, "Укажите дату")
        try:
            d = datetime.strptime(date_value, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(400, "Неверный формат даты")
    elif period == "week":
        d = now.date() - timedelta(days=now.weekday())
        start, _ = _local_day_bounds(d)
        return start, start + timedelta(days=7)
    else:
        return None, None
    return _local_day_bounds(d)


@router.get("/today-availability")
def today_availability(user=Depends(get_current_user), db: Session = Depends(get_db)):
    """Показывает, какие заявки по графику должны быть сделаны сегодня по каждой точке."""
    now = _local_now()
    weekday = now.weekday()
    start, end = _local_day_bounds(now.date())
    stores = list(db.scalars(select(Store).where(Store.active.is_(True)).order_by(Store.name)).all())
    result = []
    for store in stores:
        supplier_ids = _scheduled_supplier_ids(db, store.id, weekday)
        if not supplier_ids:
            continue
        suppliers = list(
            db.scalars(
                select(Supplier).where(Supplier.id.in_(supplier_ids), Supplier.active.is_(True)).order_by(Supplier.name)
            ).all()
        )
        done_rows = db.execute(
            select(Order.supplier_id, Order.status)
            .where(
                Order.store_id == store.id,
                Order.supplier_id.in_(supplier_ids),
                Order.created_at >= start,
                Order.created_at < end,
            )
        ).all()
        status_by_supplier: dict[int, str] = {}
        for sid, status in done_rows:
            # Любая действующая заявка или «Пропуск» закрывает обязательство. Отменённая — нет.
            if status in DONE_FOR_PROGRESS:
                status_by_supplier[sid] = status
        supplier_data = [
            {
                "id": s.id,
                "name": s.name,
                "deadline_time": s.deadline_time,
                "done": s.id in status_by_supplier,
                "status": status_by_supplier.get(s.id),
            }
            for s in suppliers
        ]
        done_count = sum(1 for x in supplier_data if x["done"])
        result.append(
            {
                "store_id": store.id,
                "store_name": store.name,
                "total": len(supplier_data),
                "done": done_count,
                "complete": done_count == len(supplier_data),
                "suppliers": supplier_data,
            }
        )
    return {"date": now.date().isoformat(), "weekday": weekday, "stores": result}


@router.get("/available-suppliers")
def available_suppliers(store_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    store = db.get(Store, store_id)
    if not store or not store.active:
        raise HTTPException(404, "Магазин не найден")
    now = _local_now()
    ids = _scheduled_supplier_ids(db, store_id, now.weekday())
    if not ids:
        return []
    start, end = _local_day_bounds(now.date())
    existing = db.execute(
        select(Order.supplier_id, Order.status).where(
            Order.store_id == store_id,
            Order.supplier_id.in_(ids),
            Order.created_at >= start,
            Order.created_at < end,
        )
    ).all()
    done = {sid: status for sid, status in existing if status in DONE_FOR_PROGRESS}
    suppliers = list(db.scalars(select(Supplier).where(Supplier.id.in_(ids), Supplier.active.is_(True)).order_by(Supplier.name)).all())
    return [
        {
            "id": s.id,
            "name": s.name,
            "deadline_time": s.deadline_time,
            "done_today": s.id in done,
            "status": done.get(s.id),
        }
        for s in suppliers
    ]


@router.get("/products")
def available_products(store_id: int, supplier_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    mappings = list(db.scalars(select(StoreProduct).where(StoreProduct.store_id == store_id)).all())
    mapped = [x.product_id for x in mappings if x.enabled]
    q = select(Product).where(Product.supplier_id == supplier_id, Product.active.is_(True))
    if mappings:
        q = q.where(Product.id.in_(mapped or [-1]))
    result = []
    for p in db.scalars(q.order_by(Product.name)).all():
        lim = _limit_for(db, p.id, store_id)
        cat = db.get(Category, p.category_id) if p.category_id else None
        result.append(
            {
                "id": p.id,
                "name": p.name,
                "unit": p.unit,
                "category_id": p.category_id,
                "category_name": cat.name if cat else None,
                "min_qty": float(lim.min_qty) if lim and lim.min_qty is not None else None,
                "max_qty": float(lim.max_qty) if lim and lim.max_qty is not None else None,
                "step_qty": float(lim.step_qty) if lim and lim.step_qty is not None else None,
            }
        )
    return result


@router.post("")
def create_order(payload: OrderIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    store = db.get(Store, payload.store_id)
    supplier = db.get(Supplier, payload.supplier_id)
    if not store or not store.active:
        raise HTTPException(404, "Магазин не найден")
    if not supplier or not supplier.active:
        raise HTTPException(404, "Поставщик не найден")

    today_suppliers = _scheduled_supplier_ids(db, payload.store_id, _local_now().weekday())
    if payload.supplier_id not in today_suppliers:
        raise HTTPException(409, "Сегодня по графику заявок этот поставщик недоступен для выбранного магазина")

    if payload.skip:
        if payload.items:
            raise HTTPException(400, "Для пропуска список товаров должен быть пустым")
        status = "skipped"
    else:
        if not payload.items:
            raise HTTPException(400, "Добавьте хотя бы один товар или отметьте «Пропуск»")
        _validate_items(db, payload.store_id, payload.supplier_id, payload.items)
        status = "new"

    order = Order(
        store_id=payload.store_id,
        supplier_id=payload.supplier_id,
        created_by=user.id,
        status=status,
        comment=payload.comment,
    )
    db.add(order)
    db.flush()
    for item in payload.items:
        db.add(OrderItem(order_id=order.id, product_id=item.product_id, quantity=item.quantity, note=None))
    db.commit()
    db.refresh(order)
    return _order_dict(db, order)


@router.get("")
def list_orders(
    status: str | None = None,
    store_id: int | None = None,
    supplier_id: int | None = None,
    period: str | None = None,
    date: str | None = None,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    q = select(Order).order_by(Order.created_at.desc()).limit(500)
    if status:
        q = q.where(Order.status == status)
    if store_id:
        q = q.where(Order.store_id == store_id)
    if supplier_id:
        q = q.where(Order.supplier_id == supplier_id)
    start, end = _period_bounds(period, date)
    if start:
        q = q.where(Order.created_at >= start)
    if end:
        q = q.where(Order.created_at < end)
    return [_order_dict(db, o, include_items=False) for o in db.scalars(q).all()]


@router.get("/{order_id}")
def get_order(order_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    o = db.get(Order, order_id)
    if not o:
        raise HTTPException(404, "Заявка не найдена")
    return _order_dict(db, o)


@router.get("/{order_id}/share-text")
def share_order_text(order_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    o = db.get(Order, order_id)
    if not o:
        raise HTTPException(404, "Заявка не найдена")
    data = _order_dict(db, o)
    lines = [f"🏪 {data['store_name']}", f"🚚 {data['supplier_name']}", "", "📦 Заявка:"]
    if data["items"]:
        for item in data["items"]:
            qty = f"{item['quantity']:g}"
            lines.append(f"• {item['name']} — {qty} {item['unit']}")
    else:
        lines.append("• Пропуск — товары не требуются")
    if data.get("comment"):
        lines.extend(["", f"💬 {data['comment']}"])
    return {"text": "\n".join(lines)}


@router.patch("/{order_id}")
def update_order(order_id: int, payload: dict, user=Depends(get_current_user), db: Session = Depends(get_db)):
    o = db.get(Order, order_id)
    if not o:
        raise HTTPException(404, "Заявка не найдена")
    if "status" in payload:
        status = str(payload["status"])
        if status not in ORDER_STATUSES:
            raise HTTPException(400, "Неизвестный статус")
        o.status = status
        if status == "skipped":
            db.execute(delete(OrderItem).where(OrderItem.order_id == o.id))
    if "comment" in payload:
        o.comment = payload["comment"]
    if "items" in payload:
        if o.status != "new":
            raise HTTPException(409, "Состав заявки можно менять только пока статус «Новая»")
        parsed = [OrderItemIn(**x) for x in payload["items"]]
        if not parsed:
            raise HTTPException(400, "В заявке должен быть хотя бы один товар")
        _validate_items(db, o.store_id, o.supplier_id, parsed)
        db.execute(delete(OrderItem).where(OrderItem.order_id == o.id))
        for item in parsed:
            db.add(OrderItem(order_id=o.id, product_id=item.product_id, quantity=item.quantity, note=None))
    db.commit()
    db.refresh(o)
    return _order_dict(db, o)


@router.delete("/{order_id}")
def delete_order(order_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    o = db.get(Order, order_id)
    if not o:
        raise HTTPException(404, "Заявка не найдена")
    db.delete(o)
    db.commit()
    return {"ok": True}
