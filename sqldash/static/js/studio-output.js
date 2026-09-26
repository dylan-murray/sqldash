export function agentOutput() {
  let pending = '', streamed = false, answered = false, problem = '', activity = 'Waiting for agent output';
  let diagnostics = '', codexStream = false;
  const diagnostic = text => { diagnostics = (diagnostics + text + '\n').slice(-20000); return ''; };
  const line = text => `${text}\n`;
  const isDiagnostic = text => /claude\.ai connectors are disabled/.test(text) || (codexStream && /^Reading additional input from stdin\.\.\.$|^\d{4}-\d{2}-\d{2}T\S+\s+(TRACE|DEBUG|INFO|WARN|ERROR)\s/.test(text));
  function event(value) {
    if (!value || typeof value !== 'object') return null;
    if (['thread.started', 'turn.started', 'turn.completed', 'turn.failed'].includes(value.type)) codexStream = true;
    if (value.type === 'thread.started' || value.type === 'turn.started') { activity = 'Agent connected'; return ''; }
    if (value.type === 'turn.completed') { activity = 'Agent finished'; return ''; }
    if (value.type === 'turn.failed' || value.type === 'error') {
      problem = 'Codex reported an error. Check the conversation and review any partial edits.';
      return line(`! ${typeof value.error?.message === 'string' ? value.error.message : typeof value.message === 'string' ? value.message : 'Codex could not complete this turn.'}`);
    }
    if (['item.started', 'item.updated', 'item.completed'].includes(value.type)) {
      const item = value.item || {}, complete = value.type === 'item.completed';
      const identified = typeof item.id === 'string' && item.id && (
        (['agent_message', 'reasoning'].includes(item.type) && typeof item.text === 'string') ||
        (item.type === 'command_execution' && typeof item.command === 'string') ||
        (item.type === 'file_change' && Array.isArray(item.changes)) ||
        (item.type === 'mcp_tool_call' && typeof item.server === 'string' && typeof item.tool === 'string') ||
        (item.type === 'web_search' && typeof item.query === 'string')
      );
      if (identified) codexStream = true;
      if (item.type === 'agent_message') {
        activity = 'Writing response';
        if (!complete) return '';
        answered = true; return line(typeof item.text === 'string' ? item.text : '');
      }
      if (item.type === 'reasoning') { activity = 'Thinking'; return diagnostic(JSON.stringify(value)); }
      const labels = {command_execution:'command',file_change:'file edit',mcp_tool_call:'tool',web_search:'web search'};
      if (labels[item.type]) {
        diagnostic(JSON.stringify(value));
        activity = complete ? 'Tool finished' : `Running ${labels[item.type]}`;
        if (complete && (item.status === 'failed' || (typeof item.exit_code === 'number' && item.exit_code !== 0))) {
          return line(`! Tool error: ${labels[item.type]} failed. See Agent diagnostics for details.`);
        }
        if (complete && item.status === 'declined') {
          activity = `${labels[item.type][0].toUpperCase()}${labels[item.type].slice(1)} declined`;
          return line(`✓ ${activity}`);
        }
        if (item.type === 'file_change') {
          if (!complete) return '';
          const paths = (Array.isArray(item.changes) ? item.changes : []).map(c => c?.path).filter(p => typeof p === 'string' && p);
          return '\n' + line(`› Edited ${paths.length ? paths.join(', ').slice(0, 200) : 'files'}`);
        }
        if (value.type === 'item.started') {
          const detail = item.type === 'command_execution' ? item.command : item.type === 'mcp_tool_call' ? item.tool : '';
          return '\n' + line(`› Running ${labels[item.type]}${typeof detail === 'string' && detail ? ` · ${detail.replace(/\s+/g, ' ').slice(0, 200)}` : ''}`);
        }
        return '';
      }
      return diagnostic(JSON.stringify(value));
    }
    if (value.type === 'stream_event') {
      const e = value.event || {};
      if (e.type === 'message_start') streamed = false;
      if (e.type === 'content_block_delta' && e.delta?.type === 'text_delta') {
        streamed = true; answered = true; activity = 'Writing response'; return e.delta.text || '';
      }
      if (e.type === 'content_block_start' && e.content_block?.type === 'tool_use') {
        activity = `Running ${e.content_block.name || 'tool'}`;
        return '';
      }
      if (e.type === 'message_stop') return '\n';
      return '';
    }
    if (value.type === 'assistant' && Array.isArray(value.message?.content)) {
      return value.message.content.map(block => {
        if (block.type === 'text') { answered = true; return streamed ? '' : line(block.text || ''); }
        if (block.type === 'tool_use') {
          activity = `Running ${block.name || 'tool'}`;
          const target = block.input?.file_path || block.input?.path;
          return '\n' + line(`› ${activity}${typeof target === 'string' ? ` · ${target}` : ''}`);
        }
        return '';
      }).join('');
    }
    if (value.type === 'user' && Array.isArray(value.message?.content)) {
      return value.message.content.filter(b => b.type === 'tool_result').map(b => {
        activity = b.is_error ? 'Tool reported an error' : 'Tool finished';
        if (!b.is_error) return line('✓ Tool finished');
        const detail = typeof b.content === 'string' ? b.content : '';
        return line(`! Tool error${detail ? `: ${detail}` : ''}`);
      }).join('');
    }
    if (value.type === 'system') {
      if (value.subtype === 'init') { activity = 'Agent connected'; return line('› Agent connected'); }
      if (value.subtype === 'api_retry') { activity = 'Agent retrying its connection'; return line(`› ${activity}`); }
      return '';
    }
    if (value.type === 'result') {
      activity = value.is_error ? 'Agent reported an error' : 'Agent finished';
      const denied = Array.isArray(value.permission_denials) ? value.permission_denials.length : 0;
      if (value.is_error) problem = 'Agent reported an error. Check its output and review any partial edits.';
      if (denied) problem = 'Agent permissions blocked tool operations. Check the output before retrying.';
      return (!answered && typeof value.result === 'string' ? line(value.result) : '') +
        (value.is_error && Array.isArray(value.errors) ? value.errors.filter(e => typeof e === 'string').map(line).join('') : '') +
        (denied ? line(`! ${denied} tool operation(s) denied by agent permissions. Update your agent's permissions locally before retrying.`) : '') +
        line(`› ${activity}`);
    }
    return typeof value.type === 'string' ? diagnostic(JSON.stringify(value)) : null;
  }
  return {
    get activity() { return activity; },
    get problem() { return problem; },
    get diagnostics() { return diagnostics; },
    push(chunk, finished = false) {
      pending += chunk;
      let output = '';
      while (pending.includes('\n')) {
        const end = pending.indexOf('\n'), raw = pending.slice(0, end);
        pending = pending.slice(end + 1);
        try { output += event(JSON.parse(raw)) ?? line(raw); }
        catch { output += isDiagnostic(raw) ? diagnostic(raw) : line(raw); }
      }
      if (pending && (finished || pending.length > 100000 || (!pending.trimStart().startsWith('{') && !pending.trimStart().startsWith('⚠')))) {
        try { output += event(JSON.parse(pending)) ?? pending; }
        catch { output += isDiagnostic(pending) ? diagnostic(pending) : pending; }
        pending = '';
      }
      if (output && activity === 'Waiting for agent output') activity = 'Receiving output';
      return output.replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, '').replace(/[\x00-\x08\x0b-\x1f\x7f]/g, '');
    }
  };
}
