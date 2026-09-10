from fastapi import APIRouter,Depends,HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.core.security import get_current_user,assert_store_access,user_store_ids
from app.core.serializers import row
from app.db.database import get_db
from app.db.models import ShiftTemplate,ShiftTemplateField,ShiftReport,ShiftReportValue

router=APIRouter(prefix="/api/shifts",tags=["shifts"])

@router.get("/templates")
def templates(store_id:int,user=Depends(get_current_user),db:Session=Depends(get_db)):
    assert_store_access(db,user,store_id)
    out=[]
    for t in db.scalars(select(ShiftTemplate).where(ShiftTemplate.store_id==store_id,ShiftTemplate.active.is_(True))).all():
        fs=db.scalars(select(ShiftTemplateField).where(ShiftTemplateField.template_id==t.id).order_by(ShiftTemplateField.sort_order)).all()
        out.append({**row(t,"id","store_id","name","shift_kind"),"fields":[row(f,"id","key","label","field_type","required","sort_order","options_json") for f in fs]})
    return out

class ShiftSubmit(BaseModel):
    store_id:int
    template_id:int
    values:dict[str,object]

@router.post("/reports")
def submit(payload:ShiftSubmit,user=Depends(get_current_user),db:Session=Depends(get_db)):
    assert_store_access(db,user,payload.store_id)
    t=db.get(ShiftTemplate,payload.template_id)
    if not t or t.store_id!=payload.store_id: raise HTTPException(400,"Шаблон не подходит точке")
    r=ShiftReport(store_id=payload.store_id,template_id=t.id,submitted_by=user.id,shift_kind=t.shift_kind)
    db.add(r); db.flush()
    fields=db.scalars(select(ShiftTemplateField).where(ShiftTemplateField.template_id==t.id)).all()
    for f in fields:
        if f.required and (f.key not in payload.values or payload.values.get(f.key) in (None,"")): raise HTTPException(400,f"Обязательное поле: {f.label}")
        if f.key in payload.values: db.add(ShiftReportValue(report_id=r.id,field_id=f.id,value_json=payload.values[f.key]))
    db.commit(); return {"id":r.id}

@router.get("/reports")
def reports(store_id:int|None=None,user=Depends(get_current_user),db:Session=Depends(get_db)):
    q=select(ShiftReport).order_by(ShiftReport.submitted_at.desc()).limit(200)
    allowed=user_store_ids(db,user)
    if user.role not in {"operations_director","leader","admin"}: q=q.where(ShiftReport.store_id.in_(allowed or [-1]))
    if store_id:q=q.where(ShiftReport.store_id==store_id)
    return [row(x,"id","store_id","template_id","submitted_by","shift_kind","status","submitted_at") for x in db.scalars(q).all()]
