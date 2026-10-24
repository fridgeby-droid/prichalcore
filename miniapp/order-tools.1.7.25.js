function productFilterControls(d){
 const f=state.productFilters||={query:'',supplier:'',category:'',active:''};
 const options=(rows,v)=>rows.map(x=>`<option value="${x.id}" ${String(x.id)===v?'selected':''}>${esc(x.name)}</option>`).join('');
 return `<div class="grid" style="margin-top:16px">${field('Поиск товара',`<input id="pfQuery" type="search" value="${esc(f.query)}" placeholder="Название товара" oninput="applyProductFilters(true)">`)}${field('Поставщик',`<select id="pfSupplier" onchange="applyProductFilters(true)"><option value="">Все поставщики</option>${options(d.suppliers,f.supplier)}</select>`)}${field('Категория',`<select id="pfCategory" onchange="applyProductFilters(true)"><option value="">Все категории</option><option value="none" ${f.category==='none'?'selected':''}>Без категории</option>${options(d.categories,f.category)}</select>`)}${field('Активность',`<select id="pfActive" onchange="applyProductFilters(true)"><option value="">Все товары</option><option value="1" ${f.active==='1'?'selected':''}>Активные</option><option value="0" ${f.active==='0'?'selected':''}>Отключённые</option></select>`)}</div><div class="row between"><span id="pfCount" class="small"></span><button class="btn secondary mini" onclick="resetProductFilters()">Сбросить фильтры</button></div>`;
}
function applyProductFilters(read=false){
 if(!document.getElementById('pfQuery'))return;
 if(read)state.productFilters={query:document.getElementById('pfQuery').value,supplier:document.getElementById('pfSupplier').value,category:document.getElementById('pfCategory').value,active:document.getElementById('pfActive').value};
 const f=state.productFilters,normalize=s=>String(s||'').toLocaleLowerCase('ru').replaceAll('ё','е').trim(),q=normalize(f.query);
 const products=new Map(state.orderAdminData.products.map(p=>[String(p.id),p]));let count=0;
 document.querySelectorAll('[data-filter-product]').forEach(el=>{const p=products.get(el.dataset.filterProduct);const visible=!!p&&normalize(p.name).includes(q)&&(!f.supplier||String(p.supplier_id)===f.supplier)&&(!f.category||(f.category==='none'?!p.category_id:String(p.category_id)===f.category))&&(!f.active||Boolean(p.active)===(f.active==='1'));el.style.display=visible?'':'none';if(visible)count++;});
 document.getElementById('pfCount').textContent=count?`Найдено: ${count} из ${products.size}`:'По выбранным фильтрам ничего не найдено';
}
function resetProductFilters(){for(const id of ['pfQuery','pfSupplier','pfCategory','pfActive'])document.getElementById(id).value='';applyProductFilters(true);}
function openBulkOrderSchedule(admin=false){
 if(!permission('orders.schedule_edit','edit'))return;
 const d=admin?state.orderAdminData.schedule:state.orderScheduleData;state.bulkScheduleAdmin=admin;
 const days=['Пн','Вт','Ср','Чт','Пт','Сб','Вс'];
 showSheet(`<h3>График поставщика для магазинов</h3>${field('Поставщик',selectHtml('bulkSchSupplier',d.suppliers.filter(x=>x.active)))}<p class="small">Выбранные дни недели заменят график этого поставщика у отмеченных магазинов.</p><div class="row"><button class="btn secondary mini" onclick="selectBulkStores(true)">Выбрать все</button><button class="btn secondary mini" onclick="selectBulkStores(false)">Снять выбор</button></div><div style="max-height:240px;overflow:auto">${d.stores.filter(x=>x.active).map(x=>`<label class="row" style="padding:8px"><input type="checkbox" style="width:auto" data-bulk-store="${x.id}" onchange="updateBulkStoreCount()">${esc(x.name)}</label>`).join('')}</div><p id="bulkSchCount" class="small">Выбрано магазинов: 0</p><div class="day-checks">${days.map((x,i)=>`<label class="pill"><input type="checkbox" style="width:auto" data-bulk-day="${i}"> ${x}</label>`).join('')}</div><button class="btn" style="margin-top:16px" onclick="saveBulkOrderSchedule(this)">Применить график</button>`);
}
function selectBulkStores(checked){document.querySelectorAll('[data-bulk-store]').forEach(x=>x.checked=checked);updateBulkStoreCount();}
function updateBulkStoreCount(){document.getElementById('bulkSchCount').textContent='Выбрано магазинов: '+document.querySelectorAll('[data-bulk-store]:checked').length;}
async function saveBulkOrderSchedule(button){
 const supplier_id=+document.getElementById('bulkSchSupplier').value,store_ids=[...document.querySelectorAll('[data-bulk-store]:checked')].map(x=>+x.dataset.bulkStore),weekdays=[...document.querySelectorAll('[data-bulk-day]:checked')].map(x=>+x.dataset.bulkDay);
 if(!supplier_id||!store_ids.length||!weekdays.length)return flash('Выберите поставщика, магазины и хотя бы один день недели','error');
 const name=document.getElementById('bulkSchSupplier').selectedOptions[0].textContent,days=['Пн','Вт','Ср','Чт','Пт','Сб','Вс'];
 if(!confirm(`Заменить график поставщика «${name}» для ${store_ids.length} магазинов? Дни: ${weekdays.map(x=>days[x]).join(', ')}.`))return;
 const admin=state.bulkScheduleAdmin;button.disabled=true;
 try{const r=await api('/api/order-settings/schedule/bulk',{method:'PUT',body:JSON.stringify({supplier_id,store_ids,weekdays})});closeWorkSheet();flash(`График сохранён для ${r.store_count} магазинов`);if(state.page==='orders'){if(admin&&state.orderTab==='admin')await renderOrderAdmin();else if(state.orderTab==='schedule')await renderOrderSchedule();}}
 catch(e){if(e.name!=='AbortError')flash(e.message,'error');}finally{button.disabled=false;}
}
