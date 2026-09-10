from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import select

from app.db.database import get_db
from app.db.models import UserStore, Store
from app.core.security import validate_telegram_init_data, upsert_telegram_user, create_session, get_current_user

router = APIRouter(prefix="/api/auth", tags=["auth"])


class TelegramAuthIn(BaseModel):
    init_data: str


@router.post("/telegram")
def telegram_auth(payload: TelegramAuthIn, db: Session = Depends(get_db)):
    tg = validate_telegram_init_data(payload.init_data)
    user = upsert_telegram_user(db, tg)
    db.commit()
    db.refresh(user)
    if user.status != "active":
        return {"status": "pending", "telegram_id": user.telegram_id, "message": "Ожидается подтверждение администратора"}
    token = create_session(db, user)
    db.commit()
    stores = []
    if user.role in {"operations_director", "leader", "admin"}:
        stores = [{"id": s.id, "name": s.name} for s in db.scalars(select(Store).where(Store.active.is_(True)).order_by(Store.name)).all()]
    else:
        rows = db.execute(select(Store).join(UserStore, UserStore.store_id == Store.id).where(UserStore.user_id == user.id, Store.active.is_(True)).order_by(Store.name)).scalars().all()
        stores = [{"id": s.id, "name": s.name} for s in rows]
    return {
        "status": "ok",
        "token": token,
        "user": {
            "id": user.id,
            "telegram_id": user.telegram_id,
            "full_name": user.full_name,
            "role": user.role,
            "stores": stores,
        },
    }


@router.get("/me")
def me(user=Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role in {"operations_director", "leader", "admin"}:
        rows = db.scalars(select(Store).where(Store.active.is_(True)).order_by(Store.name)).all()
    else:
        rows = db.execute(select(Store).join(UserStore, UserStore.store_id == Store.id).where(UserStore.user_id == user.id, Store.active.is_(True)).order_by(Store.name)).scalars().all()
    return {
        "id": user.id,
        "telegram_id": user.telegram_id,
        "full_name": user.full_name,
        "role": user.role,
        "stores": [{"id": s.id, "name": s.name} for s in rows],
    }
