from datetime import datetime
from uuid import uuid4
from decimal import Decimal
from fastapi import APIRouter,Depends,HTTPException
from pydantic import BaseModel,Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.api.admin_center import admin_only,audit
from app.db.database import get_db
from app.db.models import ImportedPriceList as Catalog,ImportedPriceItem as Item,Product,Supplier
from app.services import price_import as service
from app.services.storage import s3_photo_response

router=APIRouter(prefix='/api/admin-center/price-lists',tags=['price-lists'])
def catalog(db,cid):
    row=db.scalar(select(Catalog).where(Catalog.id==cid).with_for_update())
    if not row:raise HTTPException(404,'Прайс не найден')
    return row

def summary(r):
    return {k:getattr(r,k) for k in ('id','name','point_id','external_id','supplier_id','status','error','synced_at','auto_sync')}

@router.get('')
def listing(user=Depends(admin_only),db:Session=Depends(get_db)):
    error=None
    try:service.settings()
    except service.ImportErrorSafe as e:error=str(e)
    return {'configured':not error,'config_error':error,'catalogs':[summary(r) for r in db.scalars(select(Catalog).order_by(Catalog.id))],'suppliers':[{'id':s.id,'name':s.name} for s in db.scalars(select(Supplier).where(Supplier.active.is_(True)).order_by(Supplier.name))]}

@router.get('/remote')
def remote(point_id:str,user=Depends(admin_only),db:Session=Depends(get_db)):
    db.close()
    try:return service.fetch_catalog(point_id)['price_lists']
    except service.ImportErrorSafe as e:raise HTTPException(502,str(e))

class Connect(BaseModel):
    point_id:str=Field(min_length=1,max_length=128)
    external_id:str=Field(min_length=1,max_length=128)
    supplier_id:int

@router.post('')
def connect(payload:Connect,user=Depends(admin_only),db:Session=Depends(get_db)):
    try:base,key,connection=service.settings()
    except service.ImportErrorSafe as e:raise HTTPException(503,str(e))
    supplier=db.get(Supplier,payload.supplier_id)
    if not supplier or not supplier.active:raise HTTPException(400,'Выберите активного поставщика')
    existing=db.scalar(select(Catalog).where(Catalog.connection==connection,Catalog.base_url==base,Catalog.point_id==payload.point_id,Catalog.external_id==payload.external_id))
    if existing:raise HTTPException(409,'Прайс уже подключён')
    row=Catalog(connection=connection,base_url=base,point_id=payload.point_id,external_id=payload.external_id,name='Прайс '+payload.external_id,supplier_id=payload.supplier_id,status='queued')
    db.add(row);db.flush();audit(db,user,'connect','price_list',row.id);db.commit()
    return summary(row)

@router.post('/{cid}/sync')
def sync(cid:int,user=Depends(admin_only),db:Session=Depends(get_db)):
    row=catalog(db,cid)
    if row.status=='running' and row.lease_until and row.lease_until>datetime.utcnow():raise HTTPException(409,'Загрузка уже выполняется')
    row.status='queued';row.job_token=str(uuid4());row.lease_until=None;row.next_sync_at=None;row.error=None;row.failures=0
    audit(db,user,'sync','price_list',cid);db.commit();return summary(row)

class Settings(BaseModel):
    supplier_id:int
    auto_sync:bool=False

@router.patch('/{cid}')
def configure(cid:int,payload:Settings,user=Depends(admin_only),db:Session=Depends(get_db)):
    row=catalog(db,cid)
    supplier=db.get(Supplier,payload.supplier_id)
    if not supplier or not supplier.active:raise HTTPException(400,'Выберите активного поставщика')
    row.supplier_id=payload.supplier_id;row.auto_sync=payload.auto_sync
    for p in db.scalars(select(Product).join(Item,Item.product_id==Product.id).where(Item.price_list_id==cid).order_by(Product.id).with_for_update()):p.supplier_id=row.supplier_id
    if row.status!='running':row.next_sync_at=datetime.utcnow() if row.auto_sync else None
    audit(db,user,'configure','price_list',cid);db.commit();return summary(row)

@router.get('/{cid}/items')
def items(cid:int,user=Depends(admin_only),db:Session=Depends(get_db)):
    rows=db.execute(select(Item,Product).join(Product,Product.id==Item.product_id).where(Item.price_list_id==cid).order_by(Product.name,Item.id)).all()
    photos=service.selected_photos(db,[i for i,p in rows])
    return [{'id':i.id,'product_id':p.id,'name':p.name,'unit':p.unit,'present':i.present,'step':str(i.step) if i.step else None,'ready':bool(i.present and i.step and i.confirmed_unit==p.unit),'image_url':f'/api/admin-center/price-lists/items/{i.id}/photo' if photos.get(p.id) else None} for i,p in rows]

class Step(BaseModel):
    step:Decimal=Field(gt=0,max_digits=18,decimal_places=6)
    unit:str=Field(max_length=64)

@router.put('/items/{iid}/step')
def step(iid:int,payload:Step,user=Depends(admin_only),db:Session=Depends(get_db)):
    item=db.get(Item,iid)
    if not item:raise HTTPException(404,'Товар не найден')
    catalog(db,item.price_list_id)
    db.refresh(item)
    product=db.get(Product,item.product_id,with_for_update=True)
    if product.unit!=payload.unit:raise HTTPException(409,'Единица изменилась. Обновите список и подтвердите шаг')
    item.step=payload.step;item.confirmed_unit=product.unit;product.active=item.present and not (item.overrides or {}).get('disabled')
    audit(db,user,'step','price_item',iid,{'step':str(payload.step),'unit':payload.unit});db.commit();return {'ok':True}

@router.get('/items/{iid}/photo')
def photo(iid:int,user=Depends(admin_only),db:Session=Depends(get_db)):
    item=db.get(Item,iid)
    selected=service.selected_photos(db,[item]).get(item.product_id,[]) if item else []
    if not selected:raise HTTPException(404,'Фото отсутствует')
    return s3_photo_response(db,media_id=selected[0].media_id,thumbnail=True)
