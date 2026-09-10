from __future__ import annotations

from datetime import datetime, date
from sqlalchemy import (
    String, Integer, BigInteger, Boolean, DateTime, Date, ForeignKey, Numeric,
    Text, UniqueConstraint, Index, JSON
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.database import Base


def now_utc():
    return datetime.utcnow()


class Store(Base):
    __tablename__ = "stores"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    code: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    role: Mapped[str] = mapped_column(String(32), default="seller", index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, onupdate=now_utc)


class UserStore(Base):
    __tablename__ = "user_stores"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), primary_key=True)


class Employee(Base):
    __tablename__ = "employees"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    full_name: Mapped[str] = mapped_column(String(256), index=True)
    position: Mapped[str] = mapped_column(String(32), default="seller", index=True)
    employment_status: Mapped[str] = mapped_column(String(32), default="working", index=True)
    hire_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, nullable=True, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), unique=True, nullable=True, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, onupdate=now_utc)


class EmployeeStore(Base):
    __tablename__ = "employee_stores"
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"), primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), primary_key=True)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class Supplier(Base):
    __tablename__ = "suppliers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    deadline_time: Mapped[str] = mapped_column(String(5), default="10:00")
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class Category(Base):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id", ondelete="SET NULL"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(160), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (UniqueConstraint("supplier_id", "name", name="uq_category_supplier_name"),)


class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id", ondelete="RESTRICT"), index=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(240), index=True)
    unit: Mapped[str] = mapped_column(String(64), default="шт")
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    __table_args__ = (UniqueConstraint("supplier_id", "name", name="uq_product_supplier_name"),)


class StoreProduct(Base):
    __tablename__ = "store_products"
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class StoreSupplierSchedule(Base):
    __tablename__ = "store_supplier_schedule"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id", ondelete="CASCADE"), index=True)
    weekday: Mapped[int] = mapped_column(Integer, index=True)  # 0=Mon
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (UniqueConstraint("store_id", "supplier_id", "weekday", name="uq_store_supplier_weekday"),)


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="RESTRICT"), index=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id", ondelete="RESTRICT"), index=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="new", index=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, onupdate=now_utc)


class OrderItem(Base):
    __tablename__ = "order_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"), index=True)
    quantity: Mapped[float] = mapped_column(Numeric(12, 3))
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)


class ShiftTemplate(Base):
    __tablename__ = "shift_templates"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    shift_kind: Mapped[str] = mapped_column(String(32), default="morning", index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class ShiftTemplateField(Base):
    __tablename__ = "shift_template_fields"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("shift_templates.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(80))
    label: Mapped[str] = mapped_column(String(240))
    field_type: Mapped[str] = mapped_column(String(32), default="text")
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    options_json: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    __table_args__ = (UniqueConstraint("template_id", "key", name="uq_shift_field_key"),)


class ShiftReport(Base):
    __tablename__ = "shift_reports"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="RESTRICT"), index=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("shift_templates.id", ondelete="RESTRICT"), index=True)
    submitted_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id", ondelete="SET NULL"), nullable=True, index=True)
    work_assignment_id: Mapped[int | None] = mapped_column(ForeignKey("work_shift_assignments.id", ondelete="SET NULL"), nullable=True, index=True)
    work_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    shift_kind: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    review_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, index=True)


class ShiftReportValue(Base):
    __tablename__ = "shift_report_values"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("shift_reports.id", ondelete="CASCADE"), index=True)
    field_id: Mapped[int] = mapped_column(ForeignKey("shift_template_fields.id", ondelete="RESTRICT"), index=True)
    value_json: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSON, nullable=True)


class ShiftReviewRemark(Base):
    __tablename__ = "shift_review_remarks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("shift_reports.id", ondelete="CASCADE"), index=True)
    field_id: Mapped[int | None] = mapped_column(ForeignKey("shift_template_fields.id", ondelete="SET NULL"), nullable=True, index=True)
    reviewer_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    remark: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, index=True)


class ShiftReviewEvent(Base):
    __tablename__ = "shift_review_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("shift_reports.id", ondelete="CASCADE"), index=True)
    reviewer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(48), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot_json: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, index=True)


class InspectionTemplate(Base):
    __tablename__ = "inspection_templates"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(180), unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, onupdate=now_utc)


class InspectionTemplateStore(Base):
    __tablename__ = "inspection_template_stores"
    template_id: Mapped[int] = mapped_column(ForeignKey("inspection_templates.id", ondelete="CASCADE"), primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), primary_key=True)


class InspectionTemplateField(Base):
    __tablename__ = "inspection_template_fields"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("inspection_templates.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(80))
    label: Mapped[str] = mapped_column(String(240))
    field_type: Mapped[str] = mapped_column(String(32), default="boolean")
    required: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    options_json: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    creates_violation_on_false: Mapped[bool] = mapped_column(Boolean, default=False)
    help_text: Mapped[str | None] = mapped_column(String(500), nullable=True)
    allow_comment: Mapped[bool] = mapped_column(Boolean, default=True)
    require_photo: Mapped[bool] = mapped_column(Boolean, default=False)
    default_severity: Mapped[str] = mapped_column(String(32), default="medium")
    violation_rule_json: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    __table_args__ = (UniqueConstraint("template_id", "key", name="uq_inspection_field_key"),)


class Inspection(Base):
    __tablename__ = "inspections"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="RESTRICT"), index=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("inspection_templates.id", ondelete="RESTRICT"), index=True)
    manager_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    score: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)


class InspectionValue(Base):
    __tablename__ = "inspection_values"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    inspection_id: Mapped[int] = mapped_column(ForeignKey("inspections.id", ondelete="CASCADE"), index=True)
    field_id: Mapped[int] = mapped_column(ForeignKey("inspection_template_fields.id", ondelete="RESTRICT"), index=True)
    value_json: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSON, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    manual_violation: Mapped[bool] = mapped_column(Boolean, default=False)
    severity: Mapped[str | None] = mapped_column(String(32), nullable=True)


class Violation(Base):
    __tablename__ = "violations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="RESTRICT"), index=True)
    inspection_id: Mapped[int | None] = mapped_column(ForeignKey("inspections.id", ondelete="SET NULL"), nullable=True, index=True)
    field_id: Mapped[int | None] = mapped_column(ForeignKey("inspection_template_fields.id", ondelete="SET NULL"), nullable=True, index=True)
    inspection_value_id: Mapped[int | None] = mapped_column(ForeignKey("inspection_values.id", ondelete="SET NULL"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(300))
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    severity: Mapped[str] = mapped_column(String(32), default="medium", index=True)
    status: Mapped[str] = mapped_column(String(32), default="open", index=True)
    task_v2_id: Mapped[int | None] = mapped_column(ForeignKey("tasks_v2.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="SET NULL"), nullable=True, index=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    assigned_to: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[str] = mapped_column(String(32), default="medium", index=True)
    status: Mapped[str] = mapped_column(String(32), default="new", index=True)
    deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    require_photo: Mapped[bool] = mapped_column(Boolean, default=False)
    reminder_minutes_before: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CashCollection(Base):
    __tablename__ = "cash_collections"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="RESTRICT"), index=True)
    collected_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    handed_to: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    amount: Mapped[float] = mapped_column(Numeric(14, 2))
    status: Mapped[str] = mapped_column(String(32), default="collected", index=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, index=True)
    handed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Mentorship(Base):
    __tablename__ = "mentorships"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="SET NULL"), nullable=True, index=True)
    mentor_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    trainee_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    start_date: Mapped[date] = mapped_column(Date, default=date.today)
    stage: Mapped[str] = mapped_column(String(120), default="start")
    result_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class PlanFact(Base):
    __tablename__ = "plan_fact"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    period_type: Mapped[str] = mapped_column(String(32), default="monthly", index=True)
    period_date: Mapped[date] = mapped_column(Date, index=True)
    plan: Mapped[float] = mapped_column(Numeric(14, 2))
    fact: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    source: Mapped[str] = mapped_column(String(32), default="manual")
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, onupdate=now_utc)
    __table_args__ = (UniqueConstraint("store_id", "period_type", "period_date", name="uq_planfact_store_period"),)


class Broadcast(Base):
    __tablename__ = "broadcasts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    message: Mapped[str] = mapped_column(Text)
    role_targets: Mapped[list | None] = mapped_column(JSON, nullable=True)
    store_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    send_time: Mapped[str] = mapped_column(String(5))
    weekdays: Mapped[list | None] = mapped_column(JSON, nullable=True)
    button_text: Mapped[str | None] = mapped_column(String(120), nullable=True)
    button_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class NotificationLog(Base):
    __tablename__ = "notification_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(300), unique=True, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, index=True)


class PhotoRequest(Base):
    __tablename__ = "photo_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    entity_type: Mapped[str] = mapped_column(String(64), index=True)
    entity_id: Mapped[int] = mapped_column(Integer, index=True)
    label: Mapped[str | None] = mapped_column(String(240), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="waiting", index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class Photo(Base):
    __tablename__ = "photos"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(64), index=True)
    entity_id: Mapped[int] = mapped_column(Integer, index=True)
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="SET NULL"), nullable=True, index=True)
    uploaded_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    telegram_file_id: Mapped[str] = mapped_column(Text)
    telegram_file_unique_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    label: Mapped[str | None] = mapped_column(String(240), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, index=True)


class UnitOfMeasure(Base):
    __tablename__ = "units_of_measure"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    short_name: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class ProductOrderLimit(Base):
    __tablename__ = "product_order_limits"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=True, index=True)
    min_qty: Mapped[float | None] = mapped_column(Numeric(12, 3), nullable=True)
    max_qty: Mapped[float | None] = mapped_column(Numeric(12, 3), nullable=True)
    step_qty: Mapped[float | None] = mapped_column(Numeric(12, 3), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    __table_args__ = (UniqueConstraint("product_id", "store_id", name="uq_product_limit_store"),)


class OrderScheduleRule(Base):
    __tablename__ = "order_schedule_rules"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id", ondelete="CASCADE"), index=True)
    schedule_type: Mapped[str] = mapped_column(String(24), default="order", index=True)  # order/delivery
    weekday: Mapped[int] = mapped_column(Integer, index=True)  # 0=Mon
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    __table_args__ = (UniqueConstraint("store_id", "supplier_id", "schedule_type", "weekday", name="uq_order_schedule_rule"),)


class PriceListUploadRequest(Base):
    __tablename__ = "price_list_upload_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id", ondelete="CASCADE"), index=True)
    title: Mapped[str | None] = mapped_column(String(240), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="waiting", index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, index=True)


class PriceList(Base):
    __tablename__ = "price_lists"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(240))
    uploaded_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    telegram_file_id: Mapped[str] = mapped_column(Text)
    telegram_file_unique_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    file_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, index=True)


class WorkShiftAssignment(Base):
    __tablename__ = "work_shift_assignments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"), nullable=True, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)  # legacy v1.3
    work_date: Mapped[date] = mapped_column(Date, index=True)
    shift_type: Mapped[str] = mapped_column(String(16), index=True)  # day/night
    slot: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, onupdate=now_utc)
    __table_args__ = (
        UniqueConstraint("employee_id", "work_date", name="uq_work_shift_employee_date"),
        UniqueConstraint("store_id", "work_date", "shift_type", "slot", name="uq_work_shift_store_date_slot"),
    )


class WorkAbsence(Base):
    __tablename__ = "work_absences"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"), nullable=True, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)  # legacy v1.3
    date_from: Mapped[date] = mapped_column(Date, index=True)
    date_to: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)  # day_off/vacation/sick/training
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, onupdate=now_utc)


class WorkScheduleChange(Base):
    __tablename__ = "work_schedule_changes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action: Mapped[str] = mapped_column(String(32), index=True)
    entity_type: Mapped[str] = mapped_column(String(32), index=True)
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="SET NULL"), nullable=True, index=True)
    employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id", ondelete="SET NULL"), nullable=True, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)  # legacy v1.3
    changed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    change_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    old_json: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSON, nullable=True)
    new_json: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, index=True)


class WorkSubstitution(Base):
    __tablename__ = "work_substitutions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    work_date: Mapped[date] = mapped_column(Date, index=True)
    shift_type: Mapped[str] = mapped_column(String(16), index=True)
    absent_employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id", ondelete="SET NULL"), nullable=True, index=True)
    replacement_employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id", ondelete="SET NULL"), nullable=True, index=True)
    absent_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)  # legacy v1.3
    replacement_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)  # legacy v1.3
    reason: Mapped[str | None] = mapped_column(String(240), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="new", index=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, onupdate=now_utc)

class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value_json: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, onupdate=now_utc)


Index("ix_orders_store_supplier_created", Order.store_id, Order.supplier_id, Order.created_at)
Index("ix_tasks_assignee_status_deadline", Task.assigned_to, Task.status, Task.deadline)
Index("ix_shift_store_kind_submitted", ShiftReport.store_id, ShiftReport.shift_kind, ShiftReport.submitted_at)
Index("ix_inspection_store_completed", Inspection.store_id, Inspection.completed_at)
Index("ix_work_shift_store_date_type", WorkShiftAssignment.store_id, WorkShiftAssignment.work_date, WorkShiftAssignment.shift_type)
Index("ix_work_shift_user_date", WorkShiftAssignment.user_id, WorkShiftAssignment.work_date)
Index("ix_work_absence_user_dates", WorkAbsence.user_id, WorkAbsence.date_from, WorkAbsence.date_to)
Index("ix_work_sub_store_date", WorkSubstitution.store_id, WorkSubstitution.work_date, WorkSubstitution.shift_type)

# --- Tasks v2: multi-recipient, store targets, checklists, review, comments and attachments ---
class TaskV2(Base):
    __tablename__ = "tasks_v2"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(300), index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[str] = mapped_column(String(32), default="medium", index=True)
    deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    require_photo: Mapped[bool] = mapped_column(Boolean, default=False)
    require_file: Mapped[bool] = mapped_column(Boolean, default=False)
    require_comment: Mapped[bool] = mapped_column(Boolean, default=False)
    recurrence: Mapped[str] = mapped_column(String(32), default="none", index=True)
    recurrence_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, onupdate=now_utc)


class TaskTarget(Base):
    __tablename__ = "task_targets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks_v2.id", ondelete="CASCADE"), index=True)
    target_type: Mapped[str] = mapped_column(String(32), index=True)  # employee/store
    employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id", ondelete="SET NULL"), nullable=True, index=True)
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="SET NULL"), nullable=True, index=True)


class TaskAssignee(Base):
    __tablename__ = "task_assignees"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks_v2.id", ondelete="CASCADE"), index=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="RESTRICT"), index=True)
    source_store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="SET NULL"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="new", index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    review_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, index=True)
    __table_args__ = (UniqueConstraint("task_id", "employee_id", name="uq_task_assignee_employee"),)


class TaskChecklistItem(Base):
    __tablename__ = "task_checklist_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks_v2.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(String(500))
    require_photo: Mapped[bool] = mapped_column(Boolean, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class TaskChecklistProgress(Base):
    __tablename__ = "task_checklist_progress"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    assignee_id: Mapped[int] = mapped_column(ForeignKey("task_assignees.id", ondelete="CASCADE"), index=True)
    checklist_item_id: Mapped[int] = mapped_column(ForeignKey("task_checklist_items.id", ondelete="CASCADE"), index=True)
    done: Mapped[bool] = mapped_column(Boolean, default=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, onupdate=now_utc)
    __table_args__ = (UniqueConstraint("assignee_id", "checklist_item_id", name="uq_task_checklist_progress"),)


class TaskComment(Base):
    __tablename__ = "task_comments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks_v2.id", ondelete="CASCADE"), index=True)
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("task_assignees.id", ondelete="CASCADE"), nullable=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    text: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(32), default="comment", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, index=True)


class TaskHistory(Base):
    __tablename__ = "task_history_v2"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks_v2.id", ondelete="CASCADE"), index=True)
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("task_assignees.id", ondelete="SET NULL"), nullable=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    action: Mapped[str] = mapped_column(String(80), index=True)
    details_json: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, index=True)


class TaskAttachmentRequest(Base):
    __tablename__ = "task_attachment_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks_v2.id", ondelete="CASCADE"), index=True)
    assignee_id: Mapped[int] = mapped_column(ForeignKey("task_assignees.id", ondelete="CASCADE"), index=True)
    checklist_item_id: Mapped[int | None] = mapped_column(ForeignKey("task_checklist_items.id", ondelete="SET NULL"), nullable=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)  # photo/file
    label: Mapped[str | None] = mapped_column(String(240), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="waiting", index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, index=True)


class TaskAttachment(Base):
    __tablename__ = "task_attachments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks_v2.id", ondelete="CASCADE"), index=True)
    assignee_id: Mapped[int] = mapped_column(ForeignKey("task_assignees.id", ondelete="CASCADE"), index=True)
    checklist_item_id: Mapped[int | None] = mapped_column(ForeignKey("task_checklist_items.id", ondelete="SET NULL"), nullable=True, index=True)
    uploaded_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    telegram_file_id: Mapped[str] = mapped_column(Text)
    telegram_file_unique_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    file_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(160), nullable=True)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    label: Mapped[str | None] = mapped_column(String(240), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, index=True)
