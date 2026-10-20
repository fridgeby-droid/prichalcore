"""Migration from the previous release's product/order schemas (SQLite smoke test)."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sqlalchemy import create_engine,text,inspect
from app.db.price_migration import upgrade
from app.db.models import Base

def test_previous_schema_upgrade_is_idempotent(tmp_path):
    engine=create_engine('sqlite:///'+str(tmp_path/'upgrade.sqlite'))
    Base.metadata.create_all(engine)
    with engine.begin() as c:
        c.exec_driver_sql('DROP TABLE products')
        c.exec_driver_sql('CREATE TABLE products (id INTEGER PRIMARY KEY,supplier_id INTEGER NOT NULL,category_id INTEGER,name VARCHAR(240) NOT NULL,unit VARCHAR(64) NOT NULL,active BOOLEAN NOT NULL,created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, CONSTRAINT uq_product_supplier_name UNIQUE(supplier_id,name))')
        c.exec_driver_sql("INSERT INTO products(id,supplier_id,name,unit,active) VALUES(1,1,'Old name','кг',1)")
        c.exec_driver_sql('DROP TABLE order_items')
        c.exec_driver_sql('CREATE TABLE order_items(id INTEGER PRIMARY KEY,order_id INTEGER NOT NULL,product_id INTEGER NOT NULL REFERENCES products(id),quantity NUMERIC(12,3) NOT NULL,note TEXT)')
        c.exec_driver_sql('INSERT INTO order_items VALUES(1,1,1,0.5,NULL)')
    upgrade(engine);upgrade(engine)
    with engine.begin() as c:
        assert c.execute(text('SELECT snapshot_name,snapshot_unit,quantity FROM order_items')).one()==('Old name','кг',0.5)
        c.exec_driver_sql("INSERT INTO products(id,supplier_id,name,unit,active,created_at) VALUES(2,1,'Old name','кг',0,CURRENT_TIMESTAMP)")
        assert c.exec_driver_sql('SELECT count(*) FROM products').scalar()==2
    assert 'uq_product_supplier_name' not in {x['name'] for x in inspect(engine).get_unique_constraints('products')}
