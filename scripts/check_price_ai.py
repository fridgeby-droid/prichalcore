"""Run inside the Core container with its environment; never prints tokens/paths."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.services.price_import import fetch_catalog,download_image,settings,ImportErrorSafe

def main():
    p=argparse.ArgumentParser();p.add_argument('--point',default='23109');p.add_argument('--price',default='66');p.add_argument('--image',action='store_true');a=p.parse_args()
    try:
        listing=fetch_catalog(a.point)
        if not any(x['id']==a.price for x in listing['price_lists']):raise ImportErrorSafe('Прайс отсутствует в списке')
        data=fetch_catalog(a.point,a.price)
        result={'ok':True,'point_id':a.point,'price_id':a.price,'price_lists':len(listing['price_lists']),'product_count':data['product_count'],'folders_skipped':data['folders_skipped'],'complete_snapshot':True,'image_checked':False}
        if a.image:
            entry=next((im for product in data['products'] for im in product['images']),None)
            if entry:
                base,key,_=settings();photo=download_image(base,key,entry,None)
                result.update(image_checked=True,image_bytes=len(photo[3]),thumbnail_bytes=len(photo[4]))
            else:result['image_note']='В прайсе нет изображений'
        print(json.dumps(result,ensure_ascii=False));return 0
    except ImportErrorSafe as e:print(json.dumps({'ok':False,'error':str(e)},ensure_ascii=False));return 1
    except Exception:print(json.dumps({'ok':False,'error':'Проверка не завершена; проверьте доступность AI и конфигурацию'},ensure_ascii=False));return 1
if __name__=='__main__':sys.exit(main())
