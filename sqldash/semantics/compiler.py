"""Compiles metric queries into SQL — the injection boundary between callers and the
database: callers supply only names and values, SQL text comes only from author YAML."""

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqldash.models.semantics import Grain, MetricDef, TimeDimensionDef
from sqldash.params import bind_marker, code_only, finalize_bind
from sqldash.semantics.layer import ResolvedMetric, SemanticError

GRAINS: tuple[Grain, ...] = ("hour", "day", "week", "month", "quarter", "year")
OPS = ("=", "!=", ">", ">=", "<", "<=", "in")
_FILTER_VALUE_TYPES = (str, bool, int, float, Decimal, date, time, type(None))
_FILTER_SHAPES = 'accepted shapes: "eu", ["eu", "us"], or {op: <op>, value: <scalar or list>}'
_GRAIN_RANK = {"hour": 0, "day": 1, "week": 2, "month": 3, "quarter": 4, "year": 5}
_SPINE_DIALECTS = frozenset({"duckdb", "postgres", "snowflake", "bigquery"})
_INTERVAL_NOUN = {"hour": "hours", "day": "days", "week": "weeks", "month": "months"}
_BACKTICK_DIALECTS = frozenset({"bigquery", "mysql", "mariadb", "databricks", "spark", "hive"})
_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")
_LIMIT_DIALECTS = frozenset(
    {
        "duckdb",
        "sqlite",
        "postgres",
        "mysql",
        "mariadb",
        "redshift",
        "cockroachdb",
        "snowflake",
        "bigquery",
        "databricks",
        "spark",
        "hive",
        "athena",
        "awsathena",
        "trino",
        "presto",
        "clickhouse",
        "vertica",
    }
)
_FETCH_FIRST_DIALECTS = frozenset({"oracle", "db2"})


def dialect_kind(source, override: str | None = None) -> str:
    """The spine dialect: duckdb / postgres / snowflake / bigquery, else the raw type."""
    if override:
        raw = override.lower()
    else:
        raw = (getattr(source, "type", None) or "").lower()
        if not raw:
            url = getattr(source, "url", None) or ""
            raw = str(url).split("://", 1)[0].split("+", 1)[0].lower()
    if raw in ("postgres", "postgresql"):
        return "postgres"
    return raw or "unknown"


def _value_shape(value: Any) -> str:
    """Name the shape a caller actually passed. A filter the compiler cannot bind is
    refused here, so the refusal has to say what arrived, not only that it was wrong."""
    if isinstance(value, dict):
        keys = ", ".join(str(k) for k in value)
        return f"a dict ({keys})" if keys else "an empty dict"
    if isinstance(value, (list, tuple)):
        return "a list"
    return f"a {type(value).__name__}"


def _day_after(end: Any) -> Any:
    """The exclusive upper bound for an end bound that names a whole day, else None.

    An end is inclusive, and `ts <= '2026-08-31'` on a TIMESTAMP stops at that
    day's midnight, so the rest of the day's rows were dropped. A date-only end
    compiles as `ts < <the next day>` instead, which is the same answer on a DATE
    column. An end with a time of day is an instant and keeps `<=`, and so does
    the last day Python can represent (`9999-12-31`, a common open-ended
    sentinel), which has no next day to bind."""
    if isinstance(end, date) and not isinstance(end, datetime):
        return None if end == date.max else end + timedelta(days=1)
    if not isinstance(end, str) or not _DATE_ONLY.fullmatch(end.strip()):
        return None
    try:
        day = date.fromisoformat(end.strip())
    except ValueError:
        return None
    return None if day == date.max else (day + timedelta(days=1)).isoformat()


def _ident(dialect: str, name: str) -> str:
    """Quote an alias the compiler invents so a reserved word (`trailing`, `order`) is
    still an identifier where it is referenced back. Author column exprs are never
    quoted: on Snowflake that would change which column an unquoted name folds to."""
    if dialect in _BACKTICK_DIALECTS:
        return f"`{name}`"
    return f'"{name}"'


_SQLITE_TRUNC = {
    "hour": "strftime('%Y-%m-%d %H:00:00', {x})",
    "day": "date({x})",
    "week": "date({x}, 'weekday 0', '-6 days')",
    "month": "date({x}, 'start of month')",
    "quarter": (
        "date({x}, 'start of month', "
        "printf('-%d months', (CAST(strftime('%m', {x}) AS INTEGER) - 1) % 3))"
    ),
    "year": "date({x}, 'start of year')",
}
# MySQL/MariaDB have no DATE_TRUNC. No DATE_FORMAT either: pymysql is pyformat,
# so a literal % in the pattern would be read as a placeholder once params bind.
# WEEKDAY() is 0 on Monday, matching DATE_TRUNC's ISO weeks.
_MYSQL_TRUNC = {
    "hour": "DATE_ADD(DATE({x}), INTERVAL HOUR({x}) HOUR)",
    "day": "DATE({x})",
    "week": "DATE_SUB(DATE({x}), INTERVAL WEEKDAY({x}) DAY)",
    "month": "DATE_SUB(DATE({x}), INTERVAL DAYOFMONTH({x}) - 1 DAY)",
    "quarter": "DATE_ADD(MAKEDATE(YEAR({x}), 1), INTERVAL QUARTER({x}) - 1 QUARTER)",
    "year": "MAKEDATE(YEAR({x}), 1)",
}
_MYSQL_FAMILY = frozenset({"mysql", "mariadb"})


def _trunc(dialect: str, grain: str, expr: str) -> str:
    """DATE_TRUNC('day', x) is postgres/duckdb/snowflake. BigQuery is reversed and
    Sunday-based for WEEK; SQLite and MySQL/MariaDB have no DATE_TRUNC and build the
    bucket from date functions."""
    if dialect == "bigquery":
        part = "ISOWEEK" if grain == "week" else grain.upper()
        return f"TIMESTAMP_TRUNC({expr}, {part})"
    if dialect == "sqlite":
        return _SQLITE_TRUNC[grain].format(x=expr)
    if dialect in _MYSQL_FAMILY:
        return _MYSQL_TRUNC[grain].format(x=expr)
    return f"DATE_TRUNC('{grain}', {expr})"


def _trunc_bound(dialect: str, grain: str, placeholder: str) -> str:
    """A bound date truncated to `grain`, binding the placeholder exactly once.

    The SQLite and MySQL/MariaDB bucket templates repeat their argument, and a
    repeated placeholder needs a value per occurrence while the caller binds one:
    `quarter` on SQLite and most grains on MySQL failed with a bind-count error.
    Those go through a one-row subquery; every other dialect keeps the inline form."""
    bound = _bound(dialect, placeholder)
    inline = _trunc(dialect, grain, bound)
    if inline.count(placeholder) <= 1:
        return inline
    return f"(SELECT {_trunc(dialect, grain, 'v')} FROM (SELECT {bound} AS v) AS sqldash_start)"


def _bound(dialect: str, placeholder: str) -> str:
    """A bound date as a timestamp. SQLite has no TIMESTAMP type: the CAST takes
    numeric affinity and turns '2026-01-04' into 2026. MySQL/MariaDB cast to DATETIME."""
    if dialect == "sqlite":
        return placeholder
    if dialect in _MYSQL_FAMILY:
        return f"CAST({placeholder} AS DATETIME)"
    return f"CAST({placeholder} AS TIMESTAMP)"


TRUNC_MACRO = "SQLDASH_TRUNC"
# A call, not the bare word: `sqldash_trunc_col` is somebody's column and
# `'SQLDASH_TRUNC'` is somebody's string, and neither may turn a metric that
# compiled before into one the layer refuses. What is left is a literal holding
# `SQLDASH_TRUNC(`, which is reported as a malformed call.
_TRUNC_MACRO_TOKEN = re.compile(rf"\b{TRUNC_MACRO}\s*\(", re.IGNORECASE)
_TRUNC_MACRO_CALL = re.compile(rf"\b{TRUNC_MACRO}\s*\(\s*'([^']*)'\s*,", re.IGNORECASE)


def _macro_argument_end(text: str, start: int) -> int:
    """Index of the `)` that closes the macro call, or -1. The argument is author
    SQL, so it may nest parentheses and carry string literals of its own."""
    depth = 0
    i = start
    while i < len(text):
        char = text[i]
        if char == "'":
            i += 1
            while i < len(text):
                if text[i] == "'":
                    if text[i + 1 : i + 2] != "'":
                        break
                    i += 1
                i += 1
        elif char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                return i
            depth -= 1
        i += 1
    return -1


def expand_trunc_macro(dialect: str, text: str) -> str:
    """Resolve `SQLDASH_TRUNC('<grain>', <expr>)` in author SQL to this dialect's
    truncation.

    A metrics.yaml is portable: the same file can be pointed at another source, so
    author SQL that needs a time bucket inside an expression cannot spell one
    warehouse's `DATE_TRUNC` and still run on the next one. The macro is how an
    expression says "bucket this" without naming a dialect; `_trunc` decides the
    spelling at compile time, exactly as it does for a query's grain.

    Calls are found in `code_only(text)` and sliced out of the real text, so a
    macro the author commented out is prose: it is not expanded, and a bad grain
    or an unreadable call inside a comment is not an error (#675). A `--` inside
    a string literal is not a comment, and a literal holding `SQLDASH_TRUNC(`
    stays a malformed call, both because the spans come from the one scanner.
    """
    code = code_only(text)
    if not _TRUNC_MACRO_TOKEN.search(code):
        return text
    out: list[str] = []
    cursor = 0
    while (match := _TRUNC_MACRO_CALL.search(code, cursor)) is not None:
        end = _macro_argument_end(code, match.end())
        if end < 0:
            break
        grain = match.group(1).strip().lower()
        if grain not in GRAINS:
            raise SemanticError(
                f"{TRUNC_MACRO} grain '{match.group(1)}' is not one of: {', '.join(GRAINS)}"
            )
        inner = expand_trunc_macro(dialect, text[match.end() : end]).strip()
        if not inner:
            raise SemanticError(f"{TRUNC_MACRO}('{grain}', ...) has no expression to truncate")
        out.append(text[cursor : match.start()])
        out.append(_trunc(dialect, grain, inner))
        cursor = end + 1
    out.append(text[cursor:])
    expanded = "".join(out)
    if _TRUNC_MACRO_TOKEN.search(code_only(expanded)):
        raise SemanticError(
            f"could not read {TRUNC_MACRO} in: {text.strip()} — it takes a quoted grain "
            f"and an expression, as in {TRUNC_MACRO}('week', ordered_at)"
        )
    return expanded


def expand_metric_sql(metric: MetricDef, dialect: str) -> MetricDef:
    """The metric with every author SQL field's truncation macros resolved.

    One of the two seams author SQL leaves sqldash through; see ``exported_sql``
    for the rule about which surfaces must use them."""
    texts = [metric.expr or "", *metric.filters, *(d.expr or "" for d in metric.dimensions)]
    if metric.time_dimension is not None:
        texts.append(metric.time_dimension.expr or "")
    if not any(_TRUNC_MACRO_TOKEN.search(code_only(text)) for text in texts):
        return metric
    update: dict[str, Any] = {
        "expr": expand_trunc_macro(dialect, metric.expr) if metric.expr else metric.expr,
        "filters": [expand_trunc_macro(dialect, snippet) for snippet in metric.filters],
        "dimensions": [
            dim
            if dim.expr is None
            else dim.model_copy(update={"expr": expand_trunc_macro(dialect, dim.expr)})
            for dim in metric.dimensions
        ],
    }
    if metric.time_dimension is not None and metric.time_dimension.expr:
        update["time_dimension"] = metric.time_dimension.model_copy(
            update={"expr": expand_trunc_macro(dialect, metric.time_dimension.expr)}
        )
    return metric.model_copy(update=update)


def exported_sql(dialect: str, text: str | None) -> str | None:
    """Author SQL on its way out to something else that will execute it.

    Every exporter calls this — or ``expand_metric_sql`` for a whole metric — on
    every SQL body it writes, with the dialect of the system that will run it: a
    LookML `sql:`, a Cortex `expr:` or `base_table.definition:`. The macro is our
    own spelling and no warehouse defines it, so a body that skips this step is
    valid-looking YAML that fails when the other tool runs it. That is a silent
    failure in someone else's product, which is why it goes through a named seam
    rather than being remembered at each site (#628).

    A surface that only *describes* a definition back to a human or an agent must
    NOT call this. There the author's own text is the honest answer, and
    ``describes_macro`` is how that surface says the text is not runnable.
    """
    if text is None:
        return None
    return expand_trunc_macro(dialect, text)


def describes_macro(*texts: str | None) -> bool:
    """True when author SQL being shown (not exported) carries the macro, so the
    surface can say it is not SQL the reader can run.

    A commented-out call does not count: the note says the definition will not
    run if copied into raw SQL, and a definition that only mentions the macro in
    a comment runs fine (#675)."""
    return any(_TRUNC_MACRO_TOKEN.search(code_only(text)) for text in texts if text)


MACRO_NOTE = (
    f"uses {TRUNC_MACRO}('<grain>', ...), sqldash's dialect-neutral time bucket. "
    "sqldash resolves it per source when the metric runs; it is not a warehouse "
    "function, so it will not run if copied into raw SQL."
)


def _lookback_start(dialect: str, expr: str, count: int, unit: str) -> str:
    """First contributing instant: trunc(start) minus n-1 units.

    Shifting the raw start leaves a mid-unit date cutting the first spine
    bucket in half, so the same displayed month/week under-counts.
    """
    return _shift_back(dialect, _trunc(dialect, unit, expr), count - 1, unit)


def _shift_back(dialect: str, expr: str, count: int, unit: str) -> str:
    if dialect in ("duckdb", "postgres"):
        return f"({expr}) - INTERVAL '{count} {_INTERVAL_NOUN[unit]}'"
    if dialect == "snowflake":
        return f"DATEADD('{unit}', -{count}, {expr})"
    if dialect == "bigquery":
        return f"TIMESTAMP_SUB({expr}, INTERVAL {count} {unit.upper()})"
    raise SemanticError(
        f"trailing windows need a date spine; '{dialect}' is not one of "
        "duckdb, postgres, snowflake, bigquery"
    )


def _date_spine(dialect: str, start_sql: str, end_sql: str, unit: str) -> str:
    if dialect not in _SPINE_DIALECTS:
        raise SemanticError(
            f"trailing windows need a date spine; '{dialect}' is not one of "
            "duckdb, postgres, snowflake, bigquery"
        )
    if dialect in ("duckdb", "postgres"):
        return f"SELECT d FROM generate_series({start_sql}, {end_sql}, INTERVAL '1 {unit}') AS t(d)"
    if dialect == "bigquery":
        if unit == "hour":
            return (
                f"SELECT ts AS d FROM UNNEST(GENERATE_TIMESTAMP_ARRAY("
                f"{start_sql}, {end_sql}, INTERVAL 1 HOUR)) AS ts"
            )
        return (
            f"SELECT TIMESTAMP(d) AS d FROM UNNEST(GENERATE_DATE_ARRAY("
            f"DATE({start_sql}), DATE({end_sql}), INTERVAL 1 {unit.upper()})) AS d"
        )
    return (
        f"SELECT {start_sql} AS d\n"
        f"UNION ALL\n"
        f"SELECT DATEADD('{unit}', 1, d) FROM sqldash_spine "
        f"WHERE DATEADD('{unit}', 1, d) <= {end_sql}"
    )


@dataclass
class MetricQuery:
    """What a caller may ask of a metric — dimension names, a grain, filter values,
    a time range, and a limit. Never SQL: names are validated against the metric's
    declared dimensions and values become bind parameters in compile_metric."""

    dimensions: tuple[str, ...] = ()
    grain: Grain | None = None
    filters: dict[str, Any] = field(default_factory=dict)
    time_range: tuple[Any | None, Any | None] | None = None
    limit: int | None = None


def _wrap_trailing(
    inner: str,
    *,
    resolved: ResolvedMetric,
    query: MetricQuery,
    kind: str,
    count: int,
    unit: str,
    inner_grain: str,
    display_grain: str,
    display_start,
    bind: list[Any],
    placeholder,
) -> str:
    metric = resolved.definition
    assert metric.time_dimension is not None
    alias = _ident(kind, resolved.name.rsplit("/", 1)[-1])
    time_alias = _ident(kind, metric.time_dimension.name)
    dim_aliases = [_ident(kind, d) for d in query.dimensions]
    partition = f"PARTITION BY {', '.join(dim_aliases)} " if dim_aliases else ""
    outer = ", ".join([time_alias, *dim_aliases])

    start = end = None
    if query.time_range is not None:
        start, end = query.time_range
    if start not in (None, ""):
        bind.append(start)
        spine_start = _lookback_start(kind, f"CAST({placeholder()} AS TIMESTAMP)", count, unit)
    else:
        spine_start = f"(SELECT MIN({time_alias}) FROM sqldash_fine)"
    if end not in (None, ""):
        after = _day_after(end)
        if after is not None and inner_grain == "hour":
            bind.append(after)
            spine_end = _shift_back(kind, f"CAST({placeholder()} AS TIMESTAMP)", 1, "hour")
        else:
            bind.append(end)
            spine_end = _trunc(kind, inner_grain, f"CAST({placeholder()} AS TIMESTAMP)")
    else:
        spine_end = f"(SELECT MAX({time_alias}) FROM sqldash_fine)"

    if dim_aliases:
        dim_list = ", ".join(dim_aliases)
        keys = f"sqldash_keys AS (\nSELECT DISTINCT {dim_list} FROM sqldash_fine\n),\n"
        join_on = " AND ".join([f"f.{time_alias} = s.d", *[f"f.{d} = k.{d}" for d in dim_aliases]])
        filled = (
            f"SELECT s.d AS {time_alias}, {', '.join(f'k.{d}' for d in dim_aliases)}, "
            f"COALESCE(f.{alias}, 0) AS {alias}\n"
            f"FROM sqldash_spine s\nCROSS JOIN sqldash_keys k\n"
            f"LEFT JOIN sqldash_fine f ON {join_on}"
        )
    else:
        keys = ""
        filled = (
            f"SELECT s.d AS {time_alias}, COALESCE(f.{alias}, 0) AS {alias}\n"
            f"FROM sqldash_spine s\nLEFT JOIN sqldash_fine f ON f.{time_alias} = s.d"
        )
    windowed = (
        f"SELECT {outer}, "
        f"SUM({alias}) OVER ({partition}ORDER BY {time_alias} "
        f"ROWS BETWEEN {count - 1} PRECEDING AND CURRENT ROW) AS {alias}\n"
        f"FROM sqldash_filled"
    )
    with_kw = "WITH RECURSIVE" if kind == "snowflake" else "WITH"
    sql = (
        f"{with_kw} sqldash_fine AS (\n{inner}\n),\n"
        f"{keys}"
        f"sqldash_spine AS (\n{_date_spine(kind, spine_start, spine_end, unit)}\n),\n"
        f"sqldash_filled AS (\n{filled}\n),\n"
        f"sqldash_windowed AS (\n{windowed}\n)\n"
    )
    if display_grain != inner_grain:
        dim_sel = (", " + ", ".join(dim_aliases)) if dim_aliases else ""
        part = f"{_trunc(kind, display_grain, time_alias)}" + dim_sel
        sql += (
            f"SELECT {_trunc(kind, display_grain, time_alias)} AS {time_alias}"
            f"{dim_sel}, {alias}\n"
            f"FROM (\n"
            f"  SELECT {time_alias}{dim_sel}, {alias},\n"
            f"    ROW_NUMBER() OVER (PARTITION BY {part} ORDER BY {time_alias} DESC) "
            f"AS sqldash_rn\n"
            f"  FROM sqldash_windowed\n"
            f") t\nWHERE sqldash_rn = 1"
        )
    else:
        sql += "SELECT * FROM sqldash_windowed"
    if display_start is not None:
        bind.append(display_start)
        clause = (
            f"{time_alias} >= {_trunc(kind, display_grain, f'CAST({placeholder()} AS TIMESTAMP)')}"
        )
        sql += f" AND {clause}" if display_grain != inner_grain else f"\nWHERE {clause}"
    return sql + f"\nORDER BY {time_alias}"


def bucket_input(kind: str, time_dimension: TimeDimensionDef) -> str:
    """The time expr a grain truncates. On Snowflake a `timezone: session` column is
    read as TIMESTAMP_LTZ first, so every row is cut in the session zone rather than
    at its own offset; the other dialects already truncate a timestamptz that way."""
    if time_dimension.timezone == "session" and kind == "snowflake":
        return f"CAST({time_dimension.sql_expr} AS TIMESTAMP_LTZ)"
    return time_dimension.sql_expr


def _time_select(
    resolved: ResolvedMetric,
    metric: MetricDef,
    query: MetricQuery,
    kind: str,
    trailing: bool,
    unit: str,
) -> tuple[Grain | None, Grain | None, list[str]]:
    """The display grain, the grain the inner query buckets by, and the bucket column.

    A trailing window buckets by its own unit and lets the wrapper roll up to the
    query grain, so a query grain finer than the unit is refused."""
    wants_time = query.grain is not None or query.time_range is not None or trailing
    if not wants_time:
        return None, None, []
    if metric.time_dimension is None:
        raise SemanticError(
            f"metric '{resolved.name}' has no time_dimension — cannot query by time"
        )
    grain = query.grain or metric.time_dimension.grain
    if grain not in GRAINS:
        raise SemanticError(f"unknown grain '{grain}' — valid grains: {', '.join(GRAINS)}")
    if query.grain is None:
        return grain, None, []
    if trailing:
        if _GRAIN_RANK[query.grain] < _GRAIN_RANK[unit]:
            raise SemanticError(
                f"metric '{resolved.name}' window is in {unit}s — "
                f"query grain '{query.grain}' is finer"
            )
        inner_grain: Grain = unit  # type: ignore[assignment]
    else:
        inner_grain = grain
    bucket = (
        f"{_trunc(kind, inner_grain, bucket_input(kind, metric.time_dimension))}"
        f" AS {_ident(kind, metric.time_dimension.name)}"
    )
    return grain, inner_grain, [bucket]


def _dimension_select(
    resolved: ResolvedMetric, metric: MetricDef, query: MetricQuery, kind: str
) -> list[str]:
    """One aliased column per requested dimension; a name the metric does not declare
    is refused."""
    select: list[str] = []
    for name in query.dimensions:
        dim = metric.dimension(name)
        if dim is None:
            valid = ", ".join(d.name for d in metric.dimensions) or "(none declared)"
            raise SemanticError(
                f"metric '{resolved.name}' has no dimension '{name}' — valid dimensions: {valid}"
            )
        select.append(f"{dim.sql_expr} AS {_ident(kind, dim.name)}")
    return select


def _filter_operand(name: str, value: Any) -> tuple[str, Any]:
    """The op and value a caller's filter asks for, refused by shape before any SQL is
    built: binding is not validation, and a dict does bind."""
    op = "="
    if isinstance(value, dict):
        if not value.keys() <= {"op", "value"} or not value:
            raise SemanticError(
                f"filter '{name}' is {_value_shape(value)}, which is not a filter "
                f"value — {_FILTER_SHAPES}"
            )
        op = str(value.get("op", "=")).lower()
        value = value.get("value")
        if op not in OPS:
            raise SemanticError(f"unsupported filter op '{op}' — valid ops: {', '.join(OPS)}")
    if value is None or (isinstance(value, str) and not value.strip()):
        # `x = NULL` is never true in SQL, so binding this returns the
        # aggregate over zero rows — a confident NULL with no error. The
        # dashboard path treats an empty filter as inactive and never gets
        # here, so a value that did arrive was passed deliberately, and the
        # caller who means "no filter" can omit it.
        raise SemanticError(
            f"filter '{name}' has no value — omit it to leave the dimension "
            f"unfiltered; comparing to NULL matches nothing and returns an "
            f"empty aggregate rather than an error"
        )
    for item in value if isinstance(value, (list, tuple)) else (value,):
        if not isinstance(item, _FILTER_VALUE_TYPES):
            raise SemanticError(
                f"filter '{name}' has {_value_shape(item)} where a value was "
                f"expected — {_FILTER_SHAPES}"
            )
    if isinstance(value, (list, tuple)) and op not in ("=", "in"):
        raise SemanticError(
            f"filter '{name}' has op '{op}' with a list value, and a list only "
            f"compiles to IN — use {{op: in, value: [...]}} to match any of the "
            f"values, or pass a single value to compare with '{op}'"
        )
    return op, value


def _filter_where(
    metric: MetricDef,
    query: MetricQuery,
    bind: list[Any],
    placeholder,
) -> list[str]:
    """One predicate per caller filter. Names are checked against the declared
    dimensions and every value becomes a bind parameter."""
    where: list[str] = []
    for name, raw in query.filters.items():
        dim = metric.dimension(name)
        if dim is None:
            valid = ", ".join(d.name for d in metric.dimensions) or "(none declared)"
            raise SemanticError(f"unknown filter dimension '{name}' — valid dimensions: {valid}")
        op, value = _filter_operand(name, raw)
        if isinstance(value, (list, tuple)) or op == "in":
            values = list(value) if isinstance(value, (list, tuple)) else [value]
            if not values:
                raise SemanticError(f"filter '{name}': 'in' requires a non-empty list")
            marks = []
            for v in values:
                bind.append(v)
                marks.append(placeholder())
            where.append(f"{dim.sql_expr} IN ({', '.join(marks)})")
        else:
            bind.append(value)
            where.append(f"{dim.sql_expr} {op} {placeholder()}")
    return where


def _from_clause(resolved: ResolvedMetric, kind: str) -> str:
    if resolved.relation.table:
        return resolved.relation.table
    relation_sql = expand_trunc_macro(kind, (resolved.relation.sql or "").strip())
    return f"(\n{relation_sql}\n) AS base"


def _as_of_end(resolved: ResolvedMetric, query: MetricQuery, count: int, unit: str):
    """The end bound of a grainless trailing window, or None to end at the latest row.
    A start bound has no meaning there and is refused."""
    start = end = None
    if query.time_range is not None:
        start, end = query.time_range
    if start not in (None, ""):
        raise SemanticError(
            f"metric '{resolved.name}' with a window and no grain is the last "
            f"{count} {unit}s as of the end — omit start, or query with a grain"
        )
    return None if end in (None, "") else end


def _as_of_end_where(
    metric: MetricDef,
    kind: str,
    count: int,
    unit: str,
    end: Any,
    bind: list[Any],
    placeholder,
) -> list[str]:
    assert metric.time_dimension is not None
    ts = metric.time_dimension.sql_expr
    after = _day_after(end)
    bound, lo, hi = (end, ">", "<=") if after is None else (after, ">=", "<")
    bind.append(bound)
    end_sql = f"CAST({placeholder()} AS TIMESTAMP)"
    bind.append(bound)
    end_sql_hi = f"CAST({placeholder()} AS TIMESTAMP)"
    return [f"{ts} {lo} {_shift_back(kind, end_sql, count, unit)}", f"{ts} {hi} {end_sql_hi}"]


def _as_of_latest_sql(
    metric: MetricDef,
    query: MetricQuery,
    kind: str,
    count: int,
    unit: str,
    from_clause: str,
    where: list[str],
    alias: str,
) -> str:
    """A grainless trailing window with no end: the last `count` units up to the
    latest row the filters leave."""
    assert metric.time_dimension is not None
    ts = metric.time_dimension.sql_expr
    extra = (" WHERE " + " AND ".join(where)) if where else ""
    dim_prefix = "".join(
        f"{metric.dimension(d).sql_expr} AS {_ident(kind, d)}, " for d in query.dimensions
    )
    group = ""
    if query.dimensions:
        positions = ", ".join(str(i + 1) for i in range(len(query.dimensions)))
        group = f"\nGROUP BY {positions}"
    return (
        f"WITH sqldash_base AS (\nSELECT * FROM {from_clause}{extra}\n),\n"
        f"sqldash_asof AS (SELECT MAX({ts}) AS as_of FROM sqldash_base)\n"
        f"SELECT {dim_prefix}{metric.expr} AS {alias}\n"
        f"FROM sqldash_base, sqldash_asof\n"
        f"WHERE {ts} > {_shift_back(kind, 'as_of', count, unit)} "
        f"AND {ts} <= as_of{group}"
    )


def _time_range_where(
    metric: MetricDef,
    query: MetricQuery,
    kind: str,
    count: int,
    unit: str,
    running_total: bool,
    trailing_buckets: bool,
    bind: list[Any],
    placeholder,
) -> tuple[list[str], Any, Any]:
    """Time-range predicates, plus the start a running total filters on after its
    window and the start a trailing wrapper displays from.

    A running total / trailing window has to see rows before the displayed
    start. WHERE is evaluated before window functions, so filtering the
    start here would shrink the frame. The end bound stays: rows after the
    window must not contribute to it."""
    assert metric.time_dimension is not None
    ts = metric.time_dimension.sql_expr
    where: list[str] = []
    start_after_window = display_start = None
    start, end = query.time_range
    if start not in (None, ""):
        if running_total:
            start_after_window = start
        elif trailing_buckets:
            bind.append(start)
            lookback = _lookback_start(kind, f"CAST({placeholder()} AS TIMESTAMP)", count, unit)
            where.append(f"{ts} >= {lookback}")
            display_start = start
        else:
            bind.append(start)
            where.append(f"{ts} >= {placeholder()}")
    if end not in (None, ""):
        after = _day_after(end)
        bind.append(end if after is None else after)
        where.append(f"{ts} {'<=' if after is None else '<'} {placeholder()}")
    return where, start_after_window, display_start


def _select_sql(select: list[str], from_clause: str, where: list[str], group_count: int) -> str:
    sql = "SELECT " + ", ".join(select) + f"\nFROM {from_clause}"
    if where:
        sql += "\nWHERE " + " AND ".join(where)
    if group_count:
        positions = ", ".join(str(i + 1) for i in range(group_count))
        sql += f"\nGROUP BY {positions}\nORDER BY {positions}"
    return sql


def _wrap_running_total(
    inner: str,
    *,
    metric: MetricDef,
    query: MetricQuery,
    kind: str,
    alias: str,
    grain: Grain | None,
    group_count: int,
    start_after_window: Any,
    bind: list[Any],
    placeholder,
) -> str:
    assert metric.time_dimension is not None
    time_alias = _ident(kind, metric.time_dimension.name)
    dim_aliases = [_ident(kind, d) for d in query.dimensions]
    partition = f"PARTITION BY {', '.join(dim_aliases)} " if dim_aliases else ""
    outer_cols = ", ".join([time_alias, *dim_aliases]) if group_count else time_alias
    running = (
        f"SELECT {outer_cols}, "
        f"SUM({alias}) OVER ({partition}ORDER BY {time_alias} "
        f"ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS {alias}\n"
        f"FROM sqldash_buckets"
    )
    if start_after_window is None:
        sql = f"WITH sqldash_buckets AS (\n{inner}\n)\n{running}"
    else:
        # The window needs its own level: a WHERE beside it is applied
        # first and takes the earlier buckets back out of the frame. Two
        # sibling CTEs rather than a nested WITH, which Snowflake rejects
        # inside a CTE body.
        bind.append(start_after_window)
        sql = (
            f"WITH sqldash_buckets AS (\n{inner}\n),\n"
            f"sqldash_running AS (\n{running}\n)\n"
            f"SELECT * FROM sqldash_running\n"
            f"WHERE {time_alias} >= {_trunc_bound(kind, grain, placeholder())}"
        )
    return sql + f"\nORDER BY {time_alias}"


def _limit_clause(query: MetricQuery, kind: str) -> str:
    """The row cap in the dialect's own syntax. SQL Server has only TOP and an
    OFFSET/FETCH that needs an ORDER BY, and a dialect not known to take LIMIT
    could reject it, so those compile no clause and the executor's fetch cap,
    which reads at most the cap plus one row, does the limiting."""
    if query.limit is None:
        return ""
    try:
        limit = int(query.limit)
    except (TypeError, ValueError) as exc:
        raise SemanticError(f"limit must be an integer, got {query.limit!r}") from exc
    if limit <= 0:
        raise SemanticError("limit must be positive")
    if kind in _LIMIT_DIALECTS:
        return f"\nLIMIT {limit}"
    if kind in _FETCH_FIRST_DIALECTS:
        return f"\nFETCH FIRST {limit} ROWS ONLY"
    return ""


def compile_metric(
    resolved: ResolvedMetric,
    query: MetricQuery,
    paramstyle: str,
    *,
    dialect: str | None = None,
) -> tuple[str, list[Any]]:
    """Compile a query against a resolved metric into ``(sql, bind)``.

    Security invariant: this is the SQL-injection boundary. Untrusted callers (UI,
    HTTP API, MCP agents) pass only metric/dimension *names* — checked against the
    metric's declared dimensions — plus whitelisted ops and grains, filter and
    time-range *values* that always become bind parameters, and a limit that is
    ``int()``-coerced before inlining. The only SQL text spliced in verbatim comes
    from the author-trusted YAML definition (exprs, static filters, the relation).

    Time buckets go through ``_trunc``, which knows the dialects without a
    postgres-style DATE_TRUNC (bigquery, sqlite, mysql/mariadb). Author SQL that
    needs a bucket *inside* an expression writes ``SQLDASH_TRUNC('<grain>', expr)``
    and gets the same treatment through ``expand_trunc_macro``.
    Trailing windows also need a date-spine dialect (duckdb/postgres/snowflake/
    bigquery); ``dialect`` overrides the source type.
    Cumulative metrics queried with a grain get wrapped in a running-total window
    over the bucketed inner query.
    """
    kind = dialect_kind(resolved.source, dialect)
    metric = expand_metric_sql(resolved.definition, kind)
    bind: list[Any] = []

    def placeholder() -> str:
        return bind_marker(paramstyle, len(bind) - 1)

    window_spec = metric.window_spec
    trailing = window_spec is not None
    count, unit = window_spec if window_spec else (0, "day")

    grain, inner_grain, select = _time_select(resolved, metric, query, kind, trailing, unit)
    select += _dimension_select(resolved, metric, query, kind)
    group_count = len(select)
    alias = _ident(kind, resolved.name.rsplit("/", 1)[-1])
    select.append(f"{metric.expr} AS {alias}")

    where = [f"({snippet})" for snippet in metric.filters]
    where += _filter_where(metric, query, bind, placeholder)
    from_clause = _from_clause(resolved, kind)

    running_total = bool(metric.cumulative and query.grain is not None)
    trailing_buckets = bool(trailing and query.grain is not None)
    start_after_window = display_start = None
    if trailing and query.grain is None:
        end = _as_of_end(resolved, query, count, unit)
        if end is None:
            sql = _as_of_latest_sql(metric, query, kind, count, unit, from_clause, where, alias)
            return finalize_bind(paramstyle, sql + _limit_clause(query, kind), bind)
        where += _as_of_end_where(metric, kind, count, unit, end, bind, placeholder)
    elif query.time_range is not None:
        bounds, start_after_window, display_start = _time_range_where(
            metric, query, kind, count, unit, running_total, trailing_buckets, bind, placeholder
        )
        where += bounds

    sql = _select_sql(select, from_clause, where, group_count)
    if trailing_buckets:
        sql = _wrap_trailing(
            sql,
            resolved=resolved,
            query=query,
            kind=kind,
            count=count,
            unit=unit,
            inner_grain=inner_grain or unit,
            display_grain=grain or unit,
            display_start=display_start,
            bind=bind,
            placeholder=placeholder,
        )
    elif running_total:
        sql = _wrap_running_total(
            sql,
            metric=metric,
            query=query,
            kind=kind,
            alias=alias,
            grain=grain,
            group_count=group_count,
            start_after_window=start_after_window,
            bind=bind,
            placeholder=placeholder,
        )
    return finalize_bind(paramstyle, sql + _limit_clause(query, kind), bind)
