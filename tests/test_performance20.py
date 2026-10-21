from datetime import datetime,timedelta
from sqlalchemy import select,delete
from test_upgrade import env
from app.db import database
from app.db.models import *
from app.api.orders import _local_now


def request(env,path,other=False):
    r=env[0].get(path,headers=env[4] if other else env[3]);assert r.status_code==200,r.text
    return int(r.headers['X-SQL-Count']),r.json()


def test_orders_and_progress_constant_queries(env):
    ids=env[2]
    with database.session_scope() as db:
        sup=Supplier(name='Supplier');db.add(sup);db.flush();sid=sup.id
        db.add(Order(store_id=ids['sid'],supplier_id=sid,created_by=ids['user'],accepted_by=ids['user'],status='new'))
        db.add(OrderScheduleRule(store_id=ids['sid'],supplier_id=sid,weekday=_local_now().weekday(),schedule_type='order'))
    before,_=request(env,'/api/orders?period=today');progress_before,_=request(env,'/api/orders/today-availability')
    with database.session_scope() as db:
        for i in range(49):
            st=Store(name='Extra '+str(i));db.add(st);db.flush()
            db.add(Order(store_id=st.id,supplier_id=sid,created_by=ids['other'],accepted_by=ids['user'],status='comment_only',comment='Text'))
            db.add(OrderScheduleRule(store_id=st.id,supplier_id=sid,weekday=_local_now().weekday(),schedule_type='order'))
    after,rows=request(env,'/api/orders?period=today');progress_after,progress=request(env,'/api/orders/today-availability')
    assert before==after and after<=6
    assert len(rows)==50 and {r['creator_name'] for r in rows}=={'Admin','Seller'}
    assert progress_before==progress_after and len(progress['stores'])==50
    assert all(s['complete'] for s in progress['stores'])
    print({'order_queries_1_50':[before,after],'progress_queries_1_50':[progress_before,progress_after]})
    count,filtered=request(env,f"/api/orders?store_id={ids['sid']}&status=new&period=today")
    assert len(filtered)==1
    with database.session_scope() as db:
        db.execute(delete(RolePermission).where(RolePermission.role_key=='seller',RolePermission.permission_key=='orders.list'))
        db.add(RolePermission(role_key='seller',permission_key='orders.list',access_level='view',data_scope='own'))
    _,own=request(env,'/api/orders',True);assert len(own)==49
    with database.session_scope() as db:
        perm=db.scalar(select(RolePermission).where(RolePermission.role_key=='seller',RolePermission.permission_key=='orders.list'));perm.data_scope='stores'
        db.add(EmployeeStore(employee_id=ids['other_employee'],store_id=ids['sid']))
    _,scoped=request(env,'/api/orders',True);assert len(scoped)==1 and scoped[0]['store_id']==ids['sid']
    with database.session_scope() as db:
        db.scalar(select(RolePermission).where(RolePermission.role_key=='seller',RolePermission.permission_key=='orders.list')).access_level='hidden'
    assert env[0].get('/api/orders',headers=env[4]).status_code==403


def test_employees_and_inspection_drafts_constant_queries(env):
    before,rows=request(env,'/api/employees')
    with database.session_scope() as db:
        for i in range(35):
            u=User(telegram_id=100+i,full_name='User '+str(i),role='seller',status='active');db.add(u);db.flush()
            e=Employee(full_name='Employee '+str(i),user_id=u.id);db.add(e);db.flush()
            db.add(EmployeeStore(employee_id=e.id,store_id=env[2]['sid']))
    after,rows=request(env,'/api/employees');assert after==before and len(rows)==37
    before,rows=request(env,'/api/inspections/drafts')
    with database.session_scope() as db:
        for i in range(35):
            st=Store(name='Store '+str(i));db.add(st);db.flush()
            db.add(Inspection(store_id=st.id,template_id=env[2]['it'],manager_id=env[2]['user'],status='draft'))
    after,rows=request(env,'/api/inspections/drafts');assert after==before and len(rows)==36
    print({'employees_queries':after,'drafts_queries_1_36':[before,after]})
    total=0;parts={}
    for k in ['meta','forms','drafts']:
        n,parts[k]=request(env,'/api/inspections/'+k);total+=n
    n,combined=request(env,'/api/inspections/conduct-context');assert combined['meta']['stores']==parts['meta']['stores'] and combined['drafts']==parts['drafts']
    assert combined['forms']==[{k:v for k,v in f.items() if k not in {'fields','used_count'}} for f in parts['forms']]
    assert n<=4 and n<total
    print({'inspection_separate_sql':total,'inspection_combined_sql':n})


def test_badges_values_queries_and_mutations(env):
    ids=env[2]
    with database.session_scope() as db:
        s=Supplier(name='Sup');db.add(s);db.flush()
        orders=[]
        for status in ['new','comment_only','skipped','accepted','cancelled']:
            o=Order(store_id=ids['sid'],supplier_id=s.id,created_by=ids['user'],status=status);db.add(o);db.flush();orders.append(o.id)
        db.get(TaskAssignee,ids['assignee']).status='new'
        task=TaskV2(title='Review',created_by=ids['user']);db.add(task);db.flush()
        db.add(TaskAssignee(task_id=task.id,employee_id=ids['other_employee'],status='review'))
        db.get(ShiftReport,ids['report']).status='review'
        db.add(TrainingAssignment(test_id=ids['test'],employee_id=ids['employee'],assigned_by=ids['user'],status='assigned'))
    count,badges=request(env,'/api/dashboard/badges')
    assert badges['orders']==2 and badges['tasks']==2 and badges['shifts']==1 and badges['testing']==1
    assert count<=8
    print({'badge_queries':count})
    assert env[0].delete('/api/orders/'+str(orders[0]),headers=env[3]).status_code==200
    _,badges=request(env,'/api/dashboard/badges');assert badges['orders']==1
    assert env[0].patch('/api/orders/'+str(orders[1]),json={'status':'accepted'},headers=env[3]).status_code==200
    _,badges=request(env,'/api/dashboard/badges');assert badges.get('orders',0)==0
