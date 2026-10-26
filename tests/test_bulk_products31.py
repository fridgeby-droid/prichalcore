from decimal import Decimal
from sqlalchemy import select
from test_upgrade import env
from test_price_lists import prices,snapshot,confirm
from app.db import database
from app.db.models import Product,Supplier,Category,ProductOrderLimit,ImportedPriceItem
from app.services import price_import as svc

def setup(env):
    with database.session_scope() as db:
        a=Supplier(name='A');b=Supplier(name='B');db.add_all([a,b]);db.flush()
        cat=Category(name='Category',supplier_id=b.id);db.add(cat);db.flush()
        ps=[Product(name=f'Item{i}',supplier_id=a.id,unit='шт',active=True) for i in range(2)];db.add_all(ps);db.flush()
        return [p.id for p in ps],a.id,b.id,cat.id

def test_bulk_changes_fields_steps_and_clears_category(env):
    c,_,_,auth,_=env;ids,a,b,cat=setup(env)
    r=c.patch('/api/order-settings/products/bulk',headers=auth,json={'ids':ids,'changes':{'supplier_id':b,'category_id':cat,'unit':'кг','step_qty':'0.25'},'expected_units':{str(i):'шт' for i in ids}})
    assert r.status_code==200,r.text
    with database.session_scope() as db:
        for i in ids:
            p=db.get(Product,i);assert (p.supplier_id,p.category_id,p.unit,p.name)==(b,cat,'кг',f'Item{ids.index(i)}')
            assert db.scalar(select(ProductOrderLimit).where(ProductOrderLimit.product_id==i)).step_qty==Decimal('.25')
    r=c.patch('/api/order-settings/products/bulk',headers=auth,json={'ids':ids,'changes':{'category_id':None,'active':False}});assert r.status_code==200,r.text
    with database.session_scope() as db:assert all(db.get(Product,i).category_id is None and not db.get(Product,i).active for i in ids)

def test_bulk_invalid_atomic_and_permissions(env):
    c,_,_,auth,seller=env;ids,a,b,cat=setup(env)
    for changes,expected,status in [({'category_id':cat},{},400),({'unit':'кг'},{str(i):'шт' for i in ids},422),({'step_qty':'0'},{str(i):'шт' for i in ids},422),({'step_qty':'1'},{str(i):'кг' for i in ids},409)]:
        r=c.patch('/api/order-settings/products/bulk',headers=auth,json={'ids':ids,'changes':changes,'expected_units':expected});assert r.status_code==status,r.text
    assert c.patch('/api/order-settings/products/bulk',headers=seller,json={'ids':ids,'changes':{'active':False}}).status_code==403
    assert c.patch('/api/order-settings/products/bulk',headers=auth,json={'ids':ids+[999999],'changes':{'active':False}}).status_code==409
    with database.session_scope() as db:assert all(db.get(Product,i).unit=='шт' and db.get(Product,i).active and db.get(Product,i).category_id is None for i in ids)

def test_import_overrides_survive_sync_and_supplier_rejected_atomically(prices):
    env,cid,sid=prices;c,_,_,auth,_=env
    svc.apply_snapshot(cid,'one',snapshot());i=confirm(prices)
    manual,a,b,cat=setup(env)
    r=c.patch('/api/order-settings/products/bulk',headers=auth,json={'ids':[manual[0],i.product_id],'changes':{'supplier_id':b}});assert r.status_code==400,r.text
    with database.session_scope() as db:assert db.get(Product,manual[0]).supplier_id==a
    r=c.patch('/api/order-settings/products/bulk',headers=auth,json={'ids':[i.product_id],'changes':{'unit':'упак','step_qty':'2'},'expected_units':{str(i.product_id):'кг'}});assert r.status_code==200,r.text
    svc.apply_snapshot(cid,'one',snapshot())
    with database.session_scope() as db:
        assert db.get(Product,i.product_id).unit=='упак'
        assert db.get(ImportedPriceItem,i.id).step==Decimal('2')
        assert db.get(ImportedPriceItem,i.id).overrides['unit']

def test_later_product_conflict_rolls_back_earlier_changes(env):
    c,_,_,auth,_=env;ids,a,b,cat=setup(env)
    with database.session_scope() as db:db.get(Product,ids[0]).supplier_id=b
    r=c.patch('/api/order-settings/products/bulk',headers=auth,json={'ids':ids,'changes':{'category_id':cat}})
    assert r.status_code==400,r.text
    with database.session_scope() as db:assert all(db.get(Product,i).category_id is None for i in ids)
