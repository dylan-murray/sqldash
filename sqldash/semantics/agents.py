"""Agents-as-code: agents.yaml resolved against the semantic layer, rendered as MCP
prompts, and its data tools run through the same bind path as query_metric."""

from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from sqldash.connectors.engine import paramstyle_for
from sqldash.models.agents import (
    RESERVED_TOOL_NAMES,
    AgentDef,
    AgentsFile,
    ToolDef,
    VerifiedExample,
    param_ref,
)
from sqldash.models.results import result_payload
from sqldash.params import ISO_DATE_TIME, ParamError, bind_sql, finite_number
from sqldash.period import compare_window
from sqldash.project.store import DashboardStore, WorkspaceStore, format_validation_error, yaml
from sqldash.semantics.bind import BoundQuery, bind_metric, bind_resolved
from sqldash.semantics.compare import compare_metric
from sqldash.semantics.layer import (
    ResolvedMetric,
    SemanticError,
    SemanticLayer,
    WorkspaceLayer,
    repo_problem,
)

AGENTS_FILES = ("agents.yaml", "agents.yml")
INSTRUCTIONS_WARN_CHARS = 4000


class AgentNotFoundError(SemanticError):
    """No agent by that name in the project (or workspace)."""


@dataclass(frozen=True)
class ResolvedTool:
    """A tool plus what it needs to run: the layer for metric bundles, the project
    source and base dir for authored SQL. `name` is repo-prefixed in a workspace."""

    name: str
    definition: ToolDef
    layer: SemanticLayer
    source: Any
    base_dir: Path | None
    repo: str | None = None


@dataclass(frozen=True)
class ResolvedAgent:
    name: str
    definition: AgentDef
    tools: tuple[ResolvedTool, ...]
    layer: SemanticLayer
    repo: str | None = None


def parse_agents_file(text: str) -> AgentsFile:
    """Parse agents.yaml text, folding YAML and schema errors into SemanticError."""
    try:
        data = yaml.load(io.StringIO(text))
    except Exception as exc:
        raise SemanticError(f"agents.yaml: invalid YAML: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise SemanticError("agents.yaml must be a YAML mapping")
    try:
        return AgentsFile.model_validate(data)
    except ValidationError as exc:
        errors = "; ".join(format_validation_error(e) for e in exc.errors())
        raise SemanticError(f"agents.yaml: {errors}") from exc


class AgentLayer:
    """One project's agents.yaml, read against that project's semantic layer."""

    def __init__(self, store: DashboardStore, layer: SemanticLayer) -> None:
        self.store = store
        self.layer = layer

    def agents_path(self) -> Path | None:
        for name in AGENTS_FILES:
            path = self.store.root / name
            if path.is_file():
                return path
        return None

    def agents_file(self) -> AgentsFile | None:
        path = self.agents_path()
        if path is None:
            return None
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise SemanticError(f"agents.yaml: file is not valid UTF-8: {exc}") from exc
        return parse_agents_file(text)

    def _project_source(self) -> tuple[Any, Path | None]:
        mf = self.layer.metrics_file()
        if mf is None:
            return None, None
        return mf.source, self.store.root

    def all_tools(self) -> list[ResolvedTool]:
        af = self.agents_file()
        if af is None:
            return []
        source, base_dir = self._project_source()
        return [
            ResolvedTool(name, definition, self.layer, source, base_dir)
            for name, definition in af.tools.items()
        ]

    def all_agents(self) -> list[ResolvedAgent]:
        af = self.agents_file()
        if af is None:
            return []
        tools = {t.name: t for t in self.all_tools()}
        return [
            ResolvedAgent(name, agent, tuple(tools[t] for t in agent.tools), self.layer)
            for name, agent in af.agents.items()
        ]

    def resolve(self, name: str) -> ResolvedAgent:
        for agent in self.all_agents():
            if agent.name == name:
                return agent
        available = ", ".join(a.name for a in self.all_agents()) or "(none defined)"
        raise AgentNotFoundError(f"no agent named '{name}' — available agents: {available}")

    def check_references(self, repo: str | None = None) -> tuple[list[str], list[str]]:
        """(errors, warnings) for everything agents.yaml points at outside itself.
        `repo` is the workspace name this project is served under, so an eval may
        scope a dashboard as `repo/name` the way listings print it."""
        af = self.agents_file()
        if af is None:
            return [], []
        errors: list[str] = []
        warnings: list[str] = []
        metrics = {m.name: m for m in self.layer.all_metrics()}
        source, _ = self._project_source()
        dashboards = set(self.store.discover())
        if repo:
            dashboards |= {f"{repo}/{name}" for name in list(dashboards)}
        for name, tool in af.tools.items():
            if tool.sql is not None and source is None:
                errors.append(
                    f"tool '{name}' has sql but there is no metrics.yaml source to run it on"
                )
            for query in tool.queries:
                metric = metrics.get(query.metric)
                if metric is None:
                    errors.append(f"tool '{name}' queries unknown metric '{query.metric}'")
                    continue
                if metric.ambiguous_with:
                    errors.append(
                        f"tool '{name}' queries '{query.metric}', which is defined inline by "
                        f"more than one dashboard and cannot be resolved by name"
                    )
                declared = _dimension_names(metric)
                for dim in [*query.dimensions, *query.filters]:
                    if dim not in declared:
                        errors.append(
                            f"tool '{name}': metric '{query.metric}' has no dimension '{dim}' "
                            f"(declared: {', '.join(sorted(declared)) or 'none'})"
                        )
        for name, agent in af.agents.items():
            allowed = agent.metrics or list(metrics)
            for metric_name in agent.metrics:
                if metric_name not in metrics:
                    errors.append(f"agent '{name}' allows unknown metric '{metric_name}'")
            reachable: set[str] = set()
            for metric_name in allowed:
                if metric_name in metrics:
                    reachable |= _dimension_names(metrics[metric_name])
            for dim in agent.dimensions:
                if dim not in reachable:
                    errors.append(
                        f"agent '{name}' allows dimension '{dim}', "
                        f"which none of its metrics declare"
                    )
            if agent.metrics:
                for tool_name in agent.tools:
                    for query in af.tools[tool_name].queries:
                        if query.metric in metrics and query.metric not in agent.metrics:
                            warnings.append(
                                f"agent '{name}' lists tool '{tool_name}', which queries "
                                f"'{query.metric}' outside the agent's metrics allow-list"
                            )
            resolved_agent = ResolvedAgent(
                name, agent, tuple(t for t in self.all_tools() if t.name in agent.tools), self.layer
            )
            for example in agent.verified:
                try:
                    prepare_verified(resolved_agent, example)
                except (SemanticError, ParamError) as exc:
                    errors.append(f"agent '{name}' verified '{example.name}': {exc}")
            errors.extend(_eval_errors(name, agent, af, metrics, reachable, dashboards))
            allowed_ok = [m for m in allowed if m in metrics]
            if not allowed_ok:
                warnings.append(
                    f"agent '{name}' has no metrics it can query "
                    f"(empty layer or a typo'd allow-list)"
                )
            if agent.sql:
                warnings.append(f"agent '{name}' allows raw SQL (sql: true)")
            if not agent.sample_questions:
                warnings.append(f"agent '{name}' has no sample_questions")
            if len(agent.instructions) > INSTRUCTIONS_WARN_CHARS:
                warnings.append(
                    f"agent '{name}' instructions are {len(agent.instructions)} characters "
                    f"(warn above {INSTRUCTIONS_WARN_CHARS})"
                )
            for use in agent.uses:
                warnings.append(
                    f"agent '{name}' uses host-side server '{use.server}' — "
                    "sqldash cannot verify it"
                )
        return errors, warnings


def _eval_errors(
    name: str, agent: AgentDef, af: AgentsFile, metrics, reachable: set[str], dashboards: set[str]
) -> list[str]:
    """An eval that expects something the agent cannot do is a contradiction, not a test."""
    errors: list[str] = []
    allowed = set(agent.metrics or metrics)
    for i, case in enumerate(agent.evals, 1):
        where = f"agent '{name}' eval {i}"
        expect = case.expect
        if expect is None or expect.tool is None:
            continue
        if expect.tool in RESERVED_TOOL_NAMES:
            if expect.tool == "run_sql" and not agent.sql:
                errors.append(f"{where} expects run_sql but the agent has sql: false")
            if expect.tool == "query_metric":
                metric = expect.args.get("name")
                if not metric:
                    errors.append(f"{where} expects query_metric without args.name")
                elif metric not in allowed:
                    errors.append(
                        f"{where} expects query_metric on '{metric}', outside the agent's metrics"
                    )
                named = metrics.get(metric) if metric in allowed else None
                declared = _dimension_names(named) if named is not None else reachable
                for dim in expect.args.get("dimensions") or []:
                    if dim not in declared:
                        owner = f"metric '{metric}'" if named is not None else "any allowed metric"
                        errors.append(
                            f"{where} expects dimension '{dim}', which {owner} does not declare"
                        )
                dashboard = expect.args.get("dashboard")
                if dashboard is not None and dashboard not in dashboards:
                    errors.append(f"{where} scopes to unknown dashboard '{dashboard}'")
                windowed = (expect.args.get("start") and expect.args.get("end")) or dashboard
                if expect.args.get("compare") and not windowed:
                    errors.append(
                        f"{where} expects a compare without start and end (or a dashboard "
                        f"whose daterange default supplies the window)"
                    )
            continue
        if expect.tool not in agent.tools:
            known = ", ".join(sorted([*RESERVED_TOOL_NAMES, *agent.tools]))
            errors.append(f"{where} expects unknown tool '{expect.tool}' (known: {known})")
            continue
        tool = af.tools[expect.tool]
        for arg in expect.args:
            if arg not in tool.params:
                errors.append(f"{where} passes '{arg}' to {expect.tool}, which has no such param")
        missing = [name for name in tool.required_params() if name not in expect.args]
        if missing:
            errors.append(
                f"{where} calls {expect.tool} without required param(s): {', '.join(missing)}"
            )
    return errors


def errors_for_agent(agent: ResolvedAgent, messages: list[str]) -> list[str]:
    """The check_references messages about this agent and no other. Workspace
    messages are `repo: agent '<short>' ...`, so a same-named agent in another
    repo must not match."""
    short = agent.name.split("/", 1)[1] if agent.repo else agent.name
    prefix = f"{agent.repo}: " if agent.repo else ""
    needle = f"{prefix}agent '{short}'"
    return [m for m in messages if m.startswith(needle)]


def _finding_about(message: str, needle: str) -> bool:
    """True when ``message`` is about ``needle``, not a later substring.

    ``agent 'a' lists tool 'rev_tool', …`` is about agent a, not about
    whoever else also lists rev_tool. ``agent 'ab'`` is not ``agent 'a'``.
    """
    if not message.startswith(needle):
        return False
    rest = message[len(needle) :]
    return not rest or not (rest[0].isalnum() or rest[0] == "_")


def findings_for_agent(
    agent: ResolvedAgent, errors: list[str], warnings: list[str]
) -> list[tuple[str, str]]:
    """check_references lines about this agent or the tools it lists."""
    short = agent.name.split("/", 1)[1] if agent.repo else agent.name
    prefix = f"{agent.repo}: " if agent.repo else ""
    needles = [f"{prefix}agent '{short}'"]
    for tool in agent.tools:
        raw = tool.name.split("__", 1)[1] if agent.repo else tool.name
        needles.append(f"{prefix}tool '{raw}'")
    out: list[tuple[str, str]] = []
    for level, messages in (("error", errors), ("warning", warnings)):
        for message in messages:
            if any(_finding_about(message, n) for n in needles):
                out.append((level, message))
    return out


class WorkspaceAgentLayer:
    """Agents of several repos: agents are 'repo/agent', tools 'repo__tool' (MCP tool
    names cannot carry a slash)."""

    def __init__(self, layers: dict[str, AgentLayer]) -> None:
        self.layers = layers

    def _loadable(
        self,
    ) -> tuple[dict[str, tuple[list[ResolvedTool], list[ResolvedAgent]]], list[str]]:
        loaded: dict[str, tuple[list[ResolvedTool], list[ResolvedAgent]]] = {}
        problems: list[str] = []
        for repo, layer in self.layers.items():
            try:
                loaded[repo] = (layer.all_tools(), layer.all_agents())
            except SemanticError as exc:
                problems.append(repo_problem(repo, exc))
        return loaded, problems

    def problems(self) -> list[str]:
        """The repos whose agents were skipped, and why: one repo's broken
        agents.yaml or metrics.yaml must not hide every other repo's agents."""
        return self._loadable()[1]

    def all_tools(self) -> list[ResolvedTool]:
        return [
            ResolvedTool(f"{repo}__{t.name}", t.definition, t.layer, t.source, t.base_dir, repo)
            for repo, (tools, _) in self._loadable()[0].items()
            for t in tools
        ]

    def all_agents(self) -> list[ResolvedAgent]:
        out = []
        for repo, (_, agents) in self._loadable()[0].items():
            for agent in agents:
                tools = tuple(
                    ResolvedTool(
                        f"{repo}__{t.name}", t.definition, t.layer, t.source, t.base_dir, repo
                    )
                    for t in agent.tools
                )
                out.append(
                    ResolvedAgent(
                        f"{repo}/{agent.name}", agent.definition, tools, agent.layer, repo
                    )
                )
        return out

    def resolve(self, name: str) -> ResolvedAgent:
        for agent in self.all_agents():
            if agent.name == name:
                return agent
        available = ", ".join(a.name for a in self.all_agents()) or "(none defined)"
        raise AgentNotFoundError(f"no agent named '{name}' — available agents: {available}")

    def check_references(self) -> tuple[list[str], list[str]]:
        errors, warnings = [], []
        for repo, layer in self.layers.items():
            e, w = layer.check_references(repo)
            errors.extend(f"{repo}: {m}" for m in e)
            warnings.extend(f"{repo}: {m}" for m in w)
        return errors, warnings


def agent_layer_for(store, layer) -> AgentLayer | WorkspaceAgentLayer:
    if isinstance(store, WorkspaceStore) and isinstance(layer, WorkspaceLayer):
        return WorkspaceAgentLayer(
            {repo: AgentLayer(s, layer.layers[repo]) for repo, s in store.repos.items()}
        )
    return AgentLayer(store, layer)


def _dimension_names(metric: ResolvedMetric) -> set[str]:
    names = {d.name for d in metric.definition.dimensions}
    if metric.definition.time_dimension:
        names.add(metric.definition.time_dimension.name)
    return names


def allowed_metrics(agent: ResolvedAgent) -> list[ResolvedMetric]:
    """The metrics the agent may query, in layer order; an empty allow-list means all."""
    metrics = [m for m in agent.layer.all_metrics() if not m.ambiguous_with]
    if not agent.definition.metrics:
        return metrics
    by_name = {m.name: m for m in metrics}
    return [by_name[n] for n in agent.definition.metrics if n in by_name]


def tool_signature(tool: ResolvedTool) -> str:
    parts = []
    for name, p in tool.definition.params.items():
        kind = p.type
        if p.options:
            kind = f"select[{', '.join(str(o) for o in p.options)}]"
        parts.append(f"{name}: {kind}" + (f" = {p.default}" if p.default is not None else ""))
    return f"{tool.name}({', '.join(parts)})"


def render_prompt(agent: ResolvedAgent) -> str:
    """The prompt a host runs to become this agent. Deterministic; no model involved."""
    d = agent.definition
    lines: list[str] = [f"# {d.title or agent.name}", "", d.description, "", "## Instructions", ""]
    lines.append(d.instructions.strip())
    if d.response:
        lines += ["", "## How to answer", "", d.response.strip()]
    lines += ["", "## Metrics you may use", ""]
    lines.append(
        "Evaluate these with the `query_metric` tool. Pass metric and dimension names "
        "and filter values; sqldash compiles and runs the SQL."
    )
    lines.append("")
    for metric in allowed_metrics(agent):
        md = metric.definition
        dims = ", ".join(x.name for x in md.dimensions) or "none"
        line = f"- `{metric.name}`"
        if md.title:
            line += f" ({md.title})"
        if md.description:
            line += f": {md.description}"
        line += f". Dimensions: {dims}"
        if md.time_dimension:
            line += f". Time: {md.time_dimension.name} (default grain {md.time_dimension.grain})"
        lines.append(line)
    if d.dimensions:
        lines += ["", f"Only group or filter by: {', '.join(d.dimensions)}."]
    if agent.tools:
        lines += ["", "## Tools", ""]
        for tool in agent.tools:
            lines.append(f"- `{tool_signature(tool)}`: {tool.definition.description}")
            for pname, p in tool.definition.params.items():
                if p.description:
                    lines.append(f"  - {pname}: {p.description}")
    if d.verified:
        lines += ["", "## Verified examples", ""]
        for example in d.verified:
            tool_name = f"{agent.repo}__{example.tool}" if agent.repo else example.tool
            arguments = json.dumps(example.args, default=str)
            lines.append(f"- {example.question}: call `{tool_name}` with `{arguments}`.")
    if d.sample_questions:
        lines += ["", "## Example questions", ""]
        lines += [f"- {q}" for q in d.sample_questions]
    if d.uses:
        lines += ["", "## Other services this agent uses", ""]
        lines += [f"- {u.server}: {u.purpose}" for u in d.uses]
    lines += ["", "## Rules", ""]
    if d.sql:
        lines.append(
            "- Prefer metrics and tools; `run_sql` is allowed only when they cannot answer."
        )
    else:
        lines.append("- Do not write SQL. Raw SQL is off for this agent; use metrics and tools.")
    lines.append("- Never quote a number you did not get from a tool result.")
    lines.append(
        "- If a question needs a metric or dimension not listed above, say so instead of guessing."
    )
    return "\n".join(lines) + "\n"


def tool_arguments(tool: ResolvedTool, arguments: dict[str, Any]) -> dict[str, Any]:
    """Fill defaults, refuse missing or out-of-options values. ParamError is the
    same domain error the MCP layer turns into `{"error"}`."""
    values: dict[str, Any] = {}
    for name, p in tool.definition.params.items():
        value = arguments.get(name)
        if value is None:
            value = p.default
        if value is None:
            raise ParamError(f"{tool.name}: missing argument '{name}'")
        if p.options is not None:
            option = select_option(p.options, value)
            if option is _NO_OPTION:
                choices = ", ".join(str(o) for o in p.options)
                raise ParamError(f"{tool.name}: '{name}' must be one of {choices}")
            value = option
        if p.type == "number":
            value = finite_number(value, f"{tool.name}: '{name}'")
        values[name] = value
    return values


_NO_OPTION = object()


def json_kind(value: Any) -> str | None:
    """The JSON type a YAML scalar stands for, or None if JSON has no such type.

    By isinstance, not exact type: the store's loader keeps quotes, so a quoted
    `'us'` is a ruamel str subclass that an exact-type lookup does not
    recognize. bool is checked before int because it subclasses int."""
    for kind, types in (("boolean", bool), ("integer", int), ("number", float), ("string", str)):
        if isinstance(value, types):
            return kind
    return None


def select_option(options: list[Any], value: Any) -> Any:
    """The declared option `value` names, as the option itself, else `_NO_OPTION`.

    `options: [1, 2, 3, 4]` is a legal select, and callers spell its values both
    ways: `1` from a host that read the advertised integer schema, `"1"` from one
    that did not. Both name option `1` and bind it in its declared type, so the
    SQL sees the same value the default would have bound (#599). A same-typed
    match wins, so `["1", 1]` still tells its two options apart."""
    for option in options:
        if json_kind(option) == json_kind(value) and option == value:
            return option
    for option in options:
        if _names_option(option, value):
            return option
    return _NO_OPTION


_INT_LITERAL = re.compile(r"-?\d+")
_DECIMAL_LITERAL = re.compile(r"-?\d+(\.\d+)?")


def _names_option(option: Any, value: Any) -> bool:
    if isinstance(option, bool) or isinstance(value, bool):
        return json_kind(option) == json_kind(value) and option == value
    if isinstance(option, int | float):
        if isinstance(value, str):
            # Only the plain spelling of the option: float() also takes "1.0",
            # "1e3" and " 1", which named integer options the enum never listed.
            literal = _INT_LITERAL if isinstance(option, int) else _DECIMAL_LITERAL
            if not literal.fullmatch(value):
                return False
            # int() for integers: float() rounds past 2**53 and can name a neighbour.
            value = int(value) if isinstance(option, int) else float(value)
        return isinstance(value, int | float) and option == value
    return isinstance(value, str | int | float) and str(option) == str(value)


def prepare_verified(
    agent: ResolvedAgent, example: VerifiedExample
) -> list[tuple[BoundQuery, dict, BoundQuery | None]]:
    """Validate the whole invocation offline; never certify a partial bundle."""
    tool_name = f"{agent.repo}__{example.tool}" if agent.repo else example.tool
    tool = next((t for t in agent.tools if t.name == tool_name), None)
    if tool is None or not tool.definition.queries:
        raise SemanticError(f"'{example.tool}' must be an allowed metric bundle")
    if unknown := set(example.args) - set(tool.definition.params):
        raise ParamError(f"unknown arguments: {', '.join(sorted(unknown))}")
    values = tool_arguments(tool, example.args)

    def sub(value):
        ref = param_ref(value)
        return values[ref] if ref else value

    prepared = []
    for query in tool.definition.queries:
        if agent.definition.metrics and query.metric not in agent.definition.metrics:
            raise SemanticError(f"metric '{query.metric}' is outside the agent's allow-list")
        resolved = tool.layer.resolve(query.metric)
        kwargs = {
            "dimensions": query.dimensions,
            "grain": query.grain,
            "filters": {k: sub(v) for k, v in query.filters.items()},
            "start": sub(query.start),
            "end": sub(query.end),
            "limit": query.limit,
            "paramstyle": "numeric",
        }
        for endpoint in (kwargs["start"], kwargs["end"]):
            if endpoint is not None and not ISO_DATE_TIME.fullmatch(str(endpoint)):
                raise ParamError("verified examples require absolute start/end dates")
        used = set(query.dimensions) | set(query.filters)
        if (
            query.grain or kwargs["start"] or kwargs["end"] or resolved.definition.window
        ) and resolved.definition.time_dimension:
            used.add(resolved.definition.time_dimension.name)
        if agent.definition.dimensions and (extra := used - set(agent.definition.dimensions)):
            raise SemanticError(
                f"dimensions outside the agent's allow-list: {', '.join(sorted(extra))}"
            )
        bound = bind_resolved(resolved, **kwargs)
        previous = None
        if query.compare:
            window = compare_window(query.compare, *(bound.time_range or (None, None)))
            if window is None:
                raise ParamError(f"compare '{query.compare}' needs start and end")
            previous = bind_resolved(
                resolved, **{**kwargs, "start": window["start"], "end": window["end"]}
            )
        prepared.append((bound, kwargs, previous))
    return prepared


def run_tool(tool: ResolvedTool, arguments: dict[str, Any], registry, row_limit: int) -> dict:
    values = tool_arguments(tool, arguments)
    if tool.definition.sql is not None:
        if tool.source is None:
            raise SemanticError(
                f"tool '{tool.name}' has sql but the project has no metrics.yaml source"
            )
        sql, bind = bind_sql(tool.definition.sql, values, paramstyle_for(tool.source))
        result = registry.run_sync(tool.source, tool.base_dir, sql, bind, row_limit)
        return {"tool": tool.name, "sql": sql, **result_payload(result, row_limit)}

    def sub(value: Any) -> Any:
        ref = param_ref(value)
        return values[ref] if ref else value

    results = []
    for query in tool.definition.queries:
        filters = {k: sub(v) for k, v in query.filters.items()} or None
        kwargs = {
            "layer": tool.layer,
            "name": query.metric,
            "dimensions": query.dimensions,
            "grain": query.grain,
            "filters": filters,
            "start": sub(query.start),
            "end": sub(query.end),
        }
        cap = min(query.limit, row_limit)
        bound = bind_metric(**kwargs)
        result = registry.run_bound(bound, cap)
        entry: dict[str, Any] = {
            "metric": query.metric,
            "sql": bound.sql,
            **result_payload(result, cap),
        }
        if query.compare is not None:
            entry["compare"] = compare_metric(
                query.compare,
                bound,
                result,
                rebind=lambda start, end, kw=kwargs: bind_metric(
                    **{**kw, "start": start, "end": end}
                ),
                run=lambda previous, c=cap: registry.run_bound(previous, c),
                grain=query.grain,
                dimensions=query.dimensions,
            ).payload()
        results.append(entry)
    return {"tool": tool.name, "results": results}


def tool_summary(tool: ResolvedTool) -> dict[str, Any]:
    d = tool.definition
    return {
        "name": tool.name,
        "description": d.description,
        "kind": "sql" if d.sql is not None else "metrics",
        "params": {
            name: {k: v for k, v in p.model_dump().items() if v is not None}
            for name, p in d.params.items()
        },
        "queries": [q.model_dump(exclude_defaults=True) for q in d.queries],
        "sql": d.sql,
    }


def agent_summary(agent: ResolvedAgent) -> dict[str, Any]:
    d = agent.definition
    return {
        "name": agent.name,
        "title": d.title or agent.name,
        "description": d.description,
        "metrics": [m.name for m in allowed_metrics(agent)],
        "metrics_restricted": bool(d.metrics),
        "dimensions": d.dimensions,
        "tools": [t.name for t in agent.tools],
        "sql": d.sql,
        "sample_questions": d.sample_questions,
        "verified": [e.model_dump(exclude_none=True) for e in d.verified],
        "uses": [{"server": u.server, "for": u.purpose} for u in d.uses],
    }


def agent_detail(agent: ResolvedAgent) -> dict[str, Any]:
    payload = agent_summary(agent)
    payload.update(
        instructions=agent.definition.instructions,
        response=agent.definition.response,
        tool_definitions=[tool_summary(t) for t in agent.tools],
        prompt=render_prompt(agent),
    )
    return payload
