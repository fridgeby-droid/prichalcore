import os,sys,tempfile,json
from pathlib import Path
sys.path.insert(0,sys.argv[1]);path=Path(tempfile.mkdtemp())/'db.sqlite'
os.environ.update(DATABASE_URL='sqlite:///'+str(path),BOT_TOKEN='test',AUTO_SET_WEBHOOK='false',ENABLE_SCHEDULER='false',APP_ENV='test',PHOTO_STORAGE='s3')
from fastapi.testclient import TestClient
from sqlalchemy import event,select
import main
if hasattr(main,'start_worker'):main.start_worker=lambda:None;main.stop_worker=lambda:None
from app.db import database
from app.db.models import User,Employee,Store,TaskV2,TaskAssignee,TaskTarget
from app.core.security import create_session
with TestClient(main.app) as client:
 with database.session_scope() as db:
  u=User(telegram_id=123,full_name='Manager',role='admin',role_key='admin',status='active',active=True);db.add(u);db.flush()
  e=Employee(full_name='Manager',position='admin',user_id=u.id,active=True);db.add(e);db.flush()
  for i in range(25):db.add(Store(name='Store '+str(i)))
  for i in range(50):
   t=TaskV2(title='Task '+str(i),created_by=u.id);db.add(t);db.flush();db.add(TaskAssignee(task_id=t.id,employee_id=e.id));db.add(TaskTarget(task_id=t.id,target_type='employee',employee_id=e.id))
  token=create_session(db,u)
 results={}
 for route in ['/api/inspections/control/weekly','/api/tasks-v2?scope=my','/api/profile/work']:
  count=[0]
  def before(*args):count[0]+=1
  event.listen(database.get_engine(),'before_cursor_execute',before)
  response=client.get(route,headers={'Authorization':'Bearer '+token})
  event.remove(database.get_engine(),'before_cursor_execute',before)
  assert response.status_code==200,response.text
  results[route]=count[0]
 print(json.dumps(results))
