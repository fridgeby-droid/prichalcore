"""Read the documented AI directory snapshot. No Saby calls or AI inference."""
import os
from datetime import datetime, date, timedelta
from urllib.parse import urlsplit
from uuid import uuid4
import httpx
from sqlalchemy import select
from fastapi import HTTPException
from app.db import database
from app.db.models import CoreDirectoryState, User, AdminAuditLog

MAX_BYTES = 5 * 1024 * 1024


def configuration():
    base = (os.getenv('PRICHAL_AI_URL') or os.getenv('AI_AGENT_URL') or '').strip().rstrip('/')
    key = os.getenv('CORE_DIRECTORY_API_KEY', '').strip()
    u = urlsplit(base)
    if u.scheme != 'https' or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise ValueError('Укажите HTTPS-адрес Причал AI в PRICHAL_AI_URL (или AI_AGENT_URL)')
    if len(key) < 32:
        raise ValueError('CORE_DIRECTORY_API_KEY должен содержать не менее 32 символов')
    return base + '/api/v1/core/directories', key


def validate_snapshot(d):
    def require(ok):
        if not ok: raise ValueError('Некорректный формат справочников Причал AI')
    def ident(v):
        require(isinstance(v, str) and 0 < len(v) <= 128 and v == v.strip() and not any(ord(c)<32 for c in v))
        return v
    def stamp(v, optional=False, day=False):
        if v is None and optional:return
        require(isinstance(v,str))
        if day: date.fromisoformat(v)
        else:
            parsed=datetime.fromisoformat(v.replace('Z','+00:00'))
            require(parsed.tzinfo is not None)
    require(isinstance(d,dict) and type(d.get('schema_version')) is int and d['schema_version']==1)
    require(d.get('complete_snapshot') is True and d.get('scope')=='retail_excluding_rc' and d.get('seller_source')=='observed_saby_payment_accounts')
    stamp(d.get('generated_at'));stamp(d.get('last_successful_sync_at'),True)
    for field,count in [('stores','store_count'),('sellers','seller_count')]:
        require(isinstance(d.get(field),list) and type(d.get(count)) is int and len(d[field])==d[count])
    points=set();sellers=set()
    for s in d['stores']:
        require(isinstance(s,dict));sid=ident(s.get('saby_point_id'));require(sid not in points and sid!='23109');points.add(sid)
        require(isinstance(s.get('name'),str) and len(s['name'])<=256)
        for k in ['address','locality']:require(isinstance(s.get(k,''),str))
        stamp(s.get('updated_at'),True)
    for s in d['sellers']:
        require(isinstance(s,dict));sid=ident(s.get('saby_seller_id'));require(sid not in sellers);sellers.add(sid)
        require(s.get('display_name') is None or isinstance(s['display_name'],str) and len(s['display_name'])<=256)
        require(isinstance(s.get('observed_names'),list) and all(isinstance(n,str) for n in s['observed_names']))
        stamp(s.get('first_seen'),True,True);stamp(s.get('last_seen'),True,True)
        require(isinstance(s.get('stores'),list));seen=set()
        for p in s['stores']:
            require(isinstance(p,dict));pid=ident(p.get('saby_point_id'));require(pid in points and pid not in seen);seen.add(pid)
            stamp(p.get('first_seen'),True,True);stamp(p.get('last_seen'),True,True)
    return d


def fetch_snapshot(url,key):
    try:
        # No redirects: never forward this service's credential to another host.
        with httpx.Client(timeout=httpx.Timeout(20,connect=5),follow_redirects=False,trust_env=False) as client:
            with client.stream('GET',url,headers={'Authorization':'Bearer '+key,'Accept':'application/json'}) as r:
                if r.status_code==401:raise ValueError('Причал AI отклонил ключ интеграции')
                if r.status_code==503:raise ValueError('Справочники Причал AI пока недоступны или API не настроен')
                if r.status_code!=200:raise ValueError('Причал AI вернул ошибку HTTP '+str(r.status_code))
                body=bytearray()
                for chunk in r.iter_bytes():
                    body.extend(chunk)
                    if len(body)>MAX_BYTES:raise ValueError('Ответ справочников превышает допустимый размер')
        import json
        return validate_snapshot(json.loads(body))
    except httpx.HTTPError:
        raise ValueError('Не удалось подключиться к Причал AI. Проверьте адрес и доступность сервера') from None
    except (TypeError,KeyError):
        raise ValueError('Некорректный формат справочников Причал AI') from None


def sync_directories(actor_id):
    attempt=str(uuid4());now=datetime.utcnow()
    with database.session_scope() as db:
        row=db.get(CoreDirectoryState,1,with_for_update=True)
        if row.status=='running' and row.attempted_at and row.attempted_at>now-timedelta(minutes=2):raise HTTPException(409,'Загрузка уже выполняется')
        row.attempt_id=attempt;row.attempted_at=now;row.status='running';row.error=None
    try:
        url,key=configuration()
        with database.session_scope() as db:
            previous=db.get(CoreDirectoryState,1).source_url
            if previous and previous!=url:raise ValueError('Адрес источника изменился. Проверьте настройки до загрузки, чтобы не смешать связи')
        snapshot=fetch_snapshot(url,key)
        with database.session_scope() as db:
            row=db.get(CoreDirectoryState,1,with_for_update=True)
            if row.attempt_id!=attempt:raise HTTPException(409,'Началась более новая загрузка')
            actor=db.get(User,actor_id,populate_existing=True)
            if not actor or not actor.active or actor.status!='active' or (actor.role_key or actor.role)!='admin':raise ValueError('Доступ администратора изменился')
            row.snapshot=snapshot;row.source_url=url;row.imported_at=datetime.utcnow();row.status='success';row.error=None
            db.add(AdminAuditLog(actor_user_id=actor_id,action='directories.sync',entity_type='saby_directories',entity_id='1',details_json={'stores':snapshot['store_count'],'sellers':snapshot['seller_count']}))
    except Exception as exc:
        # Do not persist upstream bodies, credentials, network exception strings.
        allowed=isinstance(exc,ValueError) and str(exc).startswith(('Укажите','CORE_DIRECTORY_API_KEY','Причал AI','Справочники','Не удалось','Ответ справочников','Адрес источника','Доступ администратора'))
        message=str(exc)[:240] if allowed else 'Не удалось обновить справочники. Предыдущая копия сохранена'
        with database.session_scope() as db:
            row=db.get(CoreDirectoryState,1,with_for_update=True)
            if row.attempt_id==attempt:row.status='error';row.error=message
        raise HTTPException(502,message) from None
    return {'status':'success','store_count':snapshot['store_count'],'seller_count':snapshot['seller_count']}
