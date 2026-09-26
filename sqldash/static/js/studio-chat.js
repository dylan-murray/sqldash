function appendInline(parent, text) {
  const pattern = /(`+)([\s\S]*?)\1|\*\*([^*]+)\*\*|__([^_]+)__|\*([^*\n]+)\*/g;
  let offset = 0;
  for (const match of text.matchAll(pattern)) {
    parent.append(document.createTextNode(text.slice(offset, match.index)));
    const node = document.createElement(match[1] ? 'code' : match[5] ? 'em' : 'strong');
    if (match[1]) node.textContent = match[2];
    else appendInline(node, match[3] || match[4] || match[5]);
    parent.append(node); offset = match.index + match[0].length;
  }
  parent.append(document.createTextNode(text.slice(offset)));
}

function appendMarkdown(body, text) {
  let fence = null, code = [], prose = [], lists = [];
  function paragraph() {
    if (!prose.length) return;
    const p = document.createElement('p'); appendInline(p, prose.join('\n'));
    body.append(p); prose = [];
  }
  function block() {
    const pre = document.createElement('pre'), node = document.createElement('code');
    node.textContent = code.join('\n'); pre.append(node); body.append(pre); code = [];
  }
  for (const line of text.split('\n')) {
    const marker = line.match(/^\s*(`{3,}|~{3,})(.*)$/);
    if (!fence && marker) { paragraph(); lists = []; fence = marker[1]; continue; }
    if (fence) {
      if (marker && marker[1][0] === fence[0] && marker[1].length >= fence.length && !marker[2].trim()) { block(); fence = null; }
      else code.push(line);
      continue;
    }
    if (!line.trim()) { paragraph(); continue; }
    const item = line.match(/^(\s*)([-+*]|\d+[.)])\s+(.+)$/);
    if (item) {
      paragraph();
      const indent = item[1].replace(/\t/g, '    ').length, kind = /^\d/.test(item[2]) ? 'ol' : 'ul';
      while (lists.length && lists.at(-1).indent > indent) lists.pop();
      if (lists.at(-1)?.indent === indent && lists.at(-1).kind !== kind) lists.pop();
      if (!lists.length || lists.at(-1).indent < indent) {
        const list = document.createElement(kind);
        if (kind === 'ol') list.start = Number.parseInt(item[2], 10);
        (lists.at(-1)?.node.lastElementChild || body).append(list);
        lists.push({node:list, indent, kind});
      }
      const li = document.createElement('li'); appendInline(li, item[3]); lists.at(-1).node.append(li);
      continue;
    }
    const indent = line.match(/^\s*/)[0].replace(/\t/g, '    ').length;
    while (lists.length && lists.at(-1).indent >= indent) lists.pop();
    if (lists.length) {
      appendInline(lists.at(-1).node.lastElementChild, ' ' + line.trim()); continue;
    }
    lists = [];
    const heading = line.match(/^(#{1,6})\s+(.+?)(?:\s+#+)?$/);
    if (heading) {
      paragraph(); const node = document.createElement(`h${heading[1].length}`);
      appendInline(node, heading[2]); body.append(node);
    } else prose.push(line);
  }
  if (fence) block();
  paragraph();
}

export function renderAgentChat(container, transcript, agent, request, count) {
  const expanded = new Set([...container.querySelectorAll('details[open]')].map(el => el.dataset.group));
  const fragment = document.createDocumentFragment();
  function message(label, text, kind) {
    const article = document.createElement('article'); article.className = `studio-chat-message ${kind}`;
    const heading = document.createElement('strong'); heading.className = 'studio-chat-author'; heading.textContent = label;
    if (kind === 'studio-chat-assistant') {
      const avatar = document.createElement('span'); avatar.className = 'studio-chat-avatar';
      avatar.setAttribute('aria-hidden', 'true'); avatar.textContent = '✳';
      heading.prepend(avatar);
    }
    const body = document.createElement('div'); body.className = 'studio-chat-text'; appendMarkdown(body, text);
    article.append(heading, body); fragment.append(article);
  }
  if (request || count) message('You', request || `${count} pinned request${count === 1 ? '' : 's'}`, 'studio-chat-user');
  let response = [], tools = [], group = 0, fenced = false;
  function flushResponse() {
    const text = response.join('\n').trim();
    if (text) message(agent || 'Agent', text, 'studio-chat-assistant');
    response = [];
  }
  function flushTools() {
    if (!tools.length) return;
    const details = document.createElement('details'); details.className = 'studio-chat-tools';
    details.dataset.group = String(group++); details.open = expanded.has(details.dataset.group);
    const summary = document.createElement('summary');
    const actions = tools.filter(text => /^› (Running|Edited) /.test(text));
    summary.textContent = actions.length ? `${actions.length} tool action${actions.length === 1 ? '' : 's'}` : 'Agent activity';
    if (tools.some(text => text.startsWith('!'))) summary.textContent += ' · needs attention';
    details.append(summary);
    for (const text of tools) {
      const row = document.createElement('div'); row.textContent = text.replace(/^› (Running )?/, '').replace(/^✓ /, '');
      details.append(row);
    }
    fragment.append(details); tools = [];
  }
  for (const line of transcript.split('\n')) {
    if (/^\s*(`{3,}|~{3,})/.test(line)) { fenced = !fenced; flushTools(); response.push(line); continue; }
    if (fenced) { response.push(line); continue; }
    if (/^› (Agent connected|Agent finished)$/.test(line) || line === '✓ Tool finished') continue;
    if (/^[›✓] /.test(line)) { flushResponse(); tools.push(line); continue; }
    if (line.startsWith('! Tool error:')) { flushResponse(); tools.push(line); continue; }
    if (/^! /.test(line)) {
      flushResponse(); flushTools(); message('Couldn’t complete this step', line.slice(2), 'studio-chat-error'); continue;
    }
    if (line.trim()) flushTools();
    response.push(line);
  }
  flushResponse(); flushTools();
  container.replaceChildren(fragment);
}
