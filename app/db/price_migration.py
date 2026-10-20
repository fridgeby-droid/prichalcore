from sqlalchemy import inspect,text
from sqlalchemy.schema import CreateTable,CreateIndex
from app.db.models import ImportedPriceList,ImportedPriceItem,ImportedPriceImage,Product

def upgrade(engine):
    # Imported items are keyed by source ID; duplicate display names are valid.
    uniques=inspect(engine).get_unique_constraints('products')
    if any(x.get('name')=='uq_product_supplier_name' for x in uniques):
        if engine.dialect.name=='postgresql':
            with engine.begin() as c:c.execute(text('ALTER TABLE products DROP CONSTRAINT IF EXISTS uq_product_supplier_name'))
        elif engine.dialect.name=='sqlite':
            with engine.connect() as c:
                fk=c.exec_driver_sql('PRAGMA foreign_keys').scalar();c.commit()
                c.exec_driver_sql('PRAGMA foreign_keys=OFF');c.commit()
                try:
                    with c.begin():
                        ddl=str(CreateTable(Product.__table__).compile(engine)).replace('CREATE TABLE products','CREATE TABLE products_import_upgrade',1)
                        c.exec_driver_sql(ddl)
                        cols=', '.join(x.name for x in Product.__table__.columns)
                        c.exec_driver_sql(f'INSERT INTO products_import_upgrade ({cols}) SELECT {cols} FROM products')
                        c.exec_driver_sql('DROP TABLE products')
                        c.exec_driver_sql('ALTER TABLE products_import_upgrade RENAME TO products')
                        for index in Product.__table__.indexes:c.execute(CreateIndex(index))
                finally:c.exec_driver_sql(f'PRAGMA foreign_keys={fk}');c.commit()
    cols={x['name'] for x in inspect(engine).get_columns('order_items')}
    with engine.begin() as c:
        for name,size in [('snapshot_name',240),('snapshot_unit',64)]:
            if name not in cols:c.execute(text(f'ALTER TABLE order_items ADD COLUMN {name} VARCHAR({size})'))
        c.execute(text('UPDATE order_items SET snapshot_name=(SELECT name FROM products WHERE products.id=order_items.product_id),snapshot_unit=(SELECT unit FROM products WHERE products.id=order_items.product_id) WHERE snapshot_name IS NULL'))
        if engine.dialect.name=='postgresql':c.execute(text('ALTER TABLE order_items ALTER COLUMN quantity TYPE NUMERIC(18,6)'))
    for model in [ImportedPriceList,ImportedPriceItem,ImportedPriceImage]:model.__table__.create(engine,checkfirst=True)
