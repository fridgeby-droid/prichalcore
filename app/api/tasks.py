from datetime import datetime
from fastapi import APIRouter,Depends,HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.core.security import get_current_user,TASK_CREATOR_ROLES,assert_store_access,user_store_ids
from app.core.serializers import row
from app.db.database import get_db
from app.db.models import Task,User,Photo

router=APIRouter(prefix="/api/tasks",tags=["tasks"])

class TaskIn(BaseModel):
    title:str
    description:str|None=None
    assigned_to:int
    store_id:int|None=None
    priority:str="medium"
    deadline:datetime|None=None
    require_photo:bool=False
    reminder_minutes_before:int|None=None

@router.post("")
def create(payload:TaskIn,user=Depends(get_current_user),db:Session=Depends(get_db)):
    if user.role not in TASK_CREATOR_ROLES: raise HTTPException(403,"Продавцы и наставники не могут ставить задачи")
    if payload.store_id is not None: assert_store_access(db,user,payload.store_id)
    assignee=db.get(User,payload.assigned_to)
    if not assignee or not assignee.active: raise HTTPException(400,"Исполнитель не найден")
    obj=Task(created_by=user.id,**payload.model_dump());db.add(obj);db.commit();db.refresh(obj)
    return {"id":obj.id}

@router.get("")
def list_tasks(status:str|None=None,user=Depends(get_current_user),db:Session=Depends(get_db)):
    if user.role in {"operations_director","leader","admin"}:
        q=select(Task)
    elif user.role=="manager":
        allowed=user_store_ids(db,user)
        q=select(Task).where((Task.assigned_to==user.id)|(Task.created_by==user.id)|(Task.store_id.in_(allowed or [-1])))
    else:
        q=select(Task).where(Task.assigned_to==user.id)
    if status:q=q.where(Task.status==status)
    q=q.order_by(Task.deadline.asc().nullslast(),Task.created_at.desc()).limit(300)
    return [row(x,"id","store_id","created_by","assigned_to","title","description","priority","status","deadline","require_photo","reminder_minutes_before","created_at","completed_at") for x in db.scalars(q).all()]

@router.patch("/{task_id}")
def update(task_id:int,payload:dict,user=Depends(get_current_user),db:Session=Depends(get_db)):
    obj=db.get(Task,task_id)
    if not obj: raise HTTPException(404,"Задача не найдена")
    can_manage=user.role in TASK_CREATOR_ROLES and (user.role in {"operations_director","leader","admin"} or obj.created_by==user.id)
    is_assignee=obj.assigned_to==user.id
    if not (can_manage or is_assignee): raise HTTPException(403,"Нет доступа")
    if "status" in payload:
        new_status=str(payload["status"])
        if new_status=="done" and obj.require_photo:
            photo=db.scalar(select(Photo).where(Photo.entity_type=="task",Photo.entity_id==obj.id))
            if not photo: raise HTTPException(400,"Для выполнения задачи требуется фото")
        obj.status=new_status
        if new_status=="done":obj.completed_at=datetime.utcnow()
    if can_manage:
        for k in ("title","description","priority","deadline","require_photo","reminder_minutes_before","assigned_to","store_id"):
            if k in payload:setattr(obj,k,payload[k])
    db.commit();return {"ok":True}
