from test_upgrade import env
from app.db.database import session_scope
from app.db.models import Store,Supplier,OrderScheduleRule
from sqlalchemy import select

def setup_rows(ids):
    with session_scope() as db:
        s=Store(name='Second',active=True);p=Supplier(name='First',active=True);other=Supplier(name='Other',active=True)
        db.add_all([s,p,other]);db.flush();out=(s.id,p.id,other.id)
        db.add_all([OrderScheduleRule(store_id=ids['sid'],supplier_id=p.id,weekday=0,schedule_type='order'),OrderScheduleRule(store_id=s.id,supplier_id=other.id,weekday=3,schedule_type='order')])
    return out

def snapshot():
    with session_scope() as db:return sorted((x.store_id,x.supplier_id,x.weekday) for x in db.scalars(select(OrderScheduleRule)).all())

def test_bulk_replaces_only_selection_and_is_repeatable(env):
    client,_,ids,admin,_=env;s,p,o=setup_rows(ids)
    body={'supplier_id':p,'store_ids':[ids['sid'],s,s],'weekdays':[1,4,4]}
    for _ in range(2):
        r=client.put('/api/order-settings/schedule/bulk',json=body,headers=admin)
        assert r.status_code==200,r.text
        assert r.json()['store_count']==2
        assert snapshot()==sorted([(ids['sid'],p,1),(ids['sid'],p,4),(s,p,1),(s,p,4),(s,o,3)])

def test_bulk_invalid_or_unauthorized_is_atomic(env):
    client,_,ids,admin,seller=env;s,p,o=setup_rows(ids);before=snapshot()
    for stores,days,auth in [([ids['sid'],999999],[1],admin),([ids['sid']],[],admin),([ids['sid']],[7],admin),([ids['sid']],[1],seller)]:
        r=client.put('/api/order-settings/schedule/bulk',json={'supplier_id':p,'store_ids':stores,'weekdays':days},headers=auth)
        assert r.status_code in (400,403,422),r.text
        assert snapshot()==before
