function productFilterControls(d){
 state.selectedProducts=new Set();
 const f=state.productFilters||={query:'',supplier:'',category:'',active:''};
 const options=(rows,v)=>rows.map(x=>`<option value="${x.id}" ${String(x.id)===v?'selected':''}>${esc(x.name)}</option>`).join('');
 return `<div class="grid" style="margin-top:16px">${field('Поиск товара',`<input id="pfQuery" type="search" value="${esc(f.query)}" placeholder="Название товара" oninput="applyProductFilters(true)">`)}${field('Поставщик',`<select id="pfSupplier" onchange="applyProductFilters(true)"><option value="">Все поставщики</option>${options(d.suppliers,f.supplier)}</select>`)}${field('Категория',`<select id="pfCategory" onchange="applyProductFilters(true)"><option value="">Все категории</option><option value="none" ${f.category==='none'?'selected':''}>Без категории</option>${options(d.categories,f.category)}</select>`)}${field('Активность',`<select id="pfActive" onchange="applyProductFilters(true)"><option value="">Все товары</option><option value="1" ${f.active==='1'?'selected':''}>Активные</option><option value="0" ${f.active==='0'?'selected':''}>Отключённые</option></select>`)}</div><div class="row between"><span id="pfCount" class="small"></span><button class="btn secondary mini" onclick="resetProductFilters()">Сбросить фильтры</button></div>${productBulkToolbar()}`;
}
function applyProductFilters(read=false){
 if(!document.getElementById('pfQuery'))return;
 if(read){state.selectedProducts=new Set();document.querySelectorAll('[data-product-select]').forEach(x=>{x.checked=false;x.closest('[data-filter-product]').classList.remove('is-selected')});updateProductBulkCount();}
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

function productBulkAllowed(){return permission('orders.products_edit','edit')||permission('orders.products_disable','edit');}
function productBulkCheckbox(p){return productBulkAllowed()?`<label class="product-select-zone"><input type="checkbox" data-product-select="${p.id}" aria-label="Выбрать ${esc(p.name)}" onchange="selectBulkProduct(${p.id},this.checked)"></label>`:'';}
function productBulkToolbar(){return productBulkAllowed()?`<div class="card product-bulk-toolbar"><div class="row"><button class="btn secondary mini" onclick="selectVisibleProducts()">Выбрать найденные</button><button class="btn secondary mini" onclick="clearSelectedProducts()">Снять выбор</button></div><div class="row between"><span id="productBulkCount">Выбрано: 0</span><button id="productBulkEdit" class="btn" disabled onclick="openBulkProductEditor()">Изменить выбранные</button></div></div>`:'';}
function selectBulkProduct(id,checked){state.selectedProducts??=new Set();if(checked)state.selectedProducts.add(id);else state.selectedProducts.delete(id);document.querySelector(`[data-product-select="${id}"]`)?.closest('[data-filter-product]')?.classList.toggle('is-selected',checked);updateProductBulkCount();}
function updateProductBulkCount(){const count=state.selectedProducts?.size||0,el=document.getElementById('productBulkCount'),button=document.getElementById('productBulkEdit');if(el)el.textContent='Выбрано: '+count;if(button)button.disabled=!count;}
function selectVisibleProducts(){document.querySelectorAll('[data-filter-product]').forEach(row=>{if(row.style.display==='none')return;const input=row.querySelector('[data-product-select]');if(input){input.checked=true;selectBulkProduct(+input.dataset.productSelect,true);}});}
function clearSelectedProducts(){state.selectedProducts=new Set();document.querySelectorAll('[data-product-select]').forEach(x=>{x.checked=false;x.closest('[data-filter-product]').classList.remove('is-selected')});updateProductBulkCount();}
function openBulkProductEditor(){
 const products=state.orderAdminData.products.filter(x=>state.selectedProducts?.has(x.id));if(!products.length)return;if(products.length>500)return flash('За один раз можно изменить до 500 товаров','error');state.bulkProductRows=products;
 const edit=permission('orders.products_edit','edit'),disable=permission('orders.products_disable','edit'),imported=products.some(x=>x.imported),d=state.orderAdminData;
 showSheet(`<h3>Изменить товары: ${products.length}</h3><p class="small">Заполните только поля, которые нужно изменить. Остальные значения сохранятся. При смене фильтра выбор товаров сбрасывается.</p>${edit?`${field('Категория',`<select id="bpCategory"><option value="">Не изменять</option><option value="none">Убрать категорию</option>${d.categories.filter(x=>x.active).map(x=>`<option value="${x.id}">${esc(x.name)}</option>`).join('')}</select>`)}${field('Поставщик',`<select id="bpSupplier" ${imported?'disabled':''}><option value="">Не изменять</option>${d.suppliers.filter(x=>x.active).map(x=>`<option value="${x.id}">${esc(x.name)}</option>`).join('')}</select>`)}${imported?'<p class="small">В выборе есть товары Saby. Их поставщик меняется для всего прайса в «Прайсы Saby».</p>':''}${field('Единица измерения',`<input id="bpUnit" maxlength="64" placeholder="Не изменять">`)}${field('Шаг количества',`<input id="bpStep" type="number" min="0.000001" step="any" placeholder="Не изменять">`)}<p class="small">При смене единицы укажите шаг. Один шаг будет применён ко всем выбранным товарам; проверьте их единицы. Лимиты отдельных магазинов сохранятся. Ручная единица у товаров Saby сохранится после импорта.</p>`:''}${disable?field('Активность',`<select id="bpActive"><option value="">Не изменять</option><option value="1">Включить</option><option value="0">Отключить</option></select>`):''}<p class="small">Импортированный товар станет доступен только при наличии в прайсе и подтверждённом шаге. Категория должна подходить поставщику каждого товара.</p><button class="btn" onclick="saveBulkProducts(this)">Применить изменения</button>`);
}
async function saveBulkProducts(button){
 const rows=state.bulkProductRows||[],changes={},labels=[],value=id=>document.getElementById(id)?.value||'';
 const category=value('bpCategory'),supplier=value('bpSupplier'),unit=value('bpUnit').trim(),step=value('bpStep').replace(',','.'),active=value('bpActive');
 if(category){changes.category_id=category==='none'?null:+category;labels.push('Категория: '+document.getElementById('bpCategory').selectedOptions[0].textContent);}
 if(supplier){changes.supplier_id=+supplier;labels.push('Поставщик: '+document.getElementById('bpSupplier').selectedOptions[0].textContent);}
 if(unit){changes.unit=unit;labels.push('Единица: '+unit);}
 if(step){changes.step_qty=step;labels.push('Шаг: '+step+' ('+[...new Set(rows.map(x=>unit||x.unit))].join(', ')+')');}
 if(active){changes.active=active==='1';labels.push('Активность: '+(changes.active?'включить':'отключить'));}
 if(!Object.keys(changes).length)return flash('Выберите хотя бы одно изменение','error');
 if(unit&&rows.some(x=>x.unit!==unit)&&!step)return flash('При смене единицы укажите новый шаг','error');
 if(!confirm(`Изменить ${rows.length} товаров?\n${labels.join('\n')}\nИзменения применятся ко всем выбранным товарам.`))return;
 button.disabled=true;const expected_units=Object.fromEntries(rows.map(x=>[x.id,x.unit]));
 try{const r=await api('/api/order-settings/products/bulk',{method:'PATCH',body:JSON.stringify({ids:rows.map(x=>x.id),changes,expected_units})});closeWorkSheet();clearSelectedProducts();flash(`Изменено товаров: ${r.count}`);if(state.page==='orders')await renderOrderAdmin();}
 catch(e){if(e.name!=='AbortError')flash(e.message,'error');}finally{button.disabled=false;}
}
