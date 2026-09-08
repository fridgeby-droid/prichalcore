from sqlalchemy import text
from database.db import get_engine

with get_engine().connect() as conn:
    print({
        "probe": conn.execute(text("SELECT 1")).scalar_one(),
        "database": conn.execute(text("SELECT current_database()" )).scalar_one(),
        "postgres_version": conn.execute(text("SHOW server_version" )).scalar_one(),
    })
