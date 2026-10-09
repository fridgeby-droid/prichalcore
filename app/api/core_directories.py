from typing import Literal
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, StrictStr
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.api.admin_center import admin_only, audit
from app.db.database import get_db
from app.db.models import CoreDirectoryState, CoreDirectoryLink, Store, Employee
from app.services.core_directories import configuration, sync_directories

router=APIRouter(prefix='/api/admin-center/saby',tags=['saby-directories'])


def link_data(x):
    return {'id':x.id,'kind':x.kind,'saby_id':x.saby_id,'core_id':x.store_id if x.kind=='store' else x.employee_id,'shared':x.shared,'label':x.label,'revision':x.revision}


@router.get('')
def overview(user=Depends(admin_only),db:Session=Depends(get_db)):
    row=db.get(CoreDirectoryState,1)
    try:configuration();configured=True;config_error=None
    except ValueError as e:configured=False;config_error=str(e)
    return {'configured':configured,'config_error':config_error,'status':row.status,'error':row.error,'attempted_at':row.attempted_at,'imported_at':row.imported_at,'snapshot':row.snapshot,
            'links':[link_data(x) for x in db.scalars(select(CoreDirectoryLink)).all()],
            'stores':[{'id':s.id,'name':s.name,'active':s.active} for s in db.scalars(select(Store).order_by(Store.name)).all()],
            'employees':[{'id':e.id,'name':e.full_name,'active':e.active,'status':e.employment_status} for e in db.scalars(select(Employee).order_by(Employee.full_name)).all()]}


@router.post('/sync')
def sync(user=Depends(admin_only),db:Session=Depends(get_db)):
    actor_id=user.id
    db.close() # No DB connection held while waiting for AI.
    return sync_directories(actor_id)


class LinkInput(BaseModel):
    kind:Literal['store','seller']
    saby_id:StrictStr=Field(min_length=1,max_length=128)
    core_id:int|None=Field(default=None,gt=0)
    shared:bool=False
    expected_revision:int=Field(default=0,ge=0)


@router.put('/link')
def save_link(p:LinkInput,user=Depends(admin_only),db:Session=Depends(get_db)):
    state=db.get(CoreDirectoryState,1,with_for_update=True,populate_existing=True)
    current=db.scalar(select(CoreDirectoryLink).where(CoreDirectoryLink.kind==p.kind,CoreDirectoryLink.saby_id==p.saby_id))
    if (current.revision if current else 0)!=p.expected_revision:raise HTTPException(409,'Связь уже изменена. Обновите список')
    rows=(state.snapshot or {}).get('stores' if p.kind=='store' else 'sellers',[])
    source=next((x for x in rows if x['saby_point_id' if p.kind=='store' else 'saby_seller_id']==p.saby_id),None)
    if p.shared and (p.kind!='seller' or p.core_id):raise HTTPException(400,'Общая учётка не привязывается к одному сотруднику')
    if (p.core_id or p.shared) and not source:raise HTTPException(409,'Записи нет в последней выгрузке. Можно сохранить существующую связь или снять её')
    if p.core_id:
        target=db.get(Store if p.kind=='store' else Employee,p.core_id)
        if not target or not target.active:raise HTTPException(400,'Выберите действующий магазин или сотрудника Core')
        if p.kind=='store':
            duplicate=db.scalar(select(CoreDirectoryLink).where(CoreDirectoryLink.store_id==p.core_id,CoreDirectoryLink.saby_id!=p.saby_id))
            if duplicate:raise HTTPException(409,'Этот магазин Core уже связан с другой точкой Saby')
    before=link_data(current) if current else None
    if not current:
        if not source:raise HTTPException(404,'Запись не найдена')
        current=CoreDirectoryLink(kind=p.kind,saby_id=p.saby_id,updated_by=user.id,revision=0);db.add(current)
    current.store_id=p.core_id if p.kind=='store' else None
    current.employee_id=p.core_id if p.kind=='seller' else None
    current.shared=p.shared;current.updated_by=user.id;current.updated_at=datetime.utcnow();current.revision+=1
    if source:current.label=source.get('name') if p.kind=='store' else source.get('display_name')
    db.flush();after=link_data(current)
    audit(db,user,'directories.link','saby_'+p.kind,p.saby_id,{'before':before,'after':after});db.commit()
    return after
