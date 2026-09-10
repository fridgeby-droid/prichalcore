from __future__ import annotations

from contextlib import contextmanager
from sqlalchemy import create_engine, text
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


def init_db():
    from app.db import models  # noqa: F401
    engine = get_engine()
    Base.metadata.create_all(engine)


def db_health():
    engine = get_engine()
    with engine.connect() as conn:
        probe = conn.execute(text("SELECT 1")).scalar_one()
        db_name = None
        try:
            db_name = conn.execute(text("SELECT current_database()")) .scalar_one()
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
