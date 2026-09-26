import contextlib
import inspect
import json
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Annotated, Any
from weakref import WeakKeyDictionary

from mcp.server import MCPServer
from mcp.server.mcpserver.prompts.base import Prompt
from mcp.types import CallToolResult, TextContent
from pydantic import Field

from sqldash import __version__
from sqldash.connectors.base import ConnectorError
from sqldash.execution import ExecutionRegistry
from sqldash.lint import validate_dashboard as run_validate_dashboard
from sqldash.lint import validate_metrics as run_validate_metrics
from sqldash.models.results import QueryResult, result_payload
from sqldash.models.source import redact_source, source_label
from sqldash.params import ParamError
from sqldash.period import COMPARE_MODES
from sqldash.project.catalog import list_dashboards as catalog_dashboards
from sqldash.project.catalog import list_metrics as catalog_metrics
from sqldash.project.catalog import metric_detail
from sqldash.project.sources import (
    LabeledSource,
    declared_sources,
    labeled_sources,
    pick_main_source,
    source_wire_keys,
)
from sqldash.project.store import (
    DashboardStore,
    InvalidDashboardError,
    NotFoundError,
    WorkspaceStore,
)
from sqldash.secrets import SecretError
from sqldash.semantics import MetricNotFoundError, SemanticError, SemanticLayer
from sqldash.semantics.agents import (
    ResolvedTool,
    agent_layer_for,
    json_kind,
    render_prompt,
    run_tool,
    tool_signature,
)
from sqldash.semantics.bind import bind_metric, scope_note
from sqldash.semantics.compare import compare_metric
from sqldash.semantics.compiler import GRAINS
from sqldash.semantics.layer import WorkspaceLayer
from sqldash.sqlguard import read_only_violation

DEFAULT_ROW_LIMIT = 1000

_MCP_ERRORS = (
    MetricNotFoundError,
    SemanticError,
    NotFoundError,
    InvalidDashboardError,
    ConnectorError,
    ParamError,
    SecretError,
)


def mcp_result(fn):
    """Turn a domain error into ``{"error": ...}`` so it cannot become a protocol error.

    Tools used to each wrap their body in the same try/except. A helper that
    raised *outside* that try — ``project_metrics`` on WorkspaceLayer, ``bind_sql``
    for a missing placeholder, ``paramstyle_for`` for an unloadable dialect —
    crashed the call as a protocol error instead of a verdict the agent can read.
    The wrapper is the one try. Programming errors (TypeError, etc.) still raise.
    """

    @wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except _MCP_ERRORS as exc:
            return {"error": str(exc)}

    return wrapped


def _result_payload(sql: str, result: QueryResult, row_limit: int) -> dict[str, Any]:
    return {"sql": sql, **result_payload(result, row_limit)}


_REGISTRIES: "WeakKeyDictionary[MCPServer, ExecutionRegistry]" = WeakKeyDictionary()


def registry_for(server: MCPServer) -> ExecutionRegistry:
    """The ExecutionRegistry backing a server from create_mcp_server, for shutdown."""
    return _REGISTRIES[server]


def _reject_unknown_arguments(server: MCPServer) -> None:
    """Refuse a tool argument the server never declared, instead of dropping it.

    ``mcp>=2`` builds each tool's argument model with pydantic's default
    ``extra="ignore"``, so ``get_schema {"dashboard": "probe"}`` ran unscoped
    and reported the primary source's tables as if they were the dashboard's,
    and ``run_sql {"source": "zzz"}`` answered from the primary source (#395).
    An LLM agent invents argument names, which is exactly the caller this
    server is for, so the drop is a confident answer to a question nobody
    asked. The check runs against the ``inputSchema`` the agent read from
    ``tools/list``, and the verdict is the ``{"error": ...}`` payload every
    other refusal on this surface uses.

    The schema map is filled on a miss, not once on the first call: a tool
    registered after the guard has run would otherwise never be in it, and an
    absent name reads exactly like "not a tool of ours" — the guard would wave
    through the arguments it exists to refuse. A name still missing after the
    refresh is genuinely not ours, so the server answers for it.
    """
    declared: dict[str, tuple[str, ...]] = {}

    async def guard(ctx, call_next):
        if ctx.method != "tools/call" or not isinstance(ctx.params, Mapping):
            return await call_next(ctx)
        name = ctx.params.get("name")
        arguments = ctx.params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(arguments, Mapping):
            return await call_next(ctx)
        valid = declared.get(name)
        if valid is None:
            declared.update(
                {
                    t.name: tuple(t.input_schema.get("properties") or {})
                    for t in await server.list_tools()
                }
            )
            valid = declared.get(name)
        if valid is None:
            return await call_next(ctx)
        unknown = next((key for key in arguments if key not in valid), None)
        if unknown is None:
            return await call_next(ctx)
        payload = {
            "error": (
                f"unknown argument '{unknown}' for {name} — "
                f"valid arguments: {', '.join(valid) or '(none)'}"
            )
        }
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(payload, indent=2))],
            structured_content=payload,
        )

    server.middleware.append(guard)


def _mcp_source_label(entry: "LabeledSource") -> str:
    """Same key `source list` / `labeled_sources` emit (`demo.source`)."""
    return entry.label


def _select_source(
    entries: list[LabeledSource], source: str | None
) -> LabeledSource | dict[str, str]:
    """The entry `source` names (any `source_wire_keys` spelling), else the
    `pick_main_source` choice when omitted, else an error payload listing the
    canonical labels. `get_schema` and `run_sql` share it so they cannot pick
    different sources for the same call."""
    options = ", ".join(sorted(e.label for e in entries))
    if not entries:
        return {"error": "no sources defined in this project"}
    if source is None:
        picked = pick_main_source(entries)
        if picked is None:
            return {"error": "several sources — pass one of: " + options}
        return picked
    for entry in entries:
        if source in source_wire_keys(entry):
            return entry
    return {"error": f"unknown source '{source}' — options: " + options}


_INSTRUCTIONS = (
    "Governed metrics from a sqldash project (dashboards-as-code). "
    "Start with list_metrics to discover what is measurable, then query_metric "
    "to get numbers — you pass metric/dimension NAMES and filter VALUES; "
    "sqldash compiles and runs the SQL against the owner's data source. "
    "If you AUTHOR dashboard/metric YAML: the source config is flat "
    "(type: names the database; username/password/authentication or "
    "profile: — never a 'connection:' key or nested auth block), SQL "
    "conditionals are only bare-param {% if name %}...{% elif name %}..."
    "{% else %}...{% endif %} blocks, never nested "
    "(a select on its 'all' value already counts as inactive), and params "
    "are {{ name }} bound natively. ALWAYS run validate_dashboard or "
    "validate_metrics on candidate YAML before writing a file."
)


@dataclass(frozen=True)
class _ToolContext:
    store: DashboardStore | WorkspaceStore
    layer: SemanticLayer | WorkspaceLayer
    registry: ExecutionRegistry
    row_limit: int


def create_mcp_server(
    path: Path | None = None,
    allow_sql: bool = False,
    row_limit: int = DEFAULT_ROW_LIMIT,
    workspace: list[tuple[str, Path]] | None = None,
    trace: Path | None = None,
) -> MCPServer:
    if workspace is not None:
        store = WorkspaceStore({name: DashboardStore(p) for name, p in workspace})
        layer = WorkspaceLayer({name: SemanticLayer(s) for name, s in store.repos.items()})
    else:
        store = DashboardStore(path)
        layer = SemanticLayer(store)
    registry = ExecutionRegistry(max_workers=2, still_declared=declared_sources(store, layer))
    ctx = _ToolContext(store=store, layer=layer, registry=registry, row_limit=row_limit)

    server = MCPServer("sqldash", version=__version__, instructions=_INSTRUCTIONS)
    _register_metric_tools(server, ctx)
    _register_source_tools(server, ctx)
    _register_validation_tools(server, ctx)
    _register_dashboard_tools(server, ctx)
    if allow_sql:
        _register_sql_tools(server, ctx)
    _reject_unknown_arguments(server)
    _register_agents(server, agent_layer_for(store, layer), registry, row_limit)
    if trace is not None:
        _trace_calls(server, trace)
    _REGISTRIES[server] = registry
    return server


def _register_metric_tools(server: MCPServer, ctx: _ToolContext) -> None:
    store = ctx.store
    layer = ctx.layer
    registry = ctx.registry
    row_limit = ctx.row_limit

    @server.tool()
    @mcp_result
    def list_metrics() -> dict[str, Any]:
        """List every governed metric: name, description, dimensions, time grain, synonyms.

        `cumulative: true` marks a running total and `window` a trailing aggregate;
        both share their expr with the plain metric, so read them before picking one.
        Call this first. Use query_metric to evaluate one."""
        metrics = catalog_metrics(layer)
        for summary in metrics:
            if summary["time_dimension"] is not None:
                summary["time_dimension"]["default_grain"] = summary["time_dimension"]["grain"]
        payload: dict[str, Any] = {"metrics": metrics}
        problems = layer.problems() if isinstance(layer, WorkspaceLayer) else []
        if problems:
            payload["errors"] = problems
        if not metrics:
            payload["hint"] = (
                "No metrics defined. Add a metrics.yaml to the project root or a "
                "'metrics:' block to a dashboard yaml."
            )
        return payload

    @server.tool()
    @mcp_result
    def get_metric(name: str, dashboard: str | None = None) -> dict[str, Any]:
        """Full definition of one metric, including its SQL expression, base relation,
        default filters, and (redacted) data source.

        `dashboard` scopes the lookup when a name is defined inline by more than
        one dashboard; without it such a name is refused rather than guessed."""
        resolved = layer.resolve(name, dashboard)
        summary = metric_detail(resolved)
        if summary["time_dimension"] is not None:
            summary["time_dimension"]["default_grain"] = summary["time_dimension"]["grain"]
        summary["owners"] = resolved.definition.owners
        return summary

    @server.tool()
    @mcp_result
    def query_metric(
        name: str,
        dimensions: list[str] | None = None,
        grain: str | None = None,
        filters: dict[str, Any] | None = None,
        start: str | None = None,
        end: str | None = None,
        dashboard: str | None = None,
        compare: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Evaluate a metric, optionally grouped by dimensions and/or a time grain.

        - dimensions: names from the metric's declared dimensions
        - grain: one of hour/day/week/month/quarter/year (groups by the time dimension)
        - filters: {dimension_name: value} or {dimension_name: {"op": ">=", "value": x}}
          or {dimension_name: [v1, v2]} for IN. A value is a scalar or a list of
          scalars; any other shape is refused rather than bound. A list only goes
          with no op, "=" or "in" (all mean IN); ">", "!=" and the rest take one value.
        - start/end: inclusive bounds on the metric's time dimension. Either an
          ISO date ('2026-06-01') or a relative token: '-30d', 'last_30_days',
          'mtd', 'ytd', 'today'. A token names a *window*, so it resolves to that
          window's edge for the position it is in: start='-30d' is 30 days ago,
          but end='-30d' is today, not 30 days ago. To end a range in the past,
          pass an ISO date.
        - dashboard: scopes an inline metric, and applies that dashboard's
          filter defaults (daterange and matching dimension selects). Explicit
          start/end overlay the daterange default. Whatever the dashboard
          narrowed that you did not ask for comes back in `scope_note`.
        - compare: previous_period | yoy. Runs the prior window and returns it
          plus a delta. Needs both start and end (a start alone is an open
          range with no length to shift) or a dashboard daterange default.
        - limit: the most rows you want back. Omitted, the only cap is the
          server's own row limit (`sqldash mcp --row-limit`, 1000 by default),
          which is the cap the CLI and the HTTP API use for the same query.
          A limit you pass is compiled into the SQL as `LIMIT <limit + 1>` (or
          the dialect's `FETCH FIRST`), so the warehouse stops early and the extra
          row is how `truncated` knows. SQL Server takes no such clause; there the
          server stops reading after that many rows instead.

        Returns the compiled SQL plus rows. `row_limit` is the cap that was
        applied and `truncated` says whether it clipped the answer: when it is
        true there are more rows than you received, so read the `note` and query
        again rather than reasoning over the short series. Values are safely
        bound; only declared dimension names are accepted."""
        if dimensions is None:
            dimensions = []
        if grain is not None and grain not in GRAINS:
            return {"error": f"unknown grain '{grain}' — valid grains: {', '.join(GRAINS)}"}
        dash_obj = None
        if dashboard is not None:
            dash_obj, _, _ = store.load(dashboard)
        if compare is not None and compare not in COMPARE_MODES:
            return {"error": f"unknown compare '{compare}' — use {' or '.join(COMPARE_MODES)}"}
        cap = row_limit if limit is None else min(int(limit), row_limit)
        if cap <= 0:
            return {"error": "limit must be positive"}
        bound_kwargs = {
            "layer": layer,
            "name": name,
            "scope": dashboard,
            "dash": dash_obj,
            "params": filters if dash_obj is not None else None,
            "dimensions": dimensions,
            "grain": grain,
            "filters": filters,
            "start": start,
            "end": end,
            "limit": None if limit is None else cap + 1,
        }
        bound = bind_metric(**bound_kwargs)
        result = registry.run_bound(bound, cap)
        payload = _result_payload(bound.sql, result, cap)
        # Its own field, never inside the rows an agent parses (#676, #680).
        note = scope_note(bound.scope, dashboard)
        if note is not None:
            payload["scope_note"] = note
        if compare is None:
            return payload
        payload["compare"] = compare_metric(
            compare,
            bound,
            result,
            rebind=lambda start, end: bind_metric(**{**bound_kwargs, "start": start, "end": end}),
            run=lambda previous: registry.run_bound(previous, cap),
            grain=grain,
            dimensions=dimensions,
            dash=dash_obj,
        ).payload()
        return payload


def _register_source_tools(server: MCPServer, ctx: _ToolContext) -> None:
    store = ctx.store
    layer = ctx.layer
    registry = ctx.registry

    @server.tool()
    @mcp_result
    def list_sources() -> dict[str, Any]:
        """The data sources this project runs against (credentials redacted)."""
        return {
            "sources": {
                _mcp_source_label(e): redact_source(e.source) for e in labeled_sources(store, layer)
            }
        }

    @server.tool()
    @mcp_result
    def get_schema(source: str | None = None) -> dict[str, Any]:
        """Tables and columns of a data source — what exists to build metrics on.

        `source` is a key from list_sources (e.g. "metrics.yaml" or
        "demo.source"). The older MCP spelling `dashboard:demo` is still
        accepted. Omitted, it auto-selects: the metrics.yaml source
        when exactly one exists, else the lone dashboard's main source when the
        project has exactly one dashboard; otherwise an error lists the options
        (named `sources:` entries never participate in auto-selection). Returns
        structure only, never rows. Each table and column carries `sql`: the
        identifier as it must be written in a query, quoted when the name is
        case-sensitive or not a plain identifier (write `sql`, not `name`)."""
        entry = _select_source(labeled_sources(store, layer), source)
        if isinstance(entry, dict):
            return entry
        with registry.connection(entry.source, entry.base_dir) as connector:
            tables = connector.introspect()
            quote = connector.sql_identifier
        return {
            "source": entry.label,
            "source_type": source_label(entry.source),
            "tables": [
                {
                    "schema": t.schema,
                    "name": t.name,
                    "sql": ".".join(quote(part) for part in (t.schema, t.name) if part),
                    "columns": [
                        {"name": c[0], "type": c[1], "sql": quote(c[0])} for c in t.columns
                    ],
                }
                for t in tables
            ],
        }


def _register_validation_tools(server: MCPServer, ctx: _ToolContext) -> None:
    store = ctx.store
    layer = ctx.layer
    registry = ctx.registry

    @server.tool()
    @mcp_result
    def validate_metrics(
        yaml_text: str, check_schema: bool = True, repo: str | None = None
    ) -> dict[str, Any]:
        """Validate candidate metrics.yaml content WITHOUT writing anything.

        Anything that would fail `sqldash lint` appears in `errors` and sets
        `valid` to false; `lint` carries warnings only. The exception: in a
        workspace with more than one repo and no `repo`, a source that reads
        repo files cannot be checked, so those checks are skipped and `lint`
        says so.

        Parses, lints the source config, dry-run compiles every metric (all
        dimensions + default grain), and — when the source is reachable —
        checks that relations and dimensions exist in the live schema
        (structure only, no data is read). Returns compiled SQL per metric so
        you can eyeball what would run. Write the file yourself once valid;
        sqldash's write governance is git + PR.

        In a workspace with more than one repo, pass `repo` (the repo this
        metrics.yaml belongs to) so its source resolves where `sqldash lint`
        resolves it."""
        repos = getattr(store, "repos", None)
        if repo is not None and repos is not None and repo not in repos:
            return {"error": f"no repo named '{repo}' (repos: {', '.join(sorted(repos))})"}
        return run_validate_metrics(
            yaml_text,
            store=store,
            layer=layer,
            registry=registry,
            check_schema=check_schema,
            repo=repo,
        )

    @server.tool()
    @mcp_result
    def validate_dashboard(
        yaml_text: str, check_sql: bool = True, name: str | None = None
    ) -> dict[str, Any]:
        """Validate a candidate dashboard YAML WITHOUT writing anything.

        Runs exactly what `sqldash lint` runs — source config, template tags,
        metric refs and their dimensions, params vs filters — then renders each
        tile's SQL with inactive filters off and probes it against the source
        (`check_sql`, on by default), so a typo'd table or column is caught here
        rather than in the browser. Returns the rendered SQL and per-tile
        structure so you can confirm what you meant. Write the file yourself
        once valid; sqldash's write governance is git + PR.

        Pass `name` (the dashboard this YAML will be saved as) whenever you know
        it — without it an inline metric a stored dashboard already defines can
        only be reported as a note, because that stored dashboard may be this
        same candidate. With `name` it is a verdict either way.
        In a workspace with more than one repo, `name` is 'repo/dashboard', which
        is also how relative source paths find that repo's files; without it,
        checks that need those files are skipped and `lint` says so."""
        repos = getattr(store, "repos", None)
        repo, slash, _ = (name or "").partition("/")
        if slash and repos is not None and len(repos) > 1 and repo not in repos:
            return {"error": f"no repo named '{repo}' (repos: {', '.join(sorted(repos))})"}
        return run_validate_dashboard(
            yaml_text,
            store=store,
            layer=layer,
            registry=registry,
            check_sql=check_sql,
            name=name,
        )


def _register_dashboard_tools(server: MCPServer, ctx: _ToolContext) -> None:
    store = ctx.store

    @server.tool()
    @mcp_result
    def get_dashboards() -> dict[str, Any]:
        """The project's dashboards: tiles, which metrics they use, and their raw
        SQL queries (useful context for how this team actually slices its data)."""
        dashboards = []
        for record in catalog_dashboards(store):
            if record.error is not None:
                dashboards.append(
                    {
                        "name": record.name,
                        "file": record.path.name,
                        "error": record.error,
                    }
                )
                continue
            dashboard = record.dashboard
            dashboards.append(
                {
                    "name": record.name,
                    "title": dashboard.title,
                    "description": dashboard.description,
                    "source_type": source_label(dashboard.source),
                    "filters": [f.name for f in dashboard.filters],
                    "tiles": [
                        {
                            "id": w.id,
                            "title": w.title,
                            "metric": w.metric.name if w.metric else None,
                            "query": w.query,
                        }
                        for w in dashboard.tiles
                        if w.type == "chart"
                    ],
                    "queries": dashboard.queries,
                }
            )
        return {"dashboards": dashboards}


def _register_sql_tools(server: MCPServer, ctx: _ToolContext) -> None:
    store = ctx.store
    layer = ctx.layer
    registry = ctx.registry
    row_limit = ctx.row_limit

    @server.tool()
    @mcp_result
    def run_sql(sql: str, limit: int = 100, source: str | None = None) -> dict[str, Any]:
        """Run one raw read statement (SELECT, WITH, SHOW, DESCRIBE, EXPLAIN,
        VALUES, TABLE) against one data source and name it in the result's
        `source`. `limit` caps the rows RETURNED; it does not bound what the
        statement does.

        `source` is a key from list_sources, picked the same way get_schema
        picks one. Omitted, it auto-selects the metrics.yaml source when exactly
        one exists, else the lone dashboard's main source when there is exactly
        one dashboard; otherwise it errors listing the options rather than
        guessing (named `sources:` entries never participate in auto-selection).

        Writing statements (COPY, CREATE, INSERT, UPDATE, DELETE, ATTACH,
        EXPORT, SET, PRAGMA, CALL, ...) and known side-effecting functions
        (enable_logging, checkpoint, nextval, ...) are refused by a keyword check
        on the statement text, not by the engine, so an unlisted function with
        side effects can still run — the credential's own grants are the real
        guardrail."""
        statement = sql.strip().rstrip(";").strip()
        violation = read_only_violation(statement)
        if violation is not None:
            return {"error": violation}
        n = int(limit)
        if n < 0:
            return {"error": "limit must be >= 0"}
        entry = _select_source(labeled_sources(store, layer), source)
        if isinstance(entry, dict):
            return entry
        capped = min(n, row_limit)
        result = registry.run_sync(entry.source, entry.base_dir, statement, [], capped)
        return {"source": entry.label, **_result_payload(statement, result, capped)}


def _agent_tool(tool: ResolvedTool, registry: ExecutionRegistry, row_limit: int):
    """A tool from agents.yaml as a callable MCP can introspect: the declared params
    become keyword-only arguments so `tools/list` advertises the right schema.

    A declared name is any identifier, and mcp derives its argument model from the
    Python signature: a keyword (`class`) is not a valid parameter, `_x` is refused
    by mcp, and `model_config` by pydantic (#514). So each Python parameter is a
    stand-in (`p0`, `p1`, ...) carrying the declared name as its field alias,
    which is what the schema advertises and what the call receives.

    A number is advertised as one but accepted as anything: a `float` annotation
    rounded integers past 2**53 before the tool saw them and let `"nan"` through,
    so `tool_arguments` checks it with the same rule as every other surface."""

    def call(**arguments: Any) -> dict[str, Any]:
        return run_tool(tool, arguments, registry, row_limit)

    def annotation(name: str, p) -> Any:
        if p.options:
            return Annotated[Any, Field(alias=name, json_schema_extra=_options_schema(p.options))]
        if p.type == "number":
            return Annotated[Any, Field(alias=name, json_schema_extra={"type": "number"})]
        return Annotated[str, Field(alias=name)]

    params = [
        inspect.Parameter(
            f"p{index}",
            inspect.Parameter.KEYWORD_ONLY,
            default=inspect.Parameter.empty if p.default is None else p.default,
            annotation=annotation(name, p),
        )
        for index, (name, p) in enumerate(tool.definition.params.items())
    ]
    call.__name__ = tool.name
    call.__signature__ = inspect.Signature(params, return_annotation=dict[str, Any])
    call.__annotations__ = {p.name: p.annotation for p in params} | {"return": dict[str, Any]}
    return mcp_result(call)


def _options_schema(options: list[Any]) -> dict[str, Any]:
    """A select's options as the JSON schema `tools/list` advertises.

    Typing every select `str` advertised `options: [1, 2, 3, 4]` as a string
    with the numeric default `1`, then pydantic refused the number and the
    options check the string, so no explicit value could be sent (#599). The
    argument is accepted as anything and `tool_arguments` matches it to an
    option, so a wrong value is the `{"error"}` verdict, not a protocol error.
    An option JSON cannot carry (a YAML date) drops the enum; the options
    check still holds."""
    kinds = {json_kind(option) for option in options}
    if None in kinds:
        return {}
    if kinds == {"integer", "number"}:
        kinds = {"number"}
    plain = {"boolean": bool, "integer": int, "number": float, "string": str}
    schema: dict[str, Any] = {"enum": [plain[json_kind(option)](option) for option in options]}
    if len(kinds) == 1:
        schema["type"] = kinds.pop()
    return schema


def _register_agents(
    server: MCPServer, agents, registry: ExecutionRegistry, row_limit: int
) -> None:
    """agents.yaml → one MCP prompt per agent and one MCP tool per data tool.

    The prompt is the agent: a host that picks it runs the instructions with its
    own model. sqldash serves definitions and executes the tools; it never calls
    a model. A broken agents.yaml must not take the metrics surface down with
    it, so a SemanticError here is reported on stderr by the caller's lint, not
    raised out of server construction. The same holds one level down: a single
    tool or prompt the MCP framework refuses to build is skipped with a line on
    stderr (stdout is the protocol), and everything else is still served (#514)."""
    try:
        tools = agents.all_tools()
        resolved = agents.all_agents()
    except SemanticError:
        return
    for tool in tools:
        lines = [tool.definition.description, f"Signature: {tool_signature(tool)}"]
        for name, p in tool.definition.params.items():
            if p.description:
                lines.append(f"{name}: {p.description}")
        try:
            server.add_tool(
                _agent_tool(tool, registry, row_limit), name=tool.name, description=" ".join(lines)
            )
        except Exception as exc:
            _skipped("tool", tool.name, exc)
    for agent in resolved:
        try:
            server.add_prompt(
                Prompt.from_function(
                    _agent_prompt(agent), name=agent.name, description=agent.definition.description
                )
            )
        except Exception as exc:
            _skipped("agent", agent.name, exc)


def _skipped(kind: str, name: str, exc: Exception) -> None:
    print(f"sqldash mcp: skipped {kind} '{name}' from agents.yaml: {exc}", file=sys.stderr)


def _agent_prompt(agent):
    """A zero-argument prompt function; a default-argument closure would advertise
    the agent object as a prompt argument."""

    def prompt() -> str:
        return render_prompt(agent)

    return prompt


def _call_result(result: Any) -> tuple[Any, bool]:
    """(payload, is_error) from whatever tools/call returned: the dict the server
    builds for a normal call, or the CallToolResult a middleware short-circuited with."""
    if isinstance(result, Mapping):
        structured = result.get("structuredContent", result.get("structured_content"))
        content = result.get("content") or []
        is_error = bool(result.get("isError", result.get("is_error")))
    else:
        structured = getattr(result, "structured_content", None)
        content = getattr(result, "content", None) or []
        is_error = bool(getattr(result, "is_error", False))
    if structured is not None:
        return structured, is_error or _is_error_payload(structured)
    texts = []
    for item in content:
        text = item.get("text") if isinstance(item, Mapping) else getattr(item, "text", None)
        if text:
            texts.append(text)
    payload: Any = "\n".join(texts)
    with contextlib.suppress(ValueError):
        payload = json.loads(payload)
    return payload, is_error or _is_error_payload(payload)


def _is_error_payload(payload: Any) -> bool:
    """Every refusal on this surface is `{"error": ...}` and the framework never sets
    the is_error flag for it, so error-ness has to be read off the payload."""
    return isinstance(payload, Mapping) and "error" in payload


def _trace_calls(server: MCPServer, path: Path) -> None:
    """Append one JSON line per tools/call — name, arguments, result or error —
    so `sqldash agent eval` can grade what the host asked without a judge model.
    Installed *before* the unknown-argument guard so refused calls are traced
    too; a refusal is exactly the kind of thing an eval wants to see."""

    async def trace(ctx, call_next):
        if ctx.method != "tools/call" or not isinstance(ctx.params, Mapping):
            return await call_next(ctx)
        result = await call_next(ctx)
        payload, is_error = _call_result(result)
        line = {
            "ts": time.time(),
            "tool": ctx.params.get("name"),
            "arguments": ctx.params.get("arguments") or {},
            "result": payload,
            "is_error": is_error,
        }
        with contextlib.suppress(OSError):
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(line, default=str) + "\n")
        return result

    server.middleware.insert(0, trace)
