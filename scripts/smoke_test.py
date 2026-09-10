"""Запускается локально/в терминале после настройки DATABASE_URL.
Проверяет соединение и количество таблиц.
"""
from app.db.database import init_db, db_health
from app.db.models import Base

init_db()
print("DB:", db_health())
print("TABLES:", len(Base.metadata.tables))
for name in sorted(Base.metadata.tables):
    print(" -", name)
