from copy import deepcopy
from datetime import datetime
import pytest
from test_upgrade import env
from app.services import core_directories as service
from app.db import database
from app.db.models import CoreDirectoryState, CoreDirectoryLink

URL='/api/admin-center/saby'
BIG='90071992547409931234'

def sample():
    return {'schema_version':1,'generated_at':'2026-10-09T12:00:00Z','last_successful_sync_at':None,'scope':'retail_excluding_rc','seller_source':'observed_saby_payment_accounts','complete_snapshot':True,'store_count':1,'seller_count':2,'stores':[{'saby_point_id':'5598','name':'Store','address':'','locality':'','updated_at':None}],'sellers':[{'saby_seller_id':BIG,'display_name':'Seller','observed_names':['Seller','Seller S.'],'first_seen':None,'last_seen':'2026-10-08','stores':[{'saby_point_id':'5598','first_seen':None,'last_seen':None}]},{'saby_seller_id':'2','display_name':None,'observed_names':[],'first_seen':None,'last_seen':None,'stores':[]}]}

@pytest.fixture
def connected(env,monkeypatch):
    monkeypatch.setenv('PRICHAL_AI_URL','https://ai.example');monkeypatch.setenv('CORE_DIRECTORY_API_KEY','test-key-'*5)
    monkeypatch.setattr(service,'fetch_snapshot',lambda url,key:service.validate_snapshot(sample()))
    client,_,ids,auth,other=env
    assert client.post(URL+'/sync',headers=auth).status_code==200
    return client,ids,auth,other


def test_snapshot_and_no_secrets(connected):
    c,ids,a,o=connected;r=c.get(URL,headers=a)
    assert r.json()['snapshot']['sellers'][0]['saby_seller_id']==BIG
    assert 'test-key-' not in r.text and 'ai.example' not in r.text
    assert r.headers['cache-control']=='private, no-store'

@pytest.mark.parametrize('method,suffix,payload',[('get','',None),('post','/sync',{}),('put','/link',{'kind':'seller','saby_id':BIG})])
def test_admin_only(connected,method,suffix,payload):
    c,ids,a,o=connected
    kw={'headers':o}
    if payload is not None:kw['json']=payload
    assert getattr(c,method)(URL+suffix,**kw).status_code==403


def test_link_persists_through_empty_snapshot_and_can_unlink(connected,monkeypatch):
    c,ids,a,o=connected;p={'kind':'seller','saby_id':BIG,'core_id':ids['other_employee']}
    assert c.put(URL+'/link',headers=a,json=p).json()['revision']==1
    empty=sample();empty.update(stores=[],sellers=[],store_count=0,seller_count=0)
    monkeypatch.setattr(service,'fetch_snapshot',lambda *args:empty)
    assert c.post(URL+'/sync',headers=a).status_code==200
    d=c.get(URL,headers=a).json();assert len(d['links'])==1 and d['links'][0]['core_id']==ids['other_employee']
    assert c.put(URL+'/link',headers=a,json={**p,'expected_revision':1}).status_code==409
    assert c.put(URL+'/link',headers=a,json={**p,'core_id':None,'expected_revision':1}).status_code==200


def test_old_revision_rejected_shared_account(connected):
    c,ids,a,o=connected;p={'kind':'seller','saby_id':BIG,'shared':True}
    assert c.put(URL+'/link',headers=a,json=p).status_code==200
    assert c.put(URL+'/link',headers=a,json=p).status_code==409
    assert c.put(URL+'/link',headers=a,json={**p,'core_id':ids['employee'],'expected_revision':1}).status_code==400


def test_invalid_target_and_unknown_id(connected):
    c,ids,a,o=connected
    assert c.put(URL+'/link',headers=a,json={'kind':'seller','saby_id':'absent','core_id':ids['employee']}).status_code==409
    assert c.put(URL+'/link',headers=a,json={'kind':'seller','saby_id':BIG,'core_id':999999}).status_code==400
    assert c.put(URL+'/link',headers=a,json={'kind':'seller','saby_id':123,'core_id':ids['employee']}).status_code==422


def test_error_retains_snapshot_and_redacts_details(connected,monkeypatch):
    c,ids,a,o=connected
    def fail(*args):raise RuntimeError('secret-token database password')
    monkeypatch.setattr(service,'fetch_snapshot',fail)
    r=c.post(URL+'/sync',headers=a);assert r.status_code==502 and 'secret-token' not in r.text
    d=c.get(URL,headers=a).json();assert d['snapshot']['seller_count']==2 and d['status']=='error' and 'password' not in d['error']


def test_changed_source_refused(connected,monkeypatch):
    c,ids,a,o=connected;monkeypatch.setenv('PRICHAL_AI_URL','https://different.example')
    assert c.post(URL+'/sync',headers=a).status_code==502
    assert c.get(URL,headers=a).json()['snapshot']['seller_count']==2


def test_running_sync_refused(connected):
    c,ids,a,o=connected
    with database.session_scope() as db:
        row=db.get(CoreDirectoryState,1);row.status='running';row.attempted_at=datetime.utcnow()
    assert c.post(URL+'/sync',headers=a).status_code==409

@pytest.mark.parametrize('mutation',[
 lambda d:d.update(schema_version=2),lambda d:d.update(store_count=3),lambda d:d.update(complete_snapshot=False),
 lambda d:d['stores'][0].update(saby_point_id='23109'),lambda d:d['sellers'][0].update(saby_seller_id=123),
 lambda d:d['sellers'][1].update(saby_seller_id=BIG),lambda d:d['sellers'][0]['stores'][0].update(saby_point_id='unknown')])
def test_rejects_invalid_contract(mutation):
    d=sample();mutation(d)
    with pytest.raises(ValueError):service.validate_snapshot(d)


def test_store_unique_and_multiple_accounts_per_employee(connected,monkeypatch):
    c,ids,a,o=connected;d=sample();d['stores'].append({'saby_point_id':'77','name':'Other'});d['store_count']=2
    monkeypatch.setattr(service,'fetch_snapshot',lambda *args:service.validate_snapshot(d));assert c.post(URL+'/sync',headers=a).status_code==200
    p={'kind':'store','saby_id':'5598','core_id':ids['sid']}
    assert c.put(URL+'/link',headers=a,json=p).status_code==200
    assert c.put(URL+'/link',headers=a,json={**p,'saby_id':'77'}).status_code==409
    for sid in [BIG,'2']:assert c.put(URL+'/link',headers=a,json={'kind':'seller','saby_id':sid,'core_id':ids['employee']}).status_code==200


def test_transport_headers_validation_and_redirect_block(monkeypatch):
    import httpx,json
    real=httpx.Client;seen=[]
    def handle(request):
        seen.append(request);return httpx.Response(200,json=sample())
    monkeypatch.setattr(service.httpx,'Client',lambda **kw:real(transport=httpx.MockTransport(handle),**kw))
    assert service.fetch_snapshot('https://ai.example/api/v1/core/directories','dummy') ['seller_count']==2
    assert seen[0].headers['Authorization']=='Bearer dummy'
    monkeypatch.setattr(service.httpx,'Client',lambda **kw:real(transport=httpx.MockTransport(lambda r:httpx.Response(302,headers={'location':'https://other.example'})),**kw))
    with pytest.raises(ValueError,match='302'):service.fetch_snapshot('https://ai.example/api/v1/core/directories','dummy')


def test_configuration_disabled_short_key_and_https(monkeypatch):
    monkeypatch.setenv('PRICHAL_AI_URL','https://ai.example');monkeypatch.setenv('CORE_DIRECTORY_API_KEY','short')
    with pytest.raises(ValueError):service.configuration()
    monkeypatch.setenv('CORE_DIRECTORY_API_KEY','x'*32);monkeypatch.setenv('PRICHAL_AI_URL','http://ai.example')
    with pytest.raises(ValueError):service.configuration()


def test_upgrade_from_existing_schema_and_repeated_start(env):
    from sqlalchemy import text
    engine=database.get_engine()
    CoreDirectoryLink.__table__.drop(engine)
    CoreDirectoryState.__table__.drop(engine)
    with engine.begin() as conn:conn.execute(text("DELETE FROM core_schema_versions WHERE version='directories-1.7.15'"))
    database.init_db();database.init_db()
    with database.session_scope() as db:
        assert db.get(CoreDirectoryState,1).status=='never'
        assert db.execute(text('SELECT count(*) FROM core_directory_state')).scalar_one()==1
