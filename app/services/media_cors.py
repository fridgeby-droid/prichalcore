"""Merge only Core's rule into a shared bucket; preserve every other project."""
from urllib.parse import urlsplit
from app.core.config import APP_URL,S3_BUCKET
from app.services.storage import _s3_client
from botocore.exceptions import ClientError

def configure():
    parsed=urlsplit(APP_URL)
    if parsed.scheme not in {'http','https'} or not parsed.netloc:raise ValueError('APP_URL должен содержать полный адрес Mini App')
    origin=f'{parsed.scheme}://{parsed.netloc}'
    s3=_s3_client()
    if s3 is None:return {'status':'disabled'}
    try:rules=s3.get_bucket_cors(Bucket=S3_BUCKET).get('CORSRules',[])
    except ClientError as exc:
        if exc.response.get('Error',{}).get('Code') not in {'NoSuchCORSConfiguration','NoSuchCORS'}:raise
        rules=[]
    desired={'ID':'prichal-core-direct-photo-upload','AllowedOrigins':[origin],'AllowedMethods':['PUT'],'AllowedHeaders':['content-type'],'ExposeHeaders':['ETag'],'MaxAgeSeconds':600}
    existing=next((r for r in rules if r.get('ID')==desired['ID']),None)
    if existing==desired:return {'status':'unchanged','origin':origin}
    merged=[r for r in rules if r.get('ID')!=desired['ID']]+[desired]
    s3.put_bucket_cors(Bucket=S3_BUCKET,CORSConfiguration={'CORSRules':merged})
    return {'status':'configured','origin':origin}
