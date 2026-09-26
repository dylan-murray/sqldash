"""Where SQL comments and quoted literals sit inside a block of SQL text.

One scanner, shared. `sqlguard` blanks both before it classifies keywords, and
`params` needs the same spans so a `{{ tag }}` the author commented out is left
alone. Two scanners would disagree eventually, and the first disagreement is a
`--` inside a string literal being read as a comment.

`static/js/sqltext.js` is a line-for-line port the browser uses to find the same
params (#683). Change a rule here and change it there; `tests/param_corpus.json`
fails the side you forgot.

Imports nothing from sqldash, so any module may use it.
"""

import re

COMMENT = "comment"
STRING = "string"
NOISE = (COMMENT, STRING)

_IDENT_START = "A-Za-z_\x80-\U0010ffff"
_DOLLAR_QUOTE = re.compile(rf"\$(?:[{_IDENT_START}][{_IDENT_START}0-9]*)?\$")
_POSITIONAL = re.compile(r"\$[0-9]+")
_WORD = re.compile(rf"[{_IDENT_START}][{_IDENT_START}0-9$]*|[0-9][{_IDENT_START}0-9]*")
_ESCAPE_PREFIX = re.compile(r"[0-9]*[eE]")
_CONTINUATION = re.compile(
    r"(?:[ \t\f]|--[^\n\r]*+)*+[\n\r](?:[ \t\n\r\f]|--[^\n\r]*+[\n\r])*+(?=')"
)
_LINE_COMMENT = re.compile(r"--[^\n\r]*")


_LINE_END = re.compile(r"[\r\n]")


def noise_spans(sql: str) -> list[tuple[int, int, str]]:
    """`(start, end, kind)` for every comment and quoted literal, in order.

    A `--` or `/*` inside a quoted literal does not open a comment and a quote
    inside a comment does not open a literal, so both have to be found in one
    left-to-right pass. `''`/`""` inside a literal is an escaped quote, not the
    end of it. In an escape string (`E'...'`) a backslash also escapes the next
    character, and that rule carries into any `'...'` continuing it after a
    newline. A `--` comment ends at `\r` or `\n`, and block comments nest, both
    as DuckDB's lexer reads them (#703).

    An unterminated block comment or literal is deliberately *not* a span: it is
    SQL the warehouse will reject either way, and treating it as running to the
    end of the text would hide everything after it from every caller.
    """
    spans: list[tuple[int, int, str]] = []
    index, size = 0, len(sql)
    while index < size:
        char = sql[index]
        if char == "-" and sql.startswith("--", index):
            end = _LINE_END.search(sql, index)
            end = size if end is None else end.start()
            spans.append((index, end, COMMENT))
            index = end
            continue
        if char == "/" and sql.startswith("/*", index):
            end = _block_comment_end(sql, index)
            if end >= 0:
                spans.append((index, end, COMMENT))
                index = end
                continue
        elif char in "'\"":
            end = _closing_quote(sql, index)
            if end >= 0:
                spans.append((index, end, STRING))
                index = end
                continue
        elif char == "$":
            index = _dollar(sql, index, spans)
            continue
        elif word := _WORD.match(sql, index):
            index = word.end()
            if sql.startswith("'", index) and _ESCAPE_PREFIX.fullmatch(word.group()):
                index = _escape_string(sql, index, spans)
            continue
        index += 1
    return spans


def _block_comment_end(sql: str, start: int) -> int:
    """Index just past the `*/` closing the block comment opened at `start`, or -1.

    DuckDB nests block comments, so `/* a /* b */ c */` is one comment. Ending at
    the first `*/` left ` c */` looking like code, and a quote in that stretch
    then opened a literal DuckDB never sees, hiding real SQL from the guard
    (#703). A quote inside a comment is not special, so only the markers count.
    """
    depth, index = 0, start
    while index < len(sql):
        if sql.startswith("/*", index):
            depth += 1
            index += 2
        elif sql.startswith("*/", index):
            depth -= 1
            index += 2
            if depth == 0:
                return index
        else:
            index += 1
    return -1


def _dollar(sql: str, start: int, spans: list[tuple[int, int, str]]) -> int:
    opener = _DOLLAR_QUOTE.match(sql, start)
    if opener is None:
        positional = _POSITIONAL.match(sql, start)
        return positional.end() if positional else start + 1
    close = sql.find(opener.group(), opener.end())
    if close < 0:
        return opener.end()
    end = close + len(opener.group())
    spans.append((start, end, STRING))
    return end


def _escape_string(sql: str, opener: int, spans: list[tuple[int, int, str]]) -> int:
    found: list[tuple[int, int, str]] = []
    start, quote = opener - 1, opener
    while True:
        end = _closing_quote(sql, quote, backslash=True)
        if end < 0:
            return opener
        found.append((start, end, STRING))
        gap = _CONTINUATION.match(sql, end)
        if gap is None:
            spans.extend(found)
            return end
        for comment in _LINE_COMMENT.finditer(sql, end, gap.end()):
            found.append((comment.start(), comment.end(), COMMENT))
        start = quote = gap.end()


def _closing_quote(sql: str, start: int, *, backslash: bool = False) -> int:
    quote = sql[start]
    index, size = start + 1, len(sql)
    while index < size:
        if backslash and sql[index] == "\\":
            index += 2
        elif sql[index] != quote:
            index += 1
        elif index + 1 < size and sql[index + 1] == quote:
            index += 2
        else:
            return index + 1
    return -1


def blank(sql: str, kinds: tuple[str, ...] = NOISE) -> str:
    """`sql` with every span of those kinds overwritten by spaces.

    Lengths are preserved, so a match found in the blanked text indexes straight
    back into the original; newlines survive so line-oriented patterns still see
    the same lines.
    """
    spans = [span for span in noise_spans(sql) if span[2] in kinds]
    if not spans:
        return sql
    out = list(sql)
    for start, end, _ in spans:
        for index in range(start, end):
            if out[index] != "\n":
                out[index] = " "
    return "".join(out)
