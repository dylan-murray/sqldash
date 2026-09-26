const FORMULA_TRIGGERS = ['=', '+', '-', '@', '\t', '\r'];
const PLAIN_NUMBER = /^[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$/;

export function spreadsheetSafe(value) {
  if (typeof value !== 'string' || !FORMULA_TRIGGERS.some(lead => value.startsWith(lead))) return value;
  return PLAIN_NUMBER.test(value) ? value : `'${value}`;
}

export function csvText(header, rows) {
  const flat = value => value !== null && typeof value === 'object' ? JSON.stringify(value) : value;
  const cell = value => '"' + String(spreadsheetSafe(flat(value)) ?? '').replaceAll('"', '""') + '"';
  return [header, ...rows].map(row => row.map(cell).join(',')).join('\r\n');
}
