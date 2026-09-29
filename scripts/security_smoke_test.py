"""Security regression smoke test for v1.7.10.
Run from project root: python scripts/security_smoke_test.py
Uses an isolated SQLite database and never touches production DATABASE_URL.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB_PATH = Path(tempfile.gettempdir()) / "prichal_core_security_smoke.sqlite3"
try:
    DB_PATH.unlink()
except FileNotFoundError:
    pass

os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["BOT_TOKEN"] = "test-token"
os.environ["AUTO_SET_WEBHOOK"] = "false"
os.environ["ENABLE_SCHEDULER"] = "false"
os.environ["APP_ENV"] = "test"
os.environ["BOOTSTRAP_ADMIN_TELEGRAM_ID"] = ""

from fastapi.testclient import TestClient
from sqlalchemy import select

from main import app
import app.db.database as database
from app.core.permissions import effective_permission
from app.core.security import create_session
from app.db.models import (
    Employee,
    EmployeeStore,
    Order,
    RoleDefinition,
    RolePermission,
    Store,
    Supplier,
    User,
    UserStore,
)


def auth(token: str):
    return {"Authorization": f"Bearer {token}"}


def expect(status: int, response, label: str):
    if response.status_code != status:
        raise AssertionError(f"{label}: expected HTTP {status}, got {response.status_code}: {response.text}")


with TestClient(app) as client:
    db = database.SessionLocal()
    try:
        store_a = Store(name="Security Store A", code="SEC-A")
        store_b = Store(name="Security Store B", code="SEC-B")
        supplier = Supplier(name="Security Supplier", deadline_time="10:00")
        db.add_all([store_a, store_b, supplier])
        db.flush()

        # Custom role deliberately keeps legacy/base role 'manager'. It must NOT inherit
        # manager capabilities unless the exact role permission grants them.
        custom = RoleDefinition(
            key="limited_manager",
            name="Limited manager",
            system=False,
            base_role="manager",
            hidden=False,
            archived=False,
        )
        db.add(custom)
        db.flush()
        db.add(RolePermission(
            role_key="limited_manager",
            permission_key="orders.list",
            access_level="view",
            data_scope="stores",
        ))

        limited = User(
            telegram_id=910001,
            full_name="Limited Manager",
            role="manager",
            role_key="limited_manager",
            status="active",
            active=True,
        )
        admin = User(
            telegram_id=910002,
            full_name="System Admin",
            role="admin",
            role_key="admin",
            status="active",
            active=True,
        )
        db.add_all([limited, admin])
        db.flush()
        db.add(UserStore(user_id=limited.id, store_id=store_a.id))
        emp = Employee(
            full_name="Limited Manager",
            position="manager",
            employment_status="working",
            telegram_id=limited.telegram_id,
            user_id=limited.id,
            active=True,
        )
        db.add(emp)
        db.flush()
        db.add(EmployeeStore(employee_id=emp.id, store_id=store_a.id))

        # Same creator, two stores: store scope must still hide Store B.
        db.add_all([
            Order(store_id=store_a.id, supplier_id=supplier.id, created_by=limited.id, status="new"),
            Order(store_id=store_b.id, supplier_id=supplier.id, created_by=limited.id, status="new"),
        ])
        db.commit()

        limited_token = create_session(db, limited)
        admin_token = create_session(db, admin)
        db.commit()

        # Custom role must not become implicit superuser merely because base_role=manager.
        p = effective_permission(db, limited, "orders.products_create")
        assert p["access_level"] == "hidden", p

        # Immutable system administrator remains network-wide superuser.
        p_admin = effective_permission(db, admin, "orders.products_create")
        assert p_admin == {"access_level": "edit", "data_scope": "network"}, p_admin
    finally:
        db.close()

    # 1) Store scope is enforced at API level.
    r = client.get("/api/orders", headers=auth(limited_token))
    expect(200, r, "orders list")
    rows = r.json()
    if len(rows) != 1 or rows[0]["store_id"] != store_a.id:
        raise AssertionError(f"orders scope leak: {rows}")

    # 2) Hidden product-create permission blocks direct API use despite base_role manager.
    r = client.post(
        "/api/order-settings/products",
        headers=auth(limited_token),
        json={"name": "Forbidden product", "supplier_id": supplier.id, "unit": "шт"},
    )
    expect(403, r, "product create permission")

    # 3) Hidden employees permission blocks direct API use.
    r = client.get("/api/employees", headers=auth(limited_token))
    expect(403, r, "employees view permission")

    # 4) Frozen module cannot be reached via direct API URL.
    r = client.get("/api/cash", headers=auth(limited_token))
    expect(404, r, "disabled cash module")

    # 5) System admin can access protected operational data without RolePermission rows.
    r = client.get("/api/employees", headers=auth(admin_token))
    expect(200, r, "admin superuser employees")

    # 6) Legacy admin endpoint must check role_key, not base_role. A custom role copied
    # from/derived from admin may carry base_role=admin in old data but must still fail.
    db = database.SessionLocal()
    try:
        legacy_copy = RoleDefinition(
            key="legacy_admin_copy",
            name="Legacy admin copy",
            system=False,
            base_role="admin",
            hidden=False,
            archived=False,
        )
        db.add(legacy_copy)
        db.flush()
        copied_user = User(
            telegram_id=910003,
            full_name="Copied Admin",
            role="admin",
            role_key="legacy_admin_copy",
            status="active",
            active=True,
        )
        db.add(copied_user)
        db.flush()
        copied_token = create_session(db, copied_user)
        db.commit()
    finally:
        db.close()
    r = client.get("/api/admin/stores", headers=auth(copied_token))
    expect(403, r, "role_key must gate legacy admin endpoint")

    # 7) Copying the administrator role copies its permission matrix but does not
    # create another immutable/legacy admin base role.
    r = client.post(
        "/api/admin-center/roles",
        headers=auth(admin_token),
        json={"name": "Admin-like custom", "key": "admin_like_custom", "copy_from": "admin"},
    )
    expect(200, r, "copy admin role")
    db = database.SessionLocal()
    try:
        copied_role = db.get(RoleDefinition, "admin_like_custom")
        if not copied_role or copied_role.base_role == "admin" or copied_role.system:
            raise AssertionError(f"unsafe copied admin role: {copied_role and copied_role.base_role}")
    finally:
        db.close()

print("SECURITY_SMOKE_OK")
print("- custom role permissions override legacy/base role")
print("- data scope hides foreign stores")
print("- direct API calls respect permission matrix")
print("- frozen modules return 404")
print("- system admin remains superuser")
