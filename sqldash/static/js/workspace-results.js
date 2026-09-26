import { RESULT_PAGE, renderTable } from '/static/js/charts.js';
import { csvText } from '/static/js/csv-safe.js';
import { bindResize } from '/static/js/workspace-resize.js';

export class WorkspaceResults {
  constructor(root, csv, meta) {
    Object.assign(this, {root, csv, meta});
    this.sort = null;
    this.widths = new Map();
    this.time = document.createElement('time');
    this.time.className = 'workspace-last-run';
    this.time.hidden = true;
    meta.after(this.time);
    window.addEventListener('sqldash:result', event => {
      this.result = event.detail;
      this.sort = null;
      this.shown = RESULT_PAGE;
      this.csv.textContent = 'Download CSV';
      this.time.hidden = !this.result;
      if (!this.result) { this.observer?.disconnect(); return; }
      const duration=this.result.elapsed_ms>=1000 ? `${(this.result.elapsed_ms/1000).toFixed(2)} s` : `${Math.round(this.result.elapsed_ms)} ms`;
      const status=document.createElement('span');status.className='workspace-run-status';status.textContent=`${this.result.row_count.toLocaleString()} rows${this.result.truncated?' (truncated)':''} · ${duration}`;this.meta.replaceChildren(status);
      const now = new Date();
      this.time.dateTime = now.toISOString();
      this.time.textContent = `Last run ${now.toLocaleTimeString([], {hour:'numeric', minute:'2-digit'})}`;
      this.time.title = now.toLocaleString([], {timeZoneName:'short'});
      this.render();
    });
    csv.addEventListener('click', event => {
      if (!this.sort || !this.result) return;
      event.preventDefault();
      const text = csvText(this.result.columns.map(column => column.name), this.rows());
      const url = URL.createObjectURL(new Blob([text], {type:'text/csv;charset=utf-8'}));
      const link = document.createElement('a');
      link.href = url; link.download = 'sorted-results.csv'; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    });
  }
  rows() {
    if (!this.sort) return this.result.rows;
    const {index, direction} = this.sort;
    const numeric = ['integer', 'float', 'decimal'].includes(this.result.columns[index].type);
    return [...this.result.rows].sort((a,b) => {
      if (a[index] == null) return b[index] == null ? 0 : 1;
      if (b[index] == null) return -1;
      return (numeric ? Number(a[index]) - Number(b[index]) : String(a[index]).localeCompare(String(b[index]))) * direction;
    });
  }
  render() {
    const focused = document.activeElement?.dataset.sortColumn;
    const number = tbody => [...tbody.rows].forEach((row, index, rows) => {
      this.shown = Math.max(this.shown, rows.length);
      if (row.firstElementChild?.classList.contains('row-number')) return;
      const cell = document.createElement('td'); cell.className = 'row-number'; cell.textContent = String(index + 1); row.prepend(cell);
    });
    renderTable(this.root, {format:{}}, {...this.result, rows:this.rows()}, {page:RESULT_PAGE, shown:this.shown, onRows:number});
    const table = this.root.querySelector('table');
    const heads = [...table.querySelectorAll('th')];
    const key = JSON.stringify(this.result.columns);
    const sizes = this.widths.get(key) || this.result.columns.map(() => 1);
    this.widths.set(key, sizes);
    const cols = document.createElement('colgroup');
    const gutter = document.createElement('col'); gutter.style.width='40px'; cols.append(gutter);
    heads.forEach(() => cols.append(document.createElement('col')));
    table.prepend(cols);
    const apply = () => {
      const width = Math.max(this.root.clientWidth, 40 + heads.length * 100) - 40;
      const total = sizes.reduce((a,b) => a+b, 0);
      [...cols.children].slice(1).forEach((col,index) => { col.style.width=`${width*sizes[index]/total}px`; });
    };
    table.style.minWidth = `${40 + heads.length * 100}px`;
    apply();
    this.observer?.disconnect();
    this.observer = new ResizeObserver(apply); this.observer.observe(this.root);
    heads.forEach((original,index) => {
      const column = this.result.columns[index];
      const th = document.createElement('th'); th.className=original.className;
      const active = this.sort?.index === index;
      th.setAttribute('aria-sort', active ? (this.sort.direction===1?'ascending':'descending') : 'none');
      const button = document.createElement('button');button.className='workspace-column-label';button.dataset.sortColumn=String(index);
      button.append(document.createTextNode(column.name));
      const type=document.createElement('small');type.textContent=column.type;button.append(type);
      if(active){const arrow=document.createElement('span');arrow.className='workspace-sort-arrow';arrow.textContent=this.sort.direction===1?'↑':'↓';arrow.setAttribute('aria-hidden','true');button.append(arrow);}
      button.onclick=()=>{
        this.sort=active && this.sort.direction===-1 ? null : {index,direction:active?-1:1};
        this.csv.textContent=this.sort ? 'Download sorted rows' : 'Download CSV';
        this.render();
      };
      th.append(button);original.replaceWith(th);
      if(index<heads.length-1){
        const handle=document.createElement('button');handle.className='workspace-column-resize';handle.setAttribute('role','separator');handle.setAttribute('aria-orientation','vertical');handle.setAttribute('aria-label',`Resize ${column.name}`);th.append(handle);
        const pixels=()=>[...cols.children].slice(1).map(col=>col.getBoundingClientRect().width);
        bindResize(handle,{axis:'x',initial:()=>pixels()[index],min:80,max:()=>pixels()[index]+pixels()[index+1]-80,update:width=>{
          const values=pixels();const combined=values[index]+values[index+1];values[index]=width;values[index+1]=combined-width;sizes.splice(0,sizes.length,...values);apply();
        }});
        handle.ondblclick=()=>{sizes.fill(1);apply();};
      }
    });
    const corner=document.createElement('th');corner.className='row-number';corner.textContent='#';table.tHead.rows[0].prepend(corner);
    if(focused!==undefined)this.root.querySelector(`[data-sort-column="${focused}"]`)?.focus();
  }
}
