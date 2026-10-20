from copy import deepcopy
from decimal import Decimal
import hashlib,json
import pytest
from sqlalchemy import select,func,text
from test_upgrade import env,jpeg
from app.db import database
from app.db.models import ImportedPriceList as Catalog,ImportedPriceItem as Item,ImportedPriceImage as Photo,Product,Supplier,Order,OrderItem,OrderScheduleRule
from app.services import price_import as svc
from app.api.orders import OrderItemIn,_validate_items,_order_dict,_local_now
from fastapi import HTTPException
from pydantic import ValidationError


def snapshot(products=None,**extra):
    products=products if products is not None else [{'id':'90071992547409931234','name':'Хлеб','unit':'кг','images':[]}]
    return dict(schema_version=1,complete_snapshot=True,fetched_at='2026-10-20T10:00:00Z',source_updated_at=None,actual_date='2026-10-20',price_list={'id':'66','point_id':'23109','name':'ДОСТАВКА'},revision='a'*64,product_count=len(products),folders_skipped=2,products=products,**extra)

@pytest.fixture
def prices(env,monkeypatch):
    monkeypatch.setenv('CORE_PRICE_LISTS_API_KEY','x'*32)
    monkeypatch.setattr(svc,'_s3_client',lambda:env[1])
    with database.session_scope() as db:
        supplier=Supplier(name='Supplier',active=True);db.add(supplier);db.flush()
        row=Catalog(connection='prichal-ai',base_url='https://prichal-ai.bothost.tech',point_id='23109',external_id='66',name='Delivery',supplier_id=supplier.id,status='running',job_token='one');db.add(row);db.flush();cid=row.id;sid=supplier.id
    return env,cid,sid

def imported():
    with database.session_scope() as db:return list(db.scalars(select(Item).order_by(Item.id)))
def confirm(prices,step='0.5'):
    env,cid,sid=prices;client,_,_,auth,_=env;i=imported()[0]
    r=client.put(f'/api/admin-center/price-lists/items/{i.id}/step',headers=auth,json={'step':step,'unit':'кг'});assert r.status_code==200,r.text
    return i

def test_repeat_preserves_settings_and_duplicate_names(prices):
    env,cid,sid=prices;d=snapshot();d['products'].append({**d['products'][0],'id':'another'});d['product_count']=2
    svc.apply_snapshot(cid,'one',d);i=confirm(prices);svc.apply_snapshot(cid,'one',d)
    with database.session_scope() as db:
        assert db.scalar(select(func.count(Item.id)))==2
        row=db.get(Item,i.id);assert row.step==Decimal('.5');assert row.external_id=='90071992547409931234';assert db.get(Product,row.product_id).active
        assert db.get(Catalog,cid).supplier_id==sid
        assert not db.get(Product,imported()[1].product_id).active

def test_units_require_confirmation_even_if_changed_back(prices):
    _,cid,_=prices;d=snapshot();svc.apply_snapshot(cid,'one',d);i=confirm(prices)
    d['products'][0]['unit']='шт';svc.apply_snapshot(cid,'one',d)
    d['products'][0]['unit']='кг';svc.apply_snapshot(cid,'one',d)
    with database.session_scope() as db:assert not db.get(Product,i.product_id).active;assert db.get(Item,i.id).step==Decimal('.5')
    confirm(prices)

def test_missing_and_returned_product(prices):
    _,cid,_=prices;svc.apply_snapshot(cid,'one',snapshot());i=confirm(prices)
    svc.apply_snapshot(cid,'one',snapshot([]))
    with database.session_scope() as db:assert not db.get(Item,i.id).present;assert not db.get(Product,i.product_id).active
    svc.apply_snapshot(cid,'one',snapshot());assert len(imported())==1
    with database.session_scope() as db:assert db.get(Product,i.product_id).active

@pytest.mark.parametrize('mutation',[lambda d:d.update(complete_snapshot=False),lambda d:d.update(product_count=99),lambda d:d['price_list'].update(id='67'),lambda d:d['price_list'].update(point_id='1'),lambda d:d.update(schema_version=2),lambda d:d.pop('products'),lambda d:d['products'][0].update(unit=''),lambda d:d.update(fetched_at='2020-01-01T00:00:00Z')])
def test_bad_snapshot_never_changes_catalog(prices,mutation):
    _,cid,_=prices;svc.apply_snapshot(cid,'one',snapshot());i=confirm(prices);d=snapshot();mutation(d)
    with pytest.raises(svc.ImportErrorSafe):svc.apply_snapshot(cid,'one',d)
    with database.session_scope() as db:assert db.get(Product,i.product_id).active;assert db.get(Item,i.id).present

def test_stale_worker_cannot_apply(prices):
    _,cid,_=prices;svc.apply_snapshot(cid,'one',snapshot());
    with database.session_scope() as db:db.get(Catalog,cid).job_token='two'
    with pytest.raises(svc.ImportErrorSafe):svc.apply_snapshot(cid,'one',snapshot([]))
    assert len(imported())==1 and imported()[0].present

@pytest.mark.parametrize('qty',['0','-1','NaN','Infinity','0.0000001','1000000000000'])
def test_invalid_decimal(qty):
    with pytest.raises(ValidationError):OrderItemIn(product_id=1,quantity=qty)

def test_order_decimal_and_history(prices):
    env,cid,sid=prices;client,_,ids,auth,_=env;svc.apply_snapshot(cid,'one',snapshot());i=confirm(prices,'0.1')
    with database.session_scope() as db:
        db.add(OrderScheduleRule(store_id=ids['sid'],supplier_id=sid,weekday=_local_now().weekday(),schedule_type='order',active=True))
    payload={'store_id':ids['sid'],'supplier_id':sid,'items':[{'product_id':i.product_id,'quantity':'0.3'}]}
    r=client.post('/api/orders',json=payload,headers=auth);assert r.status_code==200,r.text;o=r.json();assert o['items'][0]['quantity_text']=='0.300000'
    payload['items'][0]['quantity']='0.300001';assert client.post('/api/orders',json=payload,headers=auth).status_code==400
    d=snapshot();d['products'][0].update(name='New',unit='шт');svc.apply_snapshot(cid,'one',d)
    saved=client.get('/api/orders/'+str(o['id']),headers=auth).json();assert saved['items'][0]['name']=='Хлеб';assert saved['items'][0]['unit']=='кг'
    assert client.patch('/api/orders/'+str(o['id']),headers=auth,json={'items':[{'product_id':i.product_id,'quantity':'NaN'}]}).status_code==422

@pytest.mark.parametrize('path',['https://evil.test/x','//evil.test/x','/api/v1/core/price-list-images/../x','/api/v1/core/price-list-images/%2fsecret','/api/v1/core/price-list-images/x?token=x','/api/v1/core/price-list-images/%252f'])
def test_untrusted_paths(path):
    with pytest.raises(svc.ImportErrorSafe):svc.image_path(path)

def test_images_conditional_replacement_failure_and_absence(prices,monkeypatch):
    env,cid,sid=prices;d=snapshot();entry={'id':'image1','download_path':'/api/v1/core/price-list-images/signed-token','content_sha256':None};d['products'][0]['images']=[entry];svc.apply_snapshot(cid,'one',d)
    body=jpeg();calls=[]
    def request(base,key,path,**kw):
        calls.append(kw);sha=hashlib.sha256(body).hexdigest();return 200,{'content-type':'image/jpeg','etag':'"'+sha+'"','x-content-sha256':sha},body
    monkeypatch.setattr(svc,'request_bytes',request);assert svc.sync_images(cid,'one')==0
    with database.session_scope() as db:photo=db.scalar(select(Photo));oldmedia=photo.media_id;oldetag=photo.etag
    assert len(env[1].objects)==2
    def unchanged(*args,**kw):assert kw['etag']==oldetag;return 304,{},b''
    monkeypatch.setattr(svc,'request_bytes',unchanged);assert svc.sync_images(cid,'one')==0;assert len(env[1].objects)==2
    # Same link and price revision, changed real bytes => new S3 object and thumbnail.
    body=body+b'changed';monkeypatch.setattr(svc,'request_bytes',request);assert svc.sync_images(cid,'one')==0
    with database.session_scope() as db:photo=db.scalar(select(Photo));assert photo.media_id!=oldmedia;current=photo.media_id
    assert calls[-1]['etag']==oldetag
    monkeypatch.setattr(svc,'request_bytes',lambda *a,**kw:(_ for _ in ()).throw(svc.ImportErrorSafe('HTTP 503')))
    assert svc.sync_images(cid,'one')==1
    with database.session_scope() as db:assert db.scalar(select(Photo)).media_id==current
    d['products'][0]['images']=[];svc.apply_snapshot(cid,'one',d)
    with database.session_scope() as db:assert svc.selected_photos(db,imported())=={}

@pytest.mark.parametrize('code',[401,403,404,422,502,503,504])
def test_failed_job_preserves_data_and_retries(prices,monkeypatch,code):
    _,cid,_=prices;svc.apply_snapshot(cid,'one',snapshot());i=confirm(prices)
    monkeypatch.setattr(svc,'fetch_catalog',lambda *a:(_ for _ in ()).throw(svc.ImportErrorSafe('AI HTTP '+str(code))))
    svc.run_job((cid,'one','23109','66','prichal-ai','https://prichal-ai.bothost.tech'))
    with database.session_scope() as db:
        row=db.get(Catalog,cid);assert row.status=='error' and row.next_sync_at;assert db.get(Product,i.product_id).active

def test_admin_permissions_and_local_only_orders(prices,monkeypatch):
    env,cid,sid=prices;client,_,ids,auth,other=env;svc.apply_snapshot(cid,'one',snapshot());confirm(prices)
    monkeypatch.setattr(svc,'fetch_catalog',lambda *a:pytest.fail('Unexpected AI call'))
    assert client.get('/api/admin-center/price-lists',headers=other).status_code==403
    r=client.get(f'/api/orders/products?store_id={ids["sid"]}&supplier_id={sid}',headers=auth);assert r.status_code==200;assert r.json()[0]['step_qty']=='0.500000'
    assert 'download_path' not in client.get(f'/api/admin-center/price-lists/{cid}/items',headers=auth).text

def test_step_errors_and_supplier_preserved(prices):
    env,cid,sid=prices;client,_,_,auth,_=env;svc.apply_snapshot(cid,'one',snapshot());i=imported()[0]
    for v in ['0','-1','NaN','0.0000001']:
        assert client.put(f'/api/admin-center/price-lists/items/{i.id}/step',headers=auth,json={'step':v,'unit':'кг'}).status_code==422
    assert client.put(f'/api/admin-center/price-lists/items/{i.id}/step',headers=auth,json={'step':'1','unit':'шт'}).status_code==409
    with database.session_scope() as db:s=Supplier(name='Second',active=True);db.add(s);db.flush();new=s.id
    assert client.patch(f'/api/admin-center/price-lists/{cid}',headers=auth,json={'supplier_id':new}).status_code==200
    svc.apply_snapshot(cid,'one',snapshot())
    with database.session_scope() as db:assert db.get(Product,i.product_id).supplier_id==new

def test_recovered_job_rejects_previous_token(prices):
    from datetime import datetime,timedelta
    _,cid,_=prices
    with database.session_scope() as db:db.get(Catalog,cid).lease_until=datetime.utcnow()-timedelta(seconds=1)
    job=svc.claim_job();assert job[0]==cid and job[1]!='one';assert svc.claim_job() is None
    with pytest.raises(svc.ImportErrorSafe):svc.apply_snapshot(cid,'one',snapshot())
    svc.apply_snapshot(cid,job[1],snapshot());assert len(imported())==1

def test_http_adapter_authorization_and_redirect_rejection(monkeypatch):
    import httpx
    original=httpx.Client;seen=[]
    def handler(request):
        seen.append(request)
        if len(seen)==1:return httpx.Response(304)
        return httpx.Response(302,headers={'Location':'https://untrusted.test'})
    monkeypatch.setattr(svc.httpx,'Client',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))
    assert svc.request_bytes('https://ai.test','key','/image',etag='"sha"',image=True)[0]==304
    assert seen[0].headers['Authorization']=='Bearer key';assert seen[0].headers['If-None-Match']=='"sha"'
    with pytest.raises(svc.ImportErrorSafe):svc.request_bytes('https://ai.test','key','/image',image=True)
    assert len(seen)==2

def test_s3_failure_keeps_previous_photo(prices,monkeypatch):
    env,cid,_=prices;d=snapshot();d['products'][0]['images']=[{'id':'image','download_path':'/api/v1/core/price-list-images/token','content_sha256':None}];svc.apply_snapshot(cid,'one',d)
    body=jpeg()
    def request(*a,**kw):
        sha=hashlib.sha256(body).hexdigest();return 200,{'content-type':'image/jpeg','etag':'"'+sha+'"','x-content-sha256':sha},body
    monkeypatch.setattr(svc,'request_bytes',request);assert svc.sync_images(cid,'one')==0
    with database.session_scope() as db:old=db.scalar(select(Photo)).media_id
    body+=b'new'
    monkeypatch.setattr(env[1],'put_object',lambda **kw:(_ for _ in ()).throw(RuntimeError('S3 unavailable')))
    assert svc.sync_images(cid,'one')==1
    with database.session_scope() as db:assert db.scalar(select(Photo)).media_id==old

def test_full_job_and_admin_photo(prices,monkeypatch):
    env,cid,sid=prices;client,_,_,auth,_=env
    d=snapshot();d['products'][0]['images']=[{'id':'image','download_path':'/api/v1/core/price-list-images/token','content_sha256':None}]
    body=jpeg();sha=hashlib.sha256(body).hexdigest()
    monkeypatch.setattr(svc,'fetch_catalog',lambda *a:d)
    monkeypatch.setattr(svc,'request_bytes',lambda *a,**kw:(200,{'content-type':'image/jpeg','etag':'"'+sha+'"','x-content-sha256':sha},body))
    svc.run_job((cid,'one','23109','66','prichal-ai','https://prichal-ai.bothost.tech'))
    with database.session_scope() as db:assert db.get(Catalog,cid).status=='success'
    i=imported()[0];r=client.get(f'/api/admin-center/price-lists/items/{i.id}/photo',headers=auth);assert r.status_code==200 and r.content
