export class MetricBrowser {
  constructor(root, dashboard, open) {
    this.root = root;
    this.dashboard = dashboard;
    this.open = open;
    this.entries = [];
    this.search = root.querySelector('input');
    this.list = root.querySelector('#metric-entries');
    this.message = root.querySelector('[role=status]');
    this.search.oninput = () => this.render();
    this.load();
  }
  async load() {
    try {
      const response = await fetch(`/api/metrics?dashboard=${encodeURIComponent(this.dashboard)}`);
      if (!response.ok) throw new Error('Metrics could not be loaded. Reload to retry.');
      this.entries = (await response.json()).metrics || [];
      this.render();
    } catch (error) {
      this.message.hidden = false;
      this.message.textContent = error.message;
    }
  }
  render() {
    const needle = this.search.value.trim().toLowerCase();
    const matches = this.entries.filter(metric => [metric.name, metric.title, metric.description, ...(metric.synonyms || [])].some(value => value?.toLowerCase().includes(needle)));
    this.list.replaceChildren();
    this.message.hidden = matches.length > 0;
    this.message.textContent = this.entries.length ? 'No matching metrics.' : 'No metrics defined in this project yet.';
    for (const metric of matches) {
      const button = document.createElement('button');
      button.className = 'metric-entry';
      button.dataset.metric = metric.name;
      button.title = metric.description || metric.name;
      const title = document.createElement('span');
      title.textContent = metric.title || metric.name;
      const context = document.createElement('small');
      context.textContent = `${metric.origin === 'project' ? 'Project' : metric.dashboard || 'Dashboard'} · ${metric.source_type}`;
      button.append(title, context);
      button.onclick = () => this.open(metric);
      this.list.append(button);
    }
  }
}
