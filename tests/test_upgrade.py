import os,sys
from pathlib import Path
from io import BytesIO
from datetime import datetime,timedelta
from uuid import uuid4
import pytest
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
os.environ.update(DATABASE_URL='sqlite://',BOT_TOKEN='test',AUTO_SET_WEBHOOK='false',ENABLE_SCHEDULER='false',PHOTO_STORAGE='s3',APP_ENV='test')
from fastapi.testclient import TestClient
from sqlalchemy import select,func,event
import main
from app.db import database
from app.db.models import *
from app.core.security import create_session
from app.services import media,storage
from app.api import media as api_media

class FakeS3:
    def __init__(self):self.objects={}
    def generate_presigned_url(self,*a,**kw):return 'https://fake.invalid/'+kw['Params']['Key']
    def put_object(self,**kw):self.objects[kw['Key']]=kw['Body'];return {}
    def get_object(self,**kw):
        data=self.objects[kw['Key']];return {'Body':BytesIO(data),'ContentLength':len(data),'ContentType':'image/jpeg'}
    def delete_object(self,**kw):self.objects.pop(kw['Key'],None)

@pytest.fixture
def env(tmp_path,monkeypatch):
    if database._engine:database._engine.dispose()
    database._engine=None;database.SessionLocal=None;database.DATABASE_URL='sqlite:///'+str(tmp_path/'db.sqlite')
    monkeypatch.setattr(main,'start_worker',lambda:None);monkeypatch.setattr(main,'stop_worker',lambda:None)
    fake=FakeS3()
    for module in [media,storage,api_media]:monkeypatch.setattr(module,'_s3_client',lambda:fake)
    with TestClient(main.app) as client:
        with database.session_scope() as db:
            user=User(telegram_id=1,full_name='Admin',role='admin',role_key='admin',status='active',active=True);db.add(user);db.flush()
            employee=Employee(full_name='Admin',position='admin',employment_status='working',user_id=user.id,telegram_id=1);db.add(employee);db.flush()
            other=User(telegram_id=2,full_name='Seller',role='seller',role_key='seller',status='active',active=True);db.add(other);db.flush()
            other_employee=Employee(full_name='Seller',position='seller',employment_status='working',user_id=other.id,telegram_id=2);db.add(other_employee);db.flush()
            sid=Store(name='Store');db.add(sid);db.flush()
            section=db.scalar(select(KnowledgeSection));article=KnowledgeArticle(section_id=section.id,title='Article',content_html='<p>x</p>',status='published',author_id=user.id);db.add(article)
            st=ShiftTemplate(store_id=sid.id,name='Shift');db.add(st);db.flush()
            field=ShiftTemplateField(template_id=st.id,key='photo',label='Photo',field_type='photo',required=True);db.add(field);db.flush()
            report=ShiftReport(store_id=sid.id,template_id=st.id,submitted_by=user.id,employee_id=employee.id,shift_kind='day');db.add(report);db.flush()
            value=ShiftReportValue(report_id=report.id,field_id=field.id);db.add(value)
            it=InspectionTemplate(name='Inspection');db.add(it);db.flush()
            insp=Inspection(store_id=sid.id,template_id=it.id,manager_id=user.id);db.add(insp);db.flush()
            ifield=InspectionTemplateField(template_id=it.id,key='p',label='Photo',field_type='photo');db.add(ifield);db.flush()
            iv=InspectionValue(inspection_id=insp.id,field_id=ifield.id);db.add(iv)
            task=TaskV2(title='Task',created_by=user.id);db.add(task);db.flush()
            assignee=TaskAssignee(task_id=task.id,employee_id=employee.id,status='in_progress');db.add(assignee)
            test=TrainingTest(title='Test',created_by=user.id);db.add(test);db.flush()
            question=TrainingQuestion(test_id=test.id,text='Question');db.add(question);db.flush()
            ids={k:v.id for k,v in locals().copy().items() if k in ['user','employee','other','other_employee','sid','article','report','value','insp','iv','task','assignee','test','question','it']}
            token=create_session(db,user);other_token=create_session(db,other)
        yield client,fake,ids,{'Authorization':'Bearer '+token},{'Authorization':'Bearer '+other_token}

def jpeg():
    out=BytesIO();Image.new('RGB',(1200,600),'green').save(out,'JPEG');return out.getvalue()

def start(env,kind,identity,**extra):
    client,fake,ids,auth,other=env;payload=jpeg()
    r=client.post('/api/media/uploads',headers=auth,json={'entity_type':kind,'entity_id':identity,'size_bytes':len(payload),'request_key':str(uuid4()),**extra})
    assert r.status_code==200,r.text
    row=r.json();key=row['url'].split('https://fake.invalid/')[1];fake.objects[key]=payload
    return row,key

def finish(env,row):
    client,fake,ids,auth,other=env
    r=client.post('/api/media/uploads/'+row['upload_id']+'/complete',headers=auth,json={});assert r.status_code==200,r.text
    media.process_one(row['upload_id'])
    result=client.get('/api/media/uploads/'+row['upload_id'],headers=auth).json();assert result['status']=='ready',result
    return result['result']

@pytest.mark.parametrize('kind,key,endpoint',[('profile_avatar','employee','/api/profile/avatar'),('shift_report_field','value',None),('inspection','insp',None),('inspection_value','iv',None),('task_attachment','task',None),('knowledge_media','article',None),('test_question','question',None)])
def test_uploads_all_modules(env,kind,key,endpoint):
    client,fake,ids,auth,other=env
    extra={'assignee_id':ids['assignee']} if kind=='task_attachment' else {}
    row,tmp=start(env,kind,ids[key],**extra);result=finish(env,row)
    assert client.get('/api/media/uploads/'+row['upload_id'],headers=other).status_code==404
    duplicate=client.post('/api/media/uploads/'+row['upload_id']+'/complete',headers=auth,json={});assert duplicate.json()['result']==result
    with database.session_scope() as db:
        asset=db.get(MediaAsset,result['media_id']);assert asset.thumbnail_key;assert asset.width==1200
        original=fake.objects[asset.s3_key]
        # Replaying the upload URL cannot alter the committed image.
        fake.objects[tmp]=b'changed';assert fake.objects[asset.s3_key]==original
        assert db.scalar(select(func.count(MediaAsset.id)))==1
        if kind=='task_attachment':assert db.get(TaskAttachment,result['id']).telegram_file_id is None
    if endpoint is None:
        endpoint=f'/api/tasks-v2/{ids["task"]}/attachments/{result["id"]}/content' if kind=='task_attachment' else f'/api/knowledge/media/{result["id"]}/content' if kind=='knowledge_media' else f'/api/testing/questions/{ids["question"]}/image' if kind=='test_question' else f'/api/photos/{result["id"]}/content'
    r=client.get(endpoint+'?thumbnail=true',headers=auth);assert r.status_code==200,r.text
    im=Image.open(BytesIO(r.content));assert max(im.size)==480

def test_final_status_rechecked(env):
    client,fake,ids,auth,_=env;row,tmp=start(env,'shift_report_field',ids['value'])
    client.post('/api/media/uploads/'+row['upload_id']+'/complete',headers=auth,json={})
    with database.session_scope() as db:db.get(ShiftReport,ids['report']).status='review'
    media.process_one(row['upload_id'])
    r=client.get('/api/media/uploads/'+row['upload_id'],headers=auth).json();assert r['status']=='failed'
    with database.session_scope() as db:assert db.scalar(select(func.count(Photo.id)))==0

def test_corrupt_and_foreign_and_expired(env):
    client,fake,ids,auth,other=env
    r=client.post('/api/media/uploads',headers=other,json={'entity_type':'profile_avatar','entity_id':ids['employee'],'size_bytes':10,'request_key':str(uuid4())});assert r.status_code==403
    row,tmp=start(env,'profile_avatar',ids['employee']);fake.objects[tmp]=b'not image'
    client.post('/api/media/uploads/'+row['upload_id']+'/complete',headers=auth,json={});media.process_one(row['upload_id'])
    assert client.get('/api/media/uploads/'+row['upload_id'],headers=auth).json()['status']=='failed'
    row,tmp=start(env,'profile_avatar',ids['employee'])
    with database.session_scope() as db:db.get(MediaUpload,row['upload_id']).expires_at=datetime.utcnow()-timedelta(seconds=1)
    assert client.post('/api/media/uploads/'+row['upload_id']+'/complete',headers=auth,json={}).status_code==409

def test_media_migration_preserves_links(env):
    client,fake,ids,auth,_=env
    with database.session_scope() as db:
        db.add(StoredObject(telegram_file_id='existing',s3_key='core/photos/existing.jpg',mime_type='image/jpeg'))
        photo=Photo(entity_type='profile_avatar',entity_id=ids['employee'],uploaded_by=ids['user'],telegram_file_id='existing');db.add(photo);db.flush();pid=photo.id
    from app.db.media_migration import upgrade
    upgrade(database.get_engine());upgrade(database.get_engine())
    with database.session_scope() as db:
        photo=db.get(Photo,pid);assert photo.media_id;asset=db.get(MediaAsset,photo.media_id);assert asset.s3_key=='core/photos/existing.jpg'
        assert db.scalar(select(func.count(MediaAsset.id)))==1

def test_counts_constant(env):
    client,fake,ids,auth,_=env
    def req(path):
        r=client.get(path,headers=auth);assert r.status_code==200,r.text
        return int(r.headers['X-SQL-Count']),r.json()
    before,_=req('/api/inspections/control/weekly')
    with database.session_scope() as db:
        for i in range(24):db.add(Store(name='Extra '+str(i)))
    after,rows=req('/api/inspections/control/weekly');assert after==before
    task_before,_=req('/api/tasks-v2?scope=my')
    with database.session_scope() as db:
        for i in range(49):
            t=TaskV2(title='Task '+str(i),created_by=ids['user']);db.add(t);db.flush();db.add(TaskAssignee(task_id=t.id,employee_id=ids['employee']));db.add(TaskTarget(task_id=t.id,target_type='employee',employee_id=ids['employee']))
    task_after,rows=req('/api/tasks-v2?scope=my');assert task_after<=task_before+1;assert len(rows)==50
    for endpoint in ['/api/profile/work','/api/profile/learning','/api/profile/activity','/api/knowledge/articles','/api/inspections/forms']:
        req(endpoint)
    print({'weekly_queries':(before,after),'task_queries':(task_before,task_after)})

def test_history_filter_before_pagination(env):
    client,fake,ids,auth,_=env
    with database.session_scope() as db:
        for i in range(60):db.add(Inspection(store_id=ids['sid'],template_id=ids['it'],manager_id=ids['user'],status='completed',score=50 if i==0 else 95,completed_at=datetime.utcnow()+timedelta(seconds=i)))
    r=client.get('/api/inspections?result=poor&limit=10',headers=auth);assert r.status_code==200,r.text;assert len(r.json())==1
    for endpoint in ['/api/inspections?limit=10&offset=10','/api/inspections?result=neutral']:
        assert client.get(endpoint,headers=auth).status_code==200

def test_inbox_deduplicates(env):
    from app.services.telegram_inbox import accept_update
    accept_update({'update_id':42,'message':{'text':'/start'}});accept_update({'update_id':42,'message':{'text':'/start'}})
    with database.session_scope() as db:assert db.scalar(select(func.count(TelegramInbox.update_id)))==1

def test_shared_asset_survives_source_detach(env):
    client,fake,ids,auth,_=env
    row,tmp=start(env,'inspection_value',ids['iv']);result=finish(env,row)
    with database.session_scope() as db:
        violation=Violation(store_id=ids['sid'],inspection_id=ids['insp'],inspection_value_id=ids['iv'],title='Issue',severity='medium',status='open');db.add(violation);db.flush();vid=violation.id
    for _ in range(2):
        r=client.post(f'/api/inspections/violations/{vid}/link-task/{ids["task"]}',headers=auth);assert r.status_code==200,r.text
    with database.session_scope() as db:
        links=list(db.scalars(select(TaskAttachment)).all());assert len(links)==1;assert links[0].media_id==result['media_id'];attachment=links[0].id
    r=client.post('/api/media/detach',headers=auth,json={'entity_type':'inspection_value','entity_id':ids['iv'],'attachment_id':result['id']});assert r.status_code==200,r.text
    assert client.get(f'/api/tasks-v2/{ids["task"]}/attachments/{attachment}/content',headers=auth).status_code==200

def test_restarted_worker_resumes_and_rejects_revoked_user(env):
    client,fake,ids,auth,_=env;row,tmp=start(env,'profile_avatar',ids['employee'])
    client.post('/api/media/uploads/'+row['upload_id']+'/complete',headers=auth,json={})
    with database.session_scope() as db:
        r=db.get(MediaUpload,row['upload_id']);r.status='processing';r.updated_at=datetime.utcnow()-timedelta(minutes=10)
    media.process_one(row['upload_id'])
    assert client.get('/api/media/uploads/'+row['upload_id'],headers=auth).json()['status']=='ready'
    row,tmp=start(env,'profile_avatar',ids['employee']);client.post('/api/media/uploads/'+row['upload_id']+'/complete',headers=auth,json={})
    with database.session_scope() as db:db.get(User,ids['user']).active=False
    media.process_one(row['upload_id'])
    with database.session_scope() as db:assert db.get(MediaUpload,row['upload_id']).status=='failed'

def test_cleanup_only_own_expired_temp(env):
    client,fake,ids,auth,_=env;row,tmp=start(env,'profile_avatar',ids['employee']);result=finish(env,row)
    fake.objects['pickup/photos/keep']=b'keep'
    with database.session_scope() as db:db.get(MediaUpload,row['upload_id']).expires_at=datetime.utcnow()-timedelta(hours=2)
    media.cleanup_expired();assert tmp not in fake.objects;assert fake.objects['pickup/photos/keep']==b'keep'
    r=client.post('/api/media/uploads/'+row['upload_id']+'/complete',headers=auth,json={});assert r.json()['result']==result

def test_cors_preserves_other_projects(env,monkeypatch):
    from app.services import media_cors
    class Cors:
        def __init__(self):self.rules=[{'ID':'pickup','AllowedOrigins':['https://pickup.example'],'AllowedMethods':['GET']}]
        def get_bucket_cors(self,**kw):return {'CORSRules':list(self.rules)}
        def put_bucket_cors(self,**kw):self.rules=kw['CORSConfiguration']['CORSRules']
    cors=Cors();monkeypatch.setattr(media_cors,'_s3_client',lambda:cors);monkeypatch.setattr(media_cors,'APP_URL','https://prichalcore.bothost.tech/miniapp')
    assert media_cors.configure()['status']=='configured';assert cors.rules[0]['ID']=='pickup';assert len(cors.rules)==2
    assert media_cors.configure()['status']=='unchanged'

def test_notifications_queued_without_network(env,monkeypatch):
    from app.services import notifications,delivery_worker
    client,fake,ids,auth,_=env
    def forbid(*a,**k):raise AssertionError('Network call within request')
    monkeypatch.setattr(notifications,'send_message',forbid)
    with database.session_scope() as db:
        row=notifications.notify_employee(db,employee_id=ids['employee'],event_type='task_assigned',entity_type='task',entity_id=ids['task'],text='Test');assert row.status=='pending'
    sent=[];monkeypatch.setattr(delivery_worker,'send_message',lambda *a,**k:sent.append(a) or {'result':{'message_id':42}})
    delivery_worker.process_deliveries();delivery_worker.process_deliveries();assert len(sent)==1
    with database.session_scope() as db:assert db.scalar(select(TelegramDeliveryLog)).status=='sent'

def test_used_question_image_cannot_change(env):
    client,fake,ids,auth,_=env
    with database.session_scope() as db:
        assignment=TrainingAssignment(test_id=ids['test'],employee_id=ids['employee']);db.add(assignment);db.flush()
        db.add(TrainingAttempt(assignment_id=assignment.id,test_id=ids['test'],employee_id=ids['employee'],attempt_no=1,status='submitted',expires_at=datetime.utcnow(),question_order_json=[ids['question']]))
    response=client.post('/api/media/uploads',headers=auth,json={'entity_type':'test_question','entity_id':ids['question'],'size_bytes':100,'request_key':str(uuid4())});assert response.status_code==409

def test_task_attachment_hidden_from_other_assignee(env):
    client,fake,ids,auth,other=env
    row,tmp=start(env,'task_attachment',ids['task'],assignee_id=ids['assignee']);result=finish(env,row)
    with database.session_scope() as db:db.add(TaskAssignee(task_id=ids['task'],employee_id=ids['other_employee']))
    response=client.get(f'/api/tasks-v2/{ids["task"]}/attachments/{result["id"]}/content',headers=other)
    assert response.status_code==404

def test_presigned_put_needs_no_secret_headers():
    import boto3
    from botocore.config import Config
    from urllib.parse import urlsplit,parse_qs
    client=boto3.client('s3',endpoint_url='https://s3.twcstorage.ru',region_name='ru-1',aws_access_key_id='test',aws_secret_access_key='test',config=Config(signature_version='s3v4',s3={'addressing_style':'path'}))
    url=client.generate_presigned_url('put_object',Params={'Bucket':'test-bucket','Key':'core/photos/tmp/test'},ExpiresIn=60)
    signed=parse_qs(urlsplit(url).query)['X-Amz-SignedHeaders'][0]
    assert signed=='host'
