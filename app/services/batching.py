from sqlalchemy import select

def preload(db, model, ids):
    ids = {x for x in ids if x is not None}
    rows = list(db.scalars(select(model).where(model.id.in_(ids))).all()) if ids else []
    db.info.setdefault("batch_refs", []).extend(rows)
    return {x.id: x for x in rows}

def grouped(db, model, column, ids, order=None):
    ids = list(set(ids))
    result = {x: [] for x in ids}
    if not ids: return result
    q = select(model).where(column.in_(ids))
    if order is not None: q = q.order_by(order)
    for row in db.scalars(q).all(): result[getattr(row, column.key)].append(row)
    return result
