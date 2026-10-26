import pytest
from sqlalchemy import inspect, text, select
from test_upgrade import env
from test_usability import setup_order
from test_order_files30 import create, excel
from app.db import database
from app.db.models import OrderAttachment, Order

MARKER = 'order-attachments-1.7.32'

def remove_marker(engine):
    with engine.begin() as conn:
        conn.execute(text('DELETE FROM core_schema_versions WHERE version=:v'), {'v': MARKER})

def test_upgrade_existing_database_and_repeat_preserves_attachment(env, monkeypatch):
    client, _, _, auth, _ = env
    base, _ = setup_order(env)
    oid = create(env, base)
    engine = database.get_engine()
    OrderAttachment.__table__.drop(engine)
    remove_marker(engine)
    # A deployed old database has all old markers, so legacy create_all is skipped.
    monkeypatch.setattr(database, '_init_legacy', lambda: pytest.fail('Legacy init must be skipped'))
    database.init_db()
    assert inspect(engine).has_table('order_attachments')
    assert 'ix_order_attachments_order_id' in {x['name'] for x in inspect(engine).get_indexes('order_attachments')}
    url = f'/api/orders/{oid}/attachments'
    assert client.get(url, headers=auth).json() == []
    data = excel()
    r = client.post(url, headers=auth, files={'file': ('test.xlsx', data)})
    assert r.status_code == 200, r.text
    aid = r.json()['id']
    # Existing table without marker also upgrades safely; normal restart is a no-op.
    remove_marker(engine)
    database.init_db()
    database.init_db()
    assert client.get(url, headers=auth).json()[0]['id'] == aid
    assert client.get(f'{url}/{aid}/content', headers=auth).content == data
    with database.session_scope() as db:
        assert db.get(Order, oid) is not None
        assert db.scalar(text('SELECT count(*) FROM core_schema_versions WHERE version=:v'), {'v':MARKER}) == 1

def test_failed_table_creation_does_not_mark_migration_done(env, monkeypatch):
    engine = database.get_engine()
    OrderAttachment.__table__.drop(engine)
    remove_marker(engine)
    def fail(*args, **kwargs):
        raise RuntimeError('simulated DDL failure')
    with monkeypatch.context() as m:
        m.setattr(OrderAttachment.__table__, 'create', fail)
        with pytest.raises(RuntimeError, match='simulated DDL failure'):
            database.init_db()
    with engine.connect() as conn:
        assert conn.scalar(text('SELECT count(*) FROM core_schema_versions WHERE version=:v'), {'v':MARKER}) == 0
    database.init_db()
    assert inspect(engine).has_table('order_attachments')
