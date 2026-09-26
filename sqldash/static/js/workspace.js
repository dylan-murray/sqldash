import { WorkspaceRoles } from '/static/js/workspace-roles.js';
import { baseSource, hasSourceContext, sourceDatabase } from '/static/js/source-context.js';
import { enhanceSelects } from '/static/js/dropdown.js';
import { QueryLibrary } from '/static/js/workspace-library.js';
import { SchemaBrowser } from '/static/js/workspace-schema.js';
import { bindResize } from '/static/js/workspace-resize.js';
import { MetricBrowser } from '/static/js/workspace-metrics.js';
import { DraftStorage } from '/static/js/workspace-drafts.js';
import { dashboardPath } from '/static/js/paths.js';
const root = document.getElementById('workspace');
const frames = document.getElementById('workspace-frames');
const tablist = document.getElementById('query-tabs');
const tabs = new Map();
let active = null;
let library;
let updatingSource=false;
let updatingDatabase=false;
const databasePicker=document.getElementById('workspace-database');
const defaultDatabases=new Map();
const RECENT_KEY='sqldash-recent-databases';
const RECENT_WAREHOUSES_KEY='sqldash-recent-warehouses';
function recentPicks(base,key=RECENT_KEY){
  try{const all=JSON.parse(localStorage.getItem(key) || '{}');return Array.isArray(all[base])?all[base]:[];}
  catch{return [];}
}
function rememberPick(base,name,key=RECENT_KEY){
  try{
    const all=JSON.parse(localStorage.getItem(key) || '{}');
    all[base]=[name,...(Array.isArray(all[base])?all[base]:[]).filter(entry=>entry!==name)].slice(0,3);
    localStorage.setItem(key,JSON.stringify(all));
  }catch{/* recents are a convenience; the picker works without them */}
}
function databaseOption(name,context,fallback){
  const option=new Option(name,name);
  const comment=context.comments?.[name];
  if(comment)option.dataset.meta=comment;
  if(name===fallback)option.dataset.tag='default';
  return option;
}
function updateDatabases(tab){
  const context=tab?.databases;
  const controls=document.getElementById('workspace-database-controls');
  controls.hidden=!context || (!context.current && !context.databases.length && !context.warning);
  if(controls.hidden)return;
  const base=sourceKey(context.source);
  const override=sourceDatabase(context.source);
  if(!override && context.current)defaultDatabases.set(base,context.current);
  const fallback=defaultDatabases.get(base);
  const names=[...new Set([context.current,...context.databases].filter(Boolean))].sort((a,b)=>a.localeCompare(b));
  const groups=[];
  if(!context.current){
    const none=document.createElement('optgroup');none.label='Session';
    none.append(new Option('No database',''));groups.push(none);
  }
  const recent=recentPicks(base).filter(name=>names.includes(name));
  if(recent.length){
    const group=document.createElement('optgroup');group.label='Recent';
    group.append(...recent.map(name=>databaseOption(name,context,fallback)));groups.push(group);
  }
  const all=document.createElement('optgroup');all.label='All databases';all.dataset.count=String(names.length);
  all.append(...names.map(name=>databaseOption(name,context,fallback)));groups.push(all);
  databasePicker.replaceChildren(...groups);
  databasePicker.value=context.current?context.database:'';
  databasePicker.dataset.rowMeta=context.current?(override?'database':'default db'):'';
  enhanceSelects(controls);
  updatingDatabase=true;databasePicker.dispatchEvent(new Event('change'));updatingDatabase=false;
  databasePicker.title='Unqualified SQL runs in this database. Name another database in full to query it or join across databases.';
  databaseStatus(tab);
}
function databaseWarning(tab){
  const roles=tab?.roles?.source===tab?.databases?.source?tab.roles:null;
  if(tab?.databases?.current)return '';
  return roles?.database_warning || tab?.databases?.warning || '';
}
function databaseStatus(tab){
  const status=document.querySelector('#workspace-database-controls > p[role=status]');
  const roles=tab?.roles?.source===tab?.databases?.source?tab.roles:null;
  const warning=databaseWarning(tab);
  status.textContent=warning || (tab?.databases?.current && roles?.database_note) || '';
  status.dataset.tone=warning?'warning':'';
  status.hidden=!status.textContent;
}
databasePicker.onchange=()=>{
  if(updatingDatabase)return;
  const tab=tabs.get(active);
  if(tab?.databases)rememberPick(sourceKey(tab.databases.source),databasePicker.value);
  send(tab,{type:'workspace-database',database:databasePicker.value});
};
const warehousePicker=document.getElementById('workspace-warehouse');
let updatingWarehouse=false;
function shownSchema(tab){
  const data=tab?.schema || {status:'loading'};
  if(data.status!=='error')return data;
  if(/\b000606\b/.test(data.message || '') && tab.roles?.warning)return {...data,message:tab.roles.warning};
  const missing=databaseWarning(tab);
  return missing?{...data,message:missing}:data;
}
function warehouseOption(warehouse,context){
  const option=new Option(warehouse.label,warehouse.value);
  if(warehouse.detail)option.dataset.meta=warehouse.detail;
  if(warehouse.value===context.configured_warehouse)option.dataset.tag='default';
  return option;
}
function updateWarehouses(tab){
  const context=tab?.roles;
  const controls=document.getElementById('workspace-warehouse-controls');
  const status=controls.querySelector(':scope > p[role=status]');
  if(context?.loading || context?.pending){warehousePicker.disabled=true;const button=controls.querySelector('.dd-btn');if(button)button.disabled=true;return;}
  controls.hidden=!Array.isArray(context?.warehouses);
  if(controls.hidden)return;
  const base=sourceKey(context.source);
  const names=context.warehouses.map(warehouse=>warehouse.value);
  const groups=[];
  if(!context.warehouse){
    const none=document.createElement('optgroup');none.label='Session';
    none.append(new Option('No warehouse',''));groups.push(none);
  }
  const recent=recentPicks(base,RECENT_WAREHOUSES_KEY).filter(name=>names.includes(name));
  if(recent.length){
    const group=document.createElement('optgroup');group.label='Recent';
    group.append(...recent.map(name=>warehouseOption(context.warehouses.find(w=>w.value===name),context)));groups.push(group);
  }
  const all=document.createElement('optgroup');all.label='All warehouses';all.dataset.count=String(names.length);
  all.append(...context.warehouses.map(warehouse=>warehouseOption(warehouse,context)));groups.push(all);
  warehousePicker.replaceChildren(...groups);
  warehousePicker.value=context.warehouse || '';
  warehousePicker.dataset.rowMeta=context.warehouse?(context.selected_warehouse?'warehouse':'default wh'):'';
  warehousePicker.disabled=Boolean(tab.running) || !names.length;
  enhanceSelects(controls);
  const button=controls.querySelector('.dd-btn');
  button.disabled=warehousePicker.disabled;
  button.title='Queries, the schema and new tabs here run on this warehouse.';
  updatingWarehouse=true;warehousePicker.dispatchEvent(new Event('change'));updatingWarehouse=false;
  status.textContent=context.warning || context.warehouse_note || '';
  status.dataset.tone=context.warning?'warning':'';
  status.hidden=!status.textContent;
}
warehousePicker.onchange=()=>{
  if(updatingWarehouse || !warehousePicker.value)return;
  const tab=tabs.get(active);
  if(!tab?.roles || warehousePicker.value===tab.roles.warehouse)return;
  rememberPick(sourceKey(tab.roles.source),warehousePicker.value,RECENT_WAREHOUSES_KEY);
  send(tab,{type:'workspace-warehouse',warehouse:warehousePicker.value});
};
const roles = new WorkspaceRoles(document.getElementById('workspace-role-controls'), role => {
  const tab = tabs.get(active);
  tab.roles={...tab.roles,pending:true};roles.render(tab.roles,tab.running);
  send(tab, {type:'workspace-role', role});
});
const sourcePicker = document.getElementById("workspace-source");
sourcePicker.onchange = () => !updatingSource && send(tabs.get(active), {type:"workspace-source", source:sourcePicker.value});
function sourceKey(value) {
  value=baseSource(value);
  if(!value || value===root.dataset.defaultAlias)return root.dataset.defaultSource;
  return JSON.parse(root.dataset.localSources).includes(value) ? `${root.dataset.dashboard}.sources.${value}` : value;
}
function executionSourceKey(value) {
  return hasSourceContext(value) ? value : sourceKey(value);
}
function updateSource(tab) {
  if (!tab?.sources) return;
  const connections=document.createElement('optgroup');connections.label='Connections';connections.dataset.count=String(tab.sources.length);
  connections.append(...tab.sources.map(source => {
    const option=Object.assign(new Option(source.label, source.value), {title:sourceKey(source.value)});
    const [name,...engine]=source.label.split(' · ');
    option.dataset.short=name;
    if(engine.length)option.dataset.detail=engine.join(' · ');
    if(!source.value)option.dataset.tag='default';
    return option;
  }));
  sourcePicker.replaceChildren(connections);
  const base=baseSource(tab.state.source);
  sourcePicker.value=[...sourcePicker.options].find(option=>sourceKey(option.value)===sourceKey(base))?.value || '';
  sourcePicker.dataset.rowMeta=sourcePicker.selectedOptions[0]?.dataset.detail || '';
  roles.render(tab.roles, tab.running);
  enhanceSelects(document.querySelector(".workspace-source"));
  updatingSource=true;sourcePicker.dispatchEvent(new Event("change"));updatingSource=false;
  library?.followSource(sourceKey(tab.state.source));
  document.getElementById("workspace-context").textContent=sourcePicker.selectedOptions[0]?.textContent || "";
  document.querySelectorAll(".library-row").forEach(row=>row.classList.toggle("active",Boolean(tab.library && row.dataset.id===tab.library.id)));
}
let dialogTab = null;
const notice = message => { const el = document.getElementById('workspace-notice'); el.hidden = false; el.textContent = message; };
const storage = new DraftStorage(root.dataset.identity, notice);
const persisted = storage.load();
const send = (tab, data) => tab?.frame.contentWindow?.postMessage(data, location.origin);
const browser = new SchemaBrowser(document.getElementById('schema-browser'), sql => send(tabs.get(active), { type:'workspace-insert', sql }));
function persist() {
  storage.save({ active, tabs:[...tabs.values()].map(({ id,name,state,library }) => ({ id,name,state,library })) });
}
function select(id) {
  active = id;
  root.dataset.mode = tabs.get(id)?.state.mode || "sql";
  for (const tab of tabs.values()) {
    tab.frame.hidden = tab.id !== id;
    tab.button.setAttribute('aria-selected', String(tab.id === id));
    tab.button.tabIndex = tab.id === id ? 0 : -1;
  }
  browser.update(shownSchema(tabs.get(id)));
  document.getElementById('library-save').disabled = !tabs.get(id)?.ready;
  document.getElementById('draft-label').textContent = tabs.get(id)?.library ? 'Working copy of a library query' : 'Local draft';
  updateSource(tabs.get(id));
  updateDatabases(tabs.get(id));
  updateWarehouses(tabs.get(id));
  persist();
}
function create({ id=crypto.randomUUID(), name='Untitled query', state={}, library=null, inherited=null } = {}) {
  if (tabs.size >= 20) return notice('Twenty queries are open. Close one before opening another.');
  if (tabs.has(id)) return select(id);
  const frame = document.createElement('iframe');
  frame.title = `Query: ${name}`; frame.hidden = true;
  frame.src = `/d/${dashboardPath(root.dataset.dashboard)}/query?embedded=1`;
  const item = document.createElement('div'); item.className = 'query-tab';
  const button = document.createElement('button'); button.type='button'; button.setAttribute('role','tab'); button.textContent=name;
  button.onclick = () => active===id ? renameTab(id) : select(id);
  const rename = document.createElement('button'); rename.className='tab-action'; rename.textContent='✎'; rename.setAttribute('aria-label',`Rename ${name}`); rename.onclick = () => ask(id,'rename');
  const close = document.createElement('button'); close.className='tab-action'; close.textContent='×'; close.setAttribute('aria-label',`Close ${name}`); close.onclick = () => ask(id,'close');
  button.title='Click the active tab name or press F2 to rename';
  button.onkeydown=event=>{if(event.key==='F2'){event.preventDefault();ask(id,'rename');}};
  const kind=document.createElement('select');kind.className='query-kind';kind.setAttribute('aria-label',`Query type for ${name}`);
  for(const value of ['sql','text','metric'])kind.add(new Option(value.toUpperCase(),value));
  kind.value=state.mode || 'sql';kind.onchange=()=>send(tabs.get(id),{type:'workspace-mode',mode:kind.value});
  item.append(kind,button,close);enhanceSelects(item); tablist.append(item);
  tabs.set(id,{ id,name,state,library,inherited,frame,item,button,rename,close,kind,ready:false }); frames.append(frame);
  select(id);
}
function renameTab(id) {
  const tab=tabs.get(id);
  if(!tab)return;
  if(tab.nameInput){tab.nameInput.focus();return;}
  select(id);
  const input=document.createElement('input');
  input.type='text';input.className='query-tab-name';input.value=tab.name;
  input.maxLength=160;input.setAttribute('aria-label','Query name');
  input.title='Enter to save · Escape to cancel';
  const minimumWidth=Math.max(100,tab.button.getBoundingClientRect().width);
  const measure=document.createElement('canvas').getContext('2d');
  tab.nameInput=input;
  tab.button.before(input);tab.button.hidden=true;
  const resize=()=>{
    measure.font=getComputedStyle(input).font;
    input.style.width=`${Math.min(320,Math.max(minimumWidth,measure.measureText(input.value).width+18))}px`;
  };
  input.addEventListener('input',resize);resize();
  let finished=false;
  const finish=(save,focus=false)=>{
    if(finished)return;
    finished=true;
    const name=input.value.trim();
    if(save && name){
      tab.name=name;tab.button.textContent=name;tab.frame.title=`Query: ${name}`;
      tab.rename.setAttribute('aria-label',`Rename ${name}`);
      tab.close.setAttribute('aria-label',`Close ${name}`);
      tab.kind.setAttribute('aria-label',`Query type for ${name}`);
      persist();
    }
    tab.nameInput=null;input.remove();tab.button.hidden=false;
    if(focus)tab.button.focus({preventScroll:true});
  };
  input.addEventListener('blur',()=>finish(true));
  input.addEventListener('keydown',event=>{
    if(event.isComposing)return;
    if(event.key==='Enter' || event.key==='Escape'){
      event.preventDefault();event.stopPropagation();finish(event.key==='Enter',true);
    }
  });
  input.focus({preventScroll:true});input.select();
}
function ask(id,action) {
  const tab=tabs.get(id); if (!tab) return;
  if(action==='rename')return renameTab(id);
  dialogTab=id;
  document.getElementById('query-dialog-heading').textContent='Discard local draft?';
  document.getElementById('query-dialog-message').textContent=`“${tab.name}” will be removed from browser recovery. Saved queries and dashboard tiles stay saved.${tab.running?' Its running query will be cancelled.':''}`;
  document.getElementById('query-dialog-confirm').textContent='Discard draft';
  document.getElementById('query-dialog').showModal();
}
document.getElementById('query-dialog').addEventListener('close',event=>{
  if(event.target.returnValue!=='confirm') return;
  const tab=tabs.get(dialogTab); if(!tab) return;
  tab.closing=true;
  if(tab.ready)send(tab,{type:'workspace-dispose'});else remove(tab.id);
});
function remove(id) {
  const tab=tabs.get(id);tab.frame.remove();tab.item.remove();tabs.delete(id);
  if(!tabs.size)create();else if(active===id)select(tabs.keys().next().value);
  persist();
}
// semgrep: the handler checks event.origin first
// nosemgrep: insufficient-postmessage-origin-validation
window.addEventListener('message',event=>{
  if(event.origin!==location.origin) return;
  const tab=[...tabs.values()].find(item=>item.frame.contentWindow===event.source);if(!tab)return;
  const data=event.data;
  if(data?.type==='workspace-ready') { tab.ready=true;if(tab.id===active)document.getElementById('library-save').disabled=false;send(tab,{type:'workspace-restore',state:tab.state}); }
  if(data?.type==='workspace-save') { if(tab.id===active)library.saveCurrent(); }
  if(data?.type==='workspace-sources') { tab.sources=data.sources;if(tab.id===active)updateSource(tab); }
  if(data?.type==='workspace-state') { const previousSource=tab.state.source;tab.state=data.state;if(tab.id===active)root.dataset.mode=data.state.mode;tab.kind.value=data.state.mode;tab.item.querySelector(".dd-label").textContent=data.state.mode.toUpperCase();tab.running=data.running;if(tab.id===active){roles.render(tab.roles,tab.running);updateWarehouses(tab);}if(tab.id===active && previousSource!==data.state.source)updateSource(tab);tab.button.classList.toggle('running',data.running);if(tab.id===active)document.getElementById('draft-label').textContent=tab.library?(tab.state.sql?.trim()===tab.library.sql?.trim() && executionSourceKey(tab.state.source)===tab.library.source?'Saved in project library':'Local changes to library query'):'Local draft';persist(); }
  if(data?.type==='workspace-roles') { tab.roles=data;if(tab.id===active){roles.render(data,tab.running);updateWarehouses(tab);if(tab.databases)databaseStatus(tab);if(tab.schema?.status==='error')browser.update(shownSchema(tab));} }
  if(data?.type==='workspace-databases') { tab.databases=data;if(tab.id===active){updateDatabases(tab);if(tab.schema?.status==='error')browser.update(shownSchema(tab));} }
  if(data?.type==='workspace-schema') { tab.schema=data;if(data.status==='loading' && tab.databases && sourceKey(tab.databases.source)!==sourceKey(data.source)){tab.databases=null;if(tab.id===active)updateDatabases(tab);}if(tab.id===active)browser.update(shownSchema(tab)); }
  if(data?.type==='workspace-disposed' && tab.closing)remove(tab.id);
  if(data?.type==='workspace-dispose-error') { tab.closing=false;notice('Could not cancel the query. The draft is still open.'); }
});
function createBlank(){
  const source=tabs.get(active)?.state.source;
  create(source?{state:{source},inherited:source}:{});
}
document.getElementById('new-query').onclick=createBlank;
new MetricBrowser(document.getElementById('metric-browser'), root.dataset.dashboard, metric => {
  create({name:metric.title || metric.name, state:{mode:'metric', title:metric.title || metric.name, metric:{name:metric.name}}});
  if(matchMedia('(max-width:760px)').matches)collapse(true);
});
window.addEventListener('workspace-dashboard-query',event=>create({name:event.detail,state:{query:event.detail,title:event.detail}}));
document.getElementById('schema-refresh').onclick=()=>send(tabs.get(active),{type:'workspace-schema-refresh'});
const open=document.getElementById('sidebar-open');
function collapse(value){root.classList.toggle('collapsed',value);root.classList.toggle('mobile-open',!value);open.hidden=!value;}
document.getElementById('browse-tables').onclick=()=>collapse(root.classList.contains('mobile-open'));open.onclick=()=>collapse(false);
document.getElementById('explorer-new').onclick=createBlank;
document.getElementById('library-search-toggle').onclick=()=>{const input=document.getElementById('library-search');input.hidden=!input.hidden;if(!input.hidden)input.focus();};
const narrow = matchMedia('(max-width:760px)');
if(narrow.matches)collapse(true);
narrow.addEventListener('change', event => { if(event.matches)collapse(true); });
bindResize(document.getElementById('sidebar-resize'),{axis:'x',initial:()=>root.querySelector('aside').getBoundingClientRect().width,min:180,max:()=>Math.min(480,innerWidth-320),update:width=>root.style.setProperty('--sidebar-width',`${width}px`)});
window.addEventListener('sqldash:themechange',()=>{for(const tab of tabs.values())send(tab,{type:'workspace-theme',theme:document.documentElement.dataset.theme});});
tablist.addEventListener('keydown',event=>{
  if(event.target.getAttribute('role')!=='tab' || !['ArrowLeft','ArrowRight'].includes(event.key))return;
  const ids=[...tabs.keys()];const index=ids.indexOf(active);const next=ids[(index+(event.key==='ArrowRight'?1:ids.length-1))%ids.length];
  select(next);tabs.get(next).button.focus();event.preventDefault();
});
document.getElementById('export-drafts').onclick=()=>{
  const value=JSON.stringify({version:1,active,tabs:[...tabs.values()].map(({id,name,state,library})=>({id,name,state,library})),unreadableOriginal:storage.blocked?storage.last:null},null,2);
  const url=URL.createObjectURL(new Blob([value],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='sqldash-drafts.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
};
if(persisted?.tabs.length){for(const tab of persisted.tabs)create(tab);if(tabs.has(persisted.active))select(persisted.active);}else create();

library = new QueryLibrary({
  dashboard:root.dataset.dashboard, notice,
  open:options=>{
    const current=tabs.get(active);
    const empty=Boolean(current && !current.running && !current.library && (current.state.mode || 'sql')==='sql' && !current.state.sql?.trim() && !current.state.query && (!current.state.source || current.state.source===current.inherited) && !current.state.markdown && !current.state.metric?.name);
    create(options);
    if(empty && current.id!==active)remove(current.id);
  },
  current:() => tabs.get(active),
  saved:(entry) => {
    const tab=tabs.get(active); if(!tab)return;
    tab.library={id:entry.id,etag:entry.etag,sql:entry.sql,source:entry.source};tab.name=entry.title;tab.button.textContent=entry.title;
    tab.rename.setAttribute('aria-label',`Rename ${entry.title}`);tab.close.setAttribute('aria-label',`Close ${entry.title}`);
    document.getElementById('draft-label').textContent='Saved in project library';persist();
  },
});
document.getElementById('library-save').onclick=()=>library.saveCurrent();

updateSource(tabs.get(active));

const requestedQuery=new URL(location.href).searchParams.get('query');
if(requestedQuery)library.ready.then(()=>{
  const existing=[...tabs.values()].find(tab=>tab.library?.id===requestedQuery);
  if(existing)return select(existing.id);
  const entry=library.entries.find(entry=>entry.id===requestedQuery);
  if(entry)library.openEntry(entry);else notice('This saved query is no longer available. Your drafts are unchanged.');
});
