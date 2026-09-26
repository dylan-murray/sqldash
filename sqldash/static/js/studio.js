import { renderAgentChat } from './studio-chat.js';
import { agentOutput } from './studio-output.js';
import { initStudioTour } from './studio-tour.js';
import { withoutPeriod } from './sentence.js';
import { dashboard, dashboardName, filterValues, getEtag, mutatingHeaders } from './runner.js';

const panel = document.getElementById('studio-panel');
const el = (id) => document.getElementById(`studio-${id}`);
const storageKey = `sqldash-studio:${dashboardName}`;
let saved;
try { saved = JSON.parse(sessionStorage.getItem(storageKey)) || {}; } catch { saved = {}; }
let notes = Array.isArray(saved.notes) ? saved.notes : [];
let session = saved.session || null;
let history = Array.isArray(saved.history) ? saved.history : [];
let autoApprove = false;
let entrypoints = [];
let entrypointName = saved.entrypoint || '';
let picking = false;
let editingIndex = null;
let anchor = { tile: null, target: 'dashboard', x: .5, y: .5 };
let revision = null;
let pollGeneration = 0;
let busy = false;
let capturedImage = null;
let composerPoint = null;
let selectedNote = null;
let pinsVisible = true;
const noteHistory = [];
let clearTimer;
let currentStage = 'draft';
document.body.append(panel, el('note-form'), el('toolbar'));
el('message').value = typeof saved.message === 'string' ? saved.message : '';
function sizeMessage() { el('message').style.height = '30px'; el('message').style.height = Math.min(90, Math.max(30, el('message').scrollHeight)) + 'px'; }
el('message').addEventListener('input', sizeMessage);
initStudioTour();
const topbar = document.querySelector('.topbar');
function dock() { document.body.style.setProperty('--studio-top', `${topbar.getBoundingClientRect().bottom}px`); }
new ResizeObserver(dock).observe(topbar);
dock();

function persist() {
  try { sessionStorage.setItem(storageKey, JSON.stringify({ notes, session, history, entrypoint: entrypointName, message: el('message').value })); }
  catch { status('Browser storage is unavailable; keep this tab open to retain your notes.', true); }
  document.body.dataset.studioActive = String(!panel.hidden || !!session);
  document.body.classList.toggle('studio-open', !panel.hidden);
  el('toolbar').hidden = panel.hidden;
  requestAnimationFrame(positionPins);
}
function status(message, error = false) {
  el('status').textContent = message;
  el('status').dataset.error = String(error);
}
async function api(path, method = 'GET', body) {
  const response = await fetch(`/api/studio${path}`, {
    method, headers: mutatingHeaders({ 'Content-Type': 'application/json' }),
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  if (response.status === 204) return null;
  const result = await response.json();
  if (!response.ok) {
    const error = new Error(typeof result.detail === 'string' ? result.detail : 'Invalid Studio request. Check your notes and reload if the dashboard changed.');
    error.missingSession = response.status === 404 && result.detail === 'Studio session no longer exists';
    throw error;
  }
  return result;
}
function recoverSession(error) {
  if (!error.missingSession) return false;
  pollGeneration++; session = null; revision = null; autoApprove = false; el('auto-approve').checked = false;
  el('output').textContent = ''; persist(); stage('draft');
  status('Previous session ended. Your requests and edits are saved; ready for a new request.');
  return true;
}
function action(id, callback) {
  el(id).addEventListener('click', async () => {
    if (busy) return;
    busy = true;
    el(id).disabled = true;
    try { await callback(); } catch (error) { if (!recoverSession(error)) status(error.message, true); }
    finally { busy = false; el(id).disabled = id === 'send' ? !!session?.running : id === 'prepare' && !!session; }
  });
}
function stage(name) {
  currentStage = name; panel.dataset.stage = name;
  for (const id of ['send','pick','add','note-undo']) el(id).disabled = !!session?.running;
  el('message').disabled = false; el('prepare').disabled = !!session; el('entrypoint').disabled = !!session;
  el('note-undo').disabled = !!session?.running || !noteHistory.length;
  if (session?.running) { setPicking(false); el('note-form').hidden = true; }
  renderNotes(); updateAutoControl();
  for (const part of ['draft', 'ready', 'run', 'review-panel']) el(part).hidden = part !== name;
}
function setPicking(value) {
  picking = value;
  document.querySelector('.studio-hover')?.classList.remove('studio-hover');
  document.body.classList.toggle('studio-picking', picking);
  el('pick').setAttribute('aria-pressed', String(picking));
  el('pick').textContent = '⌖ Annotate dashboard';
  el('browse').setAttribute('aria-pressed', String(!picking));
}
function editNote(index = null) {
  if (session?.running || busy) return;
  editingIndex = index;
  if (index !== null) {
    anchor = { ...notes[index] };
    el('note').value = notes[index].note;
  } else el('note').value = '';
  el('target').value = anchor.tile || '';
  el('note-form').hidden = false;
  showPendingPin();
  el('note').focus();
}
function showPendingPin() {
  document.querySelector('.studio-pin-pending')?.remove();
  if (editingIndex === null && noteTarget(anchor)) {
    const pin = document.createElement('span'); pin.className = 'studio-pin studio-pin-pending'; pin.textContent = String(notes.length + 1);
    document.body.append(pin);
  }
  positionPins();
}
function drafting() { return !el('note-form').hidden && editingIndex === null; }
function positionComposer() {
  const form = el('note-form');
  const width = Math.min(320, innerWidth - 24);
  form.style.width = `${width}px`;
  let point = composerPoint;
  const target = noteTarget(anchor);
  if (target) { const r = target.getBoundingClientRect(); point = {x:r.left + r.width * anchor.x, y:r.top + r.height * anchor.y}; }
  point ||= {x:innerWidth / 2, y:innerHeight / 3};
  form.style.left = `${Math.max(12, Math.min(point.x + 14, innerWidth - width - 12))}px`;
  form.style.top = `${Math.max(topbar.getBoundingClientRect().bottom + 12, Math.min(point.y + 14, innerHeight - form.offsetHeight - 12))}px`;
}
function noteTarget(note) {
  if (note.selector) { try { return document.querySelector(note.selector); } catch { return null; } }
  return note.tile ? [...document.querySelectorAll('[data-tile-id]')].find(t => t.dataset.tileId === note.tile) : null;
}
function selectorFor(target) {
  const parts = [];
  while (target && target !== document.body) {
    if (target.id) { parts.unshift(`#${CSS.escape(target.id)}`); break; }
    const tag = target.tagName.toLowerCase();
    const peers = [...target.parentElement.children].filter(el => el.tagName === target.tagName);
    parts.unshift(`${tag}:nth-of-type(${peers.indexOf(target) + 1})`);
    target = target.parentElement;
  }
  return parts.join(' > ').slice(0, 1000);
}
function positionPins() {
  document.querySelectorAll('.studio-pin').forEach(pin => {
    const note = pin.classList.contains('studio-pin-pending') ? anchor : notes[Number(pin.dataset.index)];
    const target = noteTarget(note);
    if (!target) { pin.hidden = true; return; }
    const r = target.getBoundingClientRect();
    const x = r.left + r.width * note.x, y = r.top + r.height * note.y;
    pin.hidden = panel.hidden || !pinsVisible || y < topbar.getBoundingClientRect().bottom || y > innerHeight || !r.width || !r.height;
    pin.style.left = `${x}px`; pin.style.top = `${y}px`;
  });
  if (!el('note-form').hidden) positionComposer();
}
window.addEventListener('scroll', positionPins, true);
window.addEventListener('resize', positionPins);
function rememberNotes(includeEditor = false) {
  clearTimeout(clearTimer); el('cleared').hidden = true;
  const editor = includeEditor && editingIndex !== null && !el('note-form').hidden
    ? { index: editingIndex, text: el('note').value } : null;
  noteHistory.push({ notes: structuredClone(notes), editor });
  if (noteHistory.length > 20) noteHistory.shift();
}
function renderNotes() {
  const scroll = el('notes').scrollTop;
  el('notes').replaceChildren();
  document.querySelectorAll('.studio-pin').forEach(pin => pin.remove());
  const active = notes.filter(note => !note.done).length;
  el('note-count').textContent = String(notes.length);
  el('clear').disabled = !!session?.running || !notes.length;
  el('clear-undo').disabled = !!session?.running || !noteHistory.length;
  el('included-count').textContent = `${active} pinned request${active === 1 ? '' : 's'} included`;
  el('compose-link').textContent = `${active} requests →`;
  el('note-undo').disabled = !!session?.running || !noteHistory.length;
  if (!notes.length) {
    const empty = document.createElement('li'); empty.className = 'studio-empty';
    empty.textContent = 'Pin a change on the dashboard to get started.'; el('notes').append(empty);
  }
  notes.forEach((note, index) => {
    const li = document.createElement('li'); li.dataset.done = String(!!note.done);
    const button = document.createElement('button'); button.className = 'studio-note-card';
    button.setAttribute('aria-pressed', String(selectedNote === index));
    const badge = document.createElement('span'); badge.className = 'studio-note-number'; badge.textContent = String(index + 1);
    const copy = document.createElement('span'); copy.className = 'studio-note-copy';
    const title = document.createElement('strong');
    const tile = dashboard.tiles.find(t => t.id === note.tile);
    title.textContent = `${tile?.title || note.target || 'Dashboard'}${note.done ? ' · sent' : ''}`;
    const text = document.createElement('span'); text.textContent = note.note; button.title = note.note;
    const target = noteTarget(note)?.closest('.tile');
    if (target) {
      const style = getComputedStyle(target);
      button.style.setProperty('--request-color', style.getPropertyValue('--wcolor'));
      button.style.backgroundImage = style.backgroundImage;
    }
    copy.append(title, text); button.append(badge, copy);
    button.addEventListener('click', () => {
      selectedNote = index;
      noteTarget(note)?.scrollIntoView({block:'center', behavior:'instant'});
      renderNotes();
      if (!noteTarget(note)) editNote(index);
    });
    const remove = document.createElement('button'); remove.className = 'studio-note-remove';
    remove.textContent = '×'; remove.setAttribute('aria-label', `Remove request ${index + 1}`); remove.disabled = !!session?.running;
    remove.addEventListener('click', () => {
      if (session?.running || busy) return;
      rememberNotes(); notes.splice(index, 1); selectedNote = null;
      el('note-form').hidden = true; renderNotes(); persist();
    });
    li.append(button, remove); el('notes').append(li);
    if (noteTarget(note) && !note.done && !panel.hidden) {
      const pin = document.createElement('button'); pin.className = 'studio-pin'; pin.textContent = String(index + 1); pin.dataset.index = index;
      pin.dataset.phase = currentStage === 'review-panel' ? 'review' : session?.launched ? 'running' : 'draft';
      pin.setAttribute('aria-label', `Edit comment ${index + 1}`);
      pin.addEventListener('click', event => { event.stopPropagation(); if (!session?.running) { selectedNote = index; editNote(index); } });
      document.body.append(pin);
    }
  });
  el('notes').scrollTop = scroll;
  if (drafting()) showPendingPin();
  else positionPins();
}
function fitEntrypoint() {
  const select = el('entrypoint');
  const context = document.createElement('canvas').getContext('2d');
  context.font = getComputedStyle(select).font;
  select.style.width = `${Math.min(190, Math.ceil(context.measureText(select.selectedOptions[0]?.textContent || 'Choose agent').width) + 56)}px`;
}
async function refreshEntrypoints() {
  const result = await api('/entrypoints');
  entrypoints = result.entrypoints;
  el('entrypoint').replaceChildren();
  for (const entrypoint of result.entrypoints) {
    const option = document.createElement('option');
    option.value = entrypoint.name; option.textContent = entrypoint.name;
    el('entrypoint').append(option);
  }
  if (result.entrypoints.some(p => p.name === entrypointName)) el('entrypoint').value = entrypointName;
  entrypointName = el('entrypoint').value;
  fitEntrypoint(); updateAutoControl();
  el('agent-name').textContent = entrypointName || 'Your coding agent';
  el('config-path').textContent = result.path;
  el('no-agents').hidden = result.entrypoints.length > 0;
  el('entrypoint').disabled = !!session || result.entrypoints.length === 0;
  if (!result.entrypoints.length) { const option = document.createElement('option'); option.textContent = 'No agents found'; option.value = ''; el('entrypoint').append(option); }
  persist();
}
function updateAutoControl() {
  const supported = entrypoints.find(item => item.name === entrypointName)?.protocol === 'claude';
  el('auto-approve').closest('label').hidden = !supported;
  el('fresh-context').hidden = supported;
  if (!supported) { autoApprove = false; el('auto-approve').checked = false; }
  el('permission-summary').hidden = !entrypointName;
  el('permission-summary').textContent = supported
    ? (autoApprove ? 'All forwarded tools approved · No safety review' : 'Forwarded tool requests need your approval')
    : 'Agent-managed permissions · No approval prompts in Studio';
  el('permission-summary').title = 'The agent’s existing rules still apply. File and network access depend on its configuration; Studio does not enforce a project sandbox.';
}
async function open() {
  panel.hidden = false;
  const doneButton = document.getElementById('done-btn');
  if (doneButton && !doneButton.hidden) doneButton.click();
  renderNotes(); persist();
  el('close').focus();
  await refreshEntrypoints();
  if (session) {
    if (session.launched) { session.running = true; stage('run'); poll(); }
    else { stage('ready'); el('context').textContent = session.context; }
  } else stage('draft');
  window.dispatchEvent(new CustomEvent('sqldash:studio-open'));
}
el('open').addEventListener('click', () => open().catch(error => status(error.message, true)));
function closePanel() {
  panel.hidden = true; el('note-form').hidden = true; setPicking(false); renderNotes(); persist(); el('open').focus();
}
let closeChoice = null;
function askClose() {
  const running = !!session?.running;
  el('close-copy').textContent = running
    ? 'The agent is still working. Closing stops it; edits made so far stay on disk and this run’s undo history is released.'
    : 'The agent’s edits stay on disk. Closing ends this session and releases its undo history.';
  el('close-keep').textContent = running ? 'Stop agent and close' : 'Keep edits and close';
  el('close-review').hidden = running || el('review').hidden || el('review').disabled;
  el('close-sheet').hidden = false;
  el('close-keep').focus();
  return new Promise(resolve => { closeChoice = resolve; });
}
function answerClose(choice) {
  if (!closeChoice) return;
  el('close-sheet').hidden = true;
  const resolve = closeChoice; closeChoice = null; resolve(choice);
}
el('close-keep').addEventListener('click', () => answerClose('close'));
el('close-review').addEventListener('click', () => answerClose('review'));
for (const id of ['close-back', 'close-cancel']) el(id).addEventListener('click', () => answerClose('stay'));
el('close-sheet').addEventListener('keydown', event => { if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); answerClose('stay'); } });
action('close', async () => {
  if (session) {
    const choice = await askClose();
    if (choice === 'stay') { el('close').disabled = false; el('close').focus(); return; }
    if (choice === 'review') { await review(true); return; }
    try { await api(`/sessions/${session.id}`, 'DELETE'); }
    catch (error) { if (!error.missingSession) throw error; }
    pollGeneration++; session = null; revision = null; autoApprove = false; el('auto-approve').checked = false; persist(); stage('draft'); status('');
  }
  autoApprove = false; el('auto-approve').checked = false;
  closePanel();
});
panel.addEventListener('keydown', event => { if (event.key === 'Escape' && !event.defaultPrevented && el('close-sheet').hidden) el('close').click(); });
el('pick').addEventListener('click', () => { setPicking(!picking); el('note-form').hidden = true; document.querySelector('.studio-pin-pending')?.remove(); });
el('add').addEventListener('click', () => { composerPoint = null; anchor = { tile: null, target: 'dashboard', x: .5, y: .5 }; editNote(); });
el('note-cancel').addEventListener('click', () => { el('note-form').hidden = true; document.querySelector('.studio-pin-pending')?.remove(); });
el('note-form').addEventListener('submit', event => {
  event.preventDefault();
  if (session?.running || busy) return;
  const note = el('note').value.trim();
  if (!note) return;
  if (editingIndex === null && notes.filter(note => !note.done).length >= 30) { status('Send this batch before adding more than 30 notes.', true); return; }
  const tile = el('target').value || null;
  const next = { ...anchor, tile, note, done: false };
  rememberNotes();
  if (tile !== anchor.tile) { next.x = .5; next.y = .1; next.target = tile ? 'tile' : 'dashboard'; delete next.selector; }
  if (editingIndex === null) {
    while (notes.length >= 60 && notes.some(note => note.done)) notes.splice(notes.findIndex(note => note.done), 1);
    notes.push(next);
  } else notes[editingIndex] = next;
  el('note-form').hidden = true; renderNotes(); persist();
});
document.addEventListener('click', event => {
  if (!picking || !el('note-form').hidden || el('toolbar').contains(event.target) || event.target.closest('.studio-tour') || panel.contains(event.target) || el('note-form').contains(event.target) || event.target.closest('.studio-pin')) return;
  const target = event.target.closest('main.container') ? event.target : null;
  if (!target) return;
  event.preventDefault(); event.stopImmediatePropagation();
  const bounds = target.getBoundingClientRect();
  anchor = {
    tile: target.closest('[data-tile-id]')?.dataset.tileId || null,
    selector: selectorFor(target),
    target: event.target.closest('.filter')?.querySelector('[data-filter]')?.dataset.filter || event.target.tagName.toLowerCase(),
    x: Math.max(0, Math.min(1, (event.clientX - bounds.left) / bounds.width)),
    y: Math.max(0, Math.min(1, (event.clientY - bounds.top) / bounds.height)),
  };
  composerPoint = {x:event.clientX, y:event.clientY};
  editNote();
}, true);
el('entrypoint').addEventListener('change', () => { entrypointName = el('entrypoint').value; fitEntrypoint(); updateAutoControl(); el('agent-name').textContent = entrypointName; persist(); });
action('entrypoints-refresh', refreshEntrypoints);
action('check-entrypoint', async () => {
  await api('/entrypoints/check', 'POST', { entrypoint: entrypointName });
  status(`${entrypointName}: entrypoint found. This checks launch configuration, not account access.`);
});
async function screenshot() {
  if (capturedImage) return capturedImage.split(',')[1];
  const file = el('screenshot').files[0];
  if (!file) return null;
  if (file.type !== 'image/png' || file.size > 1000000) throw new Error('Use a PNG smaller than 1 MB.');
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}
async function requestBody() {
  const activeNotes = notes.filter(note => !note.done).map(({ done, ...note }) => note);
  const instruction = el('message').value.trim();
  if (instruction) activeNotes.push({note:instruction, target:'Overall instructions', tile:null, x:.5, y:.5});
  if (!activeNotes.length) throw new Error('Add a pinned request or an instruction first.');
  if (activeNotes.length > 30) throw new Error('Use at most 29 pinned requests when adding overall instructions.');
  if (!entrypointName) throw new Error('Choose an installed agent or add a custom entrypoint.');
  const filters = Object.fromEntries(Object.entries(filterValues()).map(([k, v]) => [k, String(v ?? '')]));
  return { dashboard: dashboardName, etag: getEtag(), annotations: activeNotes, filters, screenshot: await screenshot() };
}
async function prepare() {
  const draft = el('message').value;
  const data = await requestBody();
  const result = await api('/sessions', 'POST', data);
  history = [];
  session = { ...result, launched: false, draft, request:draft.trim(), requestCount:data.annotations.length - (draft.trim() ? 1 : 0) }; persist();
  el('context').textContent = result.context; stage('ready');
  status(`Ready for ${entrypointName}. Working directory: ${result.cwd}`);
  closePopover('context');
}
action('prepare', prepare);
action('back', async () => {
  await api(`/sessions/${session.id}`, 'DELETE'); session = null; persist(); stage('draft'); status('Edit your notes, then review the context again.');
});
async function launch() {
  status(`Starting ${entrypointName}…`);
  if (autoApprove) await api(`/sessions/${session.id}/permission-mode`, 'POST', {auto_approve:true});
  await api(`/sessions/${session.id}/launch`, 'POST', { entrypoint: entrypointName });
  beginTurn();
}
action('launch', launch);
function beginTurn() {
  session.launched = true; session.running = true; session.started = Date.now();
  session.transcript = ''; session.undone = false;
  notes.forEach(note => { note.done = true; });
  if (el('message').value === session.draft) el('message').value = '';
  delete session.draft; sizeMessage(); persist(); stage('run'); poll();
}
async function continueTurn() {
  const draft = el('message').value;
  const data = await requestBody();
  el('review').disabled = true; el('undo-last').disabled = true;
  let result;
  try { result = await api(`/sessions/${session.id}/turns`, 'POST', data); }
  finally { el('review').disabled = false; el('undo-last').disabled = false; }
  if (session.transcript) history.push({text:session.transcript,request:session.request,count:session.requestCount});
  while (history.length > 1 && history.reduce((n, item) => n + item.text.length, 0) > 200000) history.shift();
  session = {...session,...result,draft,request:draft.trim(),requestCount:data.annotations.length - (draft.trim() ? 1 : 0)}; beginTurn();
}
action('send', async () => { if (session?.launched) await continueTurn(); else { if (!session) await prepare(); await launch(); } });
el('auto-approve').addEventListener('change', async () => {
  const desired = el('auto-approve').checked; el('auto-approve').disabled = true;
  try { if (session) await api(`/sessions/${session.id}/permission-mode`, 'POST', {auto_approve:desired}); autoApprove = desired; }
  catch (error) { el('auto-approve').checked = autoApprove; status(error.message, true); }
  finally { el('auto-approve').disabled = false; updateAutoControl(); }
});
el('message').addEventListener('keydown', event => {
  if (event.key !== 'Enter' || event.shiftKey || event.isComposing || event.repeat) return;
  event.preventDefault();
  if (!busy && !el('send').disabled) el('send').click();
});
el('latest').addEventListener('click', () => { el('output').scrollTop = el('output').scrollHeight; el('latest').hidden = true; });
el('output').addEventListener('scroll', () => { el('latest').hidden = el('output').scrollHeight - el('output').scrollTop - el('output').clientHeight < 40; });
function renderPermissions(requests) {
  const target = el('permissions');
  const ids = requests.map(request => request.id).join(',');
  if (target.dataset.requests === ids) return;
  target.dataset.requests = ids; target.hidden = !requests.length; target.replaceChildren();
  for (const request of requests) {
    const card = document.createElement('article'); card.className = 'studio-permission';
    const title = document.createElement('strong'); title.textContent = `Allow ${request.tool}?`;
    const detail = document.createElement('details');
    const summary = document.createElement('summary'); summary.textContent = 'Requested action';
    detail.open = true; detail.append(summary);
    for (const [key, value] of Object.entries(request.input)) {
      const label = document.createElement('small'); label.textContent = ({file_path:'File', command:'Command', old_string:'Before', new_string:'After', content:'Content'})[key] || key.replaceAll('_', ' ');
      const input = document.createElement('pre'); input.textContent = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
      detail.append(label, input);
    }
    card.append(title, detail);
    const actions = document.createElement('footer'); actions.className = 'studio-permission-actions';
    for (const [decision, label] of [['deny', 'Deny'], ['allow', 'Allow once']]) {
      const button = document.createElement('button'); button.className = decision === 'allow' ? 'btn btn-primary' : 'btn'; button.textContent = label;
      button.addEventListener('click', async () => {
        const id = session?.id; if (!id) return;
        card.querySelectorAll('button').forEach(el => { el.disabled = true; });
        try { await api(`/sessions/${id}/permissions/${encodeURIComponent(request.id)}`, 'POST', {decision}); card.remove(); }
        catch (error) { status(error.message, true); card.querySelectorAll('button').forEach(el => { el.disabled = false; }); }
      });
      actions.append(button);
    }
    card.append(actions); target.append(card);
  }
}
async function poll() {
  const generation = ++pollGeneration;
  let offset = 0;
  const decoder = new TextDecoder();
  const output = agentOutput();
  let transcript = '';
  const request = session.request ?? el('message').value.trim();
  const requestCount = session.requestCount ?? notes.filter(note => !note.done).length;
  el('output').replaceChildren();
  for (const turn of history) { const node = document.createElement('div'); renderAgentChat(node, turn.text, entrypointName, turn.request, turn.count); el('output').append(node); }
  const currentChat = document.createElement('div'); el('output').append(currentChat);
  renderAgentChat(currentChat, '', entrypointName, request, requestCount);
  const activity = document.createElement('div'); activity.className = 'studio-live-activity';
  const icon = document.createElement('span'); icon.className = 'studio-live-icon'; icon.textContent = '✳'; icon.setAttribute('aria-hidden', 'true');
  const detail = document.createElement('div');
  const author = document.createElement('strong'); author.textContent = entrypointName;
  const action = document.createElement('span'); action.setAttribute('role', 'status'); action.textContent = 'Starting…';
  const elapsed = document.createElement('span'); elapsed.className = 'studio-live-elapsed'; elapsed.setAttribute('aria-hidden', 'true');
  detail.append(author, action); activity.append(icon, detail, elapsed); el('output').append(activity);
  el('output').scrollTop = el('output').scrollHeight;
  status('');
  el('undo-last').hidden = true;
  renderPermissions([]);
  el('diagnostics').hidden = true; el('diagnostics').open = false; el('latest').hidden = true;
  el('diagnostic-output').textContent = '';
  el('review').disabled = true; el('review').hidden = true; el('stop').hidden = false; el('stop').disabled = false;
  try {
    while (session && generation === pollGeneration) {
      const result = await api(`/sessions/${session.id}/output?offset=${offset}`);
      if (generation !== pollGeneration) return;
      renderPermissions(result.permissions || []);
      if (!el('auto-approve').disabled) { autoApprove = !!result.auto_approve; el('auto-approve').checked = autoApprove; updateAutoControl(); }
      session.undone = !!result.undone;
      const bytes = Uint8Array.from(atob(result.data), c => c.charCodeAt(0));
      const text = output.push(decoder.decode(bytes, { stream: result.running }), !result.running);
      const follow = el('output').scrollHeight - el('output').scrollTop - el('output').clientHeight < 40;
      if (text) {
        const previousTop = el('output').scrollTop;
        transcript = (transcript + text).slice(-100000);
        renderAgentChat(currentChat, transcript, entrypointName, request, requestCount);
        session.transcript = transcript;
        if (!follow) el('output').scrollTop = previousTop;
      }
      if (follow) el('output').scrollTop = el('output').scrollHeight;
      el('latest').hidden = follow;
      el('diagnostic-output').textContent = output.diagnostics;
      el('diagnostics').hidden = !output.diagnostics;
      if (result.truncated) status('Earlier output was trimmed to keep this session bounded.');
      offset = result.offset;
      if (result.error) {
        activity.hidden = true;
        el('review').disabled = true;
        el('stop').disabled = !result.running;
        status(result.error, true);
        if (!result.running) return;
        await new Promise(resolve => setTimeout(resolve, 500));
        continue;
      }
      if (!result.running) {
        activity.hidden = true;
        el('stop').hidden = true; el('stop').disabled = true;
        try { await review(false); }
        finally {
          if (session && generation === pollGeneration) {
            session.running = false; stage('run'); persist();
            el('review').disabled = false; el('review').hidden = false;
          }
        }
        if (!session || generation !== pollGeneration) return;
        status(output.problem || (result.cancelled ? 'Agent stopped. Review any partial edits.' : result.returncode === 0 ? 'Done. What would you like to change next?' : `Agent exited with code ${result.returncode}. Check its output and review any partial edits.`), !!output.problem || (result.returncode !== 0 && !result.cancelled));
        return;
      }
      const waiting = !!result.permissions?.length;
      activity.dataset.waiting = String(waiting);
      const label = waiting ? 'Waiting for your approval' : output.activity;
      if (action.textContent !== label) action.textContent = label;
      elapsed.textContent = `${Math.floor((Date.now() - (session.started || Date.now())) / 1000)}s`;

      await new Promise(resolve => setTimeout(resolve, 500));
    }
  } catch (error) {
    if (generation !== pollGeneration) return;
    if (!recoverSession(error)) status(`${withoutPeriod(error.message)}. Reopen Studio to retry the connection.`, true);
  } finally { activity.remove(); }
}
action('stop', async () => { await api(`/sessions/${session.id}/stop`, 'POST'); });
async function review(show = true) {
  const result = await api(`/sessions/${session.id}/review`, 'POST');
  revision = result.revision;
  const errors = result.validation.filter(f => f.severity === 'error');
  const warnings = result.validation.filter(f => f.severity === 'warning');
  el('validation').textContent = result.validation_error
    ? `${result.changes.length} changed files · ${result.validation_error}`
    : `${result.changes.length} changed files · ${errors.length} validation errors · ${warnings.length} warnings. Run sqldash lint locally for details.`;
  el('diff').replaceChildren();
  for (const change of result.changes) {
    const details = document.createElement('details'); details.open = true;
    const summary = document.createElement('summary'); summary.textContent = `${change.file} · ${change.kind}`;
    const pre = document.createElement('pre'); pre.textContent = change.diff;
    details.append(summary, pre); el('diff').append(details);
  }
  el('undo-last').hidden = !result.changes.length || !!session.undone;
  el('undo').disabled = !!session.undone;
  if (show) { stage('review-panel'); status('Changes from the latest turn.'); }
}
action('review', review); action('recheck', review);
async function undoLast() {
  await api(`/sessions/${session.id}/undo`, 'POST', {revision});
  session.undone = true; persist(); el('undo-last').hidden = true;
  await refreshCanvas(); stage('run'); status('Last edits undone. You can keep chatting.');
}
action('undo-last', undoLast); action('undo', undoLast);
action('keep', () => { stage('run'); status(''); });

let refreshTimer;
let refreshVersion = 0;
async function refreshCanvas() {
  const version = refreshVersion;
  try {
    const { refreshDashboard } = await import('./edit.js');
    await refreshDashboard();
    renderNotes();
    document.querySelector('.studio-dashboard-name').textContent = dashboard.title;
  } catch (error) { status(error.message, true); }
  finally {
    if (version !== refreshVersion) refreshTimer = setTimeout(refreshCanvas, 150);
  }
}
window.addEventListener('sqldash:studio-change', () => {
  refreshVersion++;
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(refreshCanvas, 150);
});
renderNotes(); persist();
if (session) open().catch(error => status(error.message, true));



el('entrypoint-form').addEventListener('submit', async event => {
  event.preventDefault();
  const button = event.submitter; button.disabled = true;
  try {
    const command = el('entry-command').value.trim();
    const name = el('entry-name').value.trim();
    const args = el('entry-kind').value === 'claude' ? ['-p', '--output-format', 'stream-json', '--verbose', '--include-partial-messages', '{prompt}'] : ['exec', '--skip-git-repo-check', '--json', '{prompt}'];
    await api('/entrypoints', 'POST', {name, command:[command, ...args], protocol:el('entry-kind').value === 'claude' ? 'claude' : 'text', shell:el('entry-shell').value || null});
    entrypointName = name; await refreshEntrypoints(); el('setup').open = false; closePopover('settings'); status(`${name} saved.`);
  } catch (error) { status(error.message, true); }
  finally { button.disabled = false; }
});
el('note-form').addEventListener('keydown', event => { if (event.key === 'Escape') { event.stopPropagation(); el('note-form').hidden = true; document.querySelector('.studio-pin-pending')?.remove(); } });
function showCapture(data) {
  capturedImage = data;
  el('capture-image').src = data || '';
  el('capture-preview').hidden = !data;
  el('capture-status').textContent = data ? 'Attached' : '';
}
action('capture', async () => {
  if (!navigator.mediaDevices?.getDisplayMedia) throw new Error('This browser cannot capture a tab. Attach a PNG below instead.');
  let stream;
  const video = document.createElement('video'); video.muted = true;
  try {
    stream = await navigator.mediaDevices.getDisplayMedia({video:true, audio:false, preferCurrentTab:true});
    video.srcObject = stream; await video.play();
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const canvas = document.createElement('canvas');
    let scale = Math.min(1, 1600 / video.videoWidth);
    let data;
    do {
      canvas.width = Math.round(video.videoWidth * scale); canvas.height = Math.round(video.videoHeight * scale);
      canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
      data = canvas.toDataURL('image/png'); scale *= .8;
    } while (data.length > 1333300 && canvas.width > 400);
    if (data.length > 1333300) throw new Error('Screenshot is too large. Capture a smaller window.');
    el('screenshot').value = ''; showCapture(data);
  } catch (error) {
    if (error.name === 'NotAllowedError') status('Capture cancelled.');
    else throw error;
  } finally { stream?.getTracks().forEach(track => track.stop()); video.srcObject = null; }
});
el('capture-remove').addEventListener('click', () => { showCapture(null); el('screenshot').value = ''; });
el('screenshot').addEventListener('change', async () => {
  capturedImage = null;
  try { const data = await screenshot(); showCapture(data ? `data:image/png;base64,${data}` : null); }
  catch (error) { showCapture(null); el('screenshot').value = ''; status(error.message, true); }
});

document.addEventListener('pointerover', event => {
  if (!picking) return;
  document.querySelector('.studio-hover')?.classList.remove('studio-hover');
  if (event.target.closest('main.container')) event.target.classList.add('studio-hover');
});

function closePopover(name) {
  el(name === 'context' ? 'context-popover' : 'settings').hidden = true;
  el(`${name}-toggle`).setAttribute('aria-expanded', 'false');
}
for (const name of ['settings', 'context']) {
  const popover = el(name === 'context' ? 'context-popover' : name);
  el(`${name}-toggle`).addEventListener('click', () => {
    const opening = popover.hidden;
    closePopover(name === 'settings' ? 'context' : 'settings');
    popover.hidden = !opening; el(`${name}-toggle`).setAttribute('aria-expanded', String(opening));
    if (opening && name === 'settings') el('setup').open = true;
  });
  el(`${name}-close`).addEventListener('click', () => { closePopover(name); el(`${name}-toggle`).focus(); });
  document.addEventListener('click', event => {
    if (!popover.contains(event.target) && !el(`${name}-toggle`).contains(event.target)) closePopover(name);
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !popover.hidden) {
      event.preventDefault(); event.stopImmediatePropagation(); closePopover(name); el(`${name}-toggle`).focus();
    }
  }, true);
}
el('message').addEventListener('input', persist);
el('note-close').addEventListener('click', () => el('note-cancel').click());
el('browse').addEventListener('click', () => { setPicking(false); el('note-cancel').click(); });
el('pins-toggle').addEventListener('click', () => { pinsVisible = !pinsVisible; el('pins-toggle').setAttribute('aria-pressed', String(pinsVisible)); positionPins(); });
function undoNotes() {
  if (session?.running || busy || !noteHistory.length) return;
  const draft = drafting();
  const previous = noteHistory.pop();
  notes = previous.notes; selectedNote = null; if (!draft) el('note-cancel').click(); renderNotes(); persist();
  if (draft) showPendingPin();
  else if (previous.editor) {
    selectedNote = previous.editor.index; renderNotes(); editNote(selectedNote); el('note').value = previous.editor.text;
  }
  clearTimeout(clearTimer); el('cleared').hidden = true;
}
el('note-undo').addEventListener('click', undoNotes);
el('clear-undo').addEventListener('click', undoNotes);
el('clear').addEventListener('click', () => {
  if (session?.running || busy || !notes.length) return;
  const draft = drafting();
  rememberNotes(true); notes = []; selectedNote = null; setPicking(false); if (!draft) el('note-cancel').click();
  renderNotes(); persist(); if (draft) showPendingPin(); el('cleared').hidden = false;
  clearTimer = setTimeout(() => { el('cleared').hidden = true; }, 10000);
});
el('compose-link').addEventListener('click', () => { setPicking(false); el('message').focus(); });
