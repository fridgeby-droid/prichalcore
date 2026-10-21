from test_upgrade import env
from app.db import database
from app.db.models import User,UserStore,Store,AdminAuditLog,AppSetting
from sqlalchemy import select


def test_admin_lists_constant_sql_and_permissions(env):
    c,_,ids,auth,other=env
    def get(path):
        r=c.get('/api/admin-center/'+path,headers=auth);assert r.status_code==200,r.text
        return int(r.headers['X-SQL-Count']),r.json()
    before,_=get('users');audit_before,_=get('audit')
    with database.session_scope() as db:
        for i in range(50):
            u=User(telegram_id=1000+i,full_name=f'Name {i}',role='seller',role_key='seller',status='pending',active=True);db.add(u);db.flush()
            db.add(UserStore(user_id=u.id,store_id=ids['sid']))
            db.add(AdminAuditLog(actor_user_id=u.id,action='test',entity_type='user'))
    after,users=get('users');audit_after,audit=get('audit')
    assert before==after==4 and len(users)==52
    assert audit_before==audit_after==2 and len(audit)==50
    row=next(x for x in users if x['telegram_id']==1000)
    assert row['store_ids']==[ids['sid']] and row['status']=='pending' and row['role_key']=='seller'
    assert any(x['actor']=='Name 0' for x in audit)
    count,settings=get('system');assert count==2 and 'app_timezone' in settings
    assert c.get('/api/admin-center/users',headers=other).status_code==403
    assert c.get('/api/admin-center/audit',headers=other).status_code==403
    print({'users_2_52_sql':[before,after],'audit_0_50_sql':[audit_before,audit_after],'system_sql':count})


def test_db_probe_timing_contract(env):
    c=env[0]
    r=c.get('/db-health');assert r.status_code==200,r.text
    d=r.json();assert d['database']=='sqlite' and d['probe']==1
    assert d['sample_count']==len(d['query_samples_ms'])==10
    for k in ['checkout_ms','query_first_ms','query_warm_avg_ms','query_warm_median_ms','metadata_ms','transaction_finish_ms','pool_return_ms','release_ms','total_ms']:
        assert d[k]>=0
    assert d['query_warm_avg_ms']==round(sum(d['query_samples_ms'][1:])/9,2) or abs(d['query_warm_avg_ms']-sum(d['query_samples_ms'][1:])/9)<=0.01
