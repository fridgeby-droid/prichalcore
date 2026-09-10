from fastapi import APIRouter,Depends,HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.core.security import get_current_user,assert_store_access,MANAGEMENT_ROLES,user_store_ids
from app.core.serializers import row
from app.db.database import get_db
from app.db.models import InspectionTemplate,InspectionTemplateField,Inspection,InspectionValue,Violation

router=APIRouter(prefix="/api/inspections",tags=["inspections"])

@router.get("/templates")
def templates(user=Depends(get_current_user),db:Session=Depends(get_db)):
    out=[]
    for t in db.scalars(select(InspectionTemplate).where(InspectionTemplate.active.is_(True)).order_by(InspectionTemplate.name)).all():
        fs=db.scalars(select(InspectionTemplateField).where(InspectionTemplateField.template_id==t.id).order_by(InspectionTemplateField.sort_order)).all()
        out.append({**row(t,"id","name"),"fields":[row(f,"id","key","label","field_type","required","sort_order","options_json","creates_violation_on_false") for f in fs]})
    return out

class Submit(BaseModel):
    store_id:int
    template_id:int
    values:dict[str,object]
    comment:str|None=None

@router.post("")
def create(payload:Submit,user=Depends(get_current_user),db:Session=Depends(get_db)):
    if user.role not in MANAGEMENT_ROLES: raise HTTPException(403,"Проверки доступны управляющим и руководству")
    assert_store_access(db,user,payload.store_id)
    t=db.get(InspectionTemplate,payload.template_id)
    if not t: raise HTTPException(404,"Шаблон не найден")
    fs=db.scalars(select(InspectionTemplateField).where(InspectionTemplateField.template_id==t.id)).all()
    scored=[]
    obj=Inspection(store_id=payload.store_id,template_id=t.id,manager_id=user.id,comment=payload.comment)
    db.add(obj);db.flush()
    for f in fs:
        val=payload.values.get(f.key)
        if f.required and val in (None,""): raise HTTPException(400,f"Обязательное поле: {f.label}")
        if f.key in payload.values: db.add(InspectionValue(inspection_id=obj.id,field_id=f.id,value_json=val))
        if f.field_type=="boolean" and val is not None:
            scored.append(100 if bool(val) else 0)
            if f.creates_violation_on_false and not bool(val):
                db.add(Violation(store_id=payload.store_id,inspection_id=obj.id,title=f.label,severity="medium",status="open"))
    obj.score=sum(scored)/len(scored) if scored else None
    db.commit();return {"id":obj.id,"score":float(obj.score) if obj.score is not None else None}

@router.get("")
def list_all(store_id:int|None=None,user=Depends(get_current_user),db:Session=Depends(get_db)):
    q=select(Inspection).order_by(Inspection.completed_at.desc()).limit(200)
    allowed=user_store_ids(db,user)
    if user.role not in {"operations_director","leader","admin"}:q=q.where(Inspection.store_id.in_(allowed or [-1]))
    if store_id:q=q.where(Inspection.store_id==store_id)
    return [row(x,"id","store_id","template_id","manager_id","score","comment","completed_at") for x in db.scalars(q).all()]

@router.get("/violations")
def violations(user=Depends(get_current_user),db:Session=Depends(get_db)):
    q=select(Violation).order_by(Violation.created_at.desc()).limit(200)
    allowed=user_store_ids(db,user)
    if user.role not in {"operations_director","leader","admin"}:q=q.where(Violation.store_id.in_(allowed or [-1]))
    return [row(x,"id","store_id","inspection_id","title","severity","status","created_at","resolved_at") for x in db.scalars(q).all()]
