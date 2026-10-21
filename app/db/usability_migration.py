from sqlalchemy import inspect, text


def upgrade(engine):
    columns={c['name'] for c in inspect(engine).get_columns('imported_price_items')}
    with engine.begin() as conn:
        for name, kind in [('source_name','VARCHAR(255)'),('source_unit','VARCHAR(64)'),('overrides','JSON')]:
            if name not in columns:
                conn.execute(text(f'ALTER TABLE imported_price_items ADD COLUMN {name} {kind}'))
        conn.execute(text("UPDATE imported_price_items SET source_name=(SELECT name FROM products WHERE products.id=product_id) WHERE source_name IS NULL"))
        conn.execute(text("UPDATE imported_price_items SET source_unit=(SELECT unit FROM products WHERE products.id=product_id) WHERE source_unit IS NULL"))
