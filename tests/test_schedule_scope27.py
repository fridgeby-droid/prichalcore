from test_upgrade import env
from app.db import database
from app.db.models import Store,Supplier,OrderScheduleRule,EmployeeStore,RolePermission
from sqlalchemy import select

def test_schedule_filters_stores_and_rules_by_employee_and_role(env):
    client,_,ids,admin,seller=env
    with database.session_scope() as db:
        p=db.scalar(select(RolePermission).where(RolePermission.role_key=='seller',RolePermission.permission_key=='orders.schedule_view'))
        p.access_level='view';p.data_scope='stores'
        s=Store(name='Outside',active=True);sup=Supplier(name='Supplier',active=True);db.add_all([s,sup]);db.flush();outside=s.id
        db.add_all([OrderScheduleRule(store_id=sid,supplier_id=sup.id,schedule_type='order',weekday=1) for sid in [ids['sid'],outside]])
    data=client.get('/api/order-settings/schedule',headers=seller).json()
    assert data['stores']==[] and data['rules']==[]
    with database.session_scope() as db:db.add(EmployeeStore(employee_id=ids['other_employee'],store_id=ids['sid']))
    data=client.get('/api/order-settings/schedule',headers=seller).json()
    assert {s['id'] for s in data['stores']}=={ids['sid']}
    assert {r['store_id'] for r in data['rules']}=={ids['sid']}
    data=client.get('/api/order-settings/schedule',headers=admin).json()
    assert {ids['sid'],outside}<={s['id'] for s in data['stores']}
    with database.session_scope() as db:
        p=db.scalar(select(RolePermission).where(RolePermission.role_key=='seller',RolePermission.permission_key=='orders.schedule_view'));p.data_scope='network'
    data=client.get('/api/order-settings/schedule',headers=seller).json()
    assert outside in {s['id'] for s in data['stores']}
