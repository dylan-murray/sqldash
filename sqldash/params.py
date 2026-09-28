"""The {{param}} / {% if param %} SQL template dialect — values always become native
bind parameters, never string interpolation.

Date tokens, filter defaults, and dashboard-param binding live here too so
semantics/ and the exporter never reach into api/. Runtime imports are
stdlib-only apart from `sqltext`, which imports nothing of ours; models are
TYPE_CHECKING so this module cannot cycle."""

import math
import re
from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqldash.sqltext import COMMENT, blank

if TYPE_CHECKING:
    from sqldash.models.dashboard import Dashboard, FilterDef

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


CONDITION = re.compile(r"\{%\s*(?:if|elif)\s+([A-Za-z_][A-Za-z0-9_]*)\s*%\}")
TEMPLATE_TAG = re.compile(r"\{%.*?%\}", re.DOTALL)
BLOCK_TAG = re.compile(
    r"\{%\s*(?:(?P<branch>if|elif)\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)|(?P<plain>else|endif))\s*%\}",
    re.DOTALL,
)
SUPPORTED_TAGS = "{% if param %}...{% elif param %}...{% else %}...{% endif %} blocks"
SUPPORTED_FORMS = f"{{{{ param }}}} placeholders and {SUPPORTED_TAGS}"
TAG_OPENER = re.compile(r"\{\{|\{%")
BRACE_PAIR = re.compile(r"\{\{|\}\}")
INTEGER_TEXT = re.compile(r"[+-]?[0-9]+")


class ParamError(ValueError):
    pass


def code_only(sql: str) -> str:
    """`sql` with comment text blanked, offsets and line breaks intact.

    Anything that reads a construct *sqldash* owns out of author SQL matches
    against this and indexes back into the original: every template pattern in
    this module, and `SQLDASH_TRUNC` in `semantics.compiler`. So `-- {{ region }}`
    is prose the author commented out and not a placeholder to bind (#659), and
    a commented-out macro call is not a call (#675). The warehouse throws the
    comment away; binding a value for it left the query one argument over and the
    error named nothing. Comment spans come from `sqltext`, the one scanner that
    knows a `--` inside a string literal is not a comment.
    """
    return blank(sql, (COMMENT,))


def _sub_in_code(pattern: re.Pattern, sql: str, replace: Callable[[re.Match], str]) -> str:
    code = code_only(sql)
    out: list[str] = []
    last = 0
    for match in pattern.finditer(code):
        out.append(sql[last : match.start()])
        out.append(replace(match))
        last = match.end()
    out.append(sql[last:])
    return "".join(out)


def refuse_unreadable_placeholders(sql: str) -> None:
    """Refuse a `{{ ... }}` this dialect cannot read, naming it.

    An empty tag, a dotted path, a tag wrapped around another: substitution only
    ever matched whole declared names, so anything else travelled to the
    warehouse verbatim and came back as the engine's parser error pointing at a
    brace, with nothing to say a template tag was the problem (#660). `_tags`
    already does this for `{% ... %}`.

    Comments and quoted literals are blanked first. Braces in a comment are the
    author's prose, and braces in a string literal are the author's data — a
    DuckDB struct or a JSON blob is not a tag, and refusing one would break SQL
    that runs today.
    """
    code = blank(sql)
    known = {match.start() for match in PLACEHOLDER.finditer(code)}
    for opener in TAG_OPENER.finditer(code):
        if opener.group(0) == "{%" or opener.start() in known:
            continue
        raise ParamError(
            f"unsupported template tag {_placeholder_text(sql, code, opener.start())!r} — "
            f"only {SUPPORTED_FORMS} are supported"
        )


def refuse_placeholders_in_literals(sql: str) -> None:
    """Refuse a whole `{{ name }}` written inside a quoted literal, naming the param.

    Substitution runs over `code_only`, which blanks comments and not literals, so
    `category = '{{ region }}'` became `category = '?'`: a bind marker inside the
    quotes, which the warehouse reads as the one-character string and not as a
    parameter. The value still went out with the statement, so the query carried an
    argument nothing consumed and came back as "Parameter argument/count mismatch"
    naming nothing the author wrote (#678) — the same error #659 got for a
    placeholder one character to the left, inside a comment.

    Refusing rather than blanking. A comment is inert: the warehouse throws it away,
    so leaving the placeholder alone there is the answer the author asked for. Quotes
    are not inert — leaving `'{{ region }}'` as author text runs clean and compares
    the column against those literal characters, which is nothing, so a confusing
    error would become a confident zero rows. And there is nothing here to preserve:
    this shape has never once reached a warehouse successfully, on any paramstyle,
    because the bind count can never match. `docs/dashboard-file.md` promises values
    are bound and never interpolated, so quoting a placeholder is asking for the one
    thing sqldash does not do.

    Narrower than `refuse_unreadable_placeholders`, which leaves braces in a literal
    alone: this fires only on a whole declared-name tag. `'{{ region.foo }}'` and
    `'{{}}'` stay the author's data, because a JSON blob or a DuckDB struct is not a
    tag and those run today.
    """
    in_code = {match.start() for match in PLACEHOLDER.finditer(blank(sql))}
    for match in PLACEHOLDER.finditer(code_only(sql)):
        if match.start() in in_code:
            continue
        raise ParamError(
            f"parameter '{match.group(1)}' is inside a string literal — sqldash binds "
            "parameter values as native query parameters and never interpolates them "
            "into text, so drop the quotes around the placeholder"
        )


def _placeholder_text(sql: str, code: str, start: int) -> str:
    """The tag to name in the error, or its first 40 characters when it never closes.

    `{{` counts depth so a tag wrapped around another is quoted whole — naming
    the first `}}` would report `{{ {{ region }}` and read as the wrong tag.
    """
    depth = 0
    for brace in BRACE_PAIR.finditer(code, start):
        depth += 1 if brace.group(0) == "{{" else -1
        if depth == 0:
            return sql[start : brace.end()]
    return sql[start : start + 40]


def _tags(sql: str):
    """Every template tag in order, as (match, keyword, param name or None).

    Tags are found in `code_only(sql)`, so a `{% else %}` the author commented
    out is not a branch and does not have to belong to anything (#659). Offsets
    still index the original text, which is what `render_conditionals` slices
    the kept branches out of, comments and all.

    A tag that is not one of the four block keywords over a bare param name is
    refused here, naming the tag the author actually wrote — that is the whole
    dialect, and an expression (`{% if a == 'x' %}`) is not part of it."""
    for match in TEMPLATE_TAG.finditer(code_only(sql)):
        tag = BLOCK_TAG.fullmatch(match.group(0))
        if tag is None:
            raise ParamError(
                f"unsupported template tag {match.group(0)!r} — only {SUPPORTED_TAGS} are supported"
            )
        yield match, (tag["branch"] or tag["plain"]), tag["name"]


def render_conditionals(
    sql: str, values: dict[str, Any], inactive: frozenset[str] = frozenset()
) -> str:
    """Keep one branch of each {% if %} chain: the first whose param has an active,
    non-empty value, else the {% else %} body, else nothing.

    Rendering only chooses among spans of author-written SQL — no value is ever
    substituted here, so a branch's `{{ param }}` reaches `bind_sql` exactly as
    the body of a bare `{% if %}` always has (see decisions, 2026-09-22).
    Chains do not nest, and every error names the offending tag rather than the
    `{% if %}` the author wrote correctly. Unreadable `{{ }}` tags are refused
    over the *whole* template, not the branch that survived, so lint sees one
    inside a block it would have dropped — the same reach `_tags` already has."""
    out: list[str] = []
    cursor = 0
    opener: re.Match | None = None
    keeping = True
    taken = False
    after_else = False
    for match, keyword, name in _tags(sql):
        if keeping:
            out.append(sql[cursor : match.start()])
        cursor = match.end()
        if keyword == "if":
            if opener is not None:
                raise ParamError(
                    f"nested {match.group(0)} inside {opener.group(0)} is not supported — "
                    "a conditional block cannot contain another one"
                )
            opener, after_else = match, False
            keeping = taken = _is_active(name, values, inactive)
            continue
        if opener is None:
            raise ParamError(
                f"{match.group(0)} has no {{% if param %}} to belong to — "
                f"only {SUPPORTED_TAGS} are supported"
            )
        if keyword == "endif":
            opener, keeping = None, True
            continue
        if after_else:
            raise ParamError(
                f"{match.group(0)} comes after the {{% else %}} of {opener.group(0)} — "
                "a block has one {% else %} and it is last"
            )
        after_else = keyword == "else"
        keeping = not taken and (after_else or _is_active(name, values, inactive))
        taken = taken or keeping
    if opener is not None:
        raise ParamError(f"{opener.group(0)} is never closed — add {{% endif %}}")
    out.append(sql[cursor:])
    refuse_unreadable_placeholders(sql)
    return "".join(out)


def _is_active(name: str | None, values: dict[str, Any], inactive: frozenset[str]) -> bool:
    return name in values and values[name] not in (None, "") and name not in inactive


def validate_template(sql: str) -> str | None:
    """Return what is wrong with the template, or None when it is well-formed.

    This is the whole-template view lint wants, so it also checks every branch for
    a placeholder inside a string literal. Rendering deliberately does not: a
    branch that is not taken never reaches the warehouse, and refusing it anyway
    broke a dashboard whose `{% else %}` renders fine while its filter is off
    (#678 review). Lint names the latent mistake; the page keeps working."""
    try:
        render_conditionals(sql, {})
        refuse_placeholders_in_literals(sql)
    except ParamError as exc:
        return str(exc)
    return None


def extract_params(sql: str) -> list[str]:
    """`{{ name }}`, `{% if name %}` and `{% elif name %}` names in first-appearance
    order, deduplicated.

    A param used only in a condition is still a filter — collecting only
    placeholders left those toggles always inactive, and an `{% elif %}` name
    missing from this list would leave that branch permanently unreachable. A
    name that appears only inside a comment is not a param at all.
    """
    code = code_only(sql)
    seen: list[str] = []
    marks = sorted(
        (*PLACEHOLDER.finditer(code), *CONDITION.finditer(code)),
        key=lambda m: m.start(),
    )
    for match in marks:
        name = match.group(1)
        if name not in seen:
            seen.append(name)
    return seen


def condition_params(sql: str) -> list[str]:
    """`{% if name %}` / `{% elif name %}` names: params that decide a branch rather
    than bind a value.

    The rendered SQL cannot tell you these applied. A filter that gates a clause
    leaves no placeholder behind either way, so a surface reasoning about what
    narrowed a run from the bound values alone misses it entirely (#680).
    """
    code = code_only(sql)
    seen: list[str] = []
    for match in CONDITION.finditer(code):
        if match.group(1) not in seen:
            seen.append(match.group(1))
    return seen


def bind_sql(sql: str, values: dict[str, Any], paramstyle: str) -> tuple[str, list[Any]]:
    """Replace placeholders with the dialect's bind markers, expanding list values
    into parenthesized IN lists. Placeholders inside a comment are left alone, and
    one inside a string literal is refused rather than quoted into the text."""
    refuse_unreadable_placeholders(sql)
    refuse_placeholders_in_literals(sql)
    bind: list[Any] = []

    def replace(match: re.Match) -> str:
        name = match.group(1)
        if name not in values:
            raise ParamError(f"missing value for parameter '{name}'")
        value = values[name]
        if isinstance(value, (list, tuple)):
            placeholders = [bind_marker(paramstyle, len(bind) + i) for i in range(len(value))]
            bind.extend(value)
            return "(" + ", ".join(placeholders) + ")"
        bind.append(value)
        return bind_marker(paramstyle, len(bind) - 1)

    return finalize_bind(paramstyle, _sub_in_code(PLACEHOLDER, sql, replace), bind)


PERCENT_STYLES = ("pyformat", "format")
PERCENT_MARKER = "\x00sqldash-bind\x00"


def bind_marker(paramstyle: str, index: int) -> str:
    """The marker for bind `index`. A percent style gets a stand-in that
    `finalize_bind` turns into `%s`, so it can tell our markers from the
    author's own `%`."""
    if paramstyle == "qmark":
        return "?"
    if paramstyle in PERCENT_STYLES:
        return PERCENT_MARKER
    if paramstyle == "numeric":
        return f":{index + 1}"
    if paramstyle == "numeric_dollar":
        return f"${index + 1}"
    if paramstyle == "named":
        return f":p{index + 1}"
    raise ParamError(f"unsupported paramstyle '{paramstyle}'")


def finalize_bind(
    paramstyle: str, sql: str, bind: list[Any]
) -> tuple[str, list[Any] | dict[str, Any]]:
    """Finish SQL built with `bind_marker`, and shape the binds for the driver.

    A pyformat/format driver reads every `%` in the text once any value is
    bound, so the author's `LIKE 'A%'` (or a `%` in a comment) failed as soon
    as a filter was set, while the same SQL ran unfiltered. With binds, each
    literal `%` is doubled and only our markers become `%s`; without, the
    driver never interpolates and the text goes as written. 'named' drivers
    take the binds as a mapping."""
    if paramstyle in PERCENT_STYLES:
        if bind:
            sql = sql.replace("%", "%%")
        sql = sql.replace(PERCENT_MARKER, "%s")
    if paramstyle == "named":
        return sql, {f"p{i + 1}": value for i, value in enumerate(bind)}
    return sql, bind


RELATIVE_DATE = re.compile(r"^-(\d+)([dwmy])$")
DAYS = {"d": 1, "w": 7, "m": 30, "y": 365}
PRESET_ALIASES = {"mtd": "month_to_date", "ytd": "year_to_date"}


def resolve_relative_start(token: str, today: date | None = None) -> tuple[date, str] | None:
    """Resolve a relative-date token (-30d, last_N_days, mtd/ytd) to (start, canonical name).

    A count that reaches back past year 1 names no date, so it is not a token:
    `-99999999d` raised OverflowError out of every caller (a 500 on /api/run, a
    traceback in the CLI); as an unrecognized date it gets the named error."""
    token = PRESET_ALIASES.get(token.strip(), token.strip())
    today = today or date.today()
    match = RELATIVE_DATE.match(token)
    if match:
        days = int(match.group(1)) * DAYS[match.group(2)]
        start = _days_before(today, days)
        return None if start is None else (start, f"last_{days}_days")
    match = re.match(r"^last_(\d+)_days$", token)
    if match:
        start = _days_before(today, int(match.group(1)))
        return None if start is None else (start, token)
    if token == "month_to_date":
        return today.replace(day=1), token
    if token == "year_to_date":
        return today.replace(month=1, day=1), token
    return None


def _days_before(today: date, days: int) -> date | None:
    if days > (today - date.min).days:
        return None
    return today - timedelta(days=days)


def resolve_daterange_preset(preset: str, today: date | None = None) -> dict[str, str] | None:
    """The one definition of what a preset means. `presetRange` in
    `static/js/period.js` mirrors it for the filter bar, against the same day."""
    today = today or date.today()
    resolved = resolve_relative_start(preset, today)
    if resolved is None:
        return None
    start, canonical = resolved
    return {"preset": canonical, "start": start.isoformat(), "end": today.isoformat()}


_DATE_FORMS = (
    "use an ISO date (YYYY-MM-DD) or a relative token: -30d, last_30_days, mtd, ytd, today"
)


def as_date_text(value: Any) -> Any:
    """Unquoted 20260101 is a YAML int. Token resolution is string-only."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def resolve_default(filter_def: "FilterDef") -> Any:
    """Materialize a filter's declared default into concrete values, as of today."""
    default = filter_def.default
    if default is None:
        return None
    if filter_def.type == "daterange":
        if not isinstance(default, dict):
            default = as_date_text(default)
        if isinstance(default, str):
            resolved = resolve_daterange_preset(default)
            if resolved is not None:
                return resolved
        if isinstance(default, dict):
            return {k: str(v) for k, v in default.items()}
        return None
    if filter_def.type == "date":
        default = as_date_text(default)
        if isinstance(default, str):
            if default.strip() == "today":
                return date.today().isoformat()
            resolved = resolve_relative_start(default)
            if resolved is not None:
                return resolved[0].isoformat()
    if hasattr(default, "isoformat"):
        return default.isoformat()
    return default


def coerce_value(value: Any, filter_def: "FilterDef | None", role: str | None = None) -> Any:
    """Normalize a caller-supplied value the way a declared default would be.

    Relative-date tokens are documented as working "everywhere", but they were
    only resolved for a filter's default — a caller passing `-30d` or `mtd`
    got the literal string bound into SQL and a conversion error from the
    warehouse."""
    if filter_def is None:
        return value
    if filter_def.type in ("date", "daterange"):
        return _coerce_date(value, window_end=filter_def.type == "daterange" and role == "end")
    if filter_def.type == "number":
        return finite_number(value, f"'{filter_def.name}'")
    return value


def finite_number(value: Any, label: str) -> int | float:
    """A number param's value, exactly as given.

    An integer stays an int: `float()` rounds past 2**53, so account
    9007199254740993 bound as 9007199254740992. nan and inf are refused:
    they bind fine and then match nothing (`>= nan`) or everything
    (`>= -inf`), a wrong answer with no error. A string that names an
    integral float (`12.0`, `1e3`) becomes an int, as it always has."""
    number = value
    if isinstance(value, str):
        text = value.strip()
        if INTEGER_TEXT.fullmatch(text):
            return int(text)
        try:
            number = float(text)
        except ValueError:
            number = None
        if number is not None and math.isfinite(number) and number.is_integer():
            return int(number)
    if isinstance(number, bool) or not isinstance(number, int | float):
        raise ParamError(f"{label} must be a finite number, got {value!r}")
    if isinstance(number, float) and not math.isfinite(number):
        raise ParamError(f"{label} must be a finite number, got {value!r}")
    return number


def _coerce_date(value: Any, *, window_end: bool) -> Any:
    """A date override from JSON can be any type. Validate it like the string
    form, so a number or a bool gets the named error instead of binding into
    SQL as INTEGER/BOOLEAN and failing in the warehouse."""
    if hasattr(value, "isoformat"):
        value = value.isoformat()
    value = as_date_text(value)
    if not isinstance(value, str):
        raise ParamError(f"unrecognized date {value!r}: {_DATE_FORMS}")
    return resolve_date_token(value, window_end=window_end)


def resolve_date_token(value: Any, *, window_end: bool = False) -> Any:
    """Turn a relative-date token into a concrete ISO date, or pass it through.

    Shared by every path that accepts a date — filter values, `metric query
    --start/--end`, MCP `query_metric`. It lives on its own precisely because
    those paths drifted apart once already: the token resolution was reachable
    only through a FilterDef, so the metric paths bound `-30d` into SQL as a
    literal and the warehouse rejected it (#68, then #89).

    `window_end=True` marks the end of a range named by a single token: `-30d`
    means "the last 30 days", so its start is 30 days ago and its end is today.

    An empty string is deliberately passed through rather than normalized to
    None. Normalizing would change nothing on the metric paths — `compile_metric`
    has always dropped a bound that is None or "" — but on the dashboard path
    None binds as NULL into author SQL, and `x BETWEEN a AND NULL` is never true,
    so a loud "invalid date field format" becomes a confident 0 rows."""
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped:
        if value == "":
            return value
        raise ParamError(
            f"unrecognized date {value!r} — use an ISO date (YYYY-MM-DD) or a "
            "relative token: -30d, last_30_days, mtd, ytd, today"
        )
    if stripped.lower() == "today":
        return date.today().isoformat()
    resolved = resolve_relative_start(stripped)
    if resolved is not None:
        if window_end:
            return date.today().isoformat()
        start, _ = resolved
        return start.isoformat()
    slashed = re.fullmatch(r"(\d{4})/(\d{1,2})/(\d{1,2})([T ].+)?", stripped)
    if slashed is not None:
        stripped = f"{slashed[1]}-{slashed[2]}-{slashed[3]}{slashed[4] or ''}"
    if _is_iso_date(stripped):
        return stripped
    raise ParamError(
        f"unrecognized date {value!r} — use an ISO date (YYYY-MM-DD, optional "
        "HH:MM[:SS] time and attached Z/+HH:MM offset, as DuckDB's TIMESTAMP cast "
        "reads it) or a relative token: -30d, last_30_days, mtd, ytd, today"
    )


ISO_DATE_TIME = re.compile(
    r"(\d{4})-(\d{1,2})-(\d{1,2})"
    r"(?:(?:T\s*|\s+)(\d{1,2}):(\d{1,2})"
    r"(?::(\d{1,2})(?:\.(\d*))?(Z|[+-]\d{2}(?::?\d{2})?)?)?)?"
)
_DAYS_IN_MONTH = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def _iso_parts(value: str) -> re.Match | None:
    """Match the dashed ISO forms DuckDB's `CAST(? AS TIMESTAMP)` takes.

    The grammar is the cast's, read off a live DuckDB, not the docs or
    `fromisoformat` (see the gotcha — the two disagree both ways). The
    date is validated as a calendar date by hand: year 0000 is a valid
    proleptic year to the warehouse and a `ValueError` to `datetime`.
    The separator is `T` (with optional whitespace after it) or any run
    of whitespace. Hour, minute and second are one or two digits. An
    offset (`Z`, `+HH`, `+HHMM`, `+HH:MM`) is only read after seconds
    and only when attached — DuckDB refuses `10:30Z` and `10:30:00 +00:00`.
    Hour 24 is end-of-day and rolls over, only when the rest is zero.
    """
    m = ISO_DATE_TIME.fullmatch(value)
    if m is None:
        return None
    year, month, day = int(m[1]), int(m[2]), int(m[3])
    if not 1 <= month <= 12 or not 1 <= day <= _DAYS_IN_MONTH[month - 1]:
        return None
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    if month == 2 and day == 29 and not leap:
        return None
    if m[4] is None:
        return m
    hour, minute, second = int(m[4]), int(m[5]), int(m[6] or 0)
    if hour == 24:
        if minute or second or (m[7] or "")[:6].strip("0"):
            return None
        return m
    if hour > 23 or minute > 59 or second > 59:
        return None
    return m


def _is_iso_date(value: str) -> bool:
    """A dashed calendar date DuckDB's TIMESTAMP cast accepts, with an optional
    time. Compact (`YYYYMMDD`), week, ordinal, dotted and month-name forms
    are not ISO to any warehouse and stay rejected."""
    return _iso_parts(value) is not None


def _window_instant(value: Any) -> datetime | None:
    """A resolved date bound as a comparable naive-UTC instant, or None.

    Anything that reaches here already passed `resolve_date_token`, so it
    is `ISO_DATE_TIME`-shaped. Hour 24 rolls forward the way the warehouse
    rolls it; an offset is folded in so a mixed pair still compares."""
    if not isinstance(value, str):
        return None
    m = _iso_parts(value.strip())
    if m is None:
        return None
    try:
        instant = datetime(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None
    if m[4] is None:
        return instant
    hour, minute, second = int(m[4]), int(m[5]), int(m[6] or 0)
    micro = int((m[7] or "0")[:6].ljust(6, "0"))
    shift = timedelta(hours=hour, minutes=minute, seconds=second, microseconds=micro)
    offset = m[8]
    if offset and offset != "Z":
        sign = 1 if offset[0] == "+" else -1
        digits = offset[1:].replace(":", "")
        shift -= sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:4] or 0))
    try:
        return instant + shift
    except OverflowError:
        return None


def check_date_window(
    start: Any,
    end: Any,
    *,
    start_name: str = "start",
    end_name: str = "end",
    given: tuple[Any, Any] = (None, None),
) -> None:
    """Refuse a window whose start is after its end.

    Both bounds bind in the order given, so `>= later AND <= earlier` is a
    confident empty result on every surface — a dash in the tile, `null` on
    the CLI, `[[null]]` over MCP — and nothing says why. An unknown grain or
    dimension is refused with a message; an inverted window is at least as
    common a slip and was the only one of the three that answered quietly.
    Equal bounds are a one-day window and stay valid. Tokens are compared
    after resolution, and named alongside the dates they became."""
    first, last = _window_instant(start), _window_instant(end)
    if first is None or last is None or first <= last:
        return
    origin = ""
    if all(isinstance(g, str) for g in given) and tuple(given) != (start, end):
        origin = f" (from {given[0]!r} and {given[1]!r})"
    raise ParamError(
        f"date range is inverted: {start_name} {start!r} is after {end_name} {end!r}{origin} "
        "— swap them, or pick an end on or after the start"
    )


def daterange_scalar_token(filter_def: "FilterDef") -> str | None:
    """A valid single date used as a daterange default — not a range, not a typo."""
    if filter_def.type != "daterange":
        return None
    default = filter_def.default
    if default is None or isinstance(default, dict):
        return None
    text = as_date_text(default)
    if hasattr(text, "isoformat") and not isinstance(text, str):
        text = text.isoformat()
    if not isinstance(text, str):
        return None
    if resolve_daterange_preset(text) is not None:
        return None
    try:
        resolve_date_token(text)
    except ParamError:
        return None
    return text


def missing_params_message(dashboard: "Dashboard", missing: list[str]) -> str:
    """User-facing missing-param text. A declared single-date daterange default
    is not 'no default' — name that, or the message tells them to do what they did."""
    head = "missing parameter value(s): " + ", ".join(missing)
    hints: list[str] = []
    for filter_def in dashboard.filters:
        token = daterange_scalar_token(filter_def)
        if token is None or not filter_def.bind:
            continue
        if not any(param in missing for param in filter_def.bind.values()):
            continue
        hints.append(
            f"filter '{filter_def.name}' default {token!r} is a single date; "
            "a daterange default must be a range preset "
            "(last_N_days, month_to_date, year_to_date) or {start, end}"
        )
    if hints:
        return head + " — " + "; ".join(hints)
    return head + " — pass them in 'params' or declare filter defaults"


def filter_param_roles(filter_def: "FilterDef") -> tuple[tuple[str, str | None], ...]:
    """The `{{ param }}` names one filter defines, each with the window role it fills.

    A daterange defines the two names in its `bind` — authored or derived — and every
    other type defines its own name; `bind` on those types binds nothing at runtime.
    The single derivation every surface must share: a caller that rebuilds it from
    `<name>_start`/`<name>_end` tells an author with a custom bind to declare what
    they already declared (#625).
    """
    if filter_def.type == "daterange":
        return ((filter_def.bind["start"], "start"), (filter_def.bind["end"], "end"))
    return ((filter_def.name, None),)


def filter_param_names(filter_def: "FilterDef") -> tuple[str, ...]:
    return tuple(name for name, _ in filter_param_roles(filter_def))


def filter_params(dashboard: "Dashboard") -> dict[str, tuple["FilterDef", str | None]]:
    """Every param name the dashboard's filters define, mapped to its filter and role."""
    return {name: (f, role) for f in dashboard.filters for name, role in filter_param_roles(f)}


def param_values(
    dashboard: "Dashboard", param_names: list[str], overrides: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Bind each param from overrides first, then filter defaults; the rest come back missing."""
    filters = filter_params(dashboard)

    values: dict[str, Any] = {}
    missing: list[str] = []
    for name in param_names:
        filter_def, role = filters.get(name, (None, None))
        if name in overrides and overrides[name] not in (None, ""):
            values[name] = coerce_value(overrides[name], filter_def, role)
            continue
        if filter_def is not None and filter_def.default is not None:
            default = resolve_default(filter_def)
            if role is not None and isinstance(default, dict):
                default = default.get(role)
            if default is not None:
                # role matters here too: an authored `end: -30d` means the end of
                # that window (today), exactly as it does when passed in. Dropping
                # it made the same token mean 30 days ago in YAML and today
                # everywhere else — a silently narrower range, never an error.
                values[name] = coerce_value(default, filter_def, role)
                continue
            if filter_def.type == "daterange" and filter_def.default is not None:
                text = filter_def.default
                if not isinstance(text, dict):
                    text = as_date_text(text)
                if isinstance(text, str):
                    coerce_value(text, filter_def, role)
        missing.append(name)
    for f in dashboard.filters:
        if f.type != "daterange":
            continue
        start_name, end_name = f.bind["start"], f.bind["end"]
        if start_name in values and end_name in values:
            check_date_window(
                values[start_name],
                values[end_name],
                start_name=start_name,
                end_name=end_name,
                given=(overrides.get(start_name), overrides.get(end_name)),
            )
    return values, missing


def _js_number_text(number: float) -> str:
    """`String(n)` in JavaScript: the shortest round-trip digits, plain between
    1e-6 and 1e21 and exponential outside it (`1e-7`, `1.5e+21`)."""
    if math.isnan(number):
        return "NaN"
    if math.isinf(number):
        return "Infinity" if number > 0 else "-Infinity"
    if number == 0:
        return "0"
    _, digits, exponent = Decimal(repr(abs(number))).as_tuple()
    text = "".join(map(str, digits))
    stripped = text.rstrip("0")
    exponent += len(text) - len(stripped)
    text = stripped.lstrip("0")
    count, point = len(text), len(text) + exponent
    sign = "-" if number < 0 else ""
    if count <= point <= 21:
        return sign + text + "0" * (point - count)
    if 0 < point <= 21:
        return sign + text[:point] + "." + text[point:]
    if -6 < point <= 0:
        return sign + "0." + "0" * -point + text
    tail = f".{text[1:]}" if count > 1 else ""
    shift = point - 1
    return f"{sign}{text[0]}{tail}e{'+' if shift > 0 else '-'}{abs(shift)}"


def option_value(value: Any) -> str:
    """The one spelling of a select option's value, shared with the browser: a
    boolean is `true`/`false` and a number is what `String(n)` gives, so a
    clicked cell, a URL and the rendered option all name it the same way."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _js_number_text(value)
    return str(value)


def option_kind(value: Any) -> str:
    """What an option's YAML scalar is, so the browser only compares it as a
    number or a boolean when the clicked cell is one too: `'100'` is text."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    return "string"


def select_choices(filter_def: "FilterDef") -> list[Any]:
    """Options shown in the UI. `all` is the off-sentinel; if it is the default
    *or there is no default* it must appear even when the authored list omitted
    it, or the browser falls through to the first real option and disagrees
    with CLI/API."""
    if filter_def.options_sql is not None:
        opts = list(filter_def.options or ["all"])
        default = resolve_default(filter_def)
        default = "all" if default is None else option_value(default)
        if default not in {option_value(o) for o in opts}:
            opts.append(default)
        return opts
    opts = list(filter_def.options or [])
    if filter_def.type == "select" and filter_def.default in (None, "all") and "all" not in opts:
        return ["all", *opts]
    return opts


def filter_ui_default(filter_def: "FilterDef") -> Any:
    """Value the filter bar should show. A select with no authored default sits
    on `all` (off), matching CLI/API with empty params — not the first option."""
    resolved = resolve_default(filter_def)
    if filter_def.type == "select" and resolved is None:
        return "all"
    return resolved


def inactive_params(dashboard: "Dashboard", values: dict[str, Any]) -> frozenset[str]:
    """Select filters set to 'all' count as switched off — the documented sentinel,
    whether or not `all` is listed in `options`."""
    inactive = set()
    for f in dashboard.filters:
        if f.type == "select" and values.get(f.name) == "all":
            inactive.add(f.name)
    return frozenset(inactive)


def prepare_sql(
    dashboard: "Dashboard", sql: str, overrides: dict[str, Any]
) -> tuple[str, dict[str, Any], list[str]]:
    """Render {% if %} conditionals (inactive filters count as unset), then rebind
    against only the params the rendered SQL still references."""
    names = extract_params(sql)
    values, _ = param_values(dashboard, names, overrides)
    rendered = render_conditionals(sql, values, inactive_params(dashboard, values))
    needed = extract_params(rendered)
    values, missing = param_values(dashboard, needed, overrides)
    return rendered, values, missing
