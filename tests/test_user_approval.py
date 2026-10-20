import pytest
from sqlalchemy import select,func
from test_upgrade import env
from app.db import database
from app.db.models import User,Employee,AdminAuditLog,RoleDefinition
from app.core.security import create_session

@pytest.fixture
def pending(env):
    with database.session_scope() as db:
        u=User(telegram_id=999,full_name='Pending',role='seller',role_key='seller',status='pending',active=True);db.add(u);db.flush();uid=u.id;token=create_session(db,u)
    return env,uid,{'Authorization':'Bearer '+token}

def test_admin_approval_unlocks_existing_session(pending):
    env,uid,token=pending;c,_,_,admin,other=env
    assert c.get('/api/auth/me',headers=token).status_code==403
    assert c.post(f'/api/admin-center/users/{uid}/approve',headers=other,json={'role_key':'admin'}).status_code==403
    r=c.post(f'/api/admin-center/users/{uid}/approve',headers=admin,json={'role_key':'seller'});assert r.status_code==200,r.text
    assert c.get('/api/auth/me',headers=token).status_code==200
    assert c.post(f'/api/admin-center/users/{uid}/approve',headers=admin,json={'role_key':'seller'}).status_code==200
    with database.session_scope() as db:
        assert db.get(User,uid).status=='active'
        assert db.scalar(select(func.count(AdminAuditLog.id)).where(AdminAuditLog.action=='user.approve'))==1

def test_create_and_approve_atomically(pending):
    env,uid,token=pending;c,_,ids,admin,_=env
    r=c.post('/api/employees',headers=admin,json={'full_name':'Новый','user_id':uid,'position':'mentor','store_ids':[ids['sid']],'approve_role_key':'seller'})
    assert r.status_code==200,r.text
    with database.session_scope() as db:
        u=db.get(User,uid);e=db.get(Employee,r.json()['id']);assert u.status=='active';assert u.role_key=='seller';assert e.position=='mentor';assert e.telegram_id==999

@pytest.mark.parametrize('extra',[{'approve_role_key':'missing'},{'approve_role_key':'seller','employment_status':'fired'},{'approve_role_key':'seller','store_ids':[999999]}])
def test_failed_create_does_not_approve_or_save(pending,extra):
    env,uid,_=pending;c,_,_,admin,_=env
    r=c.post('/api/employees',headers=admin,json={'full_name':'New','user_id':uid,**extra});assert r.status_code in (400,403,409),r.text
    with database.session_scope() as db:assert db.get(User,uid).status=='pending';assert db.scalar(select(Employee).where(Employee.user_id==uid)) is None

def test_plain_save_then_edit_approval(pending):
    env,uid,_=pending;c,_,_,admin,_=env
    r=c.post('/api/employees',headers=admin,json={'full_name':'New','user_id':uid});assert r.status_code==200
    with database.session_scope() as db:assert db.get(User,uid).status=='pending'
    r=c.patch('/api/employees/'+str(r.json()['id']),headers=admin,json={'full_name':'Renamed','approve_role_key':'manager'});assert r.status_code==200,r.text
    with database.session_scope() as db:assert db.get(User,uid).status=='active';assert db.get(User,uid).role_key=='manager'

@pytest.mark.parametrize('status,active',[('blocked',True),('pending',False)])
def test_disabled_user_not_reactivated(pending,status,active):
    env,uid,_=pending;c,_,_,admin,_=env
    with database.session_scope() as db:u=db.get(User,uid);u.status=status;u.active=active
    assert c.post(f'/api/admin-center/users/{uid}/approve',headers=admin,json={'role_key':'seller'}).status_code==409
