from __future__ import annotations
import re
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, delete
from sqlalchemy.orm import Session
from app.core.security import get_current_user
from app.core.permissions import catalog, all_keys, ACCESS_LEVELS, DATA_SCOPES
from app.db.database import get_db
from app.db.models import RoleDefinition, RolePermission, AdminAuditLog, User, UserStore, Store, AppSetting

router=APIRouter(prefix="/api/admin-center",tags=["admin-center"])

def admin_only(user=Depends(get_current_user)):
    if user.role!="admin": raise HTTPException(403,"Только администратор")
    return user

def audit(db,user,action,entity_type,entity_id=None,details=None):
    db.add(AdminAuditLog(actor_user_id=user.id,action=action,entity_type=entity_type,entity_id=str(entity_id) if entity_id is not None else None,details_json=details))

def role_row(r):
    return {"key":r.key,"name":r.name,"system":r.system,"base_role":r.base_role,"hidden":r.hidden,"archived":r.archived,"updated_at":r.updated_at.isoformat() if r.updated_at else None}

@router.get('/meta')
def meta(user=Depends(admin_only),db:Session=Depends(get_db)):
    roles=[role_row(r) for r in db.scalars(select(RoleDefinition).order_by(RoleDefinition.system.desc(),RoleDefinition.name)).all()]
    return {"roles":roles,"catalog":catalog(),"access_levels":ACCESS_LEVELS,"data_scopes":DATA_SCOPES}

@router.get('/roles/{role_key}')
def role_detail(role_key:str,user=Depends(admin_only),db:Session=Depends(get_db)):
    r=db.get(RoleDefinition,role_key)
    if not r: raise HTTPException(404,"Роль не найдена")
    perms={x.permission_key:{"access_level":x.access_level,"data_scope":x.data_scope} for x in db.scalars(select(RolePermission).where(RolePermission.role_key==role_key)).all()}
    users=db.scalars(select(User).where(User.role_key==role_key,User.active.is_(True))).all()
    return {**role_row(r),"permissions":perms,"users_count":len(users)}

class RoleCreate(BaseModel):
    name:str
    key:str|None=None
    copy_from:str|None=None

@router.post('/roles')
def create_role(payload:RoleCreate,user=Depends(admin_only),db:Session=Depends(get_db)):
    key=(payload.key or re.sub(r'[^a-z0-9]+','_',payload.name.lower())).strip('_')[:60]
    if not key: raise HTTPException(400,"Укажите ключ роли")
    if db.get(RoleDefinition,key): raise HTTPException(409,"Роль уже существует")
    base='seller'
    if payload.copy_from:
        src=db.get(RoleDefinition,payload.copy_from)
        if not src: raise HTTPException(404,"Исходная роль не найдена")
        base=src.base_role
    r=RoleDefinition(key=key,name=payload.name.strip(),system=False,base_role=base,hidden=False,archived=False);db.add(r);db.flush()
    source={x.permission_key:x for x in db.scalars(select(RolePermission).where(RolePermission.role_key==payload.copy_from)).all()} if payload.copy_from else {}
    for pk in all_keys():
        x=source.get(pk);db.add(RolePermission(role_key=key,permission_key=pk,access_level=x.access_level if x else 'hidden',data_scope=x.data_scope if x else 'own',updated_by=user.id))
    audit(db,user,'role.create','role',key,{"copy_from":payload.copy_from});db.commit();return role_row(r)

@router.patch('/roles/{role_key}')
def update_role(role_key:str,payload:dict,user=Depends(admin_only),db:Session=Depends(get_db)):
    r=db.get(RoleDefinition,role_key)
    if not r: raise HTTPException(404,"Роль не найдена")
    if r.system and ('name' in payload or payload.get('archived')): raise HTTPException(400,"Системную роль нельзя переименовать или архивировать")
    if not r.system and 'name' in payload: r.name=str(payload['name']).strip()
    if not r.system and 'archived' in payload: r.archived=bool(payload['archived'])
    if 'hidden' in payload and not r.system: r.hidden=bool(payload['hidden'])
    audit(db,user,'role.update','role',role_key,payload);db.commit();return role_row(r)

class PermIn(BaseModel):
    permission_key:str
    access_level:str
    data_scope:str='own'
class PermBulk(BaseModel): permissions:list[PermIn]

@router.put('/roles/{role_key}/permissions')
def save_permissions(role_key:str,payload:PermBulk,user=Depends(admin_only),db:Session=Depends(get_db)):
    r=db.get(RoleDefinition,role_key)
    if not r: raise HTTPException(404,"Роль не найдена")
    if role_key=='admin': raise HTTPException(400,"Администратор всегда имеет полный доступ")
    valid=set(all_keys())
    for p in payload.permissions:
        if p.permission_key not in valid or p.access_level not in ACCESS_LEVELS or p.data_scope not in DATA_SCOPES: raise HTTPException(400,"Некорректное разрешение")
        obj=db.get(RolePermission,{"role_key":role_key,"permission_key":p.permission_key})
        if not obj: obj=RolePermission(role_key=role_key,permission_key=p.permission_key);db.add(obj)
        obj.access_level=p.access_level;obj.data_scope=p.data_scope;obj.updated_by=user.id
    audit(db,user,'permissions.update','role',role_key,{"count":len(payload.permissions)});db.commit();return {"ok":True}

@router.get('/users')
def users(user=Depends(admin_only),db:Session=Depends(get_db)):
    rows=[]
    roles={r.key:r for r in db.scalars(select(RoleDefinition)).all()}
    for u in db.scalars(select(User).order_by(User.full_name)).all():
        stores=list(db.scalars(select(UserStore.store_id).where(UserStore.user_id==u.id)).all())
        rk=u.role_key or u.role; r=roles.get(rk)
        rows.append({"id":u.id,"full_name":u.full_name,"telegram_id":u.telegram_id,"status":u.status,"active":u.active,"role_key":rk,"role_name":r.name if r else rk,"store_ids":stores})
    return rows

@router.patch('/users/{user_id}/role')
def assign_role(user_id:int,payload:dict,user=Depends(admin_only),db:Session=Depends(get_db)):
    obj=db.get(User,user_id); rk=str(payload.get('role_key') or '')
    r=db.get(RoleDefinition,rk)
    if not obj or not r or r.archived: raise HTTPException(404,"Пользователь или роль не найдены")
    obj.role_key=r.key;obj.role=r.base_role
    audit(db,user,'user.role','user',user_id,{"role_key":rk});db.commit();return {"ok":True}

@router.get('/audit')
def audit_list(limit:int=200,user=Depends(admin_only),db:Session=Depends(get_db)):
    result=[]
    for x in db.scalars(select(AdminAuditLog).order_by(AdminAuditLog.created_at.desc()).limit(min(limit,500))).all():
        actor=db.get(User,x.actor_user_id) if x.actor_user_id else None
        result.append({"id":x.id,"actor":actor.full_name if actor else 'Система',"action":x.action,"entity_type":x.entity_type,"entity_id":x.entity_id,"details":x.details_json,"created_at":x.created_at.isoformat()})
    return result

@router.get('/system')
def system(user=Depends(admin_only),db:Session=Depends(get_db)):
    keys=['company_name','app_timezone','min_inspections_per_week','handover_morning_window','handover_evening_window','day_shift_hours','night_shift_hours','default_order_limits','module_states']
    return {k:(db.get(AppSetting,k).value_json if db.get(AppSetting,k) else None) for k in keys}

@router.put('/system')
def save_system(payload:dict,user=Depends(admin_only),db:Session=Depends(get_db)):
    for k,v in payload.items():
        obj=db.get(AppSetting,k)
        if not obj: obj=AppSetting(key=k,value_json=v);db.add(obj)
        else: obj.value_json=v
    audit(db,user,'system.update','system',None,payload);db.commit();return {"ok":True}

@router.get('/effective/{role_key}')
def effective(role_key:str,user=Depends(admin_only),db:Session=Depends(get_db)):
    r=db.get(RoleDefinition,role_key)
    if not r: raise HTTPException(404,'Роль не найдена')
    perms={x.permission_key:{"access_level":x.access_level,"data_scope":x.data_scope} for x in db.scalars(select(RolePermission).where(RolePermission.role_key==role_key)).all()}
    if role_key=='admin': perms={k:{"access_level":"edit","data_scope":"network"} for k in all_keys()}
    return {"role":role_row(r),"permissions":perms}
