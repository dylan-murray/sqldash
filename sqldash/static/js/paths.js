export function dashboardPath(name) {
  return String(name).split("/").map(encodeURIComponent).join("/");
}
