"""Explicit administrator approval; caller owns the transaction."""
from fastapi import HTTPException
from sqlalchemy import select
from app.core.permissions import role_key
from app.db.models import User,RoleDefinition,Employee,AdminAuditLog

def approve_user(db,actor,user_id,selected_role,employee=None):
    if role_key(actor)!='admin':raise HTTPException(403,'Одобрить доступ может только администратор')
    if not selected_role:raise HTTPException(400,'Выберите роль доступа')
    target=db.get(User,user_id,with_for_update=True,populate_existing=True)
    role=db.get(RoleDefinition,selected_role)
    if not target:raise HTTPException(404,'Telegram-аккаунт не найден')
    if not role or role.archived or role.hidden:raise HTTPException(400,'Роль недоступна для назначения')
    linked=employee or db.scalar(select(Employee).where(Employee.user_id==user_id))
    if linked and (not linked.active or linked.employment_status=='fired'):raise HTTPException(409,'Нельзя одобрить доступ уволенному или неактивному сотруднику')
    if target.status=='active' and target.active:
        if (target.role_key or target.role)==selected_role:return target
        raise HTTPException(409,'Аккаунт уже одобрен. Измените роль отдельно в разделе Пользователи')
    if target.status!='pending' or not target.active:raise HTTPException(409,'Аккаунт заблокирован или не ожидает одобрения')
    target.status='active';target.role_key=role.key;target.role=role.base_role
    db.add(AdminAuditLog(actor_user_id=actor.id,action='user.approve',entity_type='user',entity_id=str(target.id),details_json={'previous_status':'pending','role_key':role.key,'employee_id':linked.id if linked else None}))
    return target
