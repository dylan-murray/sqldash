export function apiToken() {
  const port = location.port || (location.protocol === "https:" ? "443" : "80");
  const prefix = `sqldash-token-${port}=`;
  for (const part of document.cookie.split("; ")) {
    if (part.startsWith(prefix)) return decodeURIComponent(part.slice(prefix.length));
  }
  return "";
}
