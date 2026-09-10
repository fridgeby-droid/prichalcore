from __future__ import annotations

from contextlib import contextmanager
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from app.core.config import DATABASE_URL


class Base(DeclarativeBase):
    pass


def normalize_database_url(url: str) -> str:
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    return url


_engine = None
SessionLocal = None


def get_engine():
    global _engine, SessionLocal
    if _engine is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is not configured")
        url = normalize_database_url(DATABASE_URL)
        kwargs = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        else:
            kwargs.update({"pool_size": 5, "max_overflow": 10, "pool_recycle": 300})
        _engine = create_engine(url, **kwargs)
        SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False, expire_on_commit=False)
    return _engine


def _postgres_schedule_upgrade(engine):
    """Upgrade v1.3 schedule tables in place without deleting schedule history."""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "work_shift_assignments" not in tables:
        return
    with engine.begin() as conn:
        def add_col(table: str, col: str, ddl: str):
            cols = {x["name"] for x in inspect(conn).get_columns(table)}
            if col not in cols:
                conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {ddl}'))

        add_col("work_shift_assignments", "employee_id", "employee_id INTEGER REFERENCES employees(id) ON DELETE CASCADE")
        add_col("work_absences", "employee_id", "employee_id INTEGER REFERENCES employees(id) ON DELETE CASCADE")
        add_col("work_schedule_changes", "employee_id", "employee_id INTEGER REFERENCES employees(id) ON DELETE SET NULL")
        add_col("work_substitutions", "absent_employee_id", "absent_employee_id INTEGER REFERENCES employees(id) ON DELETE SET NULL")
        add_col("work_substitutions", "replacement_employee_id", "replacement_employee_id INTEGER REFERENCES employees(id) ON DELETE SET NULL")

        # v1.3 required user_id for assignments/absences. v1.4 allows employees without Telegram accounts.
        conn.execute(text("ALTER TABLE work_shift_assignments ALTER COLUMN user_id DROP NOT NULL"))
        conn.execute(text("ALTER TABLE work_absences ALTER COLUMN user_id DROP NOT NULL"))

        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_work_shift_employee_date ON work_shift_assignments(employee_id, work_date)"))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_work_shift_employee_date_idx ON work_shift_assignments(employee_id, work_date) WHERE employee_id IS NOT NULL"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_work_absence_employee_dates ON work_absences(employee_id, date_from, date_to)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_work_change_employee ON work_schedule_changes(employee_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_work_sub_absent_employee ON work_substitutions(absent_employee_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_work_sub_replacement_employee ON work_substitutions(replacement_employee_id)"))


def _seed_employees_from_users():
    from app.db.models import AppSetting, Employee, EmployeeStore, User, UserStore
    get_engine()
    db = SessionLocal()
    try:
        marker = db.get(AppSetting, "employee_model_migrated_v14")
        if marker and marker.value_json is True:
            return
        users = list(db.scalars(select(User).order_by(User.id)).all())
        for u in users:
            employee = db.scalar(select(Employee).where(Employee.user_id == u.id))
            if not employee:
                employee = db.scalar(select(Employee).where(Employee.telegram_id == u.telegram_id))
            if not employee:
                employee = Employee(
                    full_name=u.full_name or u.username or str(u.telegram_id),
                    position=u.role if u.role in {"seller", "mentor", "manager", "operations_director", "leader", "admin"} else "seller",
                    employment_status="working" if u.status == "active" and u.active else "trainee",
                    telegram_id=u.telegram_id,
                    user_id=u.id,
                    active=True,
                )
                db.add(employee)
                db.flush()
            else:
                if employee.user_id is None:
                    employee.user_id = u.id
                if employee.telegram_id is None:
                    employee.telegram_id = u.telegram_id
            user_store_ids = list(db.scalars(select(UserStore.store_id).where(UserStore.user_id == u.id)).all())
            existing = set(db.scalars(select(EmployeeStore.store_id).where(EmployeeStore.employee_id == employee.id)).all())
            for sid in user_store_ids:
                if sid not in existing:
                    db.add(EmployeeStore(employee_id=employee.id, store_id=sid))
        marker = db.get(AppSetting, "employee_model_migrated_v14")
        if not marker:
            db.add(AppSetting(key="employee_model_migrated_v14", value_json=True))
        else:
            marker.value_json = True
        db.commit()
    finally:
        db.close()


def _backfill_schedule_employee_ids(engine):
    dialect = engine.dialect.name
    with engine.begin() as conn:
        if dialect == "postgresql":
            conn.execute(text("""
                UPDATE work_shift_assignments w
                SET employee_id = e.id
                FROM employees e
                WHERE w.employee_id IS NULL AND w.user_id = e.user_id
            """))
            conn.execute(text("""
                UPDATE work_absences w
                SET employee_id = e.id
                FROM employees e
                WHERE w.employee_id IS NULL AND w.user_id = e.user_id
            """))
            conn.execute(text("""
                UPDATE work_schedule_changes w
                SET employee_id = e.id
                FROM employees e
                WHERE w.employee_id IS NULL AND w.user_id = e.user_id
            """))
            conn.execute(text("""
                UPDATE work_substitutions w
                SET absent_employee_id = e.id
                FROM employees e
                WHERE w.absent_employee_id IS NULL AND w.absent_user_id = e.user_id
            """))
            conn.execute(text("""
                UPDATE work_substitutions w
                SET replacement_employee_id = e.id
                FROM employees e
                WHERE w.replacement_employee_id IS NULL AND w.replacement_user_id = e.user_id
            """))
        else:
            # Fresh SQLite test databases already have the new columns; this only handles legacy rows if present.
            conn.execute(text("UPDATE work_shift_assignments SET employee_id=(SELECT id FROM employees WHERE employees.user_id=work_shift_assignments.user_id) WHERE employee_id IS NULL AND user_id IS NOT NULL"))
            conn.execute(text("UPDATE work_absences SET employee_id=(SELECT id FROM employees WHERE employees.user_id=work_absences.user_id) WHERE employee_id IS NULL AND user_id IS NOT NULL"))
            conn.execute(text("UPDATE work_schedule_changes SET employee_id=(SELECT id FROM employees WHERE employees.user_id=work_schedule_changes.user_id) WHERE employee_id IS NULL AND user_id IS NOT NULL"))
            conn.execute(text("UPDATE work_substitutions SET absent_employee_id=(SELECT id FROM employees WHERE employees.user_id=work_substitutions.absent_user_id) WHERE absent_employee_id IS NULL AND absent_user_id IS NOT NULL"))
            conn.execute(text("UPDATE work_substitutions SET replacement_employee_id=(SELECT id FROM employees WHERE employees.user_id=work_substitutions.replacement_user_id) WHERE replacement_employee_id IS NULL AND replacement_user_id IS NOT NULL"))



def _postgres_handover_upgrade(engine):
    """Upgrade the handover/review schema in place without deleting existing data."""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "shift_reports" not in tables:
        return
    with engine.begin() as conn:
        def add_col(table: str, col: str, ddl: str):
            cols = {x["name"] for x in inspect(conn).get_columns(table)}
            if col not in cols:
                conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {ddl}'))

        add_col("shift_reports", "employee_id", "employee_id INTEGER REFERENCES employees(id) ON DELETE SET NULL")
        add_col("shift_reports", "work_assignment_id", "work_assignment_id INTEGER REFERENCES work_shift_assignments(id) ON DELETE SET NULL")
        add_col("shift_reports", "work_date", "work_date DATE")
        add_col("shift_reports", "revision", "revision INTEGER NOT NULL DEFAULT 1")
        add_col("shift_reports", "review_comment", "review_comment TEXT")
        add_col("shift_reports", "reviewed_by", "reviewed_by INTEGER REFERENCES users(id) ON DELETE SET NULL")
        add_col("shift_reports", "reviewed_at", "reviewed_at TIMESTAMP")

        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_shift_reports_employee ON shift_reports(employee_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_shift_reports_assignment ON shift_reports(work_assignment_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_shift_reports_work_date ON shift_reports(work_date)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_shift_reports_reviewed_at ON shift_reports(reviewed_at)"))


def _backfill_handover_employee_ids(engine):
    """Connect legacy reports to employees when a linked Telegram user exists."""
    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(text("""
                UPDATE shift_reports r
                SET employee_id = e.id
                FROM employees e
                WHERE r.employee_id IS NULL AND r.submitted_by = e.user_id
            """))
        else:
            conn.execute(text("UPDATE shift_reports SET employee_id=(SELECT id FROM employees WHERE employees.user_id=shift_reports.submitted_by) WHERE employee_id IS NULL"))


def _postgres_inspection_upgrade(engine):
    """Upgrade inspections to v1.7 without deleting existing inspection history."""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "inspections" not in tables:
        return
    with engine.begin() as conn:
        def add_col(table: str, col: str, ddl: str):
            cols = {x["name"] for x in inspect(conn).get_columns(table)}
            if col not in cols:
                conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {ddl}'))

        add_col("inspection_templates", "description", "description TEXT")
        add_col("inspection_templates", "updated_at", "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")

        add_col("inspection_template_fields", "help_text", "help_text VARCHAR(500)")
        add_col("inspection_template_fields", "allow_comment", "allow_comment BOOLEAN NOT NULL DEFAULT TRUE")
        add_col("inspection_template_fields", "require_photo", "require_photo BOOLEAN NOT NULL DEFAULT FALSE")
        add_col("inspection_template_fields", "default_severity", "default_severity VARCHAR(32) NOT NULL DEFAULT 'medium'")
        add_col("inspection_template_fields", "violation_rule_json", "violation_rule_json JSONB")

        add_col("inspections", "status", "status VARCHAR(32) NOT NULL DEFAULT 'completed'")
        add_col("inspections", "started_at", "started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
        # Draft inspections do not have completed_at until the manager finishes them.
        conn.execute(text("ALTER TABLE inspections ALTER COLUMN completed_at DROP NOT NULL"))
        conn.execute(text("UPDATE inspections SET status='completed' WHERE status IS NULL"))
        conn.execute(text("UPDATE inspections SET started_at=COALESCE(started_at, completed_at, CURRENT_TIMESTAMP)"))

        add_col("inspection_values", "comment", "comment TEXT")
        add_col("inspection_values", "manual_violation", "manual_violation BOOLEAN NOT NULL DEFAULT FALSE")
        add_col("inspection_values", "severity", "severity VARCHAR(32)")

        add_col("violations", "field_id", "field_id INTEGER REFERENCES inspection_template_fields(id) ON DELETE SET NULL")
        add_col("violations", "inspection_value_id", "inspection_value_id INTEGER REFERENCES inspection_values(id) ON DELETE SET NULL")
        add_col("violations", "comment", "comment TEXT")
        add_col("violations", "task_v2_id", "task_v2_id INTEGER REFERENCES tasks_v2(id) ON DELETE SET NULL")

        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_inspections_status ON inspections(status)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_inspections_started_at ON inspections(started_at)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_violations_field_id ON violations(field_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_violations_value_id ON violations(inspection_value_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_violations_task_v2_id ON violations(task_v2_id)"))

def init_db():
    from app.db import models  # noqa: F401
    engine = get_engine()
    # New employee tables are created first; legacy schedule tables are preserved.
    Base.metadata.create_all(engine)
    if engine.dialect.name == "postgresql":
        _postgres_schedule_upgrade(engine)
        _postgres_handover_upgrade(engine)
        _postgres_inspection_upgrade(engine)
    _seed_employees_from_users()
    _backfill_schedule_employee_ids(engine)
    _backfill_handover_employee_ids(engine)


def db_health():
    engine = get_engine()
    with engine.connect() as conn:
        probe = conn.execute(text("SELECT 1")).scalar_one()
        try:
            db_name = conn.execute(text("SELECT current_database()")).scalar_one()
        except Exception:
            db_name = "sqlite"
        return {"probe": probe, "database_name": db_name}


@contextmanager
def session_scope():
    get_engine()
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_db():
    get_engine()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
