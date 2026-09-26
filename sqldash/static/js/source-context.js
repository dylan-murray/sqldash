export function baseSource(value) {
  try {
    if (value?.startsWith('@role:')) {
      const [base] = JSON.parse(value.slice(6));
      return typeof base === 'string' ? base : value;
    }
    if (value?.startsWith('@context:')) {
      const {source} = JSON.parse(value.slice(9));
      return typeof source === 'string' ? source : value;
    }
  } catch { /* an unreadable key is shown as itself */ }
  return value;
}

export const hasSourceContext = value => Boolean(value?.startsWith('@role:') || value?.startsWith('@context:'));

export function sourceDatabase(value) {
  if (!value?.startsWith('@context:')) return null;
  try { const {database} = JSON.parse(value.slice(9)); return typeof database === 'string' ? database : null; }
  catch { return null; }
}
