const tg = window.Telegram?.WebApp;
if (tg) { tg.ready(); tg.expand(); }

const state = { token:null, me:null, stores:[], suppliers:[], products:[], screen:'home' };
const content = document.getElementById('content');
const nav = document.getElementById('bottomNav');
document.getElementById('refreshBtn').onclick = () => render(state.screen, true);

async function api(path, opts={}) {
  const headers = {'Content-Type':'application/json', ...(opts.headers||{})};
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const r = await fetch(path, {...opts, headers});
  const data = await r.json().catch(()=>({}));
  if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
  return data;
}
function esc(s=''){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function fmt(n){return n==null?'—':new Intl.NumberFormat('ru-RU',{maximumFractionDigits:1}).format(n)}
function toast(msg){ tg?.showPopup ? tg.showPopup({title:'Причал Core',message:String(msg),buttons:[{type:'ok'}]}) : alert(msg); }
function loading(){content.innerHTML='<section class="screen center"><div class="loader"></div><p>Загрузка…</p></section>'}
function roleName(r){return ({seller:'Продавец',mentor:'Наставник',manager:'Управляющий',operations_director:'Операционный директор',executive:'Руководитель',admin:'Администратор',pending:'Ожидает доступа'})[r]||r}

async function bootstrap(){
  try{
    if (!tg?.initData) {
      content.innerHTML='<section class="screen"><div class="notice yellow">Откройте MiniApp из Telegram-бота. В обычном браузере Telegram-авторизация недоступна.</div></section>';
      return;
    }
    const auth=await api('/api/auth/telegram',{method:'POST',body:JSON.stringify({init_data:tg.initData})});
    if(auth.status==='pending'){
      content.innerHTML=`<section class="screen"><div class="hero"><h1>Доступ запрошен</h1><p>${esc(auth.user.full_name)}</p></div><div class="notice yellow">Администратор должен назначить вам роль и магазин. После подтверждения заново откройте MiniApp.</div></section>`;
      return;
    }
    state.token=auth.token; state.me=auth.user;
    [state.stores,state.suppliers]=await Promise.all([api('/api/stores'),api('/api/suppliers')]);
    buildNav(); render('home');
  }catch(e){content.innerHTML=`<section class="screen"><div class="notice red">${esc(e.message)}</div></section>`}
}

function buildNav(){
  const management=['manager','operations_director','admin','executive'].includes(state.me.role);
  const items = management
    ? [['home','⌂','Сегодня'],['orders','▤','Заявки'],['stores','⌖','Точки'],['tasks','✓','Задачи'],['more','•••','Ещё']]
    : [['home','⌂','Сегодня'],['orders','▤','Заявки'],['shift','⇄','Пересменка'],['tasks','✓','Задачи'],['more','•••','Ещё']];
  nav.innerHTML=items.map(([id,ico,label])=>`<button class="nav-btn" data-screen="${id}"><span class="ico">${ico}</span>${label}</button>`).join('');
  nav.classList.remove('hidden'); nav.onclick=e=>{const b=e.target.closest('[data-screen]');if(b)render(b.dataset.screen)};
}
function markNav(id){document.querySelectorAll('.nav-btn').forEach(x=>x.classList.toggle('active',x.dataset.screen===id))}
async function render(id, force=false){state.screen=id;markNav(id);loading();try{if(id==='home')await home();else if(id==='orders')await orders();else if(id==='shift')shiftForm();else if(id==='stores')await storesScreen();else if(id==='tasks')await tasks();else if(id==='more')more();else if(id==='inspection')inspectionForm();else if(id==='cash')cashForm();else if(id==='incident')incidentForm();else if(id==='admin')await adminScreen();}catch(e){content.innerHTML=`<div class="notice red">${esc(e.message)}</div>`}}

async function home(){
 const d=await api('/api/dashboard');
 content.innerHTML=`<section class="screen"><div class="hero"><h1>${esc(state.me.full_name||'Причал Core')}</h1><p>${roleName(state.me.role)} · ${d.stores} точек</p></div>
 <div class="grid">
 <div class="card metric ${d.overdue_tasks?'red':''}"><div class="value">${d.overdue_tasks}</div><div class="label">Просрочено задач</div></div>
 <div class="card metric ${d.pending_orders?'yellow':''}"><div class="value">${d.pending_orders}</div><div class="label">Заявки в работе</div></div>
 <div class="card metric"><div class="value">${d.inspections_week}</div><div class="label">Проверок за неделю</div></div>
 <div class="card metric"><div class="value">${d.cash_collections_week}</div><div class="label">Инкассаций за неделю</div></div>
 </div>
 <div class="section-title">Продажи недели</div><div class="card metric"><div class="value">${d.plan_percent==null?'AI/Saby позже':fmt(d.plan_percent)+'%'}</div><div class="label">План/факт · факт ${fmt(d.revenue_week)} · план ${fmt(d.plan_week)}</div></div>
 <div class="section-title">Пересменки сегодня</div><div class="row"><div class="row-main"><div class="row-title">Утро / вечер</div><div class="row-sub">Принятые отчёты по доступным точкам</div></div><span class="badge">${d.shift_reports_today.morning} / ${d.shift_reports_today.evening}</span></div>
 </section>`;
}

async function orders(){
 const rows=await api('/api/orders');
 content.innerHTML=`<section class="screen"><div class="actions"><button class="btn" id="newOrder">+ Новая заявка</button></div><div class="section-title">Последние заявки</div><div class="list">${rows.length?rows.map(o=>`<div class="row"><div class="row-main"><div class="row-title">Заявка #${o.id}</div><div class="row-sub">${storeName(o.store_id)} · ${supplierName(o.supplier_id)} · ${new Date(o.created_at).toLocaleString('ru')}</div></div><span class="badge">${o.status}</span></div>`).join(''):'<div class="empty">Заявок пока нет</div>'}</div></section>`;
 document.getElementById('newOrder').onclick=newOrderForm;
}
function storeName(id){return state.stores.find(x=>x.id===id)?.name||`Точка ${id}`}
function supplierName(id){return state.suppliers.find(x=>x.id===id)?.name||`Поставщик ${id}`}
async function newOrderForm(){
 const storeOpts=state.stores.filter(x=>x.is_active).map(x=>`<option value="${x.id}">${esc(x.name)}</option>`).join('');
 const suppOpts=state.suppliers.map(x=>`<option value="${x.id}">${esc(x.name)}</option>`).join('');
 content.innerHTML=`<section class="screen"><div class="form"><div class="field"><label>Магазин</label><select id="oStore">${storeOpts}</select></div><div class="field"><label>Поставщик</label><select id="oSupplier"><option value="">Выберите…</option>${suppOpts}</select></div><div id="productBox"></div><div class="field"><label>Комментарий</label><textarea id="oComment"></textarea></div><button class="btn" id="saveOrder">Отправить заявку</button><button class="btn secondary" onclick="render('orders')">Отмена</button></div></section>`;
 document.getElementById('oSupplier').onchange=loadOrderProducts; document.getElementById('saveOrder').onclick=saveOrder;
}
async function loadOrderProducts(){
 const supplierId=+document.getElementById('oSupplier').value; const storeId=+document.getElementById('oStore').value; if(!supplierId)return;
 const ps=await api(`/api/products?supplier_id=${supplierId}&store_id=${storeId}`); state.products=ps;
 document.getElementById('productBox').innerHTML=`<div class="section-title">Товары</div>${ps.length?ps.map(p=>`<div class="row"><div class="row-main"><div class="row-title">${esc(p.name)}</div><div class="row-sub">${esc(p.category||'')} · ${esc(p.unit)}</div></div><input data-product="${p.id}" type="number" min="0" step="0.001" placeholder="0" style="width:84px;background:#0a1714;border:1px solid var(--line);color:white;padding:9px;border-radius:11px"></div>`).join(''):'<div class="empty">Нет доступных товаров</div>'}`;
}
async function saveOrder(){
 const items=[...document.querySelectorAll('[data-product]')].map(x=>({product_id:+x.dataset.product,quantity:+x.value})).filter(x=>x.quantity>0);
 if(!items.length)return toast('Укажите количество хотя бы одного товара');
 const body={store_id:+document.getElementById('oStore').value,supplier_id:+document.getElementById('oSupplier').value,items,comment:document.getElementById('oComment').value||null};
 await api('/api/orders',{method:'POST',body:JSON.stringify(body)});toast('Заявка создана');render('orders');
}

function shiftForm(){
 const opts=state.stores.map(x=>`<option value="${x.id}">${esc(x.name)}</option>`).join('');
 content.innerHTML=`<section class="screen"><div class="hero"><h1>Пересменка</h1><p>Утро 07:30–08:10 · вечер 19:30–20:10</p></div><div class="form"><div class="field"><label>Магазин</label><select id="sStore">${opts}</select></div><div class="field"><label>Тип</label><select id="sKind"><option value="morning">Утренняя</option><option value="evening">Вечерняя</option></select></div><div class="field"><label>Касса</label><input id="sCash" type="number" step="0.01"></div><div class="field"><label>Расхождение</label><input id="sDiff" type="number" step="0.01" value="0"></div><div class="field"><label>Проблемы / комментарий</label><textarea id="sIssues"></textarea></div><button class="btn" id="saveShift">Сдать пересменку</button></div></section>`;
 document.getElementById('saveShift').onclick=async()=>{const r=await api('/api/shift-reports',{method:'POST',body:JSON.stringify({store_id:+sStore.value,kind:sKind.value,cash_amount:sCash.value?+sCash.value:null,cash_difference:sDiff.value?+sDiff.value:0,issues:sIssues.value||null,payload:{},finalized:true})});toast('Пересменка сохранена'); const yes=confirm('Добавить фото к пересменке?'); if(yes){await api('/api/photos/request',{method:'POST',body:JSON.stringify({entity_type:'shift_report',entity_id:r.id,field_key:'report'})});toast('Теперь отправьте фото в чат с ботом');}};
}

async function storesScreen(){
 const ins=await api('/api/inspections');
 content.innerHTML=`<section class="screen"><div class="actions"><button class="btn" onclick="render('inspection')">+ Проверка точки</button><button class="btn secondary" onclick="render('cash')">Инкассация</button></div><div class="section-title">Вверенные точки</div><div class="list">${state.stores.map(s=>{const c=ins.filter(x=>x.store_id===s.id).length;return `<div class="row"><div class="row-main"><div class="row-title">${esc(s.name)}</div><div class="row-sub">Проверок в истории: ${c}</div></div><span class="badge ${c>=3?'green':'yellow'}">${c}</span></div>`}).join('')}</div></section>`;
}
function inspectionForm(){
 const opts=state.stores.map(x=>`<option value="${x.id}">${esc(x.name)}</option>`).join('');
 content.innerHTML=`<section class="screen"><div class="hero"><h1>Проверка магазина</h1><p>После проверки нарушения превращаем в задачи.</p></div><div class="form"><div class="field"><label>Магазин</label><select id="iStore">${opts}</select></div>${['Чистота','Выкладка','Ценники','Сроки','Касса','Оборудование','Внешний вид продавца','Знание ассортимента','Допродажа'].map((x,i)=>`<div class="field"><label>${x}</label><select data-check="${esc(x)}"><option value="ok">✅ Норма</option><option value="warning">⚠️ Замечание</option><option value="bad">🔴 Нарушение</option></select></div>`).join('')}<div class="field"><label>Итог / комментарий</label><textarea id="iSummary"></textarea></div><button class="btn" id="saveInspection">Сохранить проверку</button></div></section>`;
 document.getElementById('saveInspection').onclick=async()=>{const checklist={};document.querySelectorAll('[data-check]').forEach(x=>checklist[x.dataset.check]=x.value);const vals=Object.values(checklist);const score=Math.round((vals.filter(x=>x==='ok').length/vals.length)*100);const r=await api('/api/inspections',{method:'POST',body:JSON.stringify({store_id:+iStore.value,checklist,score,summary:iSummary.value||null})});toast(`Проверка сохранена: ${score}%`);if(confirm('Добавить фото проверки?')){await api('/api/photos/request',{method:'POST',body:JSON.stringify({entity_type:'inspection',entity_id:r.id,field_key:'inspection'})});toast('Отправьте фото в чат с ботом')}};
}
function cashForm(){const opts=state.stores.map(x=>`<option value="${x.id}">${esc(x.name)}</option>`).join('');content.innerHTML=`<section class="screen"><div class="form"><div class="field"><label>Магазин</label><select id="cStore">${opts}</select></div><div class="field"><label>Сумма</label><input id="cAmount" type="number" step="0.01"></div><div class="field"><label>Комментарий</label><textarea id="cNote"></textarea></div><button class="btn" id="saveCash">Зафиксировать инкассацию</button></div></section>`;saveCash.onclick=async()=>{await api('/api/cash-collections',{method:'POST',body:JSON.stringify({store_id:+cStore.value,amount:+cAmount.value,note:cNote.value||null})});toast('Инкассация сохранена');render('stores')}}

async function tasks(){const rows=await api('/api/tasks?mine=true');content.innerHTML=`<section class="screen"><div class="section-title">Мои задачи</div><div class="list">${rows.length?rows.map(t=>`<div class="row"><div class="row-main"><div class="row-title">${esc(t.title)}</div><div class="row-sub">${t.store_id?storeName(t.store_id)+' · ':''}${t.due_at?new Date(t.due_at).toLocaleString('ru'):''}</div></div><span class="badge ${t.status==='done'?'green':t.priority==='high'?'red':''}">${t.status}</span></div>`).join(''):'<div class="empty">Нет задач</div>'}</div></section>`}
function incidentForm(){const opts=state.stores.map(x=>`<option value="${x.id}">${esc(x.name)}</option>`).join('');content.innerHTML=`<section class="screen"><div class="form"><div class="field"><label>Магазин</label><select id="xStore">${opts}</select></div><div class="field"><label>Категория</label><select id="xCat"><option>Касса</option><option>Оборудование</option><option>Персонал</option><option>Клиент</option><option>Товар</option><option>Авария</option><option>Другое</option></select></div><div class="field"><label>Приоритет</label><select id="xSev"><option value="yellow">Жёлтый — сегодня</option><option value="red">Красный — немедленно</option><option value="green">Зелёный — планово</option></select></div><div class="field"><label>Что произошло?</label><textarea id="xDesc"></textarea></div><button class="btn" id="saveIncident">Сообщить</button></div></section>`;saveIncident.onclick=async()=>{await api('/api/incidents',{method:'POST',body:JSON.stringify({store_id:+xStore.value,category:xCat.value,severity:xSev.value,description:xDesc.value})});toast('Проблема зарегистрирована');render('more')}}

function more(){const admin=['admin','operations_director'].includes(state.me.role);const management=['manager','operations_director','admin'].includes(state.me.role);content.innerHTML=`<section class="screen"><div class="hero"><h1>Core</h1><p>${roleName(state.me.role)}</p></div><div class="list">${management?'<div class="row" onclick="render(\'inspection\')"><div class="row-title">🏪 Проверка точки</div><span>›</span></div><div class="row" onclick="render(\'cash\')"><div class="row-title">💰 Инкассация</div><span>›</span></div>':''}<div class="row" onclick="render('incident')"><div class="row-title">⚠️ Сообщить о проблеме</div><span>›</span></div>${admin?'<div class="row" onclick="render(\'admin\')"><div class="row-title">⚙️ Администрирование</div><span>›</span></div>':''}<div class="row" id="aiRow"><div class="row-title">✦ AI-анализ точки</div><span>›</span></div></div></section>`;document.getElementById('aiRow').onclick=async()=>{if(!state.stores.length)return;const r=await api('/api/ai/store/'+state.stores[0].id,{method:'POST'});toast(r.configured?JSON.stringify(r.result):r.message)}}

async function adminScreen(){const users=await api('/api/admin/users');content.innerHTML=`<section class="screen"><div class="tabs"><button class="chip active">Пользователи</button><button class="chip" id="addStore">+ Точка</button><button class="chip" id="addSupplier">+ Поставщик</button><button class="chip" id="addProduct">+ Товар</button></div><div class="section-title">Пользователи</div><div class="list">${users.map(u=>`<div class="row"><div class="row-main"><div class="row-title">${esc(u.full_name||u.telegram_id)}</div><div class="row-sub">${roleName(u.role)} · ${u.status}</div></div><button class="btn small secondary" data-user="${u.id}">Изменить</button></div>`).join('')}</div></section>`;document.querySelectorAll('[data-user]').forEach(b=>b.onclick=()=>editUser(users.find(x=>x.id==b.dataset.user)));addStore.onclick=storeAdminForm;addSupplier.onclick=supplierAdminForm;addProduct.onclick=productAdminForm}
function editUser(u){const storeChecks=state.stores.map(s=>`<label class="row"><span>${esc(s.name)}</span><input type="checkbox" data-us="${s.id}" ${u.store_ids.includes(s.id)?'checked':''}></label>`).join('');content.innerHTML=`<section class="screen"><div class="form"><div class="field"><label>Роль</label><select id="uRole">${['seller','mentor','manager','operations_director','executive','admin'].map(r=>`<option value="${r}" ${u.role===r?'selected':''}>${roleName(r)}</option>`).join('')}</select></div><div class="field"><label>Статус</label><select id="uStatus"><option value="active">Активен</option><option value="pending" ${u.status==='pending'?'selected':''}>Ожидает</option><option value="blocked" ${u.status==='blocked'?'selected':''}>Заблокирован</option></select></div><div class="section-title">Точки</div>${storeChecks}<button class="btn" id="saveUser">Сохранить</button></div></section>`;saveUser.onclick=async()=>{const ids=[...document.querySelectorAll('[data-us]:checked')].map(x=>+x.dataset.us);await api('/api/admin/users/'+u.id,{method:'PATCH',body:JSON.stringify({role:uRole.value,status:uStatus.value,store_ids:ids})});toast('Сохранено');render('admin')}}
function storeAdminForm(){content.innerHTML=`<section class="screen"><div class="form"><div class="field"><label>Название точки</label><input id="aName"></div><div class="field"><label>Код (необязательно)</label><input id="aCode"></div><button class="btn" id="aSave">Добавить</button></div></section>`;aSave.onclick=async()=>{await api('/api/admin/stores',{method:'POST',body:JSON.stringify({name:aName.value,code:aCode.value||null,is_active:true})});state.stores=await api('/api/stores');toast('Точка добавлена');render('admin')}}
function supplierAdminForm(){content.innerHTML=`<section class="screen"><div class="form"><div class="field"><label>Поставщик</label><input id="aName"></div><div class="field"><label>Дедлайн</label><input id="aDeadline" type="time" value="10:00"></div><button class="btn" id="aSave">Добавить</button></div></section>`;aSave.onclick=async()=>{await api('/api/admin/suppliers',{method:'POST',body:JSON.stringify({name:aName.value,default_deadline:aDeadline.value,is_active:true})});state.suppliers=await api('/api/suppliers');toast('Поставщик добавлен');render('admin')}}
function productAdminForm(){const supp=state.suppliers.map(x=>`<option value="${x.id}">${esc(x.name)}</option>`).join('');content.innerHTML=`<section class="screen"><div class="form"><div class="field"><label>Поставщик</label><select id="pSup">${supp}</select></div><div class="field"><label>Название</label><input id="pName"></div><div class="field"><label>Категория</label><input id="pCat"></div><div class="field"><label>Единица</label><input id="pUnit" value="шт"></div><button class="btn" id="pSave">Добавить</button></div></section>`;pSave.onclick=async()=>{await api('/api/admin/products',{method:'POST',body:JSON.stringify({supplier_id:+pSup.value,name:pName.value,category:pCat.value||null,unit:pUnit.value,is_active:true,store_ids:[]})});toast('Товар добавлен');render('admin')}}

bootstrap();
