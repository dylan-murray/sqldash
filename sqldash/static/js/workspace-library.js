import { baseSource } from '/static/js/source-context.js';
import { dashboardPath } from '/static/js/paths.js';
import { apiToken } from '/static/js/token.js';
import { withoutPeriod } from '/static/js/sentence.js';
export class QueryLibrary {
  constructor({dashboard,open,current,saved,notice}) {
    Object.assign(this,{dashboard,open,current,saved,notice});this.entries=[];this.errors=[];this.activeSource=null;
    this.base=`/api/dashboards/${dashboardPath(dashboard)}/library`;
    this.dialog=document.getElementById('library-dialog');this.input=document.getElementById('library-name');
    document.getElementById('library-refresh').onclick=()=>this.load();
    document.getElementById('library-search').oninput=()=>this.render();
    this.dialog.querySelector('form').addEventListener('submit',event=>this.submit(event));
    this.ready=this.load();
  }
  async request(path='',options={}) {
    const response=await fetch(this.base+path,{...options,headers:{'X-Sqldash-Token':apiToken(),'Content-Type':'application/json',...options.headers}});
    const data=await response.json();
    if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:data.detail?.message || `Request failed (${response.status})`);
    return data;
  }
  async load(){try{const data=await this.request();this.entries=data.queries;this.errors=data.errors;this.render();if(data.errors.length)this.notice(`Could not read ${data.errors.map(error=>`${error.id}.yaml`).join(', ')}; healthy queries are still available.`);}catch(error){this.notice(error.message);}}
  followSource(source){
    if(source===this.activeSource)return;
    this.activeSource=source;this.render();
  }
  render(){
    const root=document.getElementById('library-entries');root.replaceChildren();
    const term=document.getElementById('library-search').value.trim().toLowerCase();
    const entries=this.entries.filter(entry=>`${entry.title} ${entry.sql}`.toLowerCase().includes(term));
    document.getElementById('library-empty').hidden=entries.length>0;
    document.getElementById('library-empty').textContent=this.entries.length?'No matching queries.':'Save a query to reuse it across dashboards.';
    const groups=new Map();
    const folder=source=>{
      if(groups.has(source))return groups.get(source);
      const group=document.createElement('section');group.className='library-source-group';group.dataset.source=source;
      const label=document.createElement('div');label.className='library-source-label';
      const option=[...document.getElementById('workspace-source').options].find(option=>option.title===source);
      label.textContent=option?.textContent || source || 'Dashboard source';
      group.setAttribute('aria-label',label.textContent);group.append(label);
      groups.set(source,group);root.append(group);return group;
    };
    for(const entry of entries){
      const row=document.createElement('div');row.className='library-row';row.dataset.id=entry.id;row.classList.toggle('active',this.current()?.library?.id===entry.id);
      const open=document.createElement('button');open.className='workspace-query';open.textContent=entry.title;
      open.onclick=()=>this.openEntry(entry);
      const rename=document.createElement('button');rename.className='tab-action';rename.textContent='✎';rename.setAttribute('aria-label',`Rename library query ${entry.title}`);rename.onclick=()=>this.ask('rename',entry);
      const remove=document.createElement('button');remove.className='tab-action';remove.textContent='×';remove.setAttribute('aria-label',`Delete library query ${entry.title}`);remove.onclick=()=>this.ask('delete',entry);
      row.append(open,rename,remove);folder(baseSource(entry.source)).append(row);
    }
    for(const error of this.errors){
      if(term && !error.id.toLowerCase().includes(term))continue;
      const row=document.createElement('div');row.className='library-row library-row-bad';row.dataset.id=error.id;
      const label=document.createElement('span');label.className='workspace-query';label.textContent=`${error.id}.yaml`;label.title=`${error.message}: ${error.reason}`;
      const remove=document.createElement('button');remove.className='tab-action';remove.textContent='×';remove.setAttribute('aria-label',`Delete unreadable library file ${error.id}.yaml`);remove.onclick=()=>this.ask('delete',{id:error.id,title:`${error.id}.yaml`,etag:error.etag});
      row.append(label,remove);root.prepend(row);
    }
    const localSource=document.getElementById('workspace').dataset.defaultSource;
    for(const original of document.querySelectorAll('#workspace-browser [data-query]')){
      if(term && !`${original.dataset.query} ${original.dataset.sql}`.toLowerCase().includes(term))continue;
      if(entries.some(entry=>entry.source===localSource && entry.sql.trim()===original.dataset.sql.trim()))continue;
      const row=document.createElement('div');row.className='library-row';const button=document.createElement('button');button.className='workspace-query';button.textContent=original.dataset.query;button.title='Saved in this dashboard';button.onclick=()=>window.dispatchEvent(new CustomEvent('workspace-dashboard-query',{detail:original.dataset.query}));row.append(button);folder(localSource).append(row);
    }
    if(groups.size && this.activeSource!==null && !term && !groups.has(this.activeSource)){
      const empty=document.createElement('p');empty.className='hint';empty.textContent='No saved queries in this connection yet.';folder(this.activeSource).append(empty);
    }
    const sources=new Set(this.entries.map(entry=>baseSource(entry.source)));
    if(document.querySelector('#workspace-browser [data-query]'))sources.add(localSource);
    if(this.activeSource!==null)sources.add(this.activeSource);
    root.querySelectorAll('.library-source-label').forEach(label=>{label.hidden=sources.size<2;});
    if(root.querySelector('.library-row'))document.getElementById('library-empty').hidden=true;
  }
  async openEntry(entry){
    try{const data=await this.request(`/${entry.id}/open`);this.open({name:entry.title,state:data.state,library:{id:entry.id,etag:data.etag,sql:entry.sql,source:entry.source}});}catch(error){this.notice(error.message);}
  }
  saveCurrent(){
    const tab=this.current();if(!tab?.ready)return;
    if(tab.state.mode!=='sql' || !tab.state.sql?.trim())return this.notice('Write a SQL query before saving it to the library.');
    this.ask('save',tab.library ? {id:tab.library.id,etag:tab.library.etag,title:tab.name}: {title:tab.name});
    this.draft=structuredClone(tab.state);
    this.owner=tab.id;
  }
  ask(action,entry){
    this.action=action;this.entry=entry;
    document.getElementById('library-dialog-heading').textContent=action==='delete'?'Delete saved query?':action==='rename'?'Rename saved query':'Save query';
    document.getElementById('library-dialog-message').textContent=action==='delete'?`Delete “${entry.title}” from the project library? Open drafts and dashboard copies stay unchanged.`:'Saved in the project library. Dashboard tiles use their own copies.';
    this.input.hidden=action==='delete';this.input.required=action!=='delete';this.input.value=entry.title==='Untitled query'?'':entry.title;
    document.getElementById('library-name-label').hidden=action==='delete';
    document.getElementById('library-copy').hidden=action!=='save' || !entry.id;
    document.getElementById('library-confirm').textContent=action==='delete'?'Delete query':action==='rename'?'Rename':'Save query';
    document.getElementById('library-error').hidden=true;this.dialog.showModal();if(action!=='delete')this.input.focus();
  }
  async submit(event){
    if(event.submitter?.value==='cancel')return;
    event.preventDefault();if(this.busy)return;this.busy=true;
    const buttons=[...this.dialog.querySelectorAll('button')];buttons.forEach(button=>{button.disabled=true;});
    try{
      let result;
      if(this.action==='delete')await this.request(`/${this.entry.id}`,{method:'DELETE',headers:{'If-Match':this.entry.etag}});
      else if(this.action==='rename')await this.request(`/${this.entry.id}`,{method:'PATCH',headers:{'If-Match':this.entry.etag},body:JSON.stringify({title:this.input.value.trim()})});
      else{
        const update=this.entry.id && event.submitter?.value!=='copy';
        result=await this.request(update?`/${this.entry.id}`:'',{method:update?'PUT':'POST',headers:update?{'If-Match':this.entry.etag}:{},body:JSON.stringify({title:this.input.value.trim(),sql:this.draft.sql,source:this.draft.source || ''})});
        if(this.current()?.id===this.owner)this.saved(result);
      }
      this.dialog.close();await this.load();
    }catch(error){const el=document.getElementById('library-error');el.hidden=false;el.textContent=`${withoutPeriod(error.message)}. Your draft is unchanged. ${this.action==='save'?'Save a copy to keep both versions.':'Refresh the library before retrying.'}`;}
    finally{this.busy=false;buttons.forEach(button=>{button.disabled=false;});}
  }
}
