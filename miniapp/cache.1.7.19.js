/* Session-local JSON cache. Only explicitly listed read endpoints are eligible. */
class CoreDataCache {
  constructor({token, now=Date.now, changed=()=>{}, denied=()=>{}, maxBytes=4*1024*1024, maxEntries=100}) {
    Object.assign(this,{token,now,changed,denied,maxBytes,maxEntries});
    this.entries=new Map();this.pending=new Map();this.bytes=0;this.epoch=0;this.writes=0;this.owner=token();
  }
  clear(){this.entries.clear();this.pending.clear();this.bytes=0;this.epoch++;}
  sync(){if(this.owner!==this.token()){this.clear();this.owner=this.token();}}
  policy(path){
    const [p,q='']=path.split('?');
    if(new URLSearchParams(q).get('manage')==='true')return null;
    if(['/api/reference/stores','/api/order-settings/units','/api/order-settings/categories','/api/order-settings/suppliers'].includes(p))return [300000,600000];
    if(['/api/employees','/api/employees/meta','/api/tasks-v2/meta','/api/shifts/mine','/api/shifts/forms','/api/shifts/drafts','/api/inspections/meta','/api/inspections/forms','/api/inspections/drafts','/api/work-schedule/context','/api/work-schedule/my','/api/work-schedule/history','/api/work-schedule/errors','/api/work-schedule/substitutions','/api/order-settings/stores','/api/order-settings/schedule','/api/orders','/api/orders/today-availability','/api/dashboard/ui','/api/dashboard/badges','/api/profile/me','/api/profile/work','/api/profile/learning','/api/profile/activity','/api/tasks-v2','/api/inspections','/api/inspections/control/weekly','/api/inspections/violations/list','/api/shifts/control','/api/shifts/history','/api/knowledge/sections','/api/knowledge/articles','/api/knowledge/required','/api/knowledge/search','/api/knowledge/related'].includes(p))return [15000,120000];
    if(/^\/api\/knowledge\/articles\/\d+$/.test(p))return [15000,120000];
    return null;
  }
  put(key,value){
    const json=JSON.stringify(value),size=json.length*2;
    if(this.entries.has(key)){this.bytes-=this.entries.get(key).size;this.entries.delete(key);}
    if(size>this.maxBytes)return;
    this.entries.set(key,{json,size,at:this.now()});this.bytes+=size;
    while(this.entries.size>this.maxEntries||this.bytes>this.maxBytes){const [k,v]=this.entries.entries().next().value;this.entries.delete(k);this.bytes-=v.size;}
  }
  async read(path,opt,fetcher){
    this.sync();const epoch=this.epoch,owner=this.owner;
    const policy=this.owner&&!this.writes&&!opt.signal&&!opt.headers&&opt.cache!=='no-store'?this.policy(path):null;
    const valid=()=>epoch===this.epoch&&owner===this.token();
    const load=()=>{
      if(policy&&this.pending.has(path))return this.pending.get(path);
      const job=(async()=>{
        try{
          const data=await fetcher();
          if(!valid())throw new DOMException('Данные изменились. Обновите экран.','AbortError');
          if(policy)this.put(path,data);
          return data;
        }catch(e){if(valid()&&[401,403].includes(e.status)){this.clear();if(!opt.prefetch)this.denied(e);}throw e;}
      })();
      if(policy){this.pending.set(path,job);job.finally(()=>{if(this.pending.get(path)===job)this.pending.delete(path);}).catch(()=>{});}
      return job;
    };
    const hit=policy&&this.entries.get(path);
    if(hit&&this.now()-hit.at<policy[1]){
      this.entries.delete(path);this.entries.set(path,hit);
      if(this.now()-hit.at>=policy[0])load().then(data=>{if(valid()&&JSON.stringify(data)!==hit.json)this.changed(path);}).catch(e=>{if(valid()&&e.name!=='AbortError')this.changed(path,true);});
      return JSON.parse(hit.json);
    }
    if(hit){this.entries.delete(path);this.bytes-=hit.size;}
    const data=await load();return data==null?data:JSON.parse(JSON.stringify(data));
  }
  async write(fetcher){this.sync();this.clear();this.writes++;try{return await fetcher();}finally{this.writes--;this.clear();}}
}
