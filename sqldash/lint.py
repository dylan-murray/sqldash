"""Static checks over project YAML, and the validate_* probes that sit on top.

Lint-clean means well-formed, not runtime-verified. ``validate_metrics`` and
``validate_dashboard`` additionally compile and (when asked) zero-row probe
against the source — they live here so a new linter check is automatically
on the agent surface."""

import difflib
import re
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Literal

from sqlalchemy import create_engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import NoSuchModuleError

from sqldash.connectors.base import (
    CancelToken,
    ConnectionBusy,
    ConnectionLost,
    ConnectorError,
    TableInfo,
)
from sqldash.connectors.engine import paramstyle_for
from sqldash.connectors.engine_urls import DRIVERS, INSTALL_EXTRAS
from sqldash.execution import ExecutionRegistry
from sqldash.models.agents import ToolParam
from sqldash.models.chart import (
    COMBO_CHART_TYPES,
    HEATMAP_FIELDS,
    HISTOGRAM_FIELDS,
    REFERENCE_CHART_TYPES,
)
from sqldash.models.dashboard import split_page_tokens
from sqldash.models.source import is_secret_bag_key
from sqldash.params import (
    ParamError,
    as_date_text,
    bind_sql,
    check_date_window,
    daterange_scalar_token,
    extract_params,
    filter_param_names,
    filter_params,
    prepare_sql,
    render_conditionals,
    resolve_date_token,
    resolve_daterange_preset,
    validate_template,
)
from sqldash.project.drill import UNCLICKABLE_CHARTS, plan_drill
from sqldash.project.sources import (
    attach_dir_missing,
    database_file_missing,
    reads_project_files,
    source_files_dir,
)
from sqldash.project.store import (
    DashboardStore,
    InvalidDashboardError,
    Store,
    parse_dashboard,
    read_dashboard_text,
)
from sqldash.secrets import ENV_REF, SECRET_FIELDS, SecretError, sub_env_refs
from sqldash.semantics.agents import AgentLayer
from sqldash.semantics.bind import bind_resolved
from sqldash.semantics.compiler import dialect_kind, expand_trunc_macro
from sqldash.semantics.layer import (
    ResolvedMetric,
    SemanticError,
    SemanticLayer,
    expand_derived,
    parse_metrics_file,
    resolve_relation,
)
from sqldash.sqlguard import read_only_violation

Level = Literal["error", "warning"]


@dataclass
class Finding:
    file: str
    level: Level
    message: str


SNOWFLAKE_ONLY_FIELDS = (
    "account",
    "warehouse",
    "role",
    "secondary_roles",
    "authentication",
    "token",
    "private_key_path",
    "private_key_passphrase",
)
FILE_ONLY_FIELDS = ("attach_files",)
SERVER_FIELDS = ("host", "port", "username", "password")
WAREHOUSE_FIELDS = ("project", "catalog", "http_path")
FILE_DATABASES = ("duckdb", "sqlite")
KNOWN_EXTERNAL_DIALECTS = frozenset(
    {
        "mysql",
        "mariadb",
        "trino",
        "presto",
        "clickhouse",
        "redshift",
        "bigquery",
        "oracle",
        "mssql",
        "cockroachdb",
        "databricks",
        "athena",
        "vertica",
        "hana",
        "db2",
    }
)


def _set_fields(source, names, skip=()) -> list[str]:
    return [n for n in names if n not in skip and getattr(source, n, None) not in (None, False, {})]


_ENV_SENTINEL = "sqldash_env_ref"


def _is_env_secret(value: str) -> bool:
    """True when the value is only ${env:VAR} refs — before or after substitution."""
    text = value.strip()
    if not text or ENV_REF.fullmatch(text):
        return True
    return text.replace(_ENV_SENTINEL, "") == ""


def _plain_secret_fields(source) -> set[str]:
    named: set[str] = set()
    for field in SECRET_FIELDS:
        value = getattr(source, field, None)
        if value and isinstance(value, str) and not _is_env_secret(value):
            named.add(field)
    return named


def _lint_plaintext_secrets(source, file: str, where: str) -> list[Finding]:
    """Secrets belong in ${env:VAR} or a profile, not in the project file."""
    findings: list[Finding] = []

    def warn_plain(field: str) -> None:
        findings.append(
            Finding(
                file,
                "warning",
                f"{where}: {field} is a plaintext secret — use ${{env:VAR}} or a profile",
            )
        )

    for field in _plain_secret_fields(source):
        warn_plain(field)
    for bag_name in ("connect_args", "options"):
        bag = getattr(source, bag_name, None) or {}
        for key, value in bag.items():
            if not isinstance(key, str) or not is_secret_bag_key(key):
                continue
            if isinstance(value, str) and _is_env_secret(value):
                continue
            warn_plain(f"{bag_name}.{key}")
    if source.url:
        try:
            parsed = make_url(sub_env_refs(source.url, _ENV_SENTINEL))
        except Exception:
            parsed = None
        if parsed is not None:
            if parsed.password and not _is_env_secret(str(parsed.password)):
                warn_plain("url password")
            for key, values in (parsed.query or {}).items():
                if not is_secret_bag_key(key):
                    continue
                for value in values if isinstance(values, (list, tuple)) else (values,):
                    if value and not _is_env_secret(str(value)):
                        warn_plain(f"url query {key}")
    return findings


def lint_source(source, file: str, where: str, files_dir: Path | None = None) -> list[Finding]:
    """Check one source config: required fields per type, ignored-field warnings,
    and a connectionless probe that the SQLAlchemy dialect is installed."""

    findings: list[Finding] = []
    findings.extend(_lint_plaintext_secrets(source, file, where))
    skip_ignored = _plain_secret_fields(source)

    def error(msg):
        findings.append(Finding(file, "error", f"{where}: {msg}"))

    def warn(msg):
        findings.append(Finding(file, "warning", f"{where}: {msg}"))

    if files_dir is not None:
        scan_dir = source_files_dir(source, files_dir)
        missing = attach_dir_missing(source, scan_dir) or database_file_missing(source, scan_dir)
        if missing:
            error(missing)

    def check_dialect_available(stype, dialect):
        try:
            create_engine(f"{dialect}://").dispose()
        except NoSuchModuleError:
            extra = INSTALL_EXTRAS.get(stype)
            hint = f" — pip install 'sqldash[{extra}]'" if extra else ""
            warn(f"'{stype}' needs its SQLAlchemy dialect package installed to run{hint}")
        except ModuleNotFoundError as exc:
            warn(f"'{stype}' needs a driver package installed to run: {exc.name}")
        except Exception:
            pass

    if source.url:
        ignored = _set_fields(
            source,
            SERVER_FIELDS + SNOWFLAKE_ONLY_FIELDS + FILE_ONLY_FIELDS + WAREHOUSE_FIELDS,
            skip=skip_ignored,
        )
        if source.type:
            ignored.insert(0, "type")
        if ignored:
            warn(f"'url' takes precedence — ignored field(s): {', '.join(ignored)}")
        try:
            probe = sub_env_refs(source.url, "env")
            create_engine(probe).dispose()
        except NoSuchModuleError:
            warn(
                f"url dialect is not installed or not a known SQLAlchemy dialect: {source.url.split('://')[0]}"
            )
        except ModuleNotFoundError as exc:
            warn(f"url dialect needs a driver package installed: {exc.name}")
        except Exception as exc:
            error(f"url does not parse: {exc}")
        return findings

    stype = source.type
    if stype == "snowflake":
        if not source.account:
            error("snowflake sources require 'account'")
        ignored = _set_fields(
            source, ("host", "port", *FILE_ONLY_FIELDS, *WAREHOUSE_FIELDS), skip=skip_ignored
        )
        if ignored:
            warn(f"ignored for snowflake: {', '.join(ignored)}")
        return findings

    if stype in FILE_DATABASES:
        ignored = _set_fields(
            source, SERVER_FIELDS + SNOWFLAKE_ONLY_FIELDS + WAREHOUSE_FIELDS, skip=skip_ignored
        )
        if ignored:
            warn(f"ignored for {stype} (file database): {', '.join(ignored)}")
        return findings

    if stype == "bigquery":
        if not source.project and not source.host:
            error("bigquery sources require 'project'")
        ignored = _set_fields(
            source,
            (
                "username",
                "password",
                "port",
                "catalog",
                "http_path",
                *SNOWFLAKE_ONLY_FIELDS,
                *FILE_ONLY_FIELDS,
            ),
            skip=skip_ignored,
        )
        if ignored:
            warn(
                f"ignored for bigquery: {', '.join(ignored)} — auth uses "
                "application-default credentials or options: {credentials_path: ...}"
            )
        check_dialect_available("bigquery", "bigquery")
        return findings

    if stype == "databricks":
        if not source.host or not source.http_path:
            error("databricks sources require 'host' and 'http_path'")
        if not source.token and not source.password and not source.profile:
            error("databricks sources require a 'token' (e.g. token: ${env:DATABRICKS_TOKEN})")
        ignored = _set_fields(
            source,
            (
                "username",
                "database",
                "project",
                "account",
                "warehouse",
                "role",
                "authentication",
                *FILE_ONLY_FIELDS,
            ),
            skip=skip_ignored,
        )
        if ignored:
            warn(f"ignored for databricks (use catalog/schema): {', '.join(ignored)}")
        check_dialect_available("databricks", "databricks")
        return findings

    if stype == "athena":
        if not source.host:
            error("athena sources require 'host' (an AWS region like us-east-1)")
        ignored = _set_fields(
            source,
            (
                "account",
                "warehouse",
                "role",
                "token",
                "authentication",
                "project",
                "catalog",
                "http_path",
                *FILE_ONLY_FIELDS,
            ),
            skip=skip_ignored,
        )
        if ignored:
            warn(f"ignored for athena: {', '.join(ignored)}")
        check_dialect_available("athena", "awsathena+rest")
        return findings

    ignored = _set_fields(
        source, SNOWFLAKE_ONLY_FIELDS + FILE_ONLY_FIELDS + WAREHOUSE_FIELDS, skip=skip_ignored
    )
    if ignored:
        warn(f"ignored for {stype}: {', '.join(ignored)}")
    if not source.database:
        warn(f"'{stype}' sources usually need 'database'")
    dialect = source.driver or DRIVERS.get(stype, stype)
    try:
        create_engine(f"{dialect}://").dispose()
    except NoSuchModuleError:
        base = dialect.split("+")[0]
        candidates = sorted(set(list(DRIVERS) + list(KNOWN_EXTERNAL_DIALECTS) + ["snowflake"]))
        close = difflib.get_close_matches(stype, candidates, n=1, cutoff=0.75)
        if close and close[0] != stype:
            error(f"unknown database type '{stype}' — did you mean '{close[0]}'?")
        elif base in KNOWN_EXTERNAL_DIALECTS:
            extra = INSTALL_EXTRAS.get(stype)
            hint = f" — pip install 'sqldash[{extra}]'" if extra else ""
            warn(f"'{stype}' needs its SQLAlchemy dialect package installed to run{hint}")
        else:
            error(f"unknown database type '{stype}'")
    except ModuleNotFoundError as exc:
        warn(f"'{stype}' needs a driver package installed to run: {exc.name}")
    except Exception:
        pass
    return findings


NON_ADDITIVE_AGGS = re.compile(
    r"\b(AVG|MEDIAN|MIN|MAX|STDDEV\w*|VAR\w*|PERCENTILE\w*|MODE|ANY_VALUE)\s*\(|"
    r"\bCOUNT\s*\(\s*DISTINCT\b",
    re.IGNORECASE,
)


def lint_cumulative(name: str, definition, file: str) -> list[Finding]:
    """Warn when a running total or trailing window uses a non-additive aggregate."""
    if definition.cumulative:
        kind = "cumulative metric"
        hint = "a running total"
    elif getattr(definition, "window", None):
        kind = "trailing-window metric"
        hint = "a trailing window"
    else:
        return []
    if not definition.expr:
        return []
    match = NON_ADDITIVE_AGGS.search(definition.expr)
    if not match:
        return []
    return [
        Finding(
            file,
            "warning",
            f"{kind} '{name}': expr uses {match.group(0).strip().rstrip('(')}"
            f" — {hint} only makes sense for additive expressions "
            "(SUM/COUNT); the accumulated values will be misleading",
        )
    ]


def _metric_sql_texts(definition) -> list[str | None]:
    """Every field of a metric the compiler splices author SQL from."""
    texts: list[str | None] = [definition.expr, definition.sql, definition.derived]
    texts.extend(definition.filters)
    texts.extend(d.expr for d in definition.dimensions)
    if definition.time_dimension is not None:
        texts.append(definition.time_dimension.expr)
    return texts


def lint_trunc_macros(label: str, texts, file: str) -> list[Finding]:
    """Findings for `SQLDASH_TRUNC(...)` calls that will not compile.

    The macro is resolved when a metric compiles, and lint does not compile
    metrics, so an unknown grain or a call the expander cannot read reached the
    author as a failed query instead of a lint error. Reading it costs a string
    scan and needs neither a source nor a dry run. The expansion is thrown away:
    it runs through the real expander, on an arbitrary dialect, because what is
    wrong with a bad grain or an unreadable call is the same on all of them and a
    second parser here would drift from the one that matters.
    """
    findings: list[Finding] = []
    seen: set[str] = set()
    for text in texts:
        if not text:
            continue
        try:
            expand_trunc_macro("duckdb", text)
        except SemanticError as exc:
            message = f"{label}: {exc}"
            if message not in seen:
                seen.add(message)
                findings.append(Finding(file, "error", message))
    return findings


def lint_metric_trunc_macros(name: str, definition, file: str) -> list[Finding]:
    return lint_trunc_macros(f"metric '{name}'", _metric_sql_texts(definition), file)


def lint_relation_trunc_macros(relations, file: str) -> list[Finding]:
    findings: list[Finding] = []
    for name, relation in (relations or {}).items():
        findings.extend(lint_trunc_macros(f"relation '{name}'", [relation.sql], file))
    return findings


_UNRECOGNIZED_DATE = re.compile(r"unrecognized date ('[^']*')")


_REFRESH = re.compile(r"^(\d+)(s|m|h)$")
_REFRESH_UNIT_MS = {"s": 1000, "m": 60_000, "h": 3_600_000}


def _lint_refresh(file: str, refresh) -> list[Finding]:
    if refresh is None:
        return []
    token = str(refresh).strip()
    match = _REFRESH.fullmatch(token)
    if match is None:
        return [
            Finding(
                file,
                "error",
                f"refresh {token!r} is not valid — use 30s, 5m, or 1h",
            )
        ]
    ms = int(match.group(1)) * _REFRESH_UNIT_MS[match.group(2)]
    if ms < 5000:
        return [
            Finding(
                file,
                "warning",
                f"refresh {token!r} is below the 5s floor; the browser will use 5s",
            )
        ]
    return []


def _lint_date_default(file: str, name: str, value, *, window_end: bool = False) -> list[Finding]:
    value = as_date_text(value)
    if not isinstance(value, str):
        return []
    try:
        resolve_date_token(value, window_end=window_end)
    except ParamError as exc:
        return [Finding(file, "error", f"filter '{name}' default: {exc}")]
    return []


def _lint_filter_default(file: str, filter_def) -> list[Finding]:
    """Resolve authored date defaults the same way the runtime does.

    `resolve_default` stringifies YAML ints (dict roles and scalars), so an
    unquoted `20240101` becomes `'20240101'` and then `resolve_date_token`
    raises. Lint must do that too — skipping non-strings left those files
    lint-clean while the dashboard failed on open.
    """
    default = filter_def.default
    if default is None:
        return []
    if filter_def.type == "date":
        return _lint_date_default(file, filter_def.name, default)
    if filter_def.type != "daterange":
        return []
    token = daterange_scalar_token(filter_def)
    if token is not None:
        return [
            Finding(
                file,
                "error",
                f"filter '{filter_def.name}' daterange default {token!r} is a single date — "
                "use a range preset (last_N_days, month_to_date, year_to_date) or {start, end}",
            )
        ]
    if not isinstance(default, dict):
        default = as_date_text(default)
    if isinstance(default, str):
        if resolve_daterange_preset(default) is not None:
            return []
        try:
            resolve_date_token(default)
        except ParamError as exc:
            return [Finding(file, "error", f"filter '{filter_def.name}' default: {exc}")]
        return []
    if isinstance(default, dict):
        findings: list[Finding] = []
        for role in ("start", "end"):
            if role not in default:
                findings.append(
                    Finding(
                        file,
                        "error",
                        f"filter '{filter_def.name}' daterange default has no '{role}'",
                    )
                )
                continue
            findings.extend(
                _lint_date_default(
                    file, filter_def.name, str(default[role]), window_end=role == "end"
                )
            )
        if not findings:
            try:
                check_date_window(
                    resolve_date_token(str(default["start"])),
                    resolve_date_token(str(default["end"]), window_end=True),
                    given=(str(default["start"]), str(default["end"])),
                )
            except ParamError as exc:
                findings.append(
                    Finding(file, "error", f"filter '{filter_def.name}' default: {exc}")
                )
        return findings
    return []


_NO_VALUE_FOR = re.compile(r"no value for ([^—]+)")


def _params_explained_by_lint(params: set[str], lint_errors: list[str], dashboard) -> set[str]:
    """Bind params whose missing role lint already reported on that filter."""
    explained: set[str] = set()
    for filter_def in dashboard.filters:
        if filter_def.type != "daterange" or not filter_def.bind:
            continue
        prefix = f"filter '{filter_def.name}' daterange default"
        scalar = any(prefix in err and "is a single date" in err for err in lint_errors)
        for role, param in filter_def.bind.items():
            if param not in params:
                continue
            needle = f"{prefix} has no '{role}'"
            if scalar or any(needle in err for err in lint_errors):
                explained.add(param)
    return explained


def _sql_error_already_linted(sql_error: str, lint_errors: list[str], dashboard) -> bool:
    """Drop a dry-run date error only when lint already named the same defect."""
    token = _UNRECOGNIZED_DATE.search(sql_error)
    if token is not None:
        return any(token.group(0) in err for err in lint_errors)
    missing = _NO_VALUE_FOR.search(sql_error)
    if missing is None:
        return False
    params = {part.strip() for part in missing.group(1).split(",") if part.strip()}
    return bool(params) and _params_explained_by_lint(params, lint_errors, dashboard) == params


@dataclass
class _TileUsage:
    """What the tile pass learned that the orphan pass needs."""

    queries: set[str] = dataclass_field(default_factory=set)
    metric_dimensions: set[str] = dataclass_field(default_factory=set)
    any_metric_with_time: bool = False
    has_metric_tiles: bool = False


def lint_dashboard(
    dashboard, file: str, project_metrics: dict | None = None, files_dir: Path | None = None
) -> list[Finding]:
    """Every check `sqldash lint` applies to one dashboard, on an already-parsed
    Dashboard — so the MCP validator can run exactly what the CLI runs.

    Each concern is its own pass that returns findings; this is the order they
    print in, and the only place that order is decided."""
    project_metrics = project_metrics or {}
    available = dict(project_metrics)
    available.update(dict.fromkeys(dashboard.metrics))
    findings = _lint_dashboard_sources(dashboard, file, files_dir)
    for metric_name, definition in dashboard.metrics.items():
        findings.extend(lint_cumulative(metric_name, definition, file))
        findings.extend(lint_metric_trunc_macros(metric_name, definition, file))
    findings.extend(lint_relation_trunc_macros(dashboard.relations, file))
    findings.extend(_lint_filters(dashboard, file))
    findings.extend(_lint_cross_filters(dashboard, file))
    findings.extend(_lint_query_templates(dashboard, file))
    tile_findings, usage = _lint_tiles(dashboard, file, available, project_metrics)
    findings.extend(tile_findings)
    findings.extend(_lint_query_writes(dashboard, file))
    findings.extend(_lint_orphans(dashboard, file, usage))
    findings.extend(_lint_css_scope(dashboard, file))
    return findings


def _lint_css_scope(dashboard, file: str) -> list[Finding]:
    css = getattr(dashboard, "css", None)
    if not css:
        return []
    style = split_page_tokens(css)
    findings = []
    if style.dropped:
        message = "css: page-level declarations that cannot apply were dropped: " + "; ".join(
            style.dropped
        )
        if any("is a rule nested in" in reason for reason in style.dropped):
            message += (
                ". a rule nested in :root, html or body cannot apply — write it at the "
                "top level of css: or nest it inside :scope"
            )
        findings.append(Finding(file, "warning", message))
    if style.ignored:
        findings.append(
            Finding(
                file,
                "warning",
                "css: page rules nested inside another block do nothing ("
                + "; ".join(style.ignored)
                + "); a page selector only counts at the top level of css:, so move "
                "it out of the block it sits in, or set the token directly "
                "(`--page: ...`)",
            )
        )
    if style.unmatched:
        findings.append(
            Finding(
                file,
                "warning",
                "css: selectors that start at the page match nothing in the dashboard ("
                + "; ".join(f"'{part}'" for part in style.unmatched)
                + "); a :root, html or body prefix only reaches the dashboard when a space "
                'follows it, as in :root[data-theme="dark"] .tile',
            )
        )
    return findings


def _lint_dashboard_sources(dashboard, file: str, files_dir: Path | None) -> list[Finding]:
    """The default source, refresh, and every named source.

    A merged file has one key, so its findings point into it: `source.warehouse`.
    A file still on the legacy pair keeps pointing at `source` / `sources.<name>`."""
    default_name = dashboard.default_source_name
    prefix = "source" if default_name else "sources"
    where = f"source.{default_name}" if default_name else "source"
    findings = lint_source(dashboard.source, file, where, files_dir=files_dir)
    findings.extend(_lint_refresh(file, dashboard.refresh))
    for source_name, named in dashboard.sources.items():
        findings.extend(lint_source(named, file, f"{prefix}.{source_name}", files_dir=files_dir))
    return findings


def _lint_filters(dashboard, file: str) -> list[Finding]:
    """Each filter on its own: defaults, options_sql, and an empty select."""
    findings: list[Finding] = []
    for f in dashboard.filters:
        findings.extend(_lint_filter_default(file, f))
        if f.options_sql:
            problem = validate_template(f.options_sql)
            if problem:
                findings.append(Finding(file, "error", f"filter '{f.name}' options_sql: {problem}"))
            elif extract_params(f.options_sql):
                findings.append(
                    Finding(
                        file,
                        "error",
                        f"filter '{f.name}' options_sql cannot reference "
                        "{{ params }} — it runs before filters exist",
                    )
                )
        elif f.type == "select" and not f.options and f.default != "all":
            findings.append(
                Finding(
                    file,
                    "warning",
                    f"select filter '{f.name}' has no options and no options_sql — "
                    "the dropdown will be empty",
                )
            )
        if (
            f.type == "select"
            and f.default is not None
            and not f.options_sql
            and f.options is not None
            and f.default != "all"
            and f.default not in f.options
        ):
            findings.append(
                Finding(
                    file,
                    "error",
                    f"select filter '{f.name}' default {f.default!r} is not in options "
                    f"{f.options} — use a listed value, or default: all (the off sentinel)",
                )
            )
    return findings


def _lint_cross_filters(dashboard, file: str) -> list[Finding]:
    """Each `cross_filter:` names a scalar filter of this dashboard. A tile whose own
    query reads the filter it sets narrows itself to the one bar that was clicked."""
    findings: list[Finding] = []
    declared = {f.name: f for f in dashboard.filters}
    for tile in dashboard.tiles:
        kind = tile.chart.type if tile.chart else None
        if tile.cross_filter and kind in UNCLICKABLE_CHARTS:
            findings.append(
                Finding(
                    file,
                    "error",
                    f"tile '{tile.id}': a {kind} tile cannot cross-filter: its marks are "
                    "bins or cells, not rows of the result",
                )
            )
        for name in tile.cross_filter or {}:
            target = declared.get(name)
            where = f"tile '{tile.id}': cross_filter '{name}'"
            if target is None:
                listed = ", ".join(declared) or "none"
                findings.append(
                    Finding(file, "error", f"{where} is not a filter on this dashboard ({listed})")
                )
            elif target.type == "daterange":
                findings.append(
                    Finding(
                        file,
                        "error",
                        f"{where} is a date range; a click sets one value, so map a select, "
                        "text, number or date filter",
                    )
                )
            elif tile.query and name in extract_params(dashboard.queries.get(tile.query, "")):
                findings.append(
                    Finding(
                        file,
                        "warning",
                        f"{where} is also read by this tile's own query, so a click narrows "
                        "the tile to the one value it set",
                    )
                )
    return findings


def _lint_query_templates(dashboard, file: str) -> list[Finding]:
    """Every named query's conditional blocks parse."""
    findings: list[Finding] = []
    for query_name, sql in dashboard.queries.items():
        problem = validate_template(sql)
        if problem:
            findings.append(
                Finding(
                    file,
                    "error",
                    f"query '{query_name}': {problem} (a select filter sitting on its "
                    "'all' value already counts as inactive — write "
                    "{% if name %}...{% endif %}, no comparison needed)",
                )
            )
    return findings


def _lint_query_writes(dashboard, file: str) -> list[Finding]:
    """Tile SQL that opens as DML. Viewing the dashboard would run it (#500)."""
    findings: list[Finding] = []
    used_by: dict[str, list] = {}
    for tile in dashboard.tiles:
        if tile.query:
            used_by.setdefault(tile.query, []).append(tile)
    for query_name, sql in dashboard.queries.items():
        if not sql or not str(sql).strip():
            continue
        tiles = used_by.get(query_name, [])
        for label in [f"tile '{tile.id}'" for tile in tiles] or [f"query '{query_name}'"]:
            verdict = read_only_violation(sql, surface=label, scan_body=False)
            if verdict is not None:
                findings.append(Finding(file, "error", verdict))
    return findings


def _reference_findings(
    tile, file: str, available: dict, dashboard, project_metrics: dict
) -> list[Finding]:
    """References draw over an x/y plot, and a metric reference needs a metric
    the dashboard can run."""
    if tile.chart is None or not tile.chart.references:
        return []
    if tile.chart.type not in REFERENCE_CHART_TYPES:
        return [
            Finding(
                file,
                "error",
                f"tile '{tile.id}': references draw on {', '.join(REFERENCE_CHART_TYPES)} "
                f"charts, not {tile.chart.type}. Remove them or pick one of those types",
            )
        ]
    findings: list[Finding] = []
    dated = any(f.type == "daterange" for f in dashboard.filters)
    for n, ref in enumerate(tile.chart.references, start=1):
        if ref.metric is None:
            continue
        if ref.metric not in available:
            options = ", ".join(sorted(available)) or "(none defined)"
            findings.append(
                Finding(
                    file,
                    "error",
                    f"tile '{tile.id}': reference {n} names unknown metric '{ref.metric}'. "
                    f"Available: {options}",
                )
            )
            continue
        definition = dashboard.metrics.get(ref.metric) or project_metrics[ref.metric].definition
        if dated and definition.window:
            findings.append(
                Finding(
                    file,
                    "error",
                    f"tile '{tile.id}': reference {n} names '{ref.metric}', a trailing "
                    f"{definition.window} window, which is one value as of a day and not "
                    "a value over the dashboard's date range, so it cannot run under that "
                    "filter. Reference a metric without a window, or chart this one on its "
                    "own metric tile with a grain",
                )
            )
    return findings


def _note_metric_use(usage: _TileUsage, dashboard, project_metrics: dict, name: str):
    """A metric tile or a metric reference runs with the filter bar's values, so
    its dimensions and time dimension are what make those filters used."""
    usage.has_metric_tiles = True
    definition = dashboard.metrics.get(name) or project_metrics[name].definition
    declared = {d.name for d in definition.dimensions}
    usage.metric_dimensions.update(declared)
    if definition.time_dimension is not None:
        usage.any_metric_with_time = True
    return definition, declared


def _combo_errors(tile) -> list[str]:
    """Per-series marks and a second value axis need one series per y column
    on a vertical line, bar or area chart, and a right axis that something
    reads against."""
    chart = tile.chart
    if chart is None or not (chart.series or chart.axes):
        return []
    label = f"tile '{tile.id}'"
    keys = " and ".join(k for k in ("series", "axes") if getattr(chart, k))
    if chart.type not in COMBO_CHART_TYPES:
        return [f"{label}: {keys} apply to line, bar and area charts, not {chart.type}"]
    errors: list[str] = []
    if chart.group_by:
        errors.append(
            f"{label}: {keys} cannot combine with group_by, which splits one y column into "
            "a series per value. Drop group_by, or list the columns in y instead"
        )
    if chart.orientation == "horizontal":
        errors.append(f"{label}: {keys} need a vertical chart. Remove orientation: horizontal")
    if tile.metric is not None and tile.metric.compare:
        errors.append(
            f"{label}: {keys} cannot combine with compare, which draws the prior window "
            "as its own series. Remove one of them"
        )
    if chart.y:
        extra = [name for name in chart.series if name not in chart.y]
        if extra:
            errors.append(
                f"{label}: series {', '.join(repr(n) for n in extra)} "
                f"{'is' if len(extra) == 1 else 'are'} not in the chart's y "
                f"({', '.join(chart.y)}). Add {'it' if len(extra) == 1 else 'them'} to y "
                "or rename the series key"
            )
        right = [n for n in chart.y if (s := chart.series.get(n)) and s.axis == "right"]
        if right and len(right) == len(chart.y):
            errors.append(
                f"{label}: every y column is on the right axis, which leaves the left one "
                "empty. Keep at least one series on the left"
            )
    on_right = any(s.axis == "right" for s in chart.series.values())
    if "right" in chart.axes and not on_right:
        errors.append(
            f"{label}: axes.right is set but no series reads against it. Add axis: right to "
            "a series, or remove axes.right"
        )
    return errors


def _lint_tiles(
    dashboard, file: str, available: dict, project_metrics: dict
) -> tuple[list[Finding], _TileUsage]:
    """Chart options that need a type, and metric tiles against their definitions.

    Also records which queries and metric dimensions the tiles use, which is
    what the orphan pass reports against."""
    findings: list[Finding] = []
    usage = _TileUsage()
    for tile in dashboard.tiles:
        if tile.chart and tile.chart.orientation and tile.chart.type != "bar":
            findings.append(
                Finding(
                    file,
                    "error",
                    f"tile '{tile.id}': orientation is only valid on bar charts, "
                    f"not {tile.chart.type}",
                )
            )
        if tile.chart and tile.chart.stacked and tile.chart.type not in ("bar", "area"):
            findings.append(
                Finding(
                    file,
                    "error",
                    f"tile '{tile.id}': stacked is only valid on bar and area charts, "
                    f"not {tile.chart.type}",
                )
            )
        findings.extend(_reference_findings(tile, file, available, dashboard, project_metrics))
        for ref in tile.chart.references if tile.chart else []:
            if ref.metric in available:
                _note_metric_use(usage, dashboard, project_metrics, ref.metric)
        if tile.chart and tile.chart.type != "heatmap":
            for key in HEATMAP_FIELDS:
                if getattr(tile.chart, key) is not None:
                    findings.append(
                        Finding(
                            file,
                            "error",
                            f"tile '{tile.id}': {key} is only valid on heatmap charts, "
                            f"not {tile.chart.type}",
                        )
                    )
        if tile.chart and tile.chart.type == "heatmap" and len(tile.chart.y or []) > 1:
            findings.append(
                Finding(
                    file,
                    "error",
                    f"tile '{tile.id}': a heatmap takes one y column, not {len(tile.chart.y)}",
                )
            )
        if tile.chart and tile.chart.type != "histogram":
            for key in HISTOGRAM_FIELDS:
                if getattr(tile.chart, key) is not None:
                    findings.append(
                        Finding(
                            file,
                            "error",
                            f"tile '{tile.id}': {key} is only valid on histogram charts, "
                            f"not {tile.chart.type}",
                        )
                    )
        if tile.chart and tile.chart.type == "histogram":
            for key in ("y", "group_by"):
                if getattr(tile.chart, key):
                    findings.append(
                        Finding(
                            file,
                            "error",
                            f"tile '{tile.id}': a histogram counts the rows of its x column, "
                            f"so it takes no {key}",
                        )
                    )
        findings.extend(Finding(file, "error", m) for m in _combo_errors(tile))
        if tile.query:
            usage.queries.add(tile.query)
        if tile.metric is None:
            continue
        usage.has_metric_tiles = True
        metric_name = tile.metric.name
        if metric_name not in available:
            options = ", ".join(sorted(available)) or "(none defined)"
            findings.append(
                Finding(
                    file,
                    "error",
                    f"tile '{tile.id}' references unknown metric "
                    f"'{metric_name}' — available: {options}",
                )
            )
            continue
        definition, declared = _note_metric_use(usage, dashboard, project_metrics, metric_name)
        findings.extend(_lint_metric_tile(file, tile, metric_name, definition, declared, dashboard))
    return findings, usage


def _lint_metric_tile_query(
    file: str, tile, metric_name: str, definition, declared: set
) -> list[Finding]:
    """The dimensions and grain a metric tile asks for, which are exactly what
    the validate dry run compiles, so it can tell which failures lint already named."""
    findings: list[Finding] = []
    for dim in tile.metric.dimensions:
        if dim not in declared:
            valid = ", ".join(sorted(declared)) or "(none declared)"
            findings.append(
                Finding(
                    file,
                    "error",
                    f"tile '{tile.id}': metric '{metric_name}' has no "
                    f"dimension '{dim}' — valid: {valid}",
                )
            )
    if tile.metric.grain and definition.time_dimension is None:
        findings.append(
            Finding(
                file,
                "error",
                f"tile '{tile.id}': grain '{tile.metric.grain}' set but "
                f"metric '{metric_name}' has no time_dimension",
            )
        )
    return findings


def _lint_metric_tile(
    file: str, tile, metric_name: str, definition, declared: set, dashboard
) -> list[Finding]:
    """One metric tile against its definition: dimensions, compare, grain, group_by,
    and dashboard filters the metric cannot apply."""
    findings = _lint_metric_tile_query(file, tile, metric_name, definition, declared)
    # Read both spellings: `compare:` on the tile folds into the metric and is
    # not cleared, and the editor writes the folded form back on every save —
    # so checking only the tile-level key means this guardrail disappears the
    # first time anyone opens the dashboard, while rendering still reads
    # metric.compare and still draws the 0% delta.
    compare = tile.compare or tile.metric.compare
    has_grain = bool(tile.metric.grain or tile.grain)
    if compare and definition.time_dimension is None:
        findings.append(
            Finding(
                file,
                "error",
                f"tile '{tile.id}': compare '{compare}' set but metric "
                f"'{metric_name}' has no time_dimension — both windows return "
                "the same number, so the delta always reads 0%",
            )
        )
    if compare and definition.window and not has_grain:
        findings.append(
            Finding(
                file,
                "error",
                f"tile '{tile.id}': compare '{compare}' set on windowed metric "
                f"'{metric_name}' with no grain — a grainless window drops the "
                "daterange start so there is no range to shift. Query with a "
                "grain, or omit compare",
            )
        )
    elif compare and definition.time_dimension is not None:
        findings.extend(_lint_compare_window(file, tile.id, compare, dashboard))
    if tile.chart is not None:
        chart_type = tile.chart.type
    elif has_grain:
        chart_type = "area"
    else:
        chart_type = None
    if (
        tile.metric.dimensions
        and has_grain
        and chart_type in {"line", "area"}
        and (tile.chart is None or not tile.chart.group_by)
    ):
        dims = ", ".join(tile.metric.dimensions)
        hint = tile.metric.dimensions[0]
        findings.append(
            Finding(
                file,
                "warning",
                f"tile '{tile.id}': metric '{metric_name}' requests "
                f"dimension(s) {dims} on a {chart_type} chart with no "
                f"group_by — every group will plot as one interleaved "
                f"series (set chart.group_by: {hint})",
            )
        )
    for f in dashboard.filters:
        if f.type == "daterange":
            if definition.time_dimension is None:
                findings.append(
                    Finding(
                        file,
                        "warning",
                        f"tile '{tile.id}': dashboard filter '{f.name}' is a daterange "
                        f"but metric '{metric_name}' has no time_dimension, so this "
                        f"tile ignores the date filter",
                    )
                )
        elif f.name not in declared:
            findings.append(
                Finding(
                    file,
                    "warning",
                    f"tile '{tile.id}': dashboard filter '{f.name}' is not a declared "
                    f"dimension of metric '{metric_name}', so this tile ignores the filter",
                )
            )
    return findings


def _lint_compare_window(file: str, tile_id: str, compare: str, dashboard) -> list[Finding]:
    """A compare shifts the dashboard's daterange; with none there is nothing to shift,
    and every surface errors while the file used to lint clean."""
    daterange = next((f for f in dashboard.filters if f.type == "daterange"), None)
    if daterange is None:
        return [
            Finding(
                file,
                "error",
                f"tile '{tile_id}': compare '{compare}' needs a time range but the "
                "dashboard has no daterange filter — add one, or omit compare",
            )
        ]
    if daterange.default is None:
        return [
            Finding(
                file,
                "warning",
                f"tile '{tile_id}': compare '{compare}' needs a time range but "
                f"daterange filter '{daterange.name}' has no default — the tile errors "
                "until a date range is picked",
            )
        ]
    return []


def _lint_orphans(dashboard, file: str, usage: _TileUsage) -> list[Finding]:
    """Params with no filter, queries no tile uses, filters nothing reads."""
    findings: list[Finding] = []
    params_provided = set(filter_params(dashboard))

    for query_name, sql in dashboard.queries.items():
        for param in extract_params(sql):
            if param not in params_provided:
                findings.append(
                    Finding(
                        file,
                        "warning",
                        f"query '{query_name}': param '{{{{ {param} }}}}' has no "
                        "matching filter — callers must always pass a value",
                    )
                )

    for query_name in dashboard.queries:
        if query_name not in usage.queries:
            findings.append(
                Finding(
                    file,
                    "warning",
                    f"query '{query_name}' is not used by any tile "
                    "(still runnable from the query page)",
                )
            )

    all_params_used: set[str] = set()
    for sql in dashboard.queries.values():
        all_params_used.update(extract_params(sql))
    for f in dashboard.filters:
        bound = bool(all_params_used.intersection(filter_param_names(f)))
        if f.type == "daterange":
            used = bound or (usage.has_metric_tiles and usage.any_metric_with_time)
        else:
            used = bound or (usage.has_metric_tiles and f.name in usage.metric_dimensions)
        if not used:
            findings.append(
                Finding(
                    file,
                    "warning",
                    f"filter '{f.name}' is not used by any query or metric tile",
                )
            )
    return findings


def lint_project(
    store: DashboardStore,
    layer: SemanticLayer,
    *,
    check_sql: bool = False,
    workspace: Store | None = None,
    repo: str | None = None,
) -> list[Finding]:
    """Lint everything discoverable: metrics.yaml, every dashboard, and the
    references between filters, queries, tiles, and metrics.

    ``check_sql`` (``sqldash lint --strict``) zero-row probes authored SQL
    tools and every metrics.yaml metric's compiled SQL against the warehouse,
    the same path ``validate_dashboard`` uses for tiles (#430). Both probes
    share one registry, so a strict run opens one connection to the source.

    In a workspace, ``workspace`` and ``repo`` let a drill link reach the other
    repos: names resolve the way the served workspace resolves them."""
    registry = ExecutionRegistry(max_workers=1) if check_sql else None
    try:
        return _lint_project(store, layer, registry, workspace, repo)
    finally:
        if registry is not None:
            registry.shutdown()


def _lint_project(
    store: DashboardStore,
    layer: SemanticLayer,
    registry: ExecutionRegistry | None,
    workspace: Store | None = None,
    repo: str | None = None,
) -> list[Finding]:
    findings: list[Finding] = []

    metrics_path = layer.metrics_path()
    project_metrics = {}
    if metrics_path is not None:
        try:
            mf = layer.metrics_file()
            project_metrics = layer.project_metrics()
            findings.extend(
                lint_source(mf.source, metrics_path.name, "source", files_dir=metrics_path.parent)
            )
            for metric_name, resolved in project_metrics.items():
                findings.extend(
                    lint_cumulative(metric_name, resolved.definition, metrics_path.name)
                )
                findings.extend(
                    lint_metric_trunc_macros(metric_name, resolved.definition, metrics_path.name)
                )
            findings.extend(lint_relation_trunc_macros(mf.relations, metrics_path.name))
            if registry is not None:
                findings.extend(
                    Finding(metrics_path.name, level, message)
                    for level, message in _probe_project_metrics(
                        registry, mf.source, project_metrics
                    )
                )
        except SemanticError as exc:
            findings.append(Finding(metrics_path.name, "error", str(exc)))

    dashboards = store.discover()
    if not dashboards and metrics_path is None:
        findings.append(
            Finding(".", "warning", "no dashboards or metrics.yaml found in this project")
        )

    inline_definitions: dict[str, list[str]] = {}
    for name, path in dashboards.items():
        file = path.name
        try:
            dashboard = parse_dashboard(read_dashboard_text(path))
        except InvalidDashboardError as exc:
            findings.append(Finding(file, "error", str(exc)))
            continue
        for metric_name in dashboard.metrics:
            inline_definitions.setdefault(metric_name, []).append(file)
        findings.extend(lint_dashboard(dashboard, file, project_metrics, files_dir=path.parent))
        address = f"{repo}/{name}" if repo else name
        findings.extend(lint_drills(workspace or store, address, dashboard, file))

    findings.extend(lint_agents(store, layer, registry))

    # Two dashboards defining the same inline metric name is only detectable
    # across the project, so it cannot live in lint_dashboard. Resolving such a
    # name outside a dashboard's scope is refused at runtime; catching it here
    # means CI says so before anyone queries it. A metrics.yaml definition is
    # canonical and wins by design, so it is not a collision.
    for metric_name, files in sorted(inline_definitions.items()):
        if len(files) > 1 and metric_name not in project_metrics:
            where = ", ".join(sorted(files))
            findings.append(
                # Reported against the first file rather than a joined list: the
                # `file` field is counted as a file checked, so a list there
                # invents one ("3 file(s) checked" for two dashboards). The
                # message still names them all.
                Finding(
                    sorted(files)[0],
                    "error",
                    f"inline metric '{metric_name}' is defined by more than one "
                    f"dashboard ({where}) — it cannot be resolved by name alone; "
                    f"rename one or move the definition to metrics.yaml",
                )
            )
    return findings


def lint_drills(store: Store, name: str | None, dashboard, file: str) -> list[Finding]:
    """Every drill link resolves: its dashboard exists and loads, each mapped filter
    is declared there, and each value can fill the filter it names."""
    findings: list[Finding] = []
    for tile in dashboard.tiles:
        plan = plan_drill(store, name, dashboard, tile)
        if plan is None:
            continue
        findings.extend(Finding(file, "error", f"tile '{tile.id}': {e}") for e in plan["errors"])
        findings.extend(
            Finding(file, "warning", f"tile '{tile.id}': {w}") for w in plan["warnings"]
        )
    return findings


def _click_column_errors(dashboard, columns: dict[str, set[str]]) -> list[str]:
    """A drill or cross-filter reading a column the tile's query does not return
    would do nothing on click, so where the probe knows the columns, say which."""
    errors: list[str] = []
    for tile in dashboard.tiles:
        known = columns.get(tile.id)
        if known is None:
            continue
        reads = []
        if tile.drill is not None:
            reads += [("drill", c) for c in (*tile.drill.mapped_columns(), tile.drill.column)]
        reads += [("cross_filter", c) for c in (tile.cross_filter or {}).values()]
        for key, column in dict.fromkeys(reads):
            if column and column not in known:
                errors.append(
                    f"tile '{tile.id}': {key} reads column '{column}', which its query does "
                    f"not return (columns: {', '.join(sorted(known))})"
                )
    return errors


def lint_agents(
    store: DashboardStore, layer: SemanticLayer, registry: ExecutionRegistry | None = None
) -> list[Finding]:
    """agents.yaml: parse errors, then every reference it makes outside itself —
    metrics and dimensions an agent allows, metrics a tool bundle queries, the
    source a sql tool needs. The file-local rules (one body per tool, declared
    params, reserved names) are the model's. A ``registry`` means --strict:
    each read sql tool is probed through it."""
    agents = AgentLayer(store, layer)
    path = agents.agents_path()
    if path is None:
        return []
    try:
        errors, warnings = agents.check_references()
    except SemanticError as exc:
        return [Finding(path.name, "error", str(exc))]
    if registry is not None:
        errors.extend(_probe_sql_tools(registry, agents))
    return [Finding(path.name, "error", e) for e in errors] + [
        Finding(path.name, "warning", w) for w in warnings
    ]


def _probe_param_values(params: dict[str, ToolParam]) -> dict:
    """A value for every param so a zero-row probe can bind. Defaults and
    select options first; otherwise a typed placeholder."""
    values = {}
    for name, param in params.items():
        if param.default is not None:
            values[name] = param.default
        elif param.options:
            values[name] = param.options[0]
        elif param.type == "number":
            values[name] = 1
        elif param.type == "date":
            values[name] = "2026-01-01"
        else:
            values[name] = "probe"
    return values


def _probe_sql_tools(registry: ExecutionRegistry, agents: AgentLayer) -> list[str]:
    """Zero-row probe of each read sql tool, matching dashboard tile dry-run.

    Write statements are skipped: wrapping DELETE/INSERT in ``SELECT * FROM
    (...)`` is a parser error, and executing them would mutate the warehouse.
    An unreachable warehouse is one ``cannot probe`` finding, not one per tool.
    """
    source, base_dir = agents._project_source()
    if source is None:
        return []
    af = agents.agents_file()
    if af is None:
        return []
    try:
        style = paramstyle_for(source)
    except Exception:
        return []
    errors: list[str] = []
    try:
        with registry.connection(source, base_dir) as connector:
            try:
                connector.execute("SELECT 1", [], 1, CancelToken())
            except Exception as exc:
                return [f"cannot probe sql tools: {_error_summary(exc)}"]
            for name, tool in af.tools.items():
                if tool.sql is None:
                    continue
                sql = tool.sql.strip().rstrip(";").strip()
                if read_only_violation(sql, surface="sql tool"):
                    continue
                values = _probe_param_values(tool.params)
                try:
                    bound, bind = bind_sql(sql, values, style)
                except Exception as exc:
                    errors.append(f"tool '{name}': could not bind SQL: {exc}")
                    continue
                try:
                    # semgrep: probing the author's own SQL is what lint does; values stay bound
                    # nosemgrep: sqlalchemy-execute-raw-query
                    connector.execute(
                        f"SELECT * FROM ({bound}) sqldash_probe WHERE 1 = 0",
                        bind,
                        1,
                        CancelToken(),
                    )
                except (ConnectionLost, ConnectionBusy) as exc:
                    errors.append(f"cannot probe sql tools: {_error_summary(exc)}")
                    return errors
                except Exception as exc:
                    errors.append(
                        f"tool '{name}': SQL fails against the source: {_error_summary(exc)}"
                    )
    except (ConnectorError, SecretError) as exc:
        return [f"cannot probe sql tools: {_error_summary(exc)}"]
    return errors


def _error_summary(exc: Exception) -> str:
    """The warehouse's own words, without the echoed SQL and caret lines.

    Snowflake puts the useful half on the next line (``SQL compilation error:``
    then ``invalid identifier 'X'``); Postgres and DuckDB follow the message
    with a blank line or ``LINE 1:`` and the query text."""
    lines: list[str] = []
    for line in str(exc).strip().splitlines():
        if not line.strip() or line.startswith("LINE "):
            break
        lines.append(line.strip())
    return " ".join(lines) or type(exc).__name__


_UNRESOLVED_IDENTIFIER = re.compile(
    r"invalid identifier '(?P<snowflake>[^']+)'|column (?P<postgres>\S+) does not exist"
)


def _bare_column_hint(definition, error: str) -> str:
    """Name the fix when the warehouse rejected a dimension compiled from its name.

    A dimension without ``expr`` compiles as the bare identifier, which the
    warehouse case-folds: a column created as ``"order_date_lc"`` on Snowflake
    (or ``"ORDER_DATE_UC"`` on Postgres) is lint-clean and an invalid identifier
    at query time. Only the identifier the warehouse could not resolve counts,
    not any mention of the name, and DuckDB is absent because it resolves
    identifiers without case. The error names the folded spelling, so match
    without case."""
    unresolved = {
        (m["snowflake"] or m["postgres"]).rsplit(".", 1)[-1].strip('"').lower()
        for m in _UNRESOLVED_IDENTIFIER.finditer(error)
    }
    dims = [*definition.dimensions]
    if definition.time_dimension is not None:
        dims.append(definition.time_dimension)
    for dim in dims:
        if dim.expr is None and dim.name.lower() in unresolved:
            kind = "time_dimension" if dim is definition.time_dimension else "dimension"
            return (
                f"; {kind} '{dim.name}' has no expr, so it compiles as a bare column the "
                "warehouse case-folds. If the column was created quoted, set expr to its "
                "name in double quotes, exactly as created"
            )
    return ""


def _probe_project_metrics(
    registry: ExecutionRegistry, source, metrics: dict[str, ResolvedMetric]
) -> list[tuple[Level, str]]:
    """Zero-row probe of each metric compiled with every dimension and its grain.

    Lint checks names, not SQL, so an expr the warehouse cannot parse
    (``MAX(order)``) or a time dimension it cannot resolve (a quoted lowercase
    column on Snowflake) was lint-clean and failed on every query. ``WHERE 1 = 0``
    outside the aggregate lets the planner answer without scanning."""
    if not metrics:
        return []
    try:
        style = paramstyle_for(source)
    except Exception:
        return []
    base_dir = next(iter(metrics.values())).base_dir
    errors: list[str] = []
    warnings: list[str] = []
    try:
        with registry.connection(source, base_dir) as connector:
            try:
                connector.execute("SELECT 1", [], 1, CancelToken())
            except Exception as exc:
                return [("error", f"cannot probe metrics: {_error_summary(exc)}")]
            for name, resolved in metrics.items():
                definition = resolved.definition
                try:
                    bound = bind_resolved(
                        resolved,
                        dimensions=tuple(d.name for d in definition.dimensions),
                        grain=(
                            definition.time_dimension.grain if definition.time_dimension else None
                        ),
                        paramstyle=style,
                    )
                except (SemanticError, ParamError, KeyError, ValueError) as exc:
                    errors.append(f"metric '{name}': does not compile: {exc}")
                    continue
                try:
                    # semgrep: probing the author's own SQL is what lint does; values stay bound
                    # nosemgrep: sqlalchemy-execute-raw-query
                    connector.execute(
                        f"SELECT * FROM ({bound.sql}) sqldash_probe WHERE 1 = 0",
                        bound.bind,
                        1,
                        CancelToken(),
                    )
                except (ConnectionLost, ConnectionBusy) as exc:
                    errors.append(f"cannot probe metrics: {_error_summary(exc)}")
                    return [("error", message) for message in errors]
                except Exception as exc:
                    summary = _error_summary(exc)
                    errors.append(
                        f"metric '{name}': SQL fails against the source: {summary}"
                        f"{_bare_column_hint(definition, summary)}"
                    )
            if dialect_kind(source) == "snowflake" and _tz_candidates(metrics):
                try:
                    tables = connector.introspect()
                except Exception:
                    tables = []
                warnings.extend(_timestamp_tz_warnings(source, metrics, tables))
    except (ConnectorError, SecretError) as exc:
        return [("error", f"cannot probe metrics: {_error_summary(exc)}")]
    return [("error", m) for m in errors] + [("warning", m) for m in warnings]


_BARE_COLUMN = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def _tz_candidates(metrics: dict[str, ResolvedMetric]) -> dict[str, ResolvedMetric]:
    """Metrics whose time dimension is a bare column of a table and has no
    ``timezone:``, the only shape an introspected column type can speak for."""
    return {
        name: resolved
        for name, resolved in metrics.items()
        if resolved.definition.time_dimension is not None
        and resolved.definition.time_dimension.timezone is None
        and resolved.relation.table
        and _BARE_COLUMN.match(resolved.definition.time_dimension.sql_expr)
    }


def _timestamp_tz_warnings(
    source, metrics: dict[str, ResolvedMetric], tables: list[TableInfo]
) -> list[str]:
    """Snowflake's DATE_TRUNC keeps a TIMESTAMP_TZ value's own offset, so a grain
    over one returns a bucket per offset for one period. The compiler has no
    column types, which is why `timezone: session` is opt-in; `--strict` already
    holds a connection, so this is where a missing one can be pointed out."""
    types = {
        ((t.schema or "").lower(), t.name.lower(), column.lower()): str(dtype).upper()
        for t in tables
        for column, dtype in t.columns
    }
    database = (getattr(source, "database", None) or "").lower()
    default_schema = (getattr(source, "db_schema", None) or "").lower()
    warnings: list[str] = []
    for name, resolved in _tz_candidates(metrics).items():
        time_dimension = resolved.definition.time_dimension
        parts = [p.strip().strip('"').lower() for p in str(resolved.relation.table).split(".")]
        if len(parts) > 3 or (len(parts) == 3 and database and parts[0] != database):
            continue
        schema = parts[-2] if len(parts) > 1 else default_schema
        column = time_dimension.sql_expr.lower()
        if types.get((schema, parts[-1], column), "").startswith("TIMESTAMP_TZ"):
            warnings.append(
                f"metric '{name}': time_dimension '{time_dimension.name}' is a TIMESTAMP_TZ "
                f"column, and Snowflake truncates each row at its own offset, so one period "
                f"comes back once per offset. Set `timezone: session` on it to bucket in the "
                f"session time zone"
            )
    return warnings


def _metric_schema_findings(metrics, relations, tables, probed) -> list[str]:
    findings: list[str] = []
    for metric_name, definition in metrics.items():
        relation = relations.get(definition.relation) if definition.relation else None
        table = (relation.table if relation else definition.table) or None
        rel_sql = (relation.sql if relation else definition.sql) or None
        if not table:
            columns = probed.get(rel_sql) if rel_sql else None
            if columns is None:
                continue
            for dim in definition.dimensions:
                if dim.name.lower() not in columns:
                    findings.append(
                        f"metric '{metric_name}': dimension '{dim.name}' "
                        f"is not a column of the "
                        f"relation sql (columns: {', '.join(sorted(columns))})"
                    )
            if definition.time_dimension and definition.time_dimension.name.lower() not in columns:
                findings.append(
                    f"metric '{metric_name}': time_dimension "
                    f"'{definition.time_dimension.name}' is not a column of the relation sql"
                )
            continue
        short = table.split(".")[-1].lower()
        if short not in tables:
            findings.append(
                f"metric '{metric_name}': table '{table}' not found in source "
                f"(tables: {', '.join(sorted(tables)) or 'none'})"
            )
            continue
        columns = tables[short]
        for dim in definition.dimensions:
            if dim.name.lower() not in columns:
                findings.append(
                    f"metric '{metric_name}': dimension '{dim.name}' is not a column of "
                    f"'{table}' (columns: {', '.join(sorted(columns))})"
                )
        if definition.time_dimension and definition.time_dimension.name.lower() not in columns:
            findings.append(
                f"metric '{metric_name}': time_dimension "
                f"'{definition.time_dimension.name}' is not a column of '{table}'"
            )
    return findings


def _inline_resolved(name: str, metrics, relations, source, base_dir) -> ResolvedMetric:
    """A metric from candidate YAML, resolved the way the layer would once it is
    on disk: any derived expr expanded and its relation found."""
    definition = metrics[name]
    if definition.derived is not None:
        definition, relation = expand_derived(name, definition, metrics, relations)
    else:
        relation = resolve_relation(definition, relations)
    return ResolvedMetric(
        name=name,
        definition=definition,
        source=source,
        base_dir=base_dir if isinstance(base_dir, Path) else Path(base_dir),
        relation=relation,
        origin="project",
    )


def _dry_run_metrics(metrics, relations, source, base_dir) -> tuple[dict, list[str]]:
    compiled: dict[str, str] = {}
    errors: list[str] = []
    try:
        style = paramstyle_for(source)
    except Exception:
        style = "qmark"
    for metric_name, definition in metrics.items():
        try:
            resolved = _inline_resolved(metric_name, metrics, relations, source, base_dir)
            definition = resolved.definition
            dims = tuple(d.name for d in definition.dimensions)
            grain = definition.time_dimension.grain if definition.time_dimension else None
            bound = bind_resolved(resolved, dimensions=dims, grain=grain, paramstyle=style)
            compiled[metric_name] = bound.sql
        except (SemanticError, KeyError, ValueError) as exc:
            errors.append(f"metric '{metric_name}': {exc}")
    return compiled, errors


def _schema_findings(registry, source, base_dir, metrics, relations) -> tuple[bool, list[str]]:
    try:
        with registry.connection(source, base_dir) as connector:
            tables = {
                t.name.lower(): {c[0].lower() for c in t.columns} for t in connector.introspect()
            }
            sql_columns: dict[str, set[str] | None] = {}

            def probe(sql: str) -> set[str] | None:
                if sql not in sql_columns:
                    try:
                        # semgrep: probing the author's own SQL is what lint does; values stay bound
                        # nosemgrep: sqlalchemy-execute-raw-query
                        result = connector.execute(
                            f"SELECT * FROM ({sql}) sqldash_probe WHERE 1 = 0",
                            [],
                            1,
                            CancelToken(),
                        )
                        sql_columns[sql] = {c.name.lower() for c in result.columns}
                    except Exception:
                        sql_columns[sql] = None
                return sql_columns[sql]

            probed = {
                rel_sql: probe(rel_sql)
                for rel_sql in {
                    (
                        relations.get(d.relation).sql
                        if d.relation and relations.get(d.relation)
                        else d.sql
                    )
                    for d in metrics.values()
                }
                if rel_sql
            }
    except Exception as exc:
        return False, [f"schema check skipped — could not introspect source: {exc}"]
    return True, _metric_schema_findings(metrics, relations, tables, probed)


def _probe_compiled(
    registry, source, base_dir, compiled: dict[str, str], binds: dict[str, list] | None = None
) -> list[str]:
    findings: list[str] = []
    try:
        with registry.connection(source, base_dir) as connector:
            for metric_name, sql in compiled.items():
                bind = (binds or {}).get(metric_name, [])
                try:
                    # semgrep: probing the author's own SQL is what lint does; values stay bound
                    # nosemgrep: sqlalchemy-execute-raw-query
                    connector.execute(
                        f"SELECT * FROM ({sql}) sqldash_probe WHERE 1 = 0", bind, 1, CancelToken()
                    )
                except Exception as exc:
                    findings.append(
                        f"metric '{metric_name}': compiled SQL fails against the source — "
                        f"{str(exc).splitlines()[0]}"
                    )
    except Exception:
        pass
    return findings


def _metric_catalog(layer, repo: str | None = None) -> tuple[dict, dict[str, list[str]]]:
    """What a candidate file may name, and the bare names it may not.

    In a workspace a bare name belongs to the repo the candidate lives in —
    that is the one a tile resolves at serve time, so it is never ambiguous
    once ``repo`` is known (#629). Qualified `repo/metric` keys stay for every
    repo either way. Only a candidate no repo can be derived for keeps the
    ambiguity, because nothing there says which metric it would run."""
    sublayers = getattr(layer, "layers", None)
    try:
        if sublayers is None:
            return dict(layer.project_metrics()), {}
        available: dict = {}
        owners: dict[str, list[str]] = {}
        for owner, sub in sublayers.items():
            try:
                metrics = sub.project_metrics()
            except SemanticError:
                continue
            for name, resolved in metrics.items():
                qualified = f"{owner}/{name}"
                available[qualified] = resolved
                owners.setdefault(name, []).append(qualified)
        if repo is not None and repo in sublayers:
            for bare in owners:
                scoped = available.get(f"{repo}/{bare}")
                if scoped is not None:
                    available[bare] = scoped
            return available, {}
        ambiguous = {}
        for bare, quals in owners.items():
            available.setdefault(bare, available[quals[0]])
            if len(quals) > 1:
                ambiguous[bare] = sorted(quals)
        return available, ambiguous
    except SemanticError:
        return {}, {}


def _candidate_repo(layer, candidate: str) -> str | None:
    """The workspace repo a candidate file belongs to, or None outside a
    workspace and when a workspace cannot tell."""
    if getattr(layer, "layers", None) is None:
        return None
    return _owning_repo(layer, candidate)[0]


def _owning_repo(layer, candidate: str) -> tuple[str | None, str]:
    """The workspace repo a candidate file belongs to, and its name inside that
    repo: the `repo/` its name starts with, else the only repo there is."""
    repo, slash, rest = candidate.partition("/")
    if slash and repo in layer.layers:
        return repo, rest
    if len(layer.layers) == 1:
        return next(iter(layer.layers)), candidate
    return None, candidate


def _candidate_root(store, layer, candidate: str) -> Path | None:
    """The directory `sqldash lint` resolves a candidate file's relative source
    paths against: the project root, or the root of the workspace repo the
    candidate belongs to. None when a workspace cannot tell which repo."""
    if getattr(layer, "layers", None) is None:
        return store.root
    repo, _ = _owning_repo(layer, candidate)
    return None if repo is None else layer.layers[repo].store.root


def _unanchored_note(layer, how: str) -> str:
    return (
        "note: skipped the source file and SQL checks: the source reads files "
        f"relative to its repo, and in a workspace {how} to say which repo this "
        f"file belongs to (repos: {', '.join(sorted(layer.layers))})"
    )


def _inline_collision_errors(layer, dashboard, name: str | None) -> tuple[list[str], list[str]]:
    """Inline metric names the candidate takes from another dashboard.

    An unnamed candidate is not necessarily a new file — an agent validating an
    edit before writing it has nothing to put in ``name`` yet — so a single
    stored owner may well be this same file, and calling that a collision
    reports the candidate against itself (#645). What stays provable without a
    name: two or more stored owners collide whichever one the candidate is (or
    is not). The single-owner case becomes a note, because silently dropping it
    reads exactly like passing."""
    anonymous = not name
    candidate = name or ""
    for ext in (".yaml", ".yml"):
        candidate = candidate.removesuffix(ext)
    sub, prefix = layer, ""
    if getattr(layer, "layers", None) is not None:
        repo, candidate = _owning_repo(layer, candidate)
        if repo is None:
            return [], [
                "note: skipped the cross-dashboard inline metric check — in a "
                "workspace, name must be 'repo/dashboard' to say which repo "
                f"this file belongs to (repos: {', '.join(sorted(layer.layers))})"
            ]
        sub, prefix = layer.layers[repo], f"{repo}/"
    try:
        project = sub.project_metrics()
        owners = sub._inline_by_name()
    except SemanticError:
        return [], []
    errors, notes = [], []
    for metric_name in dashboard.metrics:
        if metric_name in project:
            continue
        others = sorted(
            f"{prefix}{m.dashboard}"
            for m in owners.get(metric_name, ())
            if m.dashboard and m.dashboard != candidate
        )
        if not others:
            continue
        if anonymous and len(others) == 1:
            notes.append(
                f"note: inline metric '{metric_name}' is also defined by {others[0]} — "
                f"harmless if this candidate is that dashboard, a collision if it is a "
                f"new one; pass name (the dashboard this will be saved as) to check"
            )
            continue
        errors.append(
            f"inline metric '{metric_name}' is already defined by "
            f"{', '.join(others)} — it could not be resolved by name alone; "
            f"rename one or move the definition to metrics.yaml"
        )
    return errors, notes


def _metric_tile_query_already_linted(tile, definition) -> bool:
    """Lint already errored on the dimensions or grain this tile compiles with, so
    its compile failure is that defect again in the compiler's words (#543)."""
    declared = {d.name for d in definition.dimensions}
    findings = _lint_metric_tile_query("", tile, tile.metric.name, definition, declared)
    return any(f.level == "error" for f in findings)


def _dry_run_metric_tiles(
    registry, layer, dashboard, base_dir, repo: str | None = None
) -> tuple[list[str], dict[str, set[str]]]:
    """Compile and probe every metric tile. Returns the errors and the column names
    each probe came back with, keyed by tile id. A dashboard-local metric is only
    probed for its columns: `_dry_run_metrics` already reports its failures."""
    available, ambiguous = _metric_catalog(layer, repo)
    errors: list[str] = []
    columns: dict[str, set[str]] = {}
    probed: dict[tuple, set[str] | None] = {}
    shapes: set[tuple] = set()
    for tile in dashboard.tiles:
        if not tile.metric:
            continue
        inline = tile.metric.name in dashboard.metrics
        if inline:
            resolved = _inline_dashboard_resolved(dashboard, tile.metric.name, base_dir)
        elif tile.metric.name in ambiguous:
            continue
        else:
            resolved = available.get(tile.metric.name)
        if resolved is None:
            continue
        key = (tile.metric.name, tuple(tile.metric.dimensions), tile.metric.grain)
        if key in probed:
            if probed[key] is not None:
                columns[tile.id] = probed[key]
            continue
        probed[key] = None
        try:
            bound = bind_resolved(
                resolved,
                dimensions=tuple(tile.metric.dimensions),
                grain=tile.metric.grain,
                limit=1,
            )
        except ConnectorError:
            continue
        except (SemanticError, ValueError) as exc:
            if not inline and not _metric_tile_query_already_linted(tile, resolved.definition):
                errors.append(f"tile '{tile.id}': metric does not compile — {exc}")
            continue
        shapes.add((tile.metric.name, bound.sql, repr([])))
        try:
            with registry.connection(resolved.source, resolved.base_dir) as connector:
                try:
                    # semgrep: probing the author's own SQL is what lint does; values stay bound
                    # nosemgrep: sqlalchemy-execute-raw-query
                    probe = connector.execute(
                        f"SELECT * FROM ({bound.sql}) sqldash_probe WHERE 1 = 0",
                        [],
                        1,
                        CancelToken(),
                    )
                except Exception as exc:
                    if not inline:
                        errors.append(
                            f"tile '{tile.id}': SQL fails against the source — "
                            f"{str(exc).splitlines()[0]}"
                        )
                    continue
        except Exception:
            continue
        columns[tile.id] = probed[key] = {c.name for c in probe.columns}
    errors.extend(
        _dry_run_reference_metrics(registry, dashboard, base_dir, available, ambiguous, shapes)
    )
    return errors, columns


def _dry_run_reference_metrics(
    registry, dashboard, base_dir, available, ambiguous, probed: set[tuple]
) -> list[str]:
    """A metric reference runs as its metric with no dimensions and no grain,
    under the dashboard's filters, so each one, inline or from the project, is
    probed as exactly that query. A query already probed, by another
    reference or by a metric tile of the same shape, is not probed again."""
    errors: list[str] = []
    for tile in dashboard.tiles:
        references = tile.chart.references if tile.chart else []
        for n, ref in enumerate(references, start=1):
            name = ref.metric
            if name is None or (name in ambiguous and name not in dashboard.metrics):
                continue
            label = f"tile '{tile.id}': reference {n} metric '{name}'"
            try:
                if name in dashboard.metrics:
                    resolved = _inline_resolved(
                        name, dashboard.metrics, dashboard.relations, dashboard.source, base_dir
                    )
                else:
                    resolved = available.get(name)
                    if resolved is None:
                        continue
                bound = bind_resolved(resolved, dimensions=(), grain=None, dash=dashboard, limit=1)
            except (SemanticError, KeyError, ValueError) as exc:
                errors.append(f"{label} does not compile: {exc}")
                continue
            shape = (name, bound.sql, repr(bound.bind))
            if shape in probed:
                continue
            probed.add(shape)
            errors.extend(
                f"{label} fails against the source: {msg.split(' — ', 1)[-1]}"
                for msg in _probe_compiled(
                    registry,
                    resolved.source,
                    resolved.base_dir,
                    {tile.id: bound.sql},
                    {tile.id: bound.bind},
                )
            )
    return errors


def _inline_dashboard_resolved(dashboard, name: str, base_dir) -> ResolvedMetric | None:
    """A dashboard-local metric resolved for a probe, or None when it does not
    resolve: `_dry_run_metrics` already reports why."""
    try:
        return _inline_resolved(
            name, dashboard.metrics, dashboard.relations, dashboard.source, base_dir
        )
    except (SemanticError, KeyError, ValueError):
        return None


def _render_all_conditionals(sql: str) -> str | None:
    try:
        return render_conditionals(sql, dict.fromkeys(extract_params(sql), "probe"), frozenset())
    except Exception:
        return None


def _dry_run_queries(registry, dashboard, base_dir) -> tuple[dict, list[str], dict]:
    """Render and probe every query tile. Returns the rendered SQL per tile,
    the errors, and the column names each probe came back with (only for
    tiles whose probe ran, so a chart check has real columns or none)."""
    rendered: dict[str, str] = {}
    errors: list[str] = []
    columns: dict[str, set[str]] = {}
    seen: dict[tuple[str, str | None], str] = {}
    seen_columns: dict[tuple[str, str | None], set[str]] = {}
    unresolvable: set[str] = set()
    for tile in dashboard.tiles:
        if not tile.query:
            continue
        sql = dashboard.queries.get(tile.query)
        if not sql:
            continue
        key = (tile.query, tile.source)
        if key in seen:
            rendered[tile.id] = seen[key]
            if key in seen_columns:
                columns[tile.id] = seen_columns[key]
            continue
        try:
            text, values, missing = prepare_sql(dashboard, sql, {})
        except Exception as exc:
            errors.append(f"tile '{tile.id}': could not render SQL — {exc}")
            continue
        if missing:
            errors.append(
                f"tile '{tile.id}': no value for {', '.join(missing)} — "
                "add a filter with that name or a default"
            )
            continue
        rendered[tile.id] = text
        seen[key] = text
        source = dashboard.named_source(tile.source)
        source_key = tile.source or ""
        if source_key in unresolvable:
            continue
        try:
            paramstyle = paramstyle_for(source)
        except Exception:
            unresolvable.add(source_key)
            continue
        variants = [("", text, values)]
        active = _render_all_conditionals(sql)
        if active is not None and active.strip() != text.strip():
            variants.append((" with filters active", active, dict.fromkeys(extract_params(active))))
        for label, sql_text, sql_values in variants:
            sql_text = sql_text.strip().rstrip(";").strip()
            if not sql_text:
                continue
            verdict = read_only_violation(sql_text, surface=f"tile '{tile.id}'", scan_body=False)
            if verdict is not None:
                errors.append(verdict)
                break
            bound, bind = bind_sql(sql_text, sql_values, paramstyle)
            try:
                with registry.connection(source, base_dir) as connector:
                    # semgrep: probing the author's own SQL is what lint does; values stay bound
                    # nosemgrep: sqlalchemy-execute-raw-query
                    probe = connector.execute(
                        f"SELECT * FROM ({bound}) sqldash_probe WHERE 1 = 0",
                        bind,
                        1,
                        CancelToken(),
                    )
            except Exception as exc:
                errors.append(
                    f"tile '{tile.id}': SQL fails against the source{label} — "
                    f"{str(exc).splitlines()[0]}"
                )
                break
            if not label:
                columns[tile.id] = seen_columns[key] = {c.name for c in probe.columns}
    return rendered, errors + _options_sql_errors(registry, dashboard, base_dir), columns


CHART_COLUMN_KEYS = ("x", "y", "group_by", "label", "value")


def _chart_column_warnings(dashboard, columns: dict[str, set[str]]) -> list[str]:
    """A chart whose x/y/label/value names a column the tile's query does not
    return renders empty — every value null, no error (#357). Only checked
    where the probe already told us the columns; a metric tile's columns come
    from the compiler and are left to the render-time backstop.

    Matching is case-sensitive because `dropStaleEncodings` in charts.js is:
    folding case here made lint stay silent on `x: A` over a column `a` that
    the renderer drops and infers past, which is the two-surfaces-disagree
    shape this check exists to close."""
    warnings: list[str] = []
    for tile in dashboard.tiles:
        known = columns.get(tile.id)
        if tile.chart is None or known is None:
            continue
        for key in (*CHART_COLUMN_KEYS, "series"):
            value = getattr(tile.chart, key)
            names = list(value) if isinstance(value, list | dict) else [value]
            for name in names:
                if name and name not in known:
                    warnings.append(
                        f"tile '{tile.id}': chart {key} '{name}' is not a column its query "
                        f"returns (columns: {', '.join(sorted(known))}) — the tile renders "
                        "from inferred columns until the spec matches"
                    )
    return warnings


def _options_sql_errors(registry, dashboard, base_dir) -> list[str]:
    errors: list[str] = []
    for f in dashboard.filters:
        if not f.options_sql:
            continue
        if extract_params(f.options_sql):
            continue
        try:
            bound, bind = bind_sql(f.options_sql, {}, paramstyle_for(dashboard.source))
            with registry.connection(dashboard.source, base_dir) as connector:
                # semgrep: probing the author's own SQL is what lint does; values stay bound
                # nosemgrep: sqlalchemy-execute-raw-query
                connector.execute(
                    f"SELECT * FROM ({bound}) sqldash_probe WHERE 1 = 0",
                    bind,
                    1,
                    CancelToken(),
                )
        except Exception as exc:
            errors.append(
                f"filter '{f.name}': options_sql fails against the source — "
                f"{str(exc).splitlines()[0]}"
            )
    return errors


def validate_metrics(
    yaml_text: str, *, store, layer, registry, check_schema: bool = True, repo: str | None = None
) -> dict:
    """Parse + lint + dry-run compile a candidate metrics.yaml. Does not write.

    In a workspace, ``repo`` names the repo the file belongs to."""
    try:
        mf = parse_metrics_file(yaml_text)
    except SemanticError as exc:
        return {"valid": False, "errors": [str(exc)]}
    root = _candidate_root(store, layer, f"{repo}/metrics" if repo else "metrics")
    unanchored = root is None and reads_project_files(mf.source)
    source_findings = lint_source(mf.source, "metrics.yaml", "source", files_dir=root)
    errors = [f.message for f in source_findings if f.level == "error"]
    lint_findings = [f"{f.level}: {f.message}" for f in source_findings if f.level != "error"]
    if unanchored:
        lint_findings.append(_unanchored_note(layer, "pass repo"))
        check_schema = False
    root = root or Path.cwd()
    for metric_name, definition in mf.metrics.items():
        for finding in lint_cumulative(metric_name, definition, "metrics.yaml"):
            if finding.level == "error":
                errors.append(finding.message)
            else:
                lint_findings.append(f"{finding.level}: {finding.message}")
    compiled, compile_errors = _dry_run_metrics(mf.metrics, mf.relations, mf.source, root)
    errors.extend(compile_errors)
    payload: dict = {
        "valid": not errors,
        "errors": errors,
        "lint": lint_findings,
        "compiled_sql": compiled,
        "metric_count": len(mf.metrics),
    }
    if check_schema:
        checked, findings = _schema_findings(registry, mf.source, root, mf.metrics, mf.relations)
        if checked:
            findings.extend(_probe_compiled(registry, mf.source, root, compiled))
        payload["schema_checked"] = checked
        payload["schema_findings"] = findings
        if findings and checked:
            payload["valid"] = False
    return payload


def validate_dashboard(
    yaml_text: str, *, store, layer, registry, check_sql: bool = True, name: str | None = None
) -> dict:
    """Parse + lint + probe a candidate dashboard YAML. Does not write."""
    try:
        dashboard = parse_dashboard(yaml_text)
    except InvalidDashboardError as exc:
        return {"valid": False, "errors": [str(exc)]}
    base_dir = _candidate_root(store, layer, name or "")
    repo = _candidate_repo(layer, name or "")
    unanchored = base_dir is None and any(
        reads_project_files(s) for s in (dashboard.source, *dashboard.sources.values())
    )
    available, ambiguous = _metric_catalog(layer, repo)
    findings = lint_dashboard(dashboard, "dashboard", available, files_dir=base_dir)
    address = name
    if name and repo and not name.startswith(f"{repo}/"):
        address = f"{repo}/{_owning_repo(layer, name)[1]}"
    findings.extend(lint_drills(store, address, dashboard, "dashboard"))
    errors = [f.message for f in findings if f.level == "error"]
    lint_findings = [f"{f.level}: {f.message}" for f in findings if f.level != "error"]
    if unanchored:
        lint_findings.append(_unanchored_note(layer, "name must be 'repo/dashboard'"))
        check_sql = False
    base_dir = base_dir or Path.cwd()

    for tile in dashboard.tiles:
        metric_name = tile.metric.name if tile.metric else None
        if metric_name in ambiguous and metric_name not in dashboard.metrics:
            errors.append(
                f"tile '{tile.id}': metric '{metric_name}' exists in more than one repo "
                f"— use one of: {', '.join(ambiguous[metric_name])}"
            )

    collisions, collision_notes = _inline_collision_errors(layer, dashboard, name)
    errors.extend(collisions)
    lint_findings.extend(collision_notes)
    compiled, metric_errors = _dry_run_metrics(
        dashboard.metrics, dashboard.relations, dashboard.source, base_dir
    )
    errors.extend(metric_errors)
    rendered: dict[str, str] = {}
    if check_sql:
        rendered, sql_errors, columns = _dry_run_queries(registry, dashboard, base_dir)
        errors.extend(
            e
            for e in sql_errors
            if e not in errors and not _sql_error_already_linted(e, errors, dashboard)
        )
        lint_findings.extend(f"warning: {w}" for w in _chart_column_warnings(dashboard, columns))
        tile_errors, metric_columns = _dry_run_metric_tiles(
            registry, layer, dashboard, base_dir, repo
        )
        errors.extend(_click_column_errors(dashboard, columns | metric_columns))
        errors.extend(_probe_compiled(registry, dashboard.source, base_dir, compiled))
        errors.extend(tile_errors)
    return {
        "valid": not errors,
        "errors": errors,
        "lint": lint_findings,
        "sql_checked": check_sql,
        "rendered_sql": rendered,
        "compiled_sql": compiled,
        "tiles": [
            {
                "id": w.id,
                "title": w.title,
                "kind": "metric" if w.metric else ("text" if w.type == "text" else "query"),
                "position": w.position.model_dump() if w.position else None,
            }
            for w in dashboard.tiles
        ],
    }
