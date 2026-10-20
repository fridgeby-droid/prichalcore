"""Local order catalog from the AI v0.3.18 price-list contract."""
import hashlib,json,os,re,time,threading,logging
from datetime import datetime,timedelta,timezone,date
from decimal import Decimal
from urllib.parse import urlsplit,unquote,quote
from uuid import uuid4
from io import BytesIO
import httpx
from PIL import Image,ImageOps
from sqlalchemy import select,or_
from app.db import database
from app.db.models import ImportedPriceList as Catalog,ImportedPriceItem as Item,ImportedPriceImage as Photo,Product,MediaAsset
from app.core.config import S3_BUCKET,S3_PREFIX
from app.services.storage import _s3_client

log=logging.getLogger(__name__)
_stop=threading.Event();_thread=None
class ImportErrorSafe(Exception):pass

def settings():
    base=(os.getenv('CORE_PRICE_LISTS_AI_URL') or os.getenv('PRICHAL_AI_URL') or os.getenv('AI_AGENT_URL') or 'https://prichal-ai.bothost.tech').strip().rstrip('/')
    u=urlsplit(base)
    if u.scheme!='https' or not u.hostname or u.username or u.password or u.query or u.fragment or u.path or u.port not in (None,443):raise ImportErrorSafe('Задайте базовый HTTPS-адрес AI без пути')
    key=os.getenv('CORE_PRICE_LISTS_API_KEY','').strip()
    if len(key)<32:raise ImportErrorSafe('Задайте CORE_PRICE_LISTS_API_KEY длиной не менее 32 символов')
    connection=os.getenv('CORE_PRICE_LISTS_SOURCE_ID','prichal-ai').strip()
    if not connection or len(connection)>128:raise ImportErrorSafe('Некорректный CORE_PRICE_LISTS_SOURCE_ID')
    return base,key,connection

def image_path(path):
    if not isinstance(path,str) or len(path)>16000:raise ImportErrorSafe('Некорректная ссылка фото')
    u=urlsplit(path);decoded=unquote(path)
    prefix='/api/v1/core/price-list-images/'
    if u.scheme or u.netloc or u.query or u.fragment or not path.startswith(prefix) or '\\' in decoded or any(ord(c)<33 for c in path) or not decoded.startswith(prefix):raise ImportErrorSafe('Недоверенная ссылка фото')
    tail=decoded[len(prefix):]
    if not tail or '/' in tail or '..' in tail or '%' in tail:raise ImportErrorSafe('Некорректная ссылка фото')
    return path

def require(ok):
    if not ok:raise ImportErrorSafe('Ответ AI не соответствует контракту прайсов v1')
def identity(v):
    require(isinstance(v,str) and 0<len(v)<=128 and v.strip()==v and not any(ord(c)<32 for c in v));return v

def timestamp(v,nullable=False):
    if v is None and nullable:return None
    require(isinstance(v,str))
    try:r=datetime.fromisoformat(v.replace('Z','+00:00'))
    except (ValueError,TypeError):raise ImportErrorSafe('Некорректное время снимка') from None
    require(r.tzinfo is not None);return r.astimezone(timezone.utc).replace(tzinfo=None)

def validate(data,point,price=None):
    require(isinstance(data,dict) and type(data.get('schema_version')) is int and data['schema_version']==1 and data.get('complete_snapshot') is True)
    timestamp(data.get('fetched_at'));require('source_updated_at' in data);timestamp(data['source_updated_at'],True)
    try:date.fromisoformat(data['actual_date'])
    except (KeyError,ValueError,TypeError):raise ImportErrorSafe('Некорректная дата снимка') from None
    require(isinstance(data.get('revision'),str) and 0<len(data['revision'])<=128)
    def price_row(p):
        require(isinstance(p,dict));identity(p.get('id'));require(identity(p.get('point_id'))==point)
        require(isinstance(p.get('name'),str) and 0<len(p['name'])<=240)
    if price is None:
        require(data.get('point_id')==point and isinstance(data.get('price_lists'),list));seen=set()
        for p in data['price_lists']:
            price_row(p);require(p['id'] not in seen);seen.add(p['id'])
    else:
        price_row(data.get('price_list'));require(data['price_list']['id']==price)
        require(type(data.get('product_count')) is int and isinstance(data.get('products'),list) and data['product_count']==len(data['products']))
        require(type(data.get('folders_skipped')) is int and data['folders_skipped']>=0);seen=set()
        for p in data['products']:
            require(isinstance(p,dict));pid=identity(p.get('id'));require(pid not in seen);seen.add(pid)
            for field,size in [('name',240),('unit',64)]:require(isinstance(p.get(field),str) and 0<len(p[field])<=size and p[field].strip()==p[field])
            require(isinstance(p.get('images'),list));image_ids=set()
            for im in p['images']:
                require(isinstance(im,dict));iid=identity(im.get('id'));require(iid not in image_ids);image_ids.add(iid);image_path(im.get('download_path'))
                require('content_sha256' in im and (im['content_sha256'] is None or isinstance(im['content_sha256'],str) and re.fullmatch('[a-fA-F0-9]{64}',im['content_sha256']) is not None))
    return data

def request_bytes(base,key,path,params=None,etag=None,image=False):
    headers={'Authorization':'Bearer '+key}
    if etag:headers['If-None-Match']=etag
    limit=10*1024*1024 if image else 20*1024*1024
    started=time.monotonic()
    try:
        with httpx.Client(timeout=httpx.Timeout(310 if not image else 35,connect=5),follow_redirects=False,trust_env=False) as client:
            with client.stream('GET',base+path,params=params,headers=headers) as r:
                if r.status_code==304 and image and etag:return 304,dict(r.headers),b''
                if r.status_code!=200:raise ImportErrorSafe('AI вернул HTTP '+str(r.status_code)+'. Прежние данные сохранены')
                content=bytearray()
                for chunk in r.iter_bytes():
                    content.extend(chunk)
                    if len(content)>limit or time.monotonic()-started>(45 if image else 330):raise ImportErrorSafe('Превышен размер или время ответа AI')
                return 200,dict(r.headers),bytes(content)
    except httpx.HTTPError:raise ImportErrorSafe('Не удалось получить ответ AI. Прежние данные сохранены') from None

def fetch_catalog(point,price=None):
    base,key,connection=settings()
    path='/api/v1/core/price-lists'+('/'+quote(price,safe='') if price is not None else '')
    _,_,body=request_bytes(base,key,path,{'point_id':point})
    try:data=json.loads(body)
    except (ValueError,UnicodeError):raise ImportErrorSafe('AI вернул повреждённый JSON') from None
    return validate(data,point,price)

def locked_catalog(db,cid,token=None):
    row=db.get(Catalog,cid,with_for_update=True,populate_existing=True)
    if not row or token is not None and row.job_token!=token:raise ImportErrorSafe('Загрузка заменена более новой задачей')
    return row

def apply_snapshot(cid,token,data):
    with database.session_scope() as db:
        row=locked_catalog(db,cid,token);validate(data,row.point_id,row.external_id)
        fetched=timestamp(data['fetched_at'])
        if row.fetched_at and fetched<row.fetched_at:raise ImportErrorSafe('Снимок старше сохранённых данных')
        known={x.external_id:x for x in db.scalars(select(Item).where(Item.price_list_id==cid)).all()}
        products={x.id:x for x in db.scalars(select(Product).where(Product.id.in_([i.product_id for i in known.values()])).order_by(Product.id).with_for_update()).all()}
        live=set()
        for entry in data['products']:
            live.add(entry['id']);item=known.get(entry['id'])
            if item is None:
                p=Product(supplier_id=row.supplier_id,name=entry['name'],unit=entry['unit'],active=False);db.add(p);db.flush()
                item=Item(price_list_id=cid,external_id=entry['id'],product_id=p.id,present=True,images=[]);db.add(item)
            else:
                p=products[item.product_id]
                if p.unit!=entry['unit']:item.confirmed_unit=None
            p.name=entry['name'];p.unit=entry['unit'];p.supplier_id=row.supplier_id;item.present=True
            item.images=entry['images'];p.active=bool(item.step and item.step>0 and item.confirmed_unit==p.unit)
        for sid,item in known.items():
            if sid not in live:item.present=False;products[item.product_id].active=False
        row.name=data['price_list']['name'];row.revision=data['revision'];row.fetched_at=fetched;row.synced_at=datetime.utcnow();row.lease_until=datetime.utcnow()+timedelta(minutes=10)


def download_image(base,key,entry,previous):
    path=image_path(entry['download_path']);etag=previous.etag if previous and previous.media_id else None
    status,headers,body=request_bytes(base,key,path,etag=etag,image=True)
    if status==304:return None
    mime=headers.get('content-type','').split(';')[0].strip().lower()
    require(mime in {'image/jpeg','image/png','image/webp','image/gif'})
    sha=hashlib.sha256(body).hexdigest()
    require(headers.get('x-content-sha256')==sha and headers.get('etag')=='"'+sha+'"')
    if entry.get('content_sha256') is not None:require(entry['content_sha256'].lower()==sha)
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter('error',Image.DecompressionBombWarning)
            with Image.open(BytesIO(body)) as src:
                require(src.format in {'JPEG','PNG','WEBP','GIF'} and src.width*src.height<=40_000_000)
                src.seek(0);src.load();im=ImageOps.exif_transpose(src).convert('RGB')
                im.thumbnail((480,480));out=BytesIO();im.save(out,'JPEG',quality=82)
    except ImportErrorSafe:raise
    except Exception:raise ImportErrorSafe('Не удалось безопасно обработать изображение') from None
    return sha,headers['etag'],mime,body,out.getvalue()

def sync_images(cid,token):
    base,key,connection=settings();errors=0
    with database.session_scope() as db:
        row=locked_catalog(db,cid,token)
        work=[(x.id,x.images) for x in db.scalars(select(Item).where(Item.price_list_id==cid,Item.present.is_(True))).all()]
    for itemid,images in work:
        for entry in images:
            with database.session_scope() as db:
                row=locked_catalog(db,cid,token);row.lease_until=datetime.utcnow()+timedelta(minutes=10)
                previous=db.scalar(select(Photo).where(Photo.item_id==itemid,Photo.external_id==entry['id']))
                # expire_on_commit=False permits use of metadata outside transaction.
            try:
                result=download_image(base,key,entry,previous)
                if result is None:
                    with database.session_scope() as db:
                        locked_catalog(db,cid,token);photo=db.get(Photo,previous.id);photo.error=None
                    continue
                sha,etag,mime,body,thumb=result
                # Content-addressed objects and assets make duplicate bytes reusable.
                asset_id=str(uuid4());prefix=(S3_PREFIX or 'core/photos').strip('/')+'/price-lists/'+sha
                with database.session_scope() as db:existing=db.scalar(select(MediaAsset).where(MediaAsset.s3_key==prefix+'/original'))
                if existing:asset_id=existing.id
                else:
                    client=_s3_client()
                    if client is None:raise ImportErrorSafe('S3 не настроен')
                    client.put_object(Bucket=S3_BUCKET,Key=prefix+'/original',Body=body,ContentType=mime)
                    client.put_object(Bucket=S3_BUCKET,Key=prefix+'/thumbnail.jpg',Body=thumb,ContentType='image/jpeg')
                with database.session_scope() as db:
                    locked_catalog(db,cid,token)
                    # Another worker/list may have saved identical bytes meanwhile.
                    asset=db.scalar(select(MediaAsset).where(MediaAsset.s3_key==prefix+'/original'))
                    if asset:asset_id=asset.id
                    else:db.add(MediaAsset(id=asset_id,s3_key=prefix+'/original',thumbnail_key=prefix+'/thumbnail.jpg',mime_type=mime,size_bytes=len(body)));db.flush()
                    photo=db.scalar(select(Photo).where(Photo.item_id==itemid,Photo.external_id==entry['id']))
                    if photo is None:photo=Photo(item_id=itemid,external_id=entry['id']);db.add(photo)
                    photo.media_id=asset_id;photo.etag=etag;photo.sha256=sha;photo.error=None
            except Exception:
                errors+=1
                with database.session_scope() as db:
                    locked_catalog(db,cid,token)
                    photo=db.scalar(select(Photo).where(Photo.item_id==itemid,Photo.external_id==entry['id']))
                    if photo is None:photo=Photo(item_id=itemid,external_id=entry['id']);db.add(photo)
                    photo.error='Фото не обновлено; прежнее сохранено. Будет повторная попытка'
    return errors

def selected_photos(db,items):
    ids=[i.id for i in items];allphotos=db.scalars(select(Photo).where(Photo.item_id.in_(ids))).all();by={}
    for p in allphotos:by.setdefault(p.item_id,[]).append(p)
    result={}
    for item in items:
        if not item.images:continue
        stored=by.get(item.id,[]);current={x.external_id:x for x in stored if x.media_id}
        chosen=[current[e['id']] for e in item.images if e['id'] in current]
        # New references may fail: keep the previous available photo until success.
        result[item.product_id]=chosen or [x for x in stored if x.media_id][-1:]
    return result

def claim_job():
    now=datetime.utcnow()
    with database.session_scope() as db:
        row=db.scalar(select(Catalog).where(or_(Catalog.status=='queued',(Catalog.status=='running')&(Catalog.lease_until<now),Catalog.next_sync_at<=now)).order_by(Catalog.id).with_for_update(skip_locked=True).limit(1))
        if not row:return None
        row.status='running';row.job_token=str(uuid4());row.lease_until=now+timedelta(minutes=10);row.next_sync_at=None;row.error=None
        return row.id,row.job_token,row.point_id,row.external_id,row.connection,row.base_url

def run_job(job):
    cid,token,point,pid,connection,base=job
    try:
        actual,key,conn=settings()
        if actual!=base or conn!=connection:raise ImportErrorSafe('Настройки источника изменились. Используйте соответствующее подключение')
        data=fetch_catalog(point,pid);apply_snapshot(cid,token,data);errors=sync_images(cid,token)
        with database.session_scope() as db:
            row=locked_catalog(db,cid,token);row.status='warning' if errors else 'success';row.error=f'Не обновлено фото: {errors}' if errors else None;row.failures=0;row.lease_until=None
            row.next_sync_at=datetime.utcnow()+timedelta(minutes=5 if errors else 30) if errors or row.auto_sync else None
    except Exception as exc:
        with database.session_scope() as db:
            row=db.get(Catalog,cid,with_for_update=True)
            if not row or row.job_token!=token:return
            row.status='error';row.failures+=1;row.lease_until=None;row.error=str(exc)[:240] if isinstance(exc,ImportErrorSafe) else 'Не удалось обновить прайс; предыдущий ассортимент сохранён'
            row.next_sync_at=datetime.utcnow()+timedelta(minutes=min(60,2**min(row.failures,6))) if row.failures<=5 or row.auto_sync else None

def start_worker():
    global _thread
    if _thread and _thread.is_alive():return
    _stop.clear()
    def loop():
        while not _stop.is_set():
            try:
                settings();job=claim_job()
                if job:run_job(job)
            except ImportErrorSafe:pass
            except Exception:log.warning('Price import worker failed')
            _stop.wait(5)
    _thread=threading.Thread(target=loop,name='core-price-import',daemon=True);_thread.start()

def stop_worker():
    _stop.set()
    if _thread:_thread.join(timeout=1)
