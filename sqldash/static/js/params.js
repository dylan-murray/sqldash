import { COMMENT, blank } from "./sqltext.js";

/* Mirrors extract_params in params.py, which tests/param_corpus.json pins: an
   `{% elif name %}` is a param exactly like an `{% if name %}` name, and a name
   that appears only inside a comment is not one. Drift here is silent: the
   filter bar shows the selection while the server binds the default (#217,
   #671), or a tile re-runs for a value the server ignores (#683). */
const PARAM =
  /\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}|\{%\s*(?:if|elif)\s+([A-Za-z_][A-Za-z0-9_]*)\s*%\}/g;

export function paramNamesIn(sql) {
  const names = [];
  for (const match of blank(sql, [COMMENT]).matchAll(PARAM)) {
    const name = match[1] || match[2];
    if (!names.includes(name)) names.push(name);
  }
  return names;
}
