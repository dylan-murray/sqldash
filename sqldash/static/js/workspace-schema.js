import { withoutPeriod } from '/static/js/sentence.js';
export class SchemaBrowser {
  constructor(root, insert) {
    this.root = root; this.insert = insert; this.tables = []; this.limit = 100; this.expanded = new Set(); this.groups = new Map(); this.filtered = false;
    this.tree = root.querySelector('#schema-tree'); this.message = root.querySelector('#schema-message');
    this.search = root.querySelector('input'); this.more = root.querySelector('#schema-more');
    this.message.setAttribute('role','status');this.message.setAttribute('aria-live','polite');
    this.search.addEventListener('input', () => { this.limit = 100; this.render(); });
    this.more.onclick = () => { this.limit += 100; this.render(); };
  }
  update(data) {
    if(data.source!==undefined && (data.source!==this.source || data.database!==this.database)){this.expanded.clear();this.groups.clear();this.tree.replaceChildren();this.limit=100;this.source=data.source;this.database=data.database;}
    this.tables = data.status === 'ready' ? data.tables || [] : [];
    this.status = data.status;
    const loading=data.status==='loading';
    this.root.setAttribute('aria-busy',String(loading));
    this.message.dataset.loading=String(loading);
    this.message.textContent = data.status === 'error' ? `${withoutPeriod(data.message || 'Schema unavailable')}. Use Refresh to retry; you can still write SQL.` : 'Loading database objects…';
    this.render();
  }
  summary(label,kind) {
    const summary=document.createElement('summary');
    const caret=document.createElement('span');caret.className='workspace-caret';caret.setAttribute('aria-hidden','true');
    const icon=document.createElementNS('http://www.w3.org/2000/svg','svg');icon.classList.add('workspace-line-icon');icon.setAttribute('viewBox','0 0 16 16');icon.setAttribute('aria-hidden','true');
    const shapes={table:'M2.5 3h11v10h-11zM2.5 6h11M6 6v7',schema:'M3 2.5h10v11H3zM6 2.5v11',database:'M3 4.2c0-1.1 2.2-2 5-2s5 .9 5 2-2.2 2-5 2-5-.9-5-2zM3 4.2v7.6c0 1.1 2.2 2 5 2s5-.9 5-2V4.2M3 8c0 1.1 2.2 2 5 2s5-.9 5-2'};
    const path=document.createElementNS('http://www.w3.org/2000/svg','path');path.setAttribute('d',shapes[kind]);icon.append(path);
    const name=document.createElement('span');name.textContent=label;
    summary.append(caret,icon,name);return summary;
  }
  action(label, sql, title) {
    const button = document.createElement('button'); button.type = 'button'; button.className = 'schema-insert';
    button.textContent = label; button.title = title; button.setAttribute('aria-label', title);
    button.onclick = () => this.insert(sql);
    return button;
  }
  render() {
    if(!this.filtered)for(const group of this.tree.querySelectorAll('.schema-group'))this.groups.set(group.dataset.schema,group.open);
    this.tree.replaceChildren(); this.more.hidden = true; this.tree.classList.toggle('has-root', Boolean(this.database));
    if (this.status !== 'ready') { this.message.hidden = false; return; }
    const term = this.search.value.trim().toLowerCase();
    this.filtered=Boolean(term);
    const matching = this.tables.filter(table => [table.schema, table.name, ...table.columns.map(column => column.name)].join(' ').toLowerCase().includes(term));
    this.message.hidden = matching.length > 0;
    this.message.textContent = this.tables.length ? 'No matching tables or columns.' : 'No database objects available.';
    const groups = new Map();
    let parent = this.tree;
    if (this.database && matching.length) {
      const root = document.createElement('details'); root.className = 'schema-db'; root.open = Boolean(term) || this.rootOpen !== false;
      const summary = this.summary(this.database, 'database');
      const schemas = new Set(this.tables.map(table => table.schema || 'Objects')).size;
      const count = document.createElement('span'); count.className = 'schema-count'; count.textContent = `${schemas} schema${schemas === 1 ? '' : 's'}`; summary.append(count);
      parent = document.createElement('div'); parent.className = 'schema-db-children';
      root.append(summary, parent); this.tree.append(root);
      root.addEventListener('toggle', () => { if (root.isConnected && !this.filtered) this.rootOpen = root.open; });
    }
    for (const table of matching.slice(0,this.limit)) {
      const name = table.schema || 'Objects';
      if (!groups.has(name)) {
        const group = document.createElement('details'); group.open = Boolean(term) || (this.groups.get(name) ?? false);group.className='schema-group';group.dataset.schema=name;
        const summary = this.summary(name,'schema');
        group.append(summary); parent.append(group); groups.set(name,group);
      }
      const details = document.createElement('details');details.className='schema-table';
      const summary = this.summary(table.name,'table');
      const insert=this.action('+',table.sql,`Insert ${table.name} into SQL`);insert.classList.add('schema-table-insert');insert.onclick=event=>{event.preventDefault();event.stopPropagation();this.insert(table.sql);};summary.append(insert);
      const columns=document.createElement('div');columns.className='schema-columns';
      details.append(summary,columns);
      let populated = false;
      details.addEventListener('toggle', () => {
        if(!details.isConnected)return;
        if(details.open)this.expanded.add(table.sql);else this.expanded.delete(table.sql);
        if (!details.open || populated) return; populated = true;
        for (const column of table.columns) {
          const row = document.createElement('div'); row.className = 'schema-column';
          row.append(this.action(column.name, column.sql, `Insert column ${column.name} into SQL`));
          const type = document.createElement('small'); type.textContent = column.type; row.append(type); columns.append(row);
        }
      });
      details.open = this.expanded.has(table.sql) || Boolean(term && !table.name.toLowerCase().includes(term));
      groups.get(name).append(details);
    }
    this.more.hidden = matching.length <= this.limit;
  }
}
