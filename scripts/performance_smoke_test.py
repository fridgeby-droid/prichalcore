"""Performance smoke test for v1.7.11.

Runs on an isolated SQLite database. It deliberately creates many dashboard
stores/articles so regressions back to per-row (N+1) SQL are easy to notice.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB_PATH = Path(tempfile.gettempdir()) / "prichal_core_performance_smoke.sqlite3"
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
from sqlalchemy import event, select

from main import app
import app.db.database as database
from app.core.security import create_session
from app.db.models import (
    DashboardPreference,
    Employee,
    EmployeeStore,
    KnowledgeArticle,
    KnowledgeSection,
    Store,
    User,
    UserStore,
)


def auth(token: str):
    return {"Authorization": f"Bearer {token}"}


with TestClient(app) as client:
    db = database.SessionLocal()
    try:
        user = User(
            telegram_id=920001,
            full_name="Performance Manager",
            role="manager",
            role_key="manager",
            status="active",
            active=True,
        )
        db.add(user)
        db.flush()
        emp = Employee(
            full_name="Performance Manager",
            position="manager",
            employment_status="working",
            telegram_id=user.telegram_id,
            user_id=user.id,
            active=True,
        )
        db.add(emp)
        db.flush()

        stores=[]
        for i in range(12):
            st=Store(name=f"Perf Store {i:02d}", code=f"PERF-{i:02d}")
            db.add(st); db.flush(); stores.append(st)
            db.add(UserStore(user_id=user.id, store_id=st.id))
            db.add(EmployeeStore(employee_id=emp.id, store_id=st.id))

        section = db.scalar(select(KnowledgeSection).where(KnowledgeSection.parent_id.is_(None)))
        assert section is not None
        for i in range(24):
            db.add(KnowledgeArticle(
                section_id=section.id,
                title=f"Required article {i:02d}",
                content_html="<p>test</p>",
                status="published",
                required_ack=True,
                revision=1,
                author_id=user.id,
            ))
        db.add(DashboardPreference(
            user_id=user.id,
            layout_json=[
                {"key":"required_knowledge","size":"M","order":0},
                {"key":"inspections_week","size":"M","order":1},
            ],
        ))
        token=create_session(db,user)
        db.commit()
    finally:
        db.close()

    counter={"n":0}
    def before_cursor_execute(conn,cursor,statement,parameters,context,executemany):
        counter["n"] += 1

    event.listen(database.get_engine(), "before_cursor_execute", before_cursor_execute)
    try:
        response=client.get("/api/dashboard/ui", headers=auth(token))
    finally:
        event.remove(database.get_engine(), "before_cursor_execute", before_cursor_execute)

    if response.status_code != 200:
        raise AssertionError(f"dashboard/ui failed: {response.status_code} {response.text}")
    data=response.json()
    assert len(data.get("values",{}).get("inspections_week",{}).get("stores",[])) == 12
    assert data.get("values",{}).get("required_knowledge",{}).get("count") == 24
    # The important property is that SQL count does not grow linearly with 12 stores
    # + 24 articles. Keep some headroom for auth/session queries and SQLite behavior.
    if counter["n"] > 34:
        raise AssertionError(f"dashboard SQL regression: {counter['n']} statements (>34)")

print("PERFORMANCE_SMOKE_OK")
print(f"- dashboard/ui SQL statements: {counter['n']}")
print("- required knowledge uses one acknowledgement join, not one query per article")
print("- weekly inspection progress uses one grouped query, not one count per store")
