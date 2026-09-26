export class DraftStorage {
  constructor(identity, notice) {
    this.key = `sqldash-workspace-v1:${identity}`; this.notice = notice; this.last = null; this.blocked = false;
  }
  load() {
    try {
      this.last = localStorage.getItem(this.key);
      if (!this.last) return null;
      const data = JSON.parse(this.last);
      if (data.version !== 1 || !Array.isArray(data.tabs) || data.tabs.length > 20 || data.tabs.some(tab => typeof tab.id !== 'string' || typeof tab.name !== 'string' || !tab.state || typeof tab.state !== 'object')) throw new Error('unsupported draft data');
      return data;
    } catch {
      this.blocked = true; this.notice('Draft storage could not be read. Existing data is preserved. Download drafts before reloading.'); return null;
    }
  }
  save(data) {
    if (this.blocked) return;
    try {
      if (localStorage.getItem(this.key) !== this.last) {
        this.blocked = true; this.notice('Drafts changed in another window. Download this window’s work before reloading; automatic saving is paused.'); return;
      }
      const value = JSON.stringify({ version:1, ...data });
      localStorage.setItem(this.key, value); this.last = value;
    } catch { this.notice('Browser storage is unavailable or full. Keep this page open or download your drafts.'); }
  }
}
