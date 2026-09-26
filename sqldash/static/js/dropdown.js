let openMenu = null;
const menuMotion = new WeakMap();
const reducedMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

export function menuShift({ wrap, menuW, menuH, vw, vh, pad = 8, gap = 6 }) {
  const left = wrap.left + wrap.width / 2 - menuW / 2;
  const top = wrap.bottom + gap;
  const up = top + menuH > vh - pad;
  let dx = 0;
  const overflowLeft = pad - left;
  const overflowRight = left + menuW - (vw - pad);
  if (overflowLeft > 0) dx += overflowLeft;
  if (overflowRight > 0) dx -= overflowRight;
  return { up, dx };
}

export function menuBox({ wrap, menuW, menuH, vw, vh, pad = 8, gap = 6 }) {
  const { up, dx } = menuShift({ wrap, menuW, menuH, vw, vh, pad, gap });
  return {
    up,
    left: wrap.left + wrap.width / 2,
    translate: dx ? `calc(-50% + ${Math.round(dx)}px) 0` : "-50% 0",
    top: up ? "auto" : wrap.bottom + gap,
    bottom: up ? vh - wrap.top + gap : "auto",
  };
}

function placeDdMenu(wrap, menu) {
  if (!menu || menu.hidden) return;
  const wr = wrap.getBoundingClientRect();
  menu.style.position = "fixed";
  menu.style.zIndex = "90";
  if (!menu.classList.contains("dash-info")) {
    menu.style.minWidth = `${Math.round(wr.width)}px`;
  }
  const box = menuBox({
    wrap: wr,
    menuW: menu.offsetWidth || wr.width || 160,
    menuH: menu.offsetHeight || 1,
    vw: window.innerWidth,
    vh: window.innerHeight,
  });
  menu.style.left = `${Math.round(box.left)}px`;
  menu.style.translate = box.translate;
  if (box.up) {
    menu.style.top = "auto";
    menu.style.bottom = `${Math.round(box.bottom)}px`;
    menu.classList.add("dd-up");
  } else {
    menu.style.top = `${Math.round(box.top)}px`;
    menu.style.bottom = "auto";
    menu.classList.remove("dd-up");
  }
}

function portal(wrap, menu) {
  menu.classList.add("dd-open");
  document.body.appendChild(menu);
  placeDdMenu(wrap, menu);
}

function unportal(wrap, menu) {
  menu.classList.remove("dd-open", "dd-up");
  menu.style.position = "";
  menu.style.zIndex = "";
  menu.style.top = "";
  menu.style.bottom = "";
  menu.style.left = "";
  menu.style.translate = "";
  menu.style.minWidth = "";
  if (wrap && menu.parentNode !== wrap) wrap.appendChild(menu);
}

function closeOpen() {
  if (!openMenu) return;
  const { wrap, menu, btn } = openMenu;
  openMenu = null;
  btn.setAttribute("aria-expanded", "false");
  if (menu.contains(document.activeElement)) btn.focus({ preventScroll: true });
  const finish = () => {
    menu.hidden = true;
    menu.inert = false;
    unportal(wrap, menu);
  };
  const style = getComputedStyle(menu);
  const from = { opacity: style.opacity, transform: style.transform };
  menuMotion.get(menu)?.cancel();
  menuMotion.delete(menu);
  if (!menu.classList.contains("dd-search-menu") || reducedMotion()) {
    finish();
    return;
  }
  menu.inert = true;
  const y = menu.classList.contains("dd-up") ? 2 : -2;
  const animation = menu.animate([from, { opacity: 0, transform: `translateY(${y}px)` }],
    { duration: 90, easing: "cubic-bezier(0.4, 0, 1, 1)", fill: "forwards" });
  menuMotion.set(menu, animation);
  animation.finished.then(() => {
    if (menuMotion.get(menu) !== animation) return;
    finish();
    menuMotion.delete(menu);
    animation.cancel();
  }).catch(() => {});
}

function reveal(wrap, btn, menu) {
  menuMotion.get(menu)?.cancel();
  menuMotion.delete(menu);
  menu.inert = false;
  menu.hidden = false;
  btn.setAttribute("aria-expanded", "true");
  openMenu = { wrap, btn, menu };
  portal(wrap, menu);
  requestAnimationFrame(() => {
    if (openMenu?.menu === menu) placeDdMenu(wrap, menu);
  });
  if (menu.classList.contains("dd-search-menu") && !reducedMotion()) {
    const y = menu.classList.contains("dd-up") ? 4 : -4;
    const animation = menu.animate([
      { opacity: 0, transform: `translateY(${y}px)` },
      { opacity: 1, transform: "translateY(0)" },
    ], { duration: 150, easing: "cubic-bezier(0.16, 1, 0.3, 1)" });
    menuMotion.set(menu, animation);
  }
  if (menu.getAttribute("role") === "dialog") menu.focus({ preventScroll: true });
  menu.querySelector(".dd-search")?.focus({ preventScroll: true });
}

function toggle(wrap, btn, menu, beforeOpen) {
  if (openMenu?.menu === menu) {
    closeOpen();
    return;
  }
  closeOpen();
  beforeOpen?.();
  reveal(wrap, btn, menu);
}

function wireExisting(wrap) {
  if (wrap.dataset.ddWired) return;
  const btn = wrap.querySelector(":scope > .dd-btn");
  const menu = wrap.querySelector(":scope > .dd-menu");
  if (!btn || !menu) return;
  wrap.dataset.ddWired = "1";
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    toggle(wrap, btn, menu);
  });
}

function enhance(select) {
  select.dataset.dd = "1";
  select.tabIndex = -1;
  select.setAttribute("aria-hidden", "true");

  const wrap = document.createElement("div");
  wrap.className = "dd";
  wrap.dataset.ddWired = "1";
  if (select.className) wrap.dataset.for = select.className;
  if (select.dataset.skin) wrap.dataset.skin = select.dataset.skin;
  if (select.dataset.icon) wrap.dataset.icon = select.dataset.icon;
  if (select.dataset.crumb) wrap.dataset.crumb = select.dataset.crumb;
  select.parentNode.insertBefore(wrap, select);
  wrap.appendChild(select);

  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "dd-btn";
  if (select.dataset.btn) {
    for (const cls of select.dataset.btn.split(/\s+/).filter(Boolean)) btn.classList.add(cls);
  }
  btn.setAttribute("aria-haspopup", "listbox");
  btn.setAttribute("aria-expanded", "false");
  if (select.dataset.icon) {
    const ico = document.createElement("span");
    ico.className = "dd-ico";
    ico.setAttribute("aria-hidden", "true");
    btn.appendChild(ico);
  }
  const labelSpan = document.createElement("span");
  labelSpan.className = "dd-label";
  btn.appendChild(labelSpan);
  const chev = document.createElement("span");
  chev.className = "dd-chev";
  btn.appendChild(chev);
  wrap.appendChild(btn);

  const menu = document.createElement("div");
  menu.className = "dd-menu";
  menu.setAttribute("role", "listbox");
  menu.hidden = true;
  wrap.appendChild(menu);

  function paintLabel() {
    btn.querySelector(".dd-row-meta")?.remove();
    const opt = select.options[select.selectedIndex];
    const crumb = select.dataset.crumb;
    labelSpan.replaceChildren();
    if (crumb && opt?.value) {
      const head = document.createElement("span");
      head.className = "dd-crumb-head";
      head.textContent = crumb;
      const slash = document.createElement("span");
      slash.className = "dd-crumb-slash";
      slash.textContent = "/";
      const name = document.createElement("span");
      name.className = "dd-mono";
      name.textContent = opt.textContent;
      labelSpan.append(head, slash, name);
      return;
    }
    labelSpan.textContent = opt ? opt.dataset.short || opt.textContent : "";
    if ("labelTitle" in select.dataset) labelSpan.title = labelSpan.textContent;
    if (select.dataset.rowMeta) {
      const meta = document.createElement("span");
      meta.className = "dd-row-meta";
      meta.textContent = select.dataset.rowMeta;
      labelSpan.after(meta);
    }
  }

  function sync() {
    paintLabel();
    menu.querySelectorAll(".dd-item").forEach((item) => {
      item.classList.toggle("selected", item.dataset.value === select.value);
    });
  }

  function rebuild() {
    menu.innerHTML = "";
    if (select.dataset.menuTitle) {
      const title = document.createElement("div");
      title.className = "dd-menu-title";
      title.textContent = select.dataset.menuTitle;
      menu.appendChild(title);
    }
    let optionsRoot=menu;
    const sections=select.dataset.menu==='sections';
    menu.classList.toggle('dd-sections',sections);
    if(select.dataset.icon)menu.dataset.icon=select.dataset.icon;
    if(select.dataset.search){
      menu.classList.add('dd-search-menu');menu.setAttribute('role','dialog');
      menu.setAttribute('aria-label',select.getAttribute('aria-label') || 'Choose an option');
      btn.setAttribute('aria-haspopup','dialog');
      const search=document.createElement('input');search.type='search';search.className='dd-search';
      search.placeholder=select.dataset.search;search.setAttribute('aria-label',select.dataset.search.replace(/…$/, ''));
      optionsRoot=document.createElement('div');optionsRoot.setAttribute('role','listbox');
      const empty=document.createElement('div');empty.className='dd-search-empty';empty.textContent=select.dataset.searchEmpty || 'No matching options';empty.hidden=true;empty.setAttribute('role','status');
      if(sections){
        const field=document.createElement('div');field.className='dd-search-field';
        const esc=document.createElement('kbd');esc.textContent='esc';esc.setAttribute('aria-hidden','true');
        field.append(search,esc);menu.append(field,optionsRoot,empty);
        const foot=document.createElement('div');foot.className='dd-foot';
        const hint=document.createElement('span');hint.className='dd-foot-hint';
        if(select.dataset.footer)hint.append(select.dataset.footer);
        if(select.dataset.footerCode){const code=document.createElement('code');code.textContent=select.dataset.footerCode;hint.append(' ',code);}
        const keys=document.createElement('span');keys.className='dd-foot-keys';keys.setAttribute('aria-hidden','true');
        keys.innerHTML='<kbd>↑↓</kbd>move<kbd>↵</kbd>select';
        foot.append(hint,keys);menu.append(foot);
      }else menu.append(search,optionsRoot,empty);
      search.addEventListener('input',()=>{
        const term=search.value.trim().toLocaleLowerCase();
        for(const item of optionsRoot.querySelectorAll('.dd-item'))item.hidden=!item.textContent.toLocaleLowerCase().includes(term);
        for(const head of optionsRoot.querySelectorAll('.dd-group')){
          let next=head.nextElementSibling,any=false;
          while(next && !next.classList.contains('dd-group')){if(!next.hidden)any=true;next=next.nextElementSibling;}
          head.hidden=!any;
        }
        empty.hidden=[...optionsRoot.querySelectorAll('.dd-item')].some(item=>!item.hidden);
        placeDdMenu(wrap,menu);
      });
      menu.onkeydown=event=>{
        const items=[...optionsRoot.querySelectorAll('.dd-item')].filter(item=>!item.hidden);
        const index=items.indexOf(document.activeElement);
        if(event.key==='ArrowDown' || event.key==='ArrowUp'){
          event.preventDefault();const step=event.key==='ArrowDown'?1:-1;
          const next=index<0?(step>0?0:items.length-1):(index+step+items.length)%items.length;
          items[next]?.focus();
        }else if(event.key==='Enter' && document.activeElement===search){event.preventDefault();items[0]?.click();}
        else if(event.key==='Escape'){event.preventDefault();closeOpen();btn.focus();}
      };
    }
    const entries = sections
      ? [...select.children].flatMap((child) => child.tagName === "OPTGROUP" ? [child, ...child.children] : [child])
      : [...select.options];
    for (const opt of entries) {
      if (opt.tagName === "OPTGROUP") {
        const head = document.createElement("div");
        head.className = "dd-group";
        head.setAttribute("role", "presentation");
        head.textContent = opt.label;
        if (opt.dataset.count) {
          const count = document.createElement("span");
          count.className = "dd-group-count";
          count.textContent = opt.dataset.count;
          head.append(count);
        }
        optionsRoot.appendChild(head);
        continue;
      }
      const item = document.createElement("div");
      item.className = "dd-item" + (opt.value === select.value ? " selected" : "");
      item.setAttribute("role", "option");
      if(select.dataset.search){item.tabIndex=0;item.onkeydown=e=>{if(e.key==='Enter' || e.key===' '){e.preventDefault();item.click();}};}
      item.dataset.value = opt.value;
      item.title = opt.textContent;
      if (sections) {
        const glyph = document.createElement("span");
        glyph.className = "dd-glyph";
        glyph.setAttribute("aria-hidden", "true");
        const text = document.createElement("span");
        text.className = "dd-text";
        const name = document.createElement("span");
        name.className = "dd-name";
        name.textContent = opt.dataset.short || opt.textContent;
        text.append(name);
        if (opt.dataset.tag) {
          const tag = document.createElement("span");
          tag.className = "dd-tag";
          tag.textContent = opt.dataset.tag;
          name.after(tag);
        }
        if (opt.dataset.detail) {
          const detail = document.createElement("span");
          detail.className = "dd-detail";
          detail.textContent = opt.dataset.detail;
          text.append(detail);
          item.classList.add("has-detail");
        }
        const meta = document.createElement("span");
        meta.className = "dd-meta";
        meta.textContent = opt.dataset.meta || "";
        const check = document.createElement("span");
        check.className = "dd-check";
        check.setAttribute("aria-hidden", "true");
        item.append(glyph, text, meta, check);
      } else {
        item.textContent = opt.textContent;
      }
      item.addEventListener("click", () => {
        closeOpen();
        if (sections) btn.focus();
        if (select.value !== opt.value) {
          select.value = opt.value;
          select.dispatchEvent(new Event("change", { bubbles: true }));
        }
        sync();
      });
      optionsRoot.appendChild(item);
    }
  }

  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    toggle(wrap, btn, menu, rebuild);
  });

  select.addEventListener("change", sync);
  rebuild();
  sync();
}

export function enhanceSelects(root = document) {
  root.querySelectorAll("select:not([data-native]):not([data-dd])").forEach(enhance);
  root.querySelectorAll(".dd").forEach(wireExisting);
}

if (typeof document !== "undefined") {
  document.addEventListener("pointerdown", (e) => {
    if (!openMenu) return;
    if (openMenu.wrap.contains(e.target) || openMenu.menu.contains(e.target)) return;
    closeOpen();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeOpen();
  });
  window.addEventListener("resize", () => {
    if (openMenu) placeDdMenu(openMenu.wrap, openMenu.menu);
  });
  window.addEventListener("scroll", () => {
    if (openMenu) placeDdMenu(openMenu.wrap, openMenu.menu);
  }, true);
  const boot = () => document.querySelectorAll(".dd").forEach(wireExisting);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
}
