import { WorkspaceResults } from '/static/js/workspace-results.js';
import { hasSourceContext } from '/static/js/source-context.js';
import { bindResize } from '/static/js/workspace-resize.js';
import { workspaceSnapshot, restoreWorkspace, disposeWorkspace, workspaceChart, selectWorkspaceRole, selectWorkspaceWarehouse } from '/static/js/editor.js';
const editor = ace.edit('sql-editor');
const pane = document.querySelector('.editor-pane');
const handle = document.createElement('div');
handle.className = 'editor-resize'; handle.tabIndex = 0;
handle.setAttribute('role','separator'); handle.setAttribute('aria-label','Resize SQL editor'); handle.setAttribute('aria-orientation','horizontal');
pane.after(handle);
bindResize(handle, { axis:'y', initial:() => pane.getBoundingClientRect().height, min:140, max:() => innerHeight - 180, update:height => { pane.style.height = `${height}px`; editor.resize(); } });
new ResizeObserver(() => editor.resize()).observe(pane);
window.addEventListener('message', event => {
  if (event.origin !== location.origin || event.source !== parent) return;
  if (event.data?.type === 'workspace-open') {
    const picker = document.getElementById('query-picker');
    if ([...picker.options].some(option => option.value === event.data.query)) {
      picker.value = event.data.query; picker.dispatchEvent(new Event('change', { bubbles:true }));
    }
  }
  if (event.data?.type === 'workspace-theme') {
    document.documentElement.dataset.theme = event.data.theme === 'dark' ? 'dark' : 'light';
    window.dispatchEvent(new CustomEvent('sqldash:themechange'));
  }
});

window.addEventListener('sqldash:schema', event => parent.postMessage({ type:'workspace-schema', ...event.detail }, location.origin));
window.addEventListener('message', event => {
  if (event.origin !== location.origin || event.source !== parent) return;
  if (event.data?.type === 'workspace-insert' && typeof event.data.sql === 'string') { editor.insert(event.data.sql); editor.focus(); }
  if (event.data?.type === 'workspace-schema-refresh') window.dispatchEvent(new CustomEvent('sqldash:schema-refresh'));
});

let restoring = true;
let pending = false;
const publish = () => {
  if (restoring || pending) return;
  pending = true;
  queueMicrotask(() => {
    pending = false;
    parent.postMessage({ type:'workspace-state', state:workspaceSnapshot(), running:document.getElementById('run-btn').disabled }, location.origin);
  });
};
editor.session.on('change', publish);
document.addEventListener('input', publish);
document.addEventListener('change', publish);
document.addEventListener('click', () => setTimeout(publish,0));
new MutationObserver(publish).observe(document.getElementById('run-btn'), { attributes:true, attributeFilter:['disabled'] });
window.addEventListener('message', async event => {
  if (event.origin !== location.origin || event.source !== parent) return;
  if (event.data?.type === 'workspace-restore') {
    restoring = true;
    const layout=document.querySelector('.workspace-editor');layout.inert=true;layout.dataset.ready='false';
    try { await restoreWorkspace(event.data.state || {}); }
    finally { restoring = false;layout.inert=false;layout.dataset.ready='true';publish(); }
  }
  if (event.data?.type === 'workspace-dispose') {
    try { await disposeWorkspace(); parent.postMessage({ type:'workspace-disposed' }, location.origin); }
    catch { parent.postMessage({ type:'workspace-dispose-error' }, location.origin); }
  }
});
parent.postMessage({ type:'workspace-ready' }, location.origin);

const resultsPane=document.querySelector('.results-pane');
const split=document.querySelector('.results-split');
const meta=document.getElementById('results-meta');
const resultbar=document.createElement('div');resultbar.className='workspace-resultbar';
const title=document.createElement('strong');title.className='workspace-results-title';title.innerHTML='<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="2" y="2.5" width="12" height="11" rx="1.5"/><path d="M2 6h12M6 6v7.5"/></svg><span>Results</span>';
const toggle=document.createElement('button');toggle.id='toggle-builder';toggle.className='btn';toggle.textContent='Hide chart builder';toggle.setAttribute('aria-expanded','true');
toggle.onclick=()=>{const hidden=split.classList.toggle('builder-hidden');toggle.textContent=hidden?'Show chart builder':'Hide chart builder';toggle.setAttribute('aria-expanded',String(!hidden));};
const openAdd=document.createElement('button');openAdd.type='button';openAdd.className='btn btn-primary';openAdd.textContent='Add to dashboard';
const csv=document.getElementById('csv-btn');
const resultActions=document.createElement('div');resultActions.className='workspace-result-actions';resultActions.setAttribute('role','group');resultActions.setAttribute('aria-label','Result actions');resultActions.append(csv,toggle);resultbar.append(title,meta,resultActions);resultsPane.prepend(resultbar);
const chart=document.querySelector('.chart-side');
const chartBuilder=document.createElement('aside');chartBuilder.className='workspace-chart-builder';chartBuilder.id='workspace-chart-builder';chart.before(chartBuilder);chartBuilder.append(chart);
const chartHeading=document.createElement('div');chartHeading.className='workspace-chart-heading';
const chartTitle=document.createElement('h3');chartTitle.textContent='Chart builder';
const chartClose=document.createElement('button');chartClose.className='btn btn-ghost';chartClose.setAttribute('aria-label','Collapse chart builder');chartClose.textContent='−';chartClose.onclick=()=>toggle.click();
chartHeading.append(chartTitle,chartClose);chartBuilder.prepend(chartHeading);
const settings=document.createElement('section');settings.className='workspace-chart-settings';settings.setAttribute('aria-label','Chart settings');
settings.append(chart.querySelector('.field'),document.getElementById('qb-encoding'));chart.prepend(settings);
const footer=document.createElement('div');footer.className='workspace-chart-footer';
const footerLabel=document.createElement('label');footerLabel.htmlFor='qb-title';footerLabel.textContent='Add to dashboard';
const footerRow=document.createElement('div');footerRow.className='add-row';footerRow.append(document.getElementById('qb-title'),openAdd);footer.append(footerLabel,footerRow);chartBuilder.append(footer);
new WorkspaceResults(document.getElementById('results-body'),csv,meta);
const save=document.createElement('button');save.className='btn';save.id='workspace-save';save.innerHTML='<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 2h8l3 3v9H2V2zM5 2v4h5V2M5 14V9h6v5"/></svg><span>Save query</span>';save.onclick=()=>parent.postMessage({type:'workspace-save'},location.origin);
document.querySelector('.editor-toolbar').append(save);
const sourcePicker=document.getElementById('source-picker');
const sources=()=>parent.postMessage({type:'workspace-sources',sources:[...sourcePicker.options].filter(option=>!hasSourceContext(option.value)).map(option=>({value:option.value,label:option.textContent}))},location.origin);
window.addEventListener('message',event=>{
  if(event.origin!==location.origin || event.source!==parent)return;
  if(event.data?.type==='workspace-mode' && ['sql','text','metric'].includes(event.data.mode)) { modeButtons.querySelector(`[data-mode=${event.data.mode}]`).click(); }
  if(event.data?.type==='workspace-source' && [...sourcePicker.options].some(option=>option.value===event.data.source)){
    sourcePicker.value=event.data.source;sourcePicker.dispatchEvent(new Event('change',{bubbles:true}));
  }
  if(event.data?.type==='workspace-restore')sources();
});
new MutationObserver(sources).observe(sourcePicker,{childList:true});
sources();
const modeButtons=document.getElementById('mode-toggle');
const updateMode=()=>{save.disabled=workspaceSnapshot().mode!=='sql';};
modeButtons.addEventListener('click',updateMode);
window.addEventListener('sqldash:result',updateMode);
const saveField=document.getElementById('qb-add').closest('.field');
const addDialog=document.createElement('dialog');addDialog.className='workspace-add-dialog';addDialog.setAttribute('aria-label','Add to dashboard');
const heading=document.createElement('div');heading.className='workspace-add-heading';
const headingText=document.createElement('strong');headingText.textContent='Add to dashboard';
const close=document.createElement('button');close.className='btn btn-ghost';close.textContent='×';close.setAttribute('aria-label','Close add to dashboard');close.onclick=()=>addDialog.close();heading.append(headingText,close);
addDialog.append(heading,saveField);document.body.append(addDialog);
let previousChart;
openAdd.onclick=()=>{
  const another=document.getElementById('qb-another');
  if(!another.hidden&&!another.disabled)another.click();
  previousChart=workspaceChart();
  saveField.querySelector('.add-row').prepend(document.getElementById('qb-title'));
  addDialog.showModal();
};
addDialog.addEventListener('close',()=>{footerRow.prepend(document.getElementById('qb-title'));if(previousChart)workspaceChart(previousChart);publish();});
const add=document.getElementById('qb-add');
const updateAdd=()=>{
  const another=document.getElementById('qb-another');openAdd.disabled=add.disabled && (another.hidden||another.disabled);
  openAdd.textContent=another.hidden?'Add to dashboard':'Add another tile';
};
new MutationObserver(updateAdd).observe(add,{attributes:true,childList:true,subtree:true});
updateAdd();

const run=document.getElementById('run-btn');run.innerHTML='<svg class="run-icon" viewBox="0 0 12 12" aria-hidden="true"><path d="M3 2l7 4-7 4z"/></svg><span>Run</span><kbd>⌘↵</kbd>';
const context=document.createElement('span');context.className='workspace-query-context';run.after(context);
const readout={connection:'',role:'',database:''};
const paintReadout=()=>{
  const [name,...rest]=readout.connection.split(' · ');
  const parts=[];
  const strong=(text)=>{const b=document.createElement('b');b.textContent=text;return b;};
  if(name)parts.push(['source ',strong(name)],...rest.map(part=>[part]));
  if(readout.role)parts.push(['role ',strong(readout.role)]);
  if(readout.database)parts.push(['database ',strong(readout.database)]);
  context.replaceChildren(...parts.flatMap((part,index)=>index?[' · ',...part]:part));
  context.title=context.textContent;
};
window.addEventListener('sqldash:roles',event=>{
  const detail=event.detail || {};
  if(detail.loading)return;
  readout.connection=detail.source_label || readout.connection;
  readout.role=(detail.current || []).join(', ');
  paintReadout();
});
window.addEventListener('sqldash:databases',event=>{readout.database=event.detail?.database || '';paintReadout();});
const status=document.createElement('div');status.className='workspace-editor-status';
const cursor=document.createElement('span');
const hints=document.createElement('span');hints.textContent='Spaces: 4    UTF-8    SQL · Ctrl+Space';
status.append(cursor,hints);pane.append(status);
editor.session.setTabSize(4);
const updateCursor=()=>{const position=editor.getCursorPosition();cursor.textContent=`Ln ${position.row+1}, Col ${position.column+1}`;};
editor.selection.on('changeCursor',updateCursor);updateCursor();
editor.setOptions({highlightActiveLine:true, displayIndentGuides:true, wrap:false, scrollPastEnd:.25});
editor.renderer.setPadding(18);
editor.renderer.setScrollMargin(17,17,0,0);

window.addEventListener('sqldash:databases',event=>parent.postMessage({type:'workspace-databases',...event.detail},location.origin));
window.addEventListener('message',event=>{
  if(event.origin!==location.origin || event.source!==parent)return;
  if(event.data?.type==='workspace-database' && typeof event.data.database==='string')window.dispatchEvent(new CustomEvent('sqldash:browse-database',{detail:{database:event.data.database}}));
});

window.addEventListener('sqldash:roles', event=>parent.postMessage({type:'workspace-roles',...event.detail},location.origin));
window.addEventListener('message', event=>{
  if(event.origin!==location.origin || event.source!==parent)return;
  if(event.data?.type==='workspace-role')selectWorkspaceRole(event.data.role);
  if(event.data?.type==='workspace-warehouse' && typeof event.data.warehouse==='string')selectWorkspaceWarehouse(event.data.warehouse);
});
