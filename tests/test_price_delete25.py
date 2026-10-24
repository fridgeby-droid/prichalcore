from test_price_lists import prices,env,snapshot,confirm,imported
from app.services import price_import as svc
from app.db import database
from app.db.models import ImportedPriceList as Catalog,ImportedPriceItem as Item,Product
import pytest

def test_delete_cancels_job_disables_products_and_reconnects(prices):
    env,cid,sid=prices;client,_,_,auth,seller=env
    svc.apply_snapshot(cid,'one',snapshot());i=confirm(prices)
    assert client.delete(f'/api/admin-center/price-lists/{cid}',headers=seller).status_code==403
    assert client.delete(f'/api/admin-center/price-lists/{cid}',headers=auth).status_code==200
    assert not client.get('/api/admin-center/price-lists',headers=auth).json()['catalogs']
    with database.session_scope() as db:
        assert db.get(Product,i.product_id) is not None
        assert not db.get(Product,i.product_id).active
        assert not db.get(Item,i.id).present
        assert db.get(Catalog,cid).next_sync_at is None
    assert svc.claim_job() is None
    with pytest.raises(svc.ImportErrorSafe):svc.apply_snapshot(cid,'one',snapshot())
    assert client.post(f'/api/admin-center/price-lists/{cid}/sync',headers=auth).status_code==404
    r=client.post('/api/admin-center/price-lists',headers=auth,json={'point_id':'23109','external_id':'66','supplier_id':sid})
    assert r.status_code==200,r.text
    assert r.json()['id']==cid
    job=svc.claim_job();svc.apply_snapshot(cid,job[1],snapshot())
    assert imported()[0].product_id==i.product_id
    with database.session_scope() as db:assert db.get(Product,i.product_id).active
