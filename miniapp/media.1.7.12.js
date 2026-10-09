/* Shared private photo uploader. No Telegram file transport. */
const mediaCache=new Map();let mediaBytes=0,mediaActive=0,mediaQueue=[],mediaToken=null;
function clearMediaCache(){for(const x of mediaCache.values())URL.revokeObjectURL(x.url);mediaCache.clear();mediaBytes=0;if(state.avatarUrl)URL.revokeObjectURL(state.avatarUrl);state.avatarUrl=null;}
function mediaLimit(job){return new Promise((resolve,reject)=>{mediaQueue.push({job,resolve,reject});pumpMedia()})}
function pumpMedia(){while(mediaActive<3&&mediaQueue.length){const t=mediaQueue.shift();mediaActive++;Promise.resolve().then(t.job).then(t.resolve,t.reject).finally(()=>{mediaActive--;pumpMedia()})}}
async function mediaUrl(path,thumbnail=false,signal){
 if(mediaToken!==state.token){clearMediaCache();mediaToken=state.token}
 const url=path+(thumbnail?(path.includes('?')?'&':'?')+'thumbnail=true':'');
 const key=state.token+'|'+url,hit=mediaCache.get(key);
 if(hit&&Date.now()-hit.at<60000){mediaCache.delete(key);mediaCache.set(key,hit);return hit.url}
 if(hit){URL.revokeObjectURL(hit.url);mediaBytes-=hit.size;mediaCache.delete(key)}
 return mediaLimit(async()=>{if(signal?.aborted)throw new DOMException("Cancelled","AbortError");const blob=await apiBlob(url,signal);const value={url:URL.createObjectURL(blob),size:blob.size,at:Date.now()};mediaCache.set(key,value);mediaBytes+=blob.size;while(mediaCache.size>40||mediaBytes>24*1024*1024){const [k,v]=mediaCache.entries().next().value;if(k===key)break;URL.revokeObjectURL(v.url);mediaBytes-=v.size;mediaCache.delete(k)}return value.url;});
}
const mediaRequests=new Set();
new MutationObserver(()=>{for(const r of mediaRequests){if(!r.img.isConnected){r.controller.abort();mediaRequests.delete(r)}}}).observe(document.body,{childList:true,subtree:true});
function lazyMedia(img,path,thumbnail=true){
 img.loading='lazy';img.decoding='async';
 const load=async()=>{if(!img.isConnected)return;const pending={img,controller:new AbortController()};mediaRequests.add(pending);try{const url=await mediaUrl(path,thumbnail,pending.controller.signal);if(img.isConnected)img.src=url}catch(e){if(img.isConnected&&e.name!=='AbortError')img.alt='Не удалось загрузить фото'}finally{mediaRequests.delete(pending)}};
 if(!window.IntersectionObserver){load();return}
 const observer=new IntersectionObserver(es=>{if(es.some(e=>e.isIntersecting)){observer.disconnect();load()}},{rootMargin:'200px'});observer.observe(img);
 setTimeout(()=>observer.disconnect(),120000);
}
async function viewMedia(path){try{closeWorkSheet();showSheet('<h3>Фото</h3><div id="mediaFull">Загрузка…</div><button class="btn secondary" onclick="closeWorkSheet()">Закрыть</button>');const u=await mediaUrl(path);const slot=document.getElementById('mediaFull');if(slot)slot.innerHTML=`<img src="${u}" style="width:100%;height:auto" alt="Фотография">`}catch(e){flash(e.message,'error')}}
async function preparePhoto(file,large){
 if(file.size>25*1024*1024)throw new Error('Выберите фото размером до 25 МБ');
 const url=URL.createObjectURL(file);try{
 const img=new Image();img.src=url;await img.decode();if(img.width*img.height>40000000)throw new Error('Слишком большое разрешение фотографии');
 const side=large?3200:2048,ratio=Math.min(1,side/Math.max(img.width,img.height));const canvas=document.createElement('canvas');canvas.width=Math.round(img.width*ratio);canvas.height=Math.round(img.height*ratio);canvas.getContext('2d').drawImage(img,0,0,canvas.width,canvas.height);
 const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/jpeg',large?.93:.85));if(!blob)throw new Error('Не удалось обработать фото');return blob;
 }finally{URL.revokeObjectURL(url)}
}
function putPhoto(url,blob,progress){return new Promise((resolve,reject)=>{const xhr=new XMLHttpRequest();xhr.open('PUT',url);xhr.timeout=120000;xhr.setRequestHeader('Content-Type','image/jpeg');xhr.upload.onprogress=e=>{if(e.lengthComputable)progress(Math.round(e.loaded/e.total*100))};xhr.onload=()=>xhr.status>=200&&xhr.status<300?resolve():reject(new Error('Хранилище не приняло фото. Проверьте соединение и повторите'));xhr.onerror=()=>reject(new Error('Не удалось отправить фото. Проверьте сеть; если ошибка повторяется — настройки CORS хранилища'));xhr.ontimeout=()=>reject(new Error('Загрузка заняла слишком много времени. Повторите'));xhr.send(blob)})}
function photoUpload(target,onReady){
 document.getElementById('mediaUploadDialog')?.remove();let file=null,preview=null,busy=false,upload=null,blob=null;
 const el=document.createElement('div');el.id='mediaUploadDialog';el.className='sheet-backdrop';el.innerHTML=`<div class="sheet" role="dialog" aria-modal="true" aria-label="Добавить фото"><h3>Добавить фото</h3><div class="row"><button class="btn secondary" data-pick="camera">Камера</button><button class="btn secondary" data-pick="gallery">Галерея</button></div><input hidden type="file" accept="image/*" capture="environment" data-file="camera"><input hidden type="file" accept="image/*" data-file="gallery"><img data-preview hidden style="max-height:45vh;max-width:100%;margin:12px 0;border-radius:12px"><label class="row"><input data-quality type="checkbox" style="width:auto">Мелкий текст / накладная</label><p data-message aria-live="polite">Выберите фотографию</p><progress data-progress max="100" value="0" style="width:100%" hidden></progress><div class="row"><button class="btn" data-send disabled>Загрузить</button><button class="btn secondary" data-remove hidden>Убрать</button><button class="btn secondary" data-close>Закрыть</button></div></div>`;
 document.body.append(el);const msg=el.querySelector('[data-message]'),send=el.querySelector('[data-send]'),img=el.querySelector('[data-preview]'),bar=el.querySelector('progress');
 const close=()=>{if(busy)return;el.remove();if(preview)URL.revokeObjectURL(preview)};el.querySelector('[data-close]').onclick=close;
 for(const b of el.querySelectorAll('[data-pick]'))b.onclick=()=>{if(!busy)el.querySelector(`[data-file="${b.dataset.pick}"]`).click()};
 for(const input of el.querySelectorAll('[data-file]'))input.onchange=()=>{file=input.files[0];if(!file)return;if(preview)URL.revokeObjectURL(preview);preview=URL.createObjectURL(file);img.src=preview;img.hidden=false;send.disabled=false;el.querySelector('[data-remove]').hidden=false;msg.textContent='Проверьте фото перед отправкой';upload=null;blob=null};
 el.querySelector('[data-quality]').onchange=()=>{blob=null;upload=null};
 el.querySelector('[data-remove]').onclick=()=>{if(busy)return;file=null;blob=null;upload=null;img.hidden=true;send.disabled=true;if(preview)URL.revokeObjectURL(preview);preview=null;msg.textContent='Выберите другую фотографию'};
 send.onclick=async()=>{
 if(busy||!file)return;busy=true;send.disabled=true;for(const x of el.querySelectorAll('input'))x.disabled=true;
 try{
  if(!blob){msg.textContent='Подготовка…';blob=await preparePhoto(file,el.querySelector('[data-quality]').checked)}
  if(!upload){upload=await api('/api/media/uploads',{method:'POST',body:JSON.stringify({...target,size_bytes:blob.size,request_key:crypto.randomUUID()})})}
  if(upload.status==='pending'){
   msg.textContent='Отправляется…';bar.hidden=false;await putPhoto(upload.url,blob,n=>{bar.value=n;msg.textContent=`Отправляется: ${n}%`});
   upload=await api(`/api/media/uploads/${upload.upload_id}/complete`,{method:'POST',body:'{}'});
  }
  msg.textContent='Обрабатывается…';bar.value=100;
  for(let i=0;i<90&&upload.status!=='ready';i++){
   if(['failed','expired','cancelled'].includes(upload.status)){const message=upload.error||'Создайте новую загрузку';upload=null;throw new Error(message)}
   await new Promise(r=>setTimeout(r,1000));upload=await api(`/api/media/uploads/${upload.upload_id}`);
  }
  if(upload.status!=='ready')throw new Error('Фото ещё обрабатывается. Нажмите «Повторить», чтобы проверить результат');
  clearMediaCache();busy=false;close();flash('Фото добавлено');await onReady?.(upload.result);
 }catch(e){msg.textContent=e.message||'Не удалось загрузить фото';send.textContent='Повторить';if(upload?.expires_at&&new Date(upload.expires_at)<new Date())upload=null}
 finally{busy=false;send.disabled=false;for(const x of el.querySelectorAll('input'))x.disabled=false}
 };
}
async function requestProfileAvatar(){const p=await api('/api/profile/me');photoUpload({entity_type:'profile_avatar',entity_id:p.id},async()=>{state.profileCache=await api('/api/profile/me');await renderProfileSettings();renderContextNav()})}
async function requestShiftFieldPhoto(valueId,label){photoUpload({entity_type:'shift_report_field',entity_id:valueId,label},()=>{const button=[...document.querySelectorAll('button')].find(b=>(b.getAttribute('onclick')||'').includes(`requestShiftFieldPhoto(${valueId},`));const field=button?.closest('.handover-field');const counter=field?.querySelector('.photo-count');if(counter){const n=Number(counter.textContent.replace(/\D/g,''))||0;counter.textContent='Фото: '+(n+1)}if(field&&!field.querySelector('[data-new-gallery]')){const b=document.createElement('button');b.className='btn secondary mini';b.dataset.newGallery='1';b.textContent='Посмотреть фото';b.onclick=()=>showShiftPhotos(valueId);field.append(b)}})}
async function requestPhoto(type,id,label){photoUpload({entity_type:type,entity_id:id,label},r=>{const row=type==='inspection_value'?document.querySelector(`[data-inspection-row="${id}"] .inspection-photo-actions`):document.querySelector('.inspection-photo-actions');if(row){const b=document.createElement('button');b.className='photo-chip';b.textContent='📷 Фото';b.onclick=()=>openCorePhoto(r.id);row.append(b)}})}
async function requestTestQuestionPhoto(id){photoUpload({entity_type:'test_question',entity_id:id},async()=>{const t=await api('/api/testing/tests/'+state.testEditing.id);state.testEditing=t;const img=document.querySelector(`[data-test-q-image="${id}"]`);if(img)protectedImage(`/api/testing/questions/${id}/image`,img);else flash('Фото добавлено. Оно появится при повторном открытии вопроса')})}
const originalTaskAttach=taskAttach;
taskAttach=async function(tid,aid,kind,itemId=null){if(kind!=='photo')return originalTaskAttach(tid,aid,kind,itemId);photoUpload({entity_type:'task_attachment',entity_id:tid,assignee_id:aid,checklist_item_id:itemId,label:itemId?'Фото к пункту чек-листа':'Результат задачи'},async()=>{const comment=document.getElementById('taskComment')?.value;await openTaskV2(tid);if(comment&&document.getElementById('taskComment'))document.getElementById('taskComment').value=comment})};
const originalKbMedia=requestKbMedia;
requestKbMedia=async function(kind){if(kind!=='photo')return originalKbMedia(kind);const a=state.kbEditingArticle;if(!a?.id)return flash('Сначала сохраните статью','error');photoUpload({entity_type:'knowledge_media',entity_id:a.id},async r=>{const latest=await api('/api/knowledge/articles/'+a.id);state.kbEditingArticle.media=latest.media;insertKbMedia(r.id,'photo');flash('Фото добавлено в текст статьи. Сохраните изменения')})};
async function showShiftPhotos(valueId){try{const rows=await api(`/api/photos?entity_type=shift_report_field&entity_id=${valueId}`);closeWorkSheet();showSheet('<h3>Фотографии</h3><div id="shiftPhotoGallery" class="media-grid"></div>');const box=document.getElementById('shiftPhotoGallery');for(const p of rows){const b=document.createElement('button');b.className='media-thumb';const img=document.createElement('img');img.alt='Фото пересменки';b.append(img);b.onclick=()=>viewMedia(`/api/photos/${p.id}/content`);box.append(b);lazyMedia(img,`/api/photos/${p.id}/content`)}if(!rows.length)box.textContent='Фото пока нет'}catch(e){flash(e.message,'error')}}
async function openCorePhoto(id){return viewMedia(`/api/photos/${id}/content`)}
async function protectedImage(url,img){lazyMedia(img,url,false)}
async function hydrateKbEditorThumbs(a){for(const m of a.media||[]){if(m.kind!=='photo')continue;const slot=document.querySelector(`[data-kb-thumb="${m.id}"]`);if(!slot)continue;const img=document.createElement('img');img.alt='Фото';slot.replaceWith(img);lazyMedia(img,m.content_url,true)}}
async function hydrateKbMedia(article){const body=document.getElementById('kbArticleBody');if(!body)return;const byId=Object.fromEntries((article.media||[]).map(m=>[m.id,m]));for(const n of body.querySelectorAll('[data-media-id]')){const m=byId[n.dataset.mediaId];if(!m){n.remove();continue}const slot=document.createElement('div');slot.className='kb-media-view';n.replaceWith(slot);if(m.kind==='photo'){const img=document.createElement('img');img.alt=m.caption||'Фото';slot.append(img);lazyMedia(img,m.content_url,true);img.onclick=()=>viewMedia(m.content_url)}else{mediaUrl(m.content_url).then(u=>{if(slot.isConnected){const video=document.createElement('video');video.controls=true;video.playsInline=true;video.src=u;slot.append(video)}}).catch(()=>slot.textContent='Не удалось загрузить видео')}if(m.caption){const caption=document.createElement('div');caption.className='kb-caption';caption.textContent=m.caption;slot.append(caption)}}}
window.addEventListener('pagehide',clearMediaCache);
// Manage attached photos in the same dialog. Parent status/permissions are checked server-side.
const openPhotoUploader=photoUpload;
photoUpload=function(target,onReady){
 openPhotoUploader(target,onReady);
 const dialog=document.getElementById('mediaUploadDialog');const shelf=document.createElement('div');shelf.innerHTML='<h4>Прикреплённые фото</h4><div class="media-grid"></div>';dialog.querySelector('.sheet').append(shelf);const grid=shelf.querySelector('.media-grid');
 const query=new URLSearchParams(Object.entries(target).filter(([k,v])=>v!=null&&['entity_type','entity_id','assignee_id','checklist_item_id'].includes(k)));
 api('/api/media/attachments?'+query).then(rows=>{if(!rows.length){shelf.remove();return}for(const row of rows){const card=document.createElement('div');const img=document.createElement('img');img.style='width:100%;aspect-ratio:1;object-fit:cover;border-radius:12px';img.alt='Прикреплённое фото';card.append(img);grid.append(card);lazyMedia(img,row.url,true);const b=document.createElement('button');b.className='btn secondary mini';b.textContent='Удалить';b.onclick=async()=>{if(!confirm('Удалить это фото из записи?'))return;b.disabled=true;try{await api('/api/media/detach',{method:'POST',body:JSON.stringify({...target,attachment_id:row.id})});clearMediaCache();card.remove();if(target.entity_type==='profile_avatar'||target.entity_type==='task_attachment')await onReady?.({deleted:true});if(target.entity_type==='shift_report_field'){const b=[...document.querySelectorAll('button')].find(b=>(b.getAttribute('onclick')||'').includes(`requestShiftFieldPhoto(${target.entity_id},`));const c=b?.closest('.handover-field')?.querySelector('.photo-count');if(c)c.textContent='Фото: '+Math.max(0,(Number(c.textContent.replace(/\D/g,''))||0)-1)}if(target.entity_type==='knowledge_media'){document.getElementById('kbEditor')?.querySelectorAll(`[data-media-id="${row.id}"]`).forEach(n=>n.remove());state.kbEditingArticle.media=(state.kbEditingArticle.media||[]).filter(m=>m.id!==row.id)}if(target.entity_type==='test_question'){document.querySelector(`[data-test-q-image="${target.entity_id}"]`)?.remove();const q=state.testEditing?.questions?.find(q=>q.id===target.entity_id);if(q)q.has_image=false}flash('Фото удалено')}catch(e){flash(e.message,'error');b.disabled=false}};card.append(b)}}).catch(()=>shelf.remove());
};
