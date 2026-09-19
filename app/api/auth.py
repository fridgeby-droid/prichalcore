from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import select

from app.db.database import get_db
from app.db.models import UserStore, Store, Employee, RoleDefinition, RolePermission, AppSetting
from app.core.security import validate_telegram_init_data, upsert_telegram_user, create_session, get_current_user
from app.core.permissions import role_key as effective_role_key, assigned_store_ids, all_active_store_ids

router = APIRouter(prefix="/api/auth", tags=["auth"])

def _access_payload(db,user):
    rk=getattr(user,"role_key",None) or user.role
    r=db.get(RoleDefinition,rk)
    if rk=="admin":
        from app.core.permissions import all_keys
        perms={k:{"access_level":"edit","data_scope":"network"} for k in all_keys()}
    else:
        perms={x.permission_key:{"access_level":x.access_level,"data_scope":x.data_scope} for x in db.scalars(select(RolePermission).where(RolePermission.role_key==rk)).all()}
    ms=db.get(AppSetting,"module_states")
    return rk,(r.name if r else rk),perms,(ms.value_json if ms and isinstance(ms.value_json,dict) else {})


def _visible_stores(db,user,permissions):
    # Authentication payload exposes only explicitly assigned stores. Network-wide
    # selectors are returned by module APIs after checking that module's own scope.
    # The immutable administrator is the only exception.
    ids=all_active_store_ids(db) if effective_role_key(user)=="admin" else assigned_store_ids(db,user)
    rows=db.scalars(select(Store).where(Store.id.in_(ids or [-1]),Store.active.is_(True)).order_by(Store.name)).all()
    return [{"id":s.id,"name":s.name} for s in rows]



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
    employee = db.scalar(select(Employee).where(Employee.user_id == user.id))
    role_key,role_name,permissions,module_states=_access_payload(db,user)
    stores=_visible_stores(db,user,permissions)
    return {
        "status": "ok",
        "token": token,
        "user": {
            "id": user.id,
            "telegram_id": user.telegram_id,
            "full_name": user.full_name,
            "role": user.role,
            "role_key": role_key,
            "role_name": role_name,
            "permissions": permissions,
            "module_states": module_states,
            "stores": stores,
            "theme": (employee.app_theme if employee else "light"),
        },
    }


@router.get("/me")
def me(user=Depends(get_current_user), db: Session = Depends(get_db)):
    employee = db.scalar(select(Employee).where(Employee.user_id == user.id))
    role_key,role_name,permissions,module_states=_access_payload(db,user)
    stores=_visible_stores(db,user,permissions)
    return {
        "id": user.id,
        "telegram_id": user.telegram_id,
        "full_name": user.full_name,
        "role": user.role,
        "role_key": role_key,
        "role_name": role_name,
        "permissions": permissions,
        "module_states": module_states,
        "stores": stores,
        "theme": (employee.app_theme if employee else "light"),
    }
