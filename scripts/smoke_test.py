"""Локальная проверка структуры БД и подключения.
Запуск из корня проекта: python scripts/smoke_test.py
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.database import init_db, db_health
from app.db.models import Base

init_db()
print("DB:", db_health())
print("TABLES:", len(Base.metadata.tables))
for name in sorted(Base.metadata.tables):
    print(" -", name)
