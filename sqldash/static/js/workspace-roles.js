import { enhanceSelects } from '/static/js/dropdown.js';
export class WorkspaceRoles {
  constructor(root, change) {
    this.root = root;
    this.picker = root.querySelector('select');
    this.status = root.querySelector('[role=status]');
    this.label = root.querySelector('label');
    this.picker.onchange = () => { if (!this.updating) change(this.picker.value || null); };
  }
  render(context, running) {
    this.root.hidden = false;
    this.label.textContent = context?.label || 'Role';
    const loading = !context || context.loading;
    const label = loading ? 'Loading roles…' : context.error && !context.current?.length ? 'Role unavailable' : context.current?.join(', ') || (context.switchable ? 'No active role' : 'Not applicable');
    const detailOf = value => context?.roles?.find(role => role.value === value)?.detail || '';
    const fallback = new Option(context?.selected ? 'Connection default' : `${label} · connection default`, '');
    fallback.dataset.short = context?.selected ? 'Connection default' : label;
    fallback.dataset.tag = 'default';
    if (!context?.selected) fallback.dataset.detail = detailOf(context?.current?.[0]);
    const options = [];
    for (const role of context?.roles || []) {
      const option = new Option(role.label, role.value);
      if (role.detail) option.dataset.detail = role.detail;
      options.push(option);
    }
    if (context?.selected && !options.some(option => option.value === context.selected)) {
      options.push(new Option(context.current?.join(', ') || context.selected, context.selected));
    }
    const first = document.createElement('optgroup'); first.label = 'Connection default'; first.append(fallback);
    const rest = document.createElement('optgroup'); rest.label = 'Roles'; rest.dataset.count = String(options.length); rest.append(...options);
    this.picker.replaceChildren(...(options.length ? [first, rest] : [first]));
    const secondary = context?.secondary ? ` · +${context.secondary.toLowerCase()}` : '';
    this.picker.dataset.rowMeta = loading || !context?.switchable ? '' : `${context.selected ? 'role' : 'default role'}${secondary}`;
    this.picker.dataset.footer = context?.note || '';
    this.picker.value = context?.selected || '';
    this.picker.disabled = loading || Boolean(context?.pending) || !context?.switchable || running;
    if (!context?.switchable) this.picker.options[0].textContent = label;
    enhanceSelects(this.root);
    const button = this.root.querySelector('.dd-btn');
    button.disabled = this.picker.disabled;
    button.title = context?.current?.join(', ') || label;
    this.updating = true; this.picker.dispatchEvent(new Event('change')); this.updating = false;
    this.root.setAttribute('aria-busy', String(loading || Boolean(context?.pending)));
    this.status.textContent = context?.error || (context?.pending ? 'Switching role…' : context?.switchable ? '' : context?.note || '');
    this.status.hidden = !this.status.textContent;
  }
}
