export function withoutPeriod(message) {
  return String(message ?? '').replace(/[\s.]+$/, '');
}
