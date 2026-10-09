"""Additive upgrade: retain old mappings and S3 objects for existing photos."""
from uuid import uuid4
from sqlalchemy import inspect, text, select
from sqlalchemy.orm import Session
from app.db.models import MediaAsset, StoredObject

def upgrade(engine):
    tables=['photos','task_attachments','knowledge_media','training_questions']
    with engine.begin() as conn:
        for table in tables:
            cols={x['name'] for x in inspect(conn).get_columns(table)}
            if 'media_id' not in cols:
                conn.execute(text(f'ALTER TABLE {table} ADD COLUMN media_id VARCHAR(36) REFERENCES media_assets(id)'))
            conn.execute(text(f'CREATE INDEX IF NOT EXISTS ix_{table}_media_id ON {table}(media_id)'))
            if engine.dialect.name=='postgresql' and table!='training_questions':
                conn.execute(text(f'ALTER TABLE {table} ALTER COLUMN telegram_file_id DROP NOT NULL'))
    with Session(engine) as db, db.begin():
        existing=set(db.scalars(select(MediaAsset.legacy_file_id)).all())
        for old in db.scalars(select(StoredObject)).all():
            if old.telegram_file_id not in existing:
                db.add(MediaAsset(id=str(uuid4()),s3_key=old.s3_key,mime_type=old.mime_type or 'image/jpeg',size_bytes=old.size_bytes,legacy_file_id=old.telegram_file_id,created_at=old.created_at))
        db.flush()
        for table in tables:
            field='image_telegram_file_id' if table=='training_questions' else 'telegram_file_id'
            db.execute(text(f'UPDATE {table} SET media_id=(SELECT id FROM media_assets WHERE legacy_file_id={table}.{field}) WHERE media_id IS NULL AND {field} IS NOT NULL'))
