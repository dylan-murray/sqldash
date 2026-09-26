/* A line-for-line port of sqldash/sqltext.py. The browser has to agree with the
   server on where a comment is, and the rules are not a regex: a `--` inside a
   literal is not a comment, block comments nest, and an E'...' string escapes
   with a backslash across continuation lines (#701, #703). tests/param_corpus.json
   holds both sides to the same answers (#683). */

export const COMMENT = "comment";
export const STRING = "string";

const IDENT = "A-Za-z_\\u0080-\\u{10FFFF}";
const DOLLAR_QUOTE = new RegExp(`\\$(?:[${IDENT}][${IDENT}0-9]*)?\\$`, "uy");
const POSITIONAL = /\$[0-9]+/y;
const WORD = new RegExp(`[${IDENT}][${IDENT}0-9$]*|[0-9][${IDENT}0-9]*`, "uy");
const ESCAPE_PREFIX = /^[0-9]*[eE]$/;
const CONTINUATION =
  /(?:[ \t\f]|--[^\n\r]*)*[\n\r](?:[ \t\n\r\f]|--[^\n\r]*[\n\r])*(?=')/y;
const LINE_COMMENT = /--[^\n\r]*/g;
const LINE_END = /[\r\n]/g;

function stickyMatch(re, sql, index) {
  re.lastIndex = index;
  return re.exec(sql);
}

export function noiseSpans(sql) {
  const spans = [];
  let index = 0;
  const size = sql.length;
  while (index < size) {
    const char = sql[index];
    if (char === "-" && sql.startsWith("--", index)) {
      LINE_END.lastIndex = index;
      const found = LINE_END.exec(sql);
      const end = found ? found.index : size;
      spans.push([index, end, COMMENT]);
      index = end;
      continue;
    }
    if (char === "/" && sql.startsWith("/*", index)) {
      const end = blockCommentEnd(sql, index);
      if (end >= 0) {
        spans.push([index, end, COMMENT]);
        index = end;
        continue;
      }
    } else if (char === "'" || char === '"') {
      const end = closingQuote(sql, index);
      if (end >= 0) {
        spans.push([index, end, STRING]);
        index = end;
        continue;
      }
    } else if (char === "$") {
      index = dollar(sql, index, spans);
      continue;
    } else {
      const word = stickyMatch(WORD, sql, index);
      if (word) {
        index += word[0].length;
        if (sql.startsWith("'", index) && ESCAPE_PREFIX.test(word[0])) {
          index = escapeString(sql, index, spans);
        }
        continue;
      }
    }
    index += 1;
  }
  return spans;
}

function blockCommentEnd(sql, start) {
  let depth = 0;
  let index = start;
  while (index < sql.length) {
    if (sql.startsWith("/*", index)) {
      depth += 1;
      index += 2;
    } else if (sql.startsWith("*/", index)) {
      depth -= 1;
      index += 2;
      if (depth === 0) return index;
    } else {
      index += 1;
    }
  }
  return -1;
}

function dollar(sql, start, spans) {
  const opener = stickyMatch(DOLLAR_QUOTE, sql, start);
  if (!opener) {
    const positional = stickyMatch(POSITIONAL, sql, start);
    return positional ? start + positional[0].length : start + 1;
  }
  const close = sql.indexOf(opener[0], start + opener[0].length);
  if (close < 0) return start + opener[0].length;
  const end = close + opener[0].length;
  spans.push([start, end, STRING]);
  return end;
}

function escapeString(sql, opener, spans) {
  const found = [];
  let start = opener - 1;
  let quote = opener;
  for (;;) {
    const end = closingQuote(sql, quote, true);
    if (end < 0) return opener;
    found.push([start, end, STRING]);
    const gap = stickyMatch(CONTINUATION, sql, end);
    if (!gap) {
      spans.push(...found);
      return end;
    }
    const gapEnd = end + gap[0].length;
    for (const comment of sql.slice(end, gapEnd).matchAll(LINE_COMMENT)) {
      found.push([end + comment.index, end + comment.index + comment[0].length, COMMENT]);
    }
    start = quote = gapEnd;
  }
}

function closingQuote(sql, start, backslash = false) {
  const quote = sql[start];
  let index = start + 1;
  const size = sql.length;
  while (index < size) {
    if (backslash && sql[index] === "\\") {
      index += 2;
    } else if (sql[index] !== quote) {
      index += 1;
    } else if (index + 1 < size && sql[index + 1] === quote) {
      index += 2;
    } else {
      return index + 1;
    }
  }
  return -1;
}

export function blank(sql, kinds = [COMMENT, STRING]) {
  const spans = noiseSpans(sql).filter((span) => kinds.includes(span[2]));
  if (!spans.length) return sql;
  const out = sql.split("");
  for (const [start, end] of spans) {
    for (let index = start; index < end; index += 1) {
      if (out[index] !== "\n") out[index] = " ";
    }
  }
  return out.join("");
}
