from test_upgrade import env
from test_usability import setup_order,setup_order_second
from app.db import database
from app.db.models import TelegramDeliveryLog,TelegramDestination,TelegramRoute
from app.services import delivery_worker
from app.services.notifications import combine_bulk_order_deliveries
from sqlalchemy import select,func

def test_same_chat_one_message_and_no_repeat(env,monkeypatch):
    c,_,ids,auth,_=env;b,p=setup_order(env);b2,p2=setup_order_second(env)
    orders=[c.post('/api/orders',json={**x,'comment':comment},headers=auth).json()['id'] for x,comment in [(b,'First'),(b2,'Second')]]
    with database.session_scope() as db:
        dst=TelegramDestination(title='Combined',chat_id=-100,active=True);db.add(dst);db.flush()
        for x in [b,b2]:db.add(TelegramRoute(event_type='order_accepted',target_type='supplier',target_id=x['supplier_id'],destination_id=dst.id,active=True))
    r=c.post('/api/orders/bulk',json={'ids':orders,'action':'accepted'},headers=auth);assert r.status_code==200,r.text
    with database.session_scope() as db:
        logs=list(db.scalars(select(TelegramDeliveryLog)));assert sorted(x.status for x in logs)==['grouped','pending']
        lead=next(x for x in logs if x.status=='pending');assert 'First' in lead.payload_json['text'] and 'Second' in lead.payload_json['text']
    sent=[];monkeypatch.setattr(delivery_worker,'send_message',lambda *args:sent.append(args) or {'result':{'message_id':5}})
    delivery_worker.process_deliveries();assert len(sent)==1
    with database.session_scope() as db:
        assert all(x.status=='sent' and x.telegram_message_id==5 for x in db.scalars(select(TelegramDeliveryLog)))
    c.post('/api/orders/bulk',json={'ids':orders,'action':'accepted'},headers=auth)
    delivery_worker.process_deliveries();assert len(sent)==1

def test_long_batch_separates_into_bounded_messages(env):
    with database.session_scope() as db:
        results=[]
        for i in range(3):
            log=TelegramDeliveryLog(event_type='order_accepted',entity_type='order',entity_id=i+1,chat_id=-100,dedupe_key=f'test:{i}',status='pending',payload_json={'text':'x'*1800});db.add(log);db.flush();results.append({'delivery':{'status':'pending','log_id':log.id}})
        combine_bulk_order_deliveries(db,results);db.flush()
        logs=list(db.scalars(select(TelegramDeliveryLog)))
        pending=[x for x in logs if x.status=='pending'];assert len(pending)==2
        assert all(len(x.payload_json['text'])<=3800 for x in pending)

def test_group_retry_and_exhaustion_propagate(env,monkeypatch):
    from datetime import datetime,timedelta
    with database.session_scope() as db:
        results=[]
        for i in range(2):
            r=TelegramDeliveryLog(event_type='order_accepted',entity_type='order',entity_id=i+1,chat_id=-100,dedupe_key=f'retry:{i}',status='pending',payload_json={'text':'Message'});db.add(r);db.flush();results.append({'delivery':{'status':'pending','log_id':r.id}})
        combine_bulk_order_deliveries(db,results)
    def fail(*args):raise RuntimeError('test failure')
    monkeypatch.setattr(delivery_worker,'send_message',fail)
    delivery_worker.process_deliveries()
    with database.session_scope() as db:
        logs=list(db.scalars(select(TelegramDeliveryLog)));assert sorted(r.status for r in logs)==['grouped','retry']
        lead=next(r for r in logs if r.status=='retry');lead.payload_json={**lead.payload_json,'_attempt':5};lead.sent_at=datetime.utcnow()-timedelta(minutes=2)
    delivery_worker.process_deliveries()
    with database.session_scope() as db:assert all(r.status=='error' for r in db.scalars(select(TelegramDeliveryLog)))

def test_bulk_footer_once_and_empty_comment_omitted(env):
    c,_,ids,auth,_=env;b,p=setup_order(env);b2,p2=setup_order_second(env)
    first=c.post('/api/orders',json={**b,'items':[{'product_id':p,'quantity':'1'}]},headers=auth).json()['id']
    second=c.post('/api/orders',json={**b2,'comment':'Не забыть <пакеты> & чек'},headers=auth).json()['id']
    with database.session_scope() as db:
        dst=TelegramDestination(title='Combined',chat_id=-100,active=True);db.add(dst);db.flush()
        for x in [b,b2]:db.add(TelegramRoute(event_type='order_accepted',target_type='supplier',target_id=x['supplier_id'],destination_id=dst.id,active=True))
    r=c.post('/api/orders/bulk',json={'ids':[first,second],'action':'accepted'},headers=auth);assert r.status_code==200,r.text
    with database.session_scope() as db:
        lead=db.scalar(select(TelegramDeliveryLog).where(TelegramDeliveryLog.status=='pending'))
        text=lead.payload_json['text']
        assert text.count('Отправил:')==1 and text.endswith('Отправил: Admin')
        assert 'Создал:' not in text and 'Проверил:' not in text
        assert 'Без комментария' not in text and text.count('💬')==1
        assert '&lt;пакеты&gt; &amp; чек' in text
