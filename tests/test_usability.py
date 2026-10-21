from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo
import pytest
from sqlalchemy import select,func
from test_upgrade import env
from test_price_lists import prices,snapshot,confirm,imported
from app.db import database
from app.db.models import *
from app.api.orders import _local_now
from app.services import price_import as svc


def setup_order(env):
    with database.session_scope() as db:
        s=Supplier(name='Daily',active=True,deadline_time='00:00');db.add(s);db.flush()
        db.add(OrderScheduleRule(store_id=env[2]['sid'],supplier_id=s.id,weekday=_local_now().weekday(),schedule_type='order',active=True))
        p=Product(name='Test product',supplier_id=s.id,unit='кг',active=True);db.add(p);db.flush()
        db.add(ProductOrderLimit(product_id=p.id,step_qty=Decimal('0.5'),active=True))
        return {'store_id':env[2]['sid'],'supplier_id':s.id},p.id


def test_comment_limit_delete_and_badges(env):
    c,_,ids,auth,_=env;base,pid=setup_order(env)
    assert c.post('/api/orders',json=base,headers=auth).status_code==400
    r=c.post('/api/orders',json={**base,'comment':'Не нужна доставка'},headers=auth);assert r.status_code==200,r.text
    o=r.json();assert o['status']=='comment_only' and o['creator_name']=='Admin'
    assert o['created_at'].endswith('+00:00')
    assert c.post('/api/orders',json={**base,'skip':True},headers=auth).status_code==409
    assert c.get('/api/dashboard/badges',headers=auth).json()['orders']==1
    assert c.get('/api/orders/today-availability',headers=auth).json()['stores'][0]['complete']
    assert c.patch(f"/api/orders/{o['id']}",json={'comment':' '},headers=auth).status_code==400
    assert c.patch(f"/api/orders/{o['id']}",json={'items':[{'product_id':pid,'quantity':'1.5'}]},headers=auth).json()['status']=='new'
    assert c.patch(f"/api/orders/{o['id']}",json={'items':[],'comment':'Нет потребности'},headers=auth).json()['status']=='comment_only'
    assert c.delete(f"/api/orders/{o['id']}",headers=auth).status_code==200
    assert c.get('/api/dashboard/badges',headers=auth).json().get('orders',0)==0
    r=c.post('/api/orders',json={**base,'skip':True},headers=auth);assert r.status_code==200
    assert c.get('/api/dashboard/badges',headers=auth).json().get('orders',0)==0
    assert c.get('/api/orders/today-availability',headers=auth).json()['stores'][0]['complete']


def test_bulk_routes_atomic_and_permissions(env):
    c,_,ids,auth,other=env;base,pid=setup_order(env)
    one=c.post('/api/orders',json={**base,'comment':'Комментарий'},headers=auth).json()['id']
    base2,pid2=setup_order_second(env)
    two=c.post('/api/orders',json={**base2,'items':[{'product_id':pid2,'quantity':'1'}]},headers=auth).json()['id']
    with database.session_scope() as db:
        for n,b in enumerate([base,base2]):
            dst=TelegramDestination(title=f'Dest {n}',chat_id=-100-n,active=True);db.add(dst);db.flush()
            db.add(TelegramRoute(event_type='order_accepted',target_type='supplier',target_id=b['supplier_id'],destination_id=dst.id,active=True))
    assert c.post('/api/orders/bulk',json={'ids':[one,two],'action':'accepted'},headers=other).status_code==403
    r=c.post('/api/orders/bulk',json={'ids':[one,two],'action':'accepted'},headers=auth);assert r.status_code==200,r.text
    with database.session_scope() as db:
        logs=list(db.scalars(select(TelegramDeliveryLog).where(TelegramDeliveryLog.event_type=='order_accepted')))
        assert len(logs)==2 and {x.chat_id for x in logs}=={-100,-101}
        assert all(x.status=='pending' for x in logs)
    assert c.post('/api/orders/bulk',json={'ids':[one,two],'action':'accepted'},headers=auth).status_code==200
    with database.session_scope() as db:assert db.scalar(select(func.count(TelegramDeliveryLog.id)))==2
    r=c.post('/api/orders/bulk',json={'ids':[one,99999],'action':'delete'},headers=auth);assert r.status_code==409
    assert c.get(f'/api/orders/{one}',headers=auth).status_code==200


def setup_order_second(env):
    with database.session_scope() as db:
        s=Supplier(name='Other supplier',active=True);db.add(s);db.flush()
        db.add(OrderScheduleRule(store_id=env[2]['sid'],supplier_id=s.id,weekday=_local_now().weekday(),schedule_type='order',active=True))
        p=Product(name='Other',supplier_id=s.id,unit='шт',active=True);db.add(p);db.flush()
        return {'store_id':env[2]['sid'],'supplier_id':s.id},p.id


def test_import_manual_overrides_reset_and_unit_confirmation(prices):
    env,cid,sid=prices;c,_,ids,auth,_=env
    svc.apply_snapshot(cid,'one',snapshot());confirm(prices)
    item=imported()[0];url=f'/api/order-settings/products/{item.product_id}'
    r=c.patch(url,json={'name':'Ручное имя','unit':'уп','step_qty':'6','expected_unit':'уп'},headers=auth);assert r.status_code==200,r.text
    changed=snapshot([{'id':item.external_id,'name':'Новое Saby','unit':'шт','images':[]}])
    svc.apply_snapshot(cid,'one',changed)
    with database.session_scope() as db:
        i=db.get(ImportedPriceItem,item.id);p=db.get(Product,item.product_id)
        assert (p.name,p.unit)==('Ручное имя','уп') and not p.active
        assert i.source_name=='Новое Saby' and i.confirmed_unit is None
    r=c.patch(url,json={'reset_source':['name','unit'],'step_qty':'2','expected_unit':'шт'},headers=auth);assert r.status_code==200,r.text
    with database.session_scope() as db:
        p=db.get(Product,item.product_id);assert (p.name,p.unit,p.active)==('Новое Saby','шт',True)
    c.patch(url,json={'active':False},headers=auth);svc.apply_snapshot(cid,'one',changed)
    with database.session_scope() as db:assert not db.get(Product,item.product_id).active


@pytest.mark.parametrize('value',['0','-1','NaN','Infinity','0.0000001'])
def test_invalid_product_steps(env,value):
    base,pid=setup_order(env)
    assert env[0].patch(f'/api/order-settings/products/{pid}',json={'step_qty':value},headers=env[3]).status_code==422


def test_profiles_access_and_store_filter(env):
    c,_,ids,auth,other=env
    assert c.get('/api/profile/me?employee_id='+str(ids['employee']),headers=other).status_code==403
    r=c.get('/api/profile/me?employee_id='+str(ids['other_employee']),headers=auth);assert r.status_code==200 and r.json()['full_name']=='Seller'
    for path in ['work','learning','activity']:
        assert c.get('/api/profile/'+path+'?employee_id='+str(ids['other_employee']),headers=auth).status_code==200
    with database.session_scope() as db:db.add(EmployeeStore(employee_id=ids['other_employee'],store_id=ids['sid']))
    rows=c.get('/api/employees?store_id='+str(ids['sid']),headers=auth).json()
    assert [r['id'] for r in rows]==[ids['other_employee']]
    for theme in ['forest','rose','midnight']:
        assert c.patch('/api/profile/me',json={'app_theme':theme},headers=auth).status_code==200


def test_overdue_manager_queue_once(env,monkeypatch):
    from app.services import scheduler
    c,_,ids,auth,_=env;base,pid=setup_order(env)
    with database.session_scope() as db:
        e=db.get(Employee,ids['other_employee']);e.position='manager'
        db.add(EmployeeStore(employee_id=e.id,store_id=ids['sid']))
    scheduler.process_order_deadlines();scheduler.process_order_deadlines()
    with database.session_scope() as db:
        logs=list(db.scalars(select(TelegramDeliveryLog).where(TelegramDeliveryLog.event_type=='scheduled_alert')))
        assert len(logs)==1 and logs[0].chat_id==2
        assert 'Daily' in logs[0].payload_json['text'] and 'Store' in logs[0].payload_json['text']
    progress=c.get('/api/orders/today-availability',headers=auth).json()['stores'][0]
    assert progress['overdue']
    with database.session_scope() as db:
        from sqlalchemy import delete
        db.execute(delete(NotificationLog));db.execute(delete(TelegramDeliveryLog))
    assert c.post('/api/orders',json={**base,'comment':'Не требуется'},headers=auth).status_code==200
    scheduler.process_order_deadlines()
    with database.session_scope() as db:assert db.scalar(select(func.count(TelegramDeliveryLog.id)))==0


def test_cancelled_occupies_day_until_deleted(env):
    c,_,ids,auth,_=env;base,pid=setup_order(env)
    o=c.post('/api/orders',json={**base,'comment':'Текст'},headers=auth).json()
    assert c.patch(f"/api/orders/{o['id']}",json={'status':'cancelled'},headers=auth).status_code==200
    assert c.post('/api/orders',json={**base,'comment':'Повтор'},headers=auth).status_code==409
    with database.session_scope() as db:
        from app.api.orders import _local_day_bounds
        from datetime import timedelta
        db.get(Order,o['id']).created_at=_local_day_bounds()[0]-timedelta(seconds=1)
    assert c.post('/api/orders',json={**base,'comment':'Сегодня'},headers=auth).status_code==200


def test_old_source_schema_upgrade(tmp_path):
    from sqlalchemy import create_engine,text,inspect
    from app.db.usability_migration import upgrade
    engine=create_engine('sqlite:///'+str(tmp_path/'old.db'))
    with engine.begin() as c:
        c.exec_driver_sql('CREATE TABLE products (id INTEGER PRIMARY KEY,name TEXT,unit TEXT)')
        c.exec_driver_sql("INSERT INTO products VALUES (1,'Name','кг')")
        c.exec_driver_sql('CREATE TABLE imported_price_items (id INTEGER PRIMARY KEY,product_id INTEGER,step NUMERIC,confirmed_unit TEXT)')
        c.exec_driver_sql("INSERT INTO imported_price_items VALUES (1,1,0.5,'кг')")
    upgrade(engine);upgrade(engine)
    with engine.connect() as c:
        assert c.exec_driver_sql('SELECT source_name,source_unit,step,confirmed_unit FROM imported_price_items').one()==('Name','кг',0.5,'кг')
