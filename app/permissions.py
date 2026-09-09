from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import User, UserRole, UserStore

GLOBAL_ROLES = {UserRole.admin, UserRole.operations_director, UserRole.executive}


def store_ids_for_user(db: Session, user: User) -> list[int]:
    if user.role in GLOBAL_ROLES:
        from .models import Store
        return list(db.scalars(select(Store.id).where(Store.is_active.is_(True))).all())
    return list(db.scalars(select(UserStore.store_id).where(UserStore.user_id == user.id)).all())


def can_access_store(db: Session, user: User, store_id: int) -> bool:
    return user.role in GLOBAL_ROLES or store_id in store_ids_for_user(db, user)
