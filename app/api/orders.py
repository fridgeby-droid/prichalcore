from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import APP_TIMEZONE
from app.core.security import get_current_user
from app.core.permissions import require_access, assigned_store_ids, assert_store_scope, assert_own_or_scope
from app.db.database import get_db
from app.services.notifications import send_order_accepted
from app.db.models import (
    ImportedPriceItem, ImportedPriceList,
    Employee,
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
ORDER_STATUSES = {"new", "accepted", "cancelled", "skipped", "comment_only"}
DONE_FOR_PROGRESS = {"new", "accepted", "skipped", "comment_only"}


class OrderItemIn(BaseModel):
    product_id: int
    quantity: Decimal = Field(gt=0,max_digits=18,decimal_places=6)


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
                ((ProductOrderLimit.store_id==store_id) | ProductOrderLimit.store_id.is_(None)),
            )
        ).all()
    )
    specific = next((x for x in rows if x.store_id == store_id), None)
    return specific or next((x for x in rows if x.store_id is None), None)


def _validate_items(db: Session, store_id: int, supplier_id: int, items: list[OrderItemIn]):
    ids=[i.product_id for i in items]
    imported=list(db.scalars(select(ImportedPriceItem).where(ImportedPriceItem.product_id.in_(ids))))
    list(db.scalars(select(ImportedPriceList).where(ImportedPriceList.id.in_({i.price_list_id for i in imported})).order_by(ImportedPriceList.id).with_for_update()))
    imported={i.product_id:i for i in db.scalars(select(ImportedPriceItem).where(ImportedPriceItem.product_id.in_(ids)).execution_options(populate_existing=True))}
    products={p.id:p for p in db.scalars(select(Product).where(Product.id.in_(ids)).order_by(Product.id).with_for_update().execution_options(populate_existing=True))}
    mappings=list(db.scalars(select(StoreProduct).where(StoreProduct.store_id==store_id)))
    allowed={m.product_id for m in mappings if m.enabled}
    seen=set()
    for item in items:
        if item.product_id in seen:raise HTTPException(400,'Один товар нельзя добавлять дважды')
        seen.add(item.product_id)
        p=products.get(item.product_id)
        if not p or not p.active or p.supplier_id!=supplier_id or (mappings and p.id not in allowed):raise HTTPException(400,'Товар недоступен для заявки')
        qty=item.quantity
        imp=imported.get(p.id)
        if imp:
            if not imp.present or not imp.step or imp.confirmed_unit!=p.unit:raise HTTPException(400,'Шаг товара требует настройки')
            if qty % imp.step != 0:raise HTTPException(400,f'{p.name}: количество должно быть кратно {imp.step} {p.unit}')
        lim=_limit_for(db,p.id,store_id)
        if lim:
            if lim.min_qty is not None and qty<lim.min_qty:raise HTTPException(400,'Количество меньше минимума')
            if lim.max_qty is not None and qty>lim.max_qty:raise HTTPException(400,'Количество больше максимума')
            if not imp and lim.step_qty and (qty-Decimal(str(lim.min_qty or 0))) % lim.step_qty != 0:raise HTTPException(400,'Количество не кратно шагу')


def _order_dict(db: Session, o: Order, include_items: bool = True, refs=None):
    if refs is None:
        store = db.get(Store, o.store_id)
        supplier = db.get(Supplier, o.supplier_id)
        creator = db.scalar(select(Employee).where(Employee.user_id == o.created_by))
        accepted_by = db.get(User, o.accepted_by) if o.accepted_by else None
    else:
        stores, suppliers, employees, users = refs
        store=stores.get(o.store_id);supplier=suppliers.get(o.supplier_id)
        creator=employees.get(o.created_by);accepted_by=users.get(o.accepted_by)
    data = {
        "id": o.id,
        "store_id": o.store_id,
        "store_name": store.name if store else f"Точка #{o.store_id}",
        "supplier_id": o.supplier_id,
        "supplier_name": supplier.name if supplier else f"Поставщик #{o.supplier_id}",
        "created_by": o.created_by,
        "creator_name": creator.full_name if creator else "Сотрудник не привязан",
        "status": o.status,
        "comment": o.comment,
        "accepted_by": getattr(o, "accepted_by", None),
        "accepted_by_name": (accepted_by.full_name or accepted_by.username or str(accepted_by.telegram_id)) if accepted_by else None,
        "accepted_at": o.accepted_at.replace(tzinfo=ZoneInfo("UTC")).isoformat() if getattr(o, "accepted_at", None) else None,
        "created_at": o.created_at.replace(tzinfo=ZoneInfo("UTC")).isoformat(),
        "updated_at": o.updated_at.replace(tzinfo=ZoneInfo("UTC")).isoformat(),
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
                "name": oi.snapshot_name or p.name,
                "unit": oi.snapshot_unit or p.unit,
                "quantity": float(oi.quantity),
                "quantity_text": str(oi.quantity),
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
    p = require_access(db, user, "orders.create", "edit")
    now = _local_now()
    weekday = now.weekday()
    start, end = _local_day_bounds(now.date())
    sq=select(Store).where(Store.active.is_(True))
    if p["data_scope"]!="network": sq=sq.where(Store.id.in_(assigned_store_ids(db,user) or [-1]))
    stores=list(db.scalars(sq.order_by(Store.name)).all())
    store_ids=[s.id for s in stores]
    scheduled={}
    for store_id,supplier in db.execute(select(OrderScheduleRule.store_id,Supplier).join(Supplier,Supplier.id==OrderScheduleRule.supplier_id).where(OrderScheduleRule.store_id.in_(store_ids),OrderScheduleRule.schedule_type=="order",OrderScheduleRule.weekday==weekday,OrderScheduleRule.active.is_(True),Supplier.active.is_(True)).distinct().order_by(Supplier.name)):
        scheduled.setdefault(store_id,[]).append(supplier)
    done={}
    for store_id,sid,status in db.execute(select(Order.store_id,Order.supplier_id,Order.status).where(Order.store_id.in_(store_ids),Order.created_at>=start,Order.created_at<end,Order.status.in_(DONE_FOR_PROGRESS)).order_by(Order.id)):
        done.setdefault(store_id,{})[sid]=status
    result = []
    for store in stores:
        suppliers=scheduled.get(store.id,[])
        if not suppliers:continue
        status_by_supplier=done.get(store.id,{})
        supplier_data = [
            {
                "id": s.id,
                "name": s.name,
                "deadline_time": s.deadline_time,
                "done": s.id in status_by_supplier,
                "status": status_by_supplier.get(s.id),
                "overdue": s.id not in status_by_supplier and bool(s.deadline_time and now.strftime("%H:%M") >= s.deadline_time),
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
                "overdue": any(x["overdue"] for x in supplier_data),
                "suppliers": supplier_data,
            }
        )
    return {"date": now.date().isoformat(), "weekday": weekday, "stores": result}


@router.get("/available-suppliers")
def available_suppliers(store_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_access(db,user,"orders.create","edit"); assert_store_scope(db,user,"orders.create",store_id,minimum="edit",own_as_assigned=True)
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
            "exists_today": s.id in {sid for sid, _ in existing},
            "status": done.get(s.id),
        }
        for s in suppliers
    ]


@router.get("/products")
def available_products(store_id: int, supplier_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_access(db,user,"orders.create","edit"); assert_store_scope(db,user,"orders.create",store_id,minimum="edit",own_as_assigned=True)
    mappings = list(db.scalars(select(StoreProduct).where(StoreProduct.store_id == store_id)).all())
    mapped = [x.product_id for x in mappings if x.enabled]
    q = select(Product).where(Product.supplier_id == supplier_id, Product.active.is_(True))
    if mappings:
        q = q.where(Product.id.in_(mapped or [-1]))
    from app.services.price_import import selected_photos
    products=list(db.scalars(q.order_by(Product.name)))
    imported={i.product_id:i for i in db.scalars(select(ImportedPriceItem).where(ImportedPriceItem.product_id.in_([p.id for p in products])))}
    photos=selected_photos(db,list(imported.values()))
    limits=list(db.scalars(select(ProductOrderLimit).where(ProductOrderLimit.product_id.in_([p.id for p in products]),ProductOrderLimit.active.is_(True),((ProductOrderLimit.store_id==store_id)|ProductOrderLimit.store_id.is_(None)))))
    limit_by={l.product_id:l for l in limits if l.store_id is None}
    limit_by.update({l.product_id:l for l in limits if l.store_id==store_id})
    categories={c.id:c for c in db.scalars(select(Category).where(Category.id.in_([p.category_id for p in products if p.category_id])))}
    result = []
    for p in products:
        imp=imported.get(p.id)
        if imp and (not imp.present or not imp.step or imp.confirmed_unit!=p.unit):continue
        lim = limit_by.get(p.id)
        cat = categories.get(p.category_id)
        result.append(
            {
                "id": p.id,
                "name": p.name,
                "unit": p.unit,
                "category_id": p.category_id,
                "category_name": cat.name if cat else None,
                "min_qty": float(lim.min_qty) if lim and lim.min_qty is not None else None,
                "max_qty": float(lim.max_qty) if lim and lim.max_qty is not None else None,
                "step_qty": str(imp.step) if imp else (str(lim.step_qty) if lim and lim.step_qty is not None else None),
                "image_url": f"/api/orders/products/{p.id}/photo?store_id={store_id}" if photos.get(p.id) else None,
            }
        )
    return result


@router.get('/products/{product_id}/photo')
def product_photo(product_id:int,store_id:int,user=Depends(get_current_user),db:Session=Depends(get_db)):
    require_access(db,user,'orders.create','edit');assert_store_scope(db,user,'orders.create',store_id,minimum='edit',own_as_assigned=True)
    mappings=list(db.scalars(select(StoreProduct).where(StoreProduct.store_id==store_id)))
    if mappings and product_id not in {m.product_id for m in mappings if m.enabled}:raise HTTPException(404,'Фото отсутствует')
    from app.services.price_import import selected_photos
    from app.services.storage import s3_photo_response
    item=db.scalar(select(ImportedPriceItem).where(ImportedPriceItem.product_id==product_id))
    photos=selected_photos(db,[item]).get(product_id,[]) if item else []
    if not photos:raise HTTPException(404,'Фото отсутствует')
    return s3_photo_response(db,media_id=photos[0].media_id,thumbnail=True)


@router.post("")
def create_order(payload: OrderIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_access(db,user,"orders.create","edit"); assert_store_scope(db,user,"orders.create",payload.store_id,minimum="edit",own_as_assigned=True)
    # Lock the store before checking the daily limit; concurrent creates serialize.
    store = db.scalar(select(Store).where(Store.id == payload.store_id).with_for_update())
    supplier = db.get(Supplier, payload.supplier_id)
    if not store or not store.active:
        raise HTTPException(404, "Магазин не найден")
    if not supplier or not supplier.active:
        raise HTTPException(404, "Поставщик не найден")

    now=_local_now()
    today_suppliers = _scheduled_supplier_ids(db, payload.store_id, now.weekday())
    if payload.supplier_id not in today_suppliers:
        raise HTTPException(409, "Сегодня по графику заявок этот поставщик недоступен для выбранного магазина")

    start, end = _local_day_bounds(now.date())
    if db.scalar(select(Order.id).where(Order.store_id == payload.store_id, Order.supplier_id == payload.supplier_id, Order.created_at >= start, Order.created_at < end).limit(1)):
        raise HTTPException(409, "Заявка этому поставщику от магазина сегодня уже создана. Измените её или удалите перед созданием новой.")

    if payload.skip:
        if payload.items:
            raise HTTPException(400, "Для пропуска список товаров должен быть пустым")
        status = "skipped"
    else:
        if not payload.items and not (payload.comment or "").strip():
            raise HTTPException(400, "Добавьте товар, напишите комментарий или отметьте «Пропуск»")
        _validate_items(db, payload.store_id, payload.supplier_id, payload.items)
        status = "new" if payload.items else "comment_only"

    order = Order(
        store_id=payload.store_id,
        supplier_id=payload.supplier_id,
        created_by=user.id,
        created_at=now.astimezone(ZoneInfo("UTC")).replace(tzinfo=None),
        status=status,
        comment=payload.comment,
    )
    db.add(order)
    db.flush()
    for item in payload.items:
        db.add(OrderItem(order_id=order.id, product_id=item.product_id, quantity=item.quantity, snapshot_name=db.get(Product,item.product_id).name, snapshot_unit=db.get(Product,item.product_id).unit, note=None))
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
    p=require_access(db,user,"orders.list","view")
    q=select(Order).order_by(Order.created_at.desc()).limit(500)
    if p["data_scope"]=="own": q=q.where(Order.created_by==user.id)
    elif p["data_scope"]=="stores": q=q.where(Order.store_id.in_(assigned_store_ids(db,user) or [-1]))
    if store_id and p["data_scope"]!="network" and store_id not in assigned_store_ids(db,user): raise HTTPException(403,"Нет доступа к магазину")
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
    rows=list(db.scalars(q))
    if not rows:return []
    from app.services.batching import preload
    stores=preload(db,Store,[o.store_id for o in rows])
    suppliers=preload(db,Supplier,[o.supplier_id for o in rows])
    employees={e.user_id:e for e in db.scalars(select(Employee).where(Employee.user_id.in_({o.created_by for o in rows})))}
    users=preload(db,User,[o.accepted_by for o in rows])
    refs=(stores,suppliers,employees,users)
    return [_order_dict(db,o,include_items=False,refs=refs) for o in rows]


@router.get("/{order_id}")
def get_order(order_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    o = db.get(Order, order_id)
    if not o:
        raise HTTPException(404, "Заявка не найдена")
    assert_own_or_scope(db,user,"orders.list",owner_user_id=o.created_by,store_id=o.store_id,minimum="view")
    return _order_dict(db, o)


@router.get("/{order_id}/share-text")
def share_order_text(order_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    o = db.get(Order, order_id)
    if not o:
        raise HTTPException(404, "Заявка не найдена")
    assert_own_or_scope(db,user,"orders.list",owner_user_id=o.created_by,store_id=o.store_id,minimum="view")
    data = _order_dict(db, o)
    lines = [f"🏪 {data['store_name']}", f"🚚 {data['supplier_name']}", "", "📦 Заявка:"]
    if data["items"]:
        for item in data["items"]:
            qty = item['quantity_text'].rstrip('0').rstrip('.')
            lines.append(f"• {item['name']} — {qty} {item['unit']}")
    else:
        lines.append("• Пропуск — товары не требуются" if o.status == "skipped" else "• Только комментарий")
    if data.get("comment"):
        lines.extend(["", f"💬 {data['comment']}"])
    return {"text": "\n".join(lines)}


def _apply_order_update(db, o, payload, user):
    if not payload or set(payload)-{"status","items","comment"}:
        raise HTTPException(422,"Укажите статус, состав или комментарий")
    previously_accepted=o.accepted_at is not None
    old_status = o.status
    requested = payload.get("status", old_status)
    if requested not in ORDER_STATUSES:
        raise HTTPException(400, "Неизвестный статус")
    if "status" in payload:
        key = "orders.accept" if requested == "accepted" else "orders.cancel" if requested == "cancelled" else "orders.edit_new"
        assert_own_or_scope(db,user,key,owner_user_id=o.created_by,store_id=o.store_id,minimum="edit")
    if "items" in payload or "comment" in payload:
        assert_own_or_scope(db,user,"orders.edit_new",owner_user_id=o.created_by,store_id=o.store_id,minimum="edit")
        if old_status not in {"new", "comment_only"}:
            raise HTTPException(409,"Редактирование доступно для новой заявки и заявки только с комментарием")
    comment = payload.get("comment", o.comment)
    if comment is not None and not isinstance(comment, str):
        raise HTTPException(422,"Комментарий должен быть текстом")
    parsed = None
    if "items" in payload:
        try:
            parsed = [OrderItemIn(**x) for x in payload["items"]]
        except (ValidationError, TypeError, ValueError):
            raise HTTPException(422,"Некорректный состав или количество")
        _validate_items(db,o.store_id,o.supplier_id,parsed)
    has_items = bool(parsed) if parsed is not None else bool(db.scalar(select(OrderItem.id).where(OrderItem.order_id==o.id).limit(1)))
    if requested == "skipped":
        has_items = False
        parsed = []
    if requested in {"new", "comment_only"}:
        if not has_items and not (comment or "").strip():
            raise HTTPException(400,"Укажите товары или комментарий")
        if requested == "comment_only" and has_items and "status" in payload:
            raise HTTPException(400,"Для статуса «Только комментарий» уберите товары")
        requested = "new" if has_items else "comment_only"
    if parsed is not None:
        db.execute(delete(OrderItem).where(OrderItem.order_id==o.id))
        for item in parsed:
            product=db.get(Product,item.product_id)
            db.add(OrderItem(order_id=o.id,product_id=item.product_id,quantity=item.quantity,snapshot_name=product.name,snapshot_unit=product.unit))
    o.comment=comment
    o.status=requested
    if requested == "accepted" and old_status != "accepted":
        o.accepted_by=user.id
        o.accepted_at=datetime.utcnow()
        db.flush()
        # Queue in the same transaction as acceptance. Delivery runs separately.
        return send_order_accepted(db,o.id,force=previously_accepted)
    return None


@router.patch("/{order_id}")
def update_order(order_id: int, payload: dict, user=Depends(get_current_user), db: Session = Depends(get_db)):
    o=db.scalar(select(Order).where(Order.id==order_id).with_for_update())
    if not o:raise HTTPException(404,"Заявка не найдена")
    delivery=_apply_order_update(db,o,payload,user)
    db.commit()
    result=_order_dict(db,o)
    result["telegram_delivery"]=delivery
    return result


class BulkOrderIn(BaseModel):
    ids: list[int] = Field(min_length=1,max_length=100)
    action: str


@router.post("/bulk")
def bulk_orders(payload: BulkOrderIn,user=Depends(get_current_user),db:Session=Depends(get_db)):
    require_access(db,user,"orders.bulk","edit")
    if payload.action not in {"delete", "accepted", "cancelled", "new"}:
        raise HTTPException(400,"Неизвестное массовое действие")
    ids=sorted(set(payload.ids))
    rows=list(db.scalars(select(Order).where(Order.id.in_(ids)).order_by(Order.id).with_for_update()))
    if len(rows)!=len(ids):raise HTTPException(409,"Список заявок изменился. Обновите его")
    deliveries=[]
    for o in rows:
        assert_own_or_scope(db,user,"orders.bulk",owner_user_id=o.created_by,store_id=o.store_id,minimum="edit")
        if payload.action=="delete":
            assert_own_or_scope(db,user,"orders.delete",owner_user_id=o.created_by,store_id=o.store_id,minimum="edit")
            db.delete(o)
        else:
            deliveries.append({"order_id":o.id,"delivery":_apply_order_update(db,o,{"status":payload.action},user)})
    db.commit()
    return {"ok":True,"count":len(rows),"deliveries":deliveries}


@router.delete("/{order_id}")
def delete_order(order_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    o = db.get(Order, order_id)
    if not o:
        raise HTTPException(404, "Заявка не найдена")
    require_access(db,user,"orders.delete","edit"); assert_own_or_scope(db,user,"orders.delete",owner_user_id=o.created_by,store_id=o.store_id,minimum="edit")
    db.delete(o)
    db.commit()
    return {"ok": True}
