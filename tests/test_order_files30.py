from io import BytesIO
from datetime import datetime,timedelta
from openpyxl import Workbook
from sqlalchemy import select
from test_upgrade import env
from test_usability import setup_order,setup_order_second
from app.db import database
from app.db.models import Order,OrderAttachment,TelegramDeliveryLog,TelegramDestination,TelegramRoute
from app.services import delivery_worker

def excel():
    out=BytesIO();w=Workbook();w.active.append(['Товар','Количество']);w.active.append(['Хлеб',5]);w.save(out);return out.getvalue()

def create(env,base):
    return env[0].post('/api/orders',json={**base,'comment':'См. Excel'},headers=env[3]).json()['id']

def test_excel_access_limits_delete_and_accept_lock(env):
    c,fake,ids,auth,other=env;b,_=setup_order(env);oid=create(env,b);url=f'/api/orders/{oid}/attachments';data=excel()
    assert c.post(url,headers=other,files={'file':('test.xlsx',data)}).status_code==403
    assert c.post(url,headers=auth,files={'file':('bad.xlsx',b'not excel')}).status_code==400
    assert c.post(url,headers=auth,files={'file':('bad.exe',data)}).status_code==400
    assert c.post(url,headers=auth,files={'file':('large.xls',b'x'*(10*1024*1024+1))}).status_code==400
    files=[]
    for i in range(5):
        r=c.post(url,headers=auth,files={'file':(f'Заявка{i}.xlsx',data)});assert r.status_code==200,r.text;files.append(r.json())
    assert c.post(url,headers=auth,files={'file':('six.xlsx',data)}).status_code==400
    fid=files[0]['id'];assert c.get(f'{url}/{fid}/content',headers=auth).content==data
    assert c.get(f'{url}/{fid}/content',headers=other).status_code==403
    assert c.delete(f'{url}/{fid}',headers=auth).status_code==200
    assert c.get(f'{url}/{fid}/content',headers=auth).status_code==404
    assert len(c.get(url,headers=auth).json())==4
    assert c.patch(f'/api/orders/{oid}',headers=auth,json={'status':'accepted'}).status_code==200
    assert c.post(url,headers=auth,files={'file':('new.xlsx',data)}).status_code==409
    assert c.delete(f"{url}/{files[1]['id']}",headers=auth).status_code==409
    assert c.get(f"{url}/{files[1]['id']}/content",headers=auth).content==data

def test_bulk_files_follow_text_and_retry_resumes(env,monkeypatch):
    c,fake,ids,auth,_=env;b,_=setup_order(env);b2,_=setup_order_second(env);oids=[create(env,x) for x in [b,b2]]
    data=excel()
    for oid in oids:assert c.post(f'/api/orders/{oid}/attachments',headers=auth,files={'file':(f'{oid}.xlsx',data)}).status_code==200
    with database.session_scope() as db:
        dst=TelegramDestination(title='Supplier',chat_id=-100,active=True);db.add(dst);db.flush()
        for x in [b,b2]:db.add(TelegramRoute(event_type='order_accepted',target_type='supplier',target_id=x['supplier_id'],destination_id=dst.id,active=True))
    r=c.post('/api/orders/bulk',headers=auth,json={'ids':oids,'action':'accepted'});assert r.status_code==200,r.text
    sent=[];failed=[False]
    def document(chat,name,content,mime,caption):
        if name==f'{oids[1]}.xlsx' and not failed[0]:failed[0]=True;raise RuntimeError('Temporary error')
        assert content==data;assert f'№{name.split(".")[0]}' in caption;sent.append(('file',name))
    monkeypatch.setattr(delivery_worker,'_s3_client',lambda:fake)
    monkeypatch.setattr(delivery_worker,'send_message',lambda *args:sent.append(('text',args[0])) or {'result':{'message_id':9}})
    monkeypatch.setattr(delivery_worker,'send_document',document)
    delivery_worker.process_deliveries();assert sent==[('text',-100),('file',f'{oids[0]}.xlsx')]
    with database.session_scope() as db:
        r=db.scalar(select(TelegramDeliveryLog).where(TelegramDeliveryLog.status=='retry'));assert r;r.sent_at=datetime.utcnow()-timedelta(minutes=2)
    delivery_worker.process_deliveries();assert sent==[('text',-100),('file',f'{oids[0]}.xlsx'),('file',f'{oids[1]}.xlsx')]
    with database.session_scope() as db:assert all(x.status=='sent' for x in db.scalars(select(TelegramDeliveryLog)))

def test_files_follow_only_their_destination_and_deleted_file_is_excluded(env):
    c,_,_,auth,_=env;b,_=setup_order(env);b2,_=setup_order_second(env);oids=[create(env,x) for x in [b,b2]];data=excel()
    for oid in oids:
        r=c.post(f'/api/orders/{oid}/attachments',headers=auth,files={'file':(f'{oid}.xlsx',data)});assert r.status_code==200
    dropped=c.post(f'/api/orders/{oids[0]}/attachments',headers=auth,files={'file':('removed.xlsx',data)}).json()['id']
    assert c.delete(f'/api/orders/{oids[0]}/attachments/{dropped}',headers=auth).status_code==200
    assert c.get(f'/api/orders/{oids[1]}/attachments/{dropped}/content',headers=auth).status_code==404
    with database.session_scope() as db:
        for n,x in enumerate([b,b2]):
            dst=TelegramDestination(title=str(n),chat_id=-100-n,active=True);db.add(dst);db.flush()
            db.add(TelegramRoute(event_type='order_accepted',target_type='supplier',target_id=x['supplier_id'],destination_id=dst.id,active=True))
    assert c.post('/api/orders/bulk',headers=auth,json={'ids':oids,'action':'accepted'}).status_code==200
    with database.session_scope() as db:
        logs=list(db.scalars(select(TelegramDeliveryLog).order_by(TelegramDeliveryLog.id)))
        assert len(logs)==2
        for n,r in enumerate(logs):
            assert r.chat_id==-100-n
            assert [d['filename'] for d in r.payload_json['documents']]==[f'{oids[n]}.xlsx']
