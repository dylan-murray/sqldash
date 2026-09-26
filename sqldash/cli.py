import json
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING

import typer
import uvicorn

from sqldash.redact import mask_url_userinfo

if TYPE_CHECKING:
    from sqldash.project.sources import LabeledSource

app = typer.Typer(help="Single-file SQL dashboards. Pure Python, no cloud.", no_args_is_help=True)
export_app = typer.Typer(help="Export the semantic layer (to agents, Cortex, ...)")
app.add_typer(export_app, name="export")
import_app = typer.Typer(help="Import definitions from other BI tools into sqldash")
app.add_typer(import_app, name="import")
repo_app = typer.Typer(help="The workspace registry: repos 'sqldash serve' serves all at once")
app.add_typer(repo_app, name="repo")


def _version_callback(value: bool) -> None:
    if value:
        from sqldash import __version__

        typer.echo(f"sqldash {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show the installed sqldash version and exit",
    ),
) -> None:
    """Single-file SQL dashboards. Pure Python, no cloud."""


@repo_app.command("add")
def repo_add(
    target: str = typer.Argument(..., help="A git URL or a local directory"),
    name: str = typer.Option(None, "--name", "-n", help="Registry name (default: repo basename)"),
    branch: str = typer.Option(None, "--branch", "-b", help="Branch to serve (git URLs only)"),
) -> None:
    """Register a repo; 'sqldash serve' (no target) then serves all registered repos."""
    from sqldash.workspace import WorkspaceError, add_repo, registry_path

    try:
        repo_name, entry = add_repo(target, name=name, branch=branch)
    except WorkspaceError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    where = mask_url_userinfo(entry.get("url") or entry.get("path"))
    typer.echo(f"registered '{repo_name}' -> {where}  ({registry_path()})")
    typer.echo("serve everything with: sqldash serve")


@repo_app.command("list")
def repo_list(as_json: bool = typer.Option(False, "--json")) -> None:
    """List registered repos."""
    from sqldash.workspace import load_registry

    repos = load_registry()
    for entry in repos.values():
        if entry.get("url"):
            entry["url"] = mask_url_userinfo(entry["url"])
    if as_json:
        typer.echo(json.dumps({"repos": repos}, indent=2))
        return
    if not repos:
        typer.echo("(no repos registered — add one with 'sqldash repo add <url|path>')")
        return
    for repo_name, entry in repos.items():
        where = entry.get("url") or entry.get("path")
        suffix = f"  (branch {entry['branch']})" if entry.get("branch") else ""
        typer.echo(f"{repo_name:24} {where}{suffix}")


@repo_app.command("remove")
def repo_remove(name: str = typer.Argument(..., help="Registered repo name")) -> None:
    """Remove a repo from the registry (never touches the repo itself)."""
    from sqldash.workspace import WorkspaceError, remove_repo

    try:
        entry = remove_repo(name)
    except WorkspaceError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"removed '{name}' ({mask_url_userinfo(entry.get('url') or entry.get('path'))})")


@app.command("add", hidden=True)
def add_alias(
    target: str = typer.Argument(..., help="A git URL or a local directory"),
    name: str = typer.Option(None, "--name", "-n"),
    branch: str = typer.Option(None, "--branch", "-b"),
) -> None:
    """Shorthand for 'sqldash repo add'."""
    repo_add(target, name=name, branch=branch)


dashboard_app = typer.Typer(help="Inspect dashboards (agent/script friendly; --json everywhere)")
app.add_typer(dashboard_app, name="dashboard")
metric_app = typer.Typer(help="Inspect and evaluate semantic-layer metrics")
app.add_typer(metric_app, name="metric")
agent_app = typer.Typer(help="Inspect the agents this project serves over MCP")
app.add_typer(agent_app, name="agent")
source_app = typer.Typer(help="Inspect and test data sources")
app.add_typer(source_app, name="source")


def _open_project(target: str, branch: str | None = None):
    """Open a project for the read-only commands.

    With no target and nothing in the current directory, fall back to the
    registered repos — the same rule `serve` uses. Without this, `dashboard
    list` and friends printed "(no dashboards)" for a workspace user while
    `serve` and `mcp --all` showed everything, and the README points agents at
    exactly these commands.
    """
    from sqldash.project.store import DashboardStore, WorkspaceStore
    from sqldash.semantics import SemanticLayer
    from sqldash.semantics.layer import WorkspaceLayer

    if target == ".":
        cwd = Path(".")
        has_local = (cwd / ".sqldash").is_dir() or any(cwd.glob("*.yaml")) or any(cwd.glob("*.yml"))
        if not has_local:
            workspace = _registered_repos()
            if workspace:
                stores = {name: DashboardStore(root) for name, root in workspace}
                return (
                    WorkspaceStore(stores),
                    WorkspaceLayer({name: SemanticLayer(s) for name, s in stores.items()}),
                )

    path = _resolve_target(target, branch)
    store = DashboardStore(path.resolve())
    return store, SemanticLayer(store)


def _registered_repos():
    """Registered repos resolved to checkouts that already exist locally.

    Deliberately offline: `serve` clones and pulls, but a read-only listing must
    not do git network work behind the user's back — it would stall with no
    explanation, and a transient failure would degrade to "(no dashboards)",
    the very symptom this fallback exists to fix. A URL repo that has never
    been served is skipped; run `sqldash serve` (or `--all`) to fetch it.
    """
    from sqldash.gitrepo import repo_cache_dir
    from sqldash.workspace import load_registry

    repos = load_registry()
    if not repos:
        return None
    resolved = []
    for name, entry in repos.items():
        if entry.get("url"):
            cached = repo_cache_dir(entry["url"], entry.get("branch"))
            if cached.is_dir():
                resolved.append((name, cached))
        else:
            root = Path(entry["path"]).expanduser()
            if root.is_dir():
                resolved.append((name, root))
    listed = {name for name, _ in resolved}
    skipped = [name for name in repos if name not in listed]
    if skipped and resolved:
        # Silently incomplete is a close cousin of silently empty.
        typer.echo(
            f"note: skipping {', '.join(sorted(skipped))} — not checked out locally; "
            "run 'sqldash serve' to fetch",
            err=True,
        )
    return resolved or None


def _emit(payload, as_json: bool, table_lines) -> None:
    if as_json:
        typer.echo(json.dumps(payload, indent=2, default=str))
    else:
        for line in table_lines:
            typer.echo(line)


@dashboard_app.command("list")
def dashboard_list(
    target: str = typer.Argument(".", help="Project dir, dashboard .yaml, or git URL"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List every dashboard in the project."""
    from sqldash.models.source import source_label
    from sqldash.project.catalog import list_dashboards

    store, _ = _open_project(target)
    rows = []
    for record in list_dashboards(store):
        if record.error is not None:
            rows.append({"name": record.name, "file": record.path.name, "error": record.error})
            continue
        dashboard = record.dashboard
        rows.append(
            {
                "name": record.name,
                "title": dashboard.title,
                "file": record.path.name,
                "source": source_label(dashboard.source),
                "tiles": len(dashboard.tiles),
                "metrics_used": sorted({w.metric.name for w in dashboard.tiles if w.metric}),
            }
        )
    _emit(
        {"dashboards": rows},
        as_json,
        [
            f"{r['name']:24} {r.get('title', '?'):32} {r.get('source', '?'):10} "
            f"{r.get('tiles', '-')} tile(s)  [{r['file']}]"
            + (f"  ERROR: {r['error']}" if "error" in r else "")
            for r in rows
        ]
        or ["(no dashboards)"],
    )


@dashboard_app.command("show")
def dashboard_show(
    name: str = typer.Argument(..., help="Dashboard name"),
    target: str = typer.Option(".", "--target", "-t"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Full detail for one dashboard: source, filters, tiles, queries."""
    from sqldash.models.source import redact_source
    from sqldash.project.store import InvalidDashboardError, NotFoundError

    store, _ = _open_project(target)
    try:
        dashboard, _, _ = store.load(name)
    except NotFoundError as exc:
        known = ", ".join(sorted(store.discover())) or "(none)"
        typer.echo(f"error: {exc} — available: {known}", err=True)
        raise typer.Exit(1) from exc
    except InvalidDashboardError as exc:
        # `dashboard list` surfaces broken files as ERROR rows, so showing one is
        # the natural next step and must not be a traceback either.
        typer.echo(f"error: {name} does not parse — {exc}", err=True)
        raise typer.Exit(1) from exc
    payload = {
        "name": name,
        "title": dashboard.title,
        "description": dashboard.description,
        "source": redact_source(dashboard.source),
        "sources": {k: redact_source(v) for k, v in dashboard.sources.items()},
        "filters": [f.model_dump(exclude_none=True) for f in dashboard.filters],
        "tiles": [
            {
                "id": w.id,
                "title": w.title,
                "query": w.query,
                "metric": w.metric.name if w.metric else None,
                "type": w.type,
            }
            for w in dashboard.tiles
        ],
        "queries": dashboard.queries,
    }
    lines = [f"{dashboard.title} ({name})"]
    if dashboard.description:
        lines.append(f"  {dashboard.description}")
    lines.append(f"  filters: {', '.join(f.name for f in dashboard.filters) or '(none)'}")
    for w in payload["tiles"]:
        kind = (
            (w["metric"] and f"metric:{w['metric']}")
            or (w["query"] and f"query:{w['query']}")
            or "text"
        )
        lines.append(f"  - {w['id']:24} {kind}")
    _emit(payload, as_json, lines)


@metric_app.command("list")
def metric_list(
    target: str = typer.Argument(".", help="Project dir, dashboard .yaml, or git URL"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List every metric in the semantic layer."""
    from sqldash.project.catalog import list_metrics
    from sqldash.semantics import SemanticError

    _, layer = _open_project(target)
    try:
        metrics = list_metrics(layer)
    except SemanticError as exc:
        # One unresolvable definition made the whole listing a rich traceback,
        # while `metric query` reported the same error as a line of text. Author
        # error in a YAML file is not an internal fault.
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    problems = _report_problems(layer)
    _emit(
        {"metrics": metrics, "errors": problems},
        as_json,
        [
            f"{m['name']:24} "
            f"{_metric_label(m):28} "
            f"{m['source_type']:10} "
            f"dims: {', '.join(d['name'] for d in m['dimensions']) or '-'}"
            + (
                f"  time: {m['time_dimension']['name']}/{m['time_dimension']['grain']}"
                if m.get("time_dimension")
                else ""
            )
            + _metric_semantics(m, "  ")
            for m in metrics
        ]
        or ["(no metrics — add a metrics.yaml)"],
    )


def _report_problems(*layers) -> list[str]:
    """What a workspace listing skipped, on stderr and repo-prefixed, the way
    `source list` reports it. A single project has nothing to report here: its
    only repo failing is the SemanticError the caller already exits on."""
    problems: list[str] = []
    for layer in layers:
        for problem in layer.problems() if hasattr(layer, "problems") else ():
            if problem not in problems:
                problems.append(problem)
    for problem in problems:
        typer.echo(f"error: {problem}", err=True)
    return problems


def _metric_label(summary: dict) -> str:
    """What a listing calls a metric. A name several dashboards define inline
    cannot be resolved by that name, so showing one of their titles would claim
    a uniqueness the resolve path refuses."""
    if summary["ambiguous_with"]:
        return f"(ambiguous: {', '.join(summary['ambiguous_with'])})"
    return summary["title"]


def _metric_semantics(summary: dict, prefix: str) -> str:
    """A running total or trailing window shares its expr with the plain metric,
    so a listing that omits the flag shows the two as the same row."""
    if summary.get("cumulative"):
        return f"{prefix}cumulative: running total"
    if summary.get("window"):
        return f"{prefix}window: {summary['window']}"
    return ""


def _relation_lines(relation: dict, name: str | None) -> list[str]:
    """A relation's name is not always its table's, so the two print on separate
    lines. Inline `table:`/`sql:` metrics and derived ones have no name to show."""
    indent = "    " if name else "  "
    lines = [f"  relation: {name}"] if name else []
    if "table" in relation:
        return [*lines, f"{indent}table: {relation['table']}"]
    body = relation["sql"].strip().splitlines()
    return [*lines, f"{indent}sql:", *(f"{indent}  {line}" for line in body)]


@metric_app.command("show")
def metric_show(
    name: str = typer.Argument(..., help="Metric name"),
    target: str = typer.Option(".", "--target", "-t"),
    dashboard: str = typer.Option(
        None, "--dashboard", help="Resolve inline metrics in this dashboard's scope"
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Full definition of one metric."""
    from sqldash.project.catalog import metric_detail
    from sqldash.project.store import InvalidDashboardError, NotFoundError
    from sqldash.semantics import MetricNotFoundError, SemanticError

    _, layer = _open_project(target)
    try:
        # --dashboard names a file, so a typo surfaces as a store error rather
        # than a semantic one; both are user error, not a traceback. So is a
        # metric whose own definition cannot be resolved.
        resolved = layer.resolve(name, dashboard)
    except (SemanticError, MetricNotFoundError, NotFoundError, InvalidDashboardError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    payload = metric_detail(resolved)
    lines = [f"{payload['title']} ({name})"]
    if payload.get("description"):
        lines.append(f"  {payload['description']}")
    lines.append(f"  expr: {payload['expr']}")
    # The reader most likely to paste the expr somewhere that runs it (#677).
    if payload.get("expr_note"):
        lines.append(f"  note: {payload['expr_note']}")
    lines.extend(_relation_lines(payload["relation"], resolved.definition.relation))
    lines.append(f"  dimensions: {', '.join(d['name'] for d in payload['dimensions']) or '-'}")
    if payload.get("time_dimension"):
        td = payload["time_dimension"]
        lines.append(f"  time: {td['name']} (default grain {td['grain']})")
    if semantics := _metric_semantics(payload, "  "):
        lines.append(semantics)
    _emit(payload, as_json, lines)


@agent_app.command("list")
def agent_list(
    target: str = typer.Argument(".", help="Project dir, dashboard .yaml, or git URL"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List every agent in agents.yaml."""
    from sqldash.semantics import SemanticError
    from sqldash.semantics.agents import agent_layer_for, agent_summary

    store, layer = _open_project(target)
    agent_layer = agent_layer_for(store, layer)
    try:
        agents = [agent_summary(a) for a in agent_layer.all_agents()]
    except SemanticError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    problems = _report_problems(agent_layer)
    _emit(
        {"agents": agents, "errors": problems},
        as_json,
        [
            f"{a['name']:24} "
            f"{a['title']:28} "
            f"metrics: {len(a['metrics'])}{'' if a['metrics_restricted'] else ' (all)'}  "
            f"tools: {', '.join(a['tools']) or '-'}"
            for a in agents
        ]
        or ["(no agents — add an agents.yaml)"],
    )


@agent_app.command("show")
def agent_show(
    name: str = typer.Argument(..., help="Agent name"),
    target: str = typer.Option(".", "--target", "-t"),
    as_json: bool = typer.Option(False, "--json"),
    prompt: bool = typer.Option(False, "--prompt", help="Print only the rendered MCP prompt"),
) -> None:
    """Full definition of one agent, including the prompt a host receives."""
    from sqldash.semantics import SemanticError
    from sqldash.semantics.agents import (
        agent_detail,
        agent_layer_for,
        findings_for_agent,
        tool_signature,
    )

    store, layer = _open_project(target)
    agents = agent_layer_for(store, layer)
    try:
        agent = agents.resolve(name)
        errors, warnings = agents.check_references()
    except SemanticError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    notes = findings_for_agent(agent, errors, warnings)
    payload = agent_detail(agent)
    payload["findings"] = [{"level": level, "message": message} for level, message in notes]
    if prompt:
        typer.echo(payload["prompt"], nl=False)
        prompt_errors = [message for level, message in notes if level == "error"]
        if prompt_errors:
            for message in prompt_errors:
                typer.echo(f"error: {message}", err=True)
            raise typer.Exit(1)
        return
    lines = [f"{payload['title']} ({name})", f"  {payload['description']}", "  instructions:"]
    lines += [f"    {line}" for line in payload["instructions"].strip().splitlines()]
    if payload["response"]:
        lines.append("  response:")
        lines += [f"    {line}" for line in payload["response"].strip().splitlines()]
    scope = "" if payload["metrics_restricted"] else " (all)"
    lines.append(f"  metrics{scope}: {', '.join(payload['metrics']) or '-'}")
    lines.append(f"  dimensions: {', '.join(payload['dimensions']) or '(any declared)'}")
    lines.append(f"  raw sql: {'allowed' if payload['sql'] else 'off'}")
    lines.append(f"  tools: {', '.join(tool_signature(t) for t in agent.tools) or '-'}")
    if payload["sample_questions"]:
        lines.append("  sample questions:")
        lines += [f"    - {q}" for q in payload["sample_questions"]]
    if payload["uses"]:
        lines.append("  uses: " + ", ".join(f"{u['server']} ({u['for']})" for u in payload["uses"]))
    for level, message in notes:
        lines.append(f"  {level}: {message}")
    _emit(payload, as_json, lines)
    if any(level == "error" for level, _ in notes):
        raise typer.Exit(1)


@agent_app.command("eval")
def agent_eval(
    name: str = typer.Argument(..., help="Agent name"),
    target: str = typer.Option(".", "--target", "-t"),
    runner: str = typer.Option(
        None,
        "--runner",
        help=(
            "Shell command that answers a question, e.g. "
            "'claude -p --append-system-prompt \"$(cat $SQLDASH_AGENT_PROMPT_FILE)\"'. "
            "The question is appended as its last argument and piped to stdin. "
            "Without it only the static checks run"
        ),
    ),
    timeout: float = typer.Option(300.0, help="Seconds per question"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Run an agent's evals: static checks, then (with --runner) each question answered
    by the runner and graded against the result sqldash computes itself. Exit 1 when
    any case fails."""
    from sqldash.semantics import SemanticError
    from sqldash.semantics.agents import agent_layer_for, errors_for_agent
    from sqldash.semantics.evals import run_evals

    store, layer = _open_project(target)
    agents = agent_layer_for(store, layer)
    try:
        agent = agents.resolve(name)
        errors, _ = agents.check_references()
    except SemanticError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    errors = errors_for_agent(agent, errors)
    cases = agent.definition.evals
    payload: dict = {
        "agent": agent.name,
        "static_errors": errors,
        "eval_count": len(cases),
        "mode": "static" if runner is None else "graded",
        "cases": [],
    }
    lines = list(errors)
    failed = bool(errors)
    if not cases:
        lines.append(f"{agent.name}: no evals defined")
    elif runner is None:
        lines.append(
            f"{agent.name}: {len(cases)} eval(s) "
            f"{'fail' if errors else 'pass'} the static checks; add --runner to execute them"
        )
    else:
        results = run_evals(agent, runner, timeout=timeout)
        payload["cases"] = [r.payload() for r in results]
        for r in results:
            lines.append(f"{'PASS' if r.passed else 'FAIL'}  {r.question}  [{r.evidence}]")
            lines += [f"      - {f}" for f in r.failures]
            lines += [f"      ~ {w}" for w in r.warnings]
            if r.calls:
                lines.append(f"      calls: {'; '.join(r.calls)}")
        passed = sum(1 for r in results if r.passed)
        lines.append(f"{passed}/{len(results)} passed")
        failed = failed or passed != len(results)
    payload["passed"] = not failed
    _emit(payload, as_json, lines)
    if failed:
        raise typer.Exit(1)


@metric_app.command("query")
def metric_query_cmd(
    name: str = typer.Argument(..., help="Metric name"),
    target: str = typer.Option(".", "--target", "-t"),
    dimension: list[str] = typer.Option([], "--dimension", "-d"),
    grain: str = typer.Option(None, "--grain", "-g"),
    param: list[str] = typer.Option([], "--param", "-p", help="Filter as dimension=value"),
    start: str = typer.Option(
        None, "--start", help="ISO date, or a token: -30d, last_30_days, mtd, ytd, today"
    ),
    end: str = typer.Option(
        None, "--end", help="ISO date. A token here means today — a window ends now"
    ),
    fmt: str = typer.Option("table", "--format", "-f", help="table | csv | json"),
    dashboard: str = typer.Option(
        None,
        "--dashboard",
        help="Resolve inline metrics in this dashboard's scope and apply its filter defaults",
    ),
    compare: str = typer.Option(
        None,
        "--compare",
        help=(
            "previous_period | yoy: second window + delta, matching the tile. "
            "Needs both --start and --end, or a --dashboard daterange default"
        ),
    ),
    row_limit: int = typer.Option(1000, help="Max rows returned"),
) -> None:
    """Evaluate a metric: group by dimensions and/or a time grain, filter by values."""
    from sqldash import query_command

    fmt = query_command.require_fmt(fmt)
    store, layer = _open_project(target)
    query_command.metric_query(
        store, layer, name, dimension, grain, param, start, end, fmt, dashboard, compare, row_limit
    )


def _resolve_all_repos(announce: bool = False):
    """Resolve every registered repo to a local checkout, or exit with a friendly error."""
    from sqldash.gitrepo import GitError
    from sqldash.workspace import WorkspaceError, load_registry, resolve_workspace

    repos = load_registry()
    if not repos:
        typer.echo(
            "error: no repos registered — add one with 'sqldash repo add <url|path>'", err=True
        )
        raise typer.Exit(1)
    if announce:
        typer.echo(f"syncing {len(repos)} repo(s) ...")
    try:
        return resolve_workspace(repos)
    except (GitError, WorkspaceError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc


def _labeled_sources(store, layer) -> "list[LabeledSource]":
    from sqldash.project.sources import labeled_sources

    return labeled_sources(store, layer)


def _only_or_exit(targets, only: str):
    matched = [e for e in targets if e.label == only]
    if not matched:
        typer.echo(f"error: no source named '{only}' — run 'sqldash source list'", err=True)
        raise typer.Exit(1)
    return matched


@source_app.command("list")
def source_list(
    target: str = typer.Argument(".", help="Project dir, dashboard .yaml, or git URL"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List every data source in the project (credentials redacted)."""
    from sqldash.models.source import redact_source
    from sqldash.project.sources import source_problems

    store, layer = _open_project(target)
    sources = {e.label: redact_source(e.source) for e in _labeled_sources(store, layer)}
    problems = source_problems(store, layer)
    for problem in problems:
        typer.echo(f"error: {problem}", err=True)
    _emit(
        {"sources": sources, "errors": problems},
        as_json,
        [f"{where:36} {cfg.get('type', '?')}" for where, cfg in sources.items()]
        or ["(no sources)"],
    )


@source_app.command("test")
def source_test(
    target: str = typer.Argument(".", help="Project dir, dashboard .yaml, or git URL"),
    only: str = typer.Option(None, "--only", help="Test just one source by its listed name"),
) -> None:
    """Connect to each source and run SELECT 1 — verifies credentials and drivers."""
    from sqldash.execution import ExecutionRegistry
    from sqldash.models.source import source_label
    from sqldash.project.sources import (
        attach_dir_missing,
        database_file_missing,
        source_files_dir,
        source_problems,
    )

    store, layer = _open_project(target)
    targets = _labeled_sources(store, layer)
    # Before anything is connected to: a source that could not even be read is
    # the answer to "test my connection", and skipping it silently made this
    # command exit 0 with no output at all on a project with a broken source.
    problems = source_problems(store, layer)
    for problem in problems:
        typer.echo(f"FAIL  {problem}", err=True)
    if only:
        targets = _only_or_exit(targets, only)
    if not targets and not problems:
        typer.echo("error: no sources defined", err=True)
        raise typer.Exit(1)
    registry = ExecutionRegistry(max_workers=1)
    failed = bool(problems)
    try:
        for entry in targets:
            started = time.monotonic()
            scan_dir = source_files_dir(entry.source, entry.base_dir)
            missing = attach_dir_missing(entry.source, scan_dir) or database_file_missing(
                entry.source, scan_dir
            )
            if missing:
                failed = True
                typer.echo(f"FAIL  {entry.label:36} {source_label(entry.source):10} {missing}")
                continue
            try:
                registry.run_sync(entry.source, entry.base_dir, "SELECT 1", [], 1, timeout=30)
                ms = (time.monotonic() - started) * 1000
                typer.echo(f"ok    {entry.label:36} {source_label(entry.source):10} {ms:6.0f}ms")
            except Exception as exc:
                failed = True
                typer.echo(f"FAIL  {entry.label:36} {source_label(entry.source):10} {exc}")
    finally:
        registry.shutdown()
    if failed:
        raise typer.Exit(1)


@source_app.command("describe")
def source_describe(
    target: str = typer.Argument(".", help="Project dir, dashboard .yaml, or git URL"),
    only: str = typer.Option(None, "--only", help="Describe one source by its listed name"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Tables and columns of a source — the raw material for metrics and queries."""
    from sqldash.execution import ExecutionRegistry

    store, layer = _open_project(target)
    targets = _labeled_sources(store, layer)
    if only:
        targets = _only_or_exit(targets, only)
    else:
        distinct = {json.dumps(e.source.model_dump(), sort_keys=True, default=str) for e in targets}
        if len(distinct) > 1:
            names = ", ".join(e.label for e in targets)
            typer.echo(
                f"error: several sources — pick one with --only (options: {names})", err=True
            )
            raise typer.Exit(1)
        targets = targets[:1]
    if not targets:
        typer.echo("error: no sources defined", err=True)
        raise typer.Exit(1)
    entry = targets[0]
    where, source, base_dir = entry.label, entry.source, entry.base_dir
    registry = ExecutionRegistry(max_workers=1)
    try:
        with registry.connection(source, base_dir) as connector:
            tables = connector.introspect()
    except Exception as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    finally:
        registry.shutdown()
    if as_json:
        typer.echo(
            json.dumps(
                {
                    "source": where,
                    "tables": [
                        {
                            "schema": t.schema,
                            "name": t.name,
                            "columns": [{"name": c[0], "type": c[1]} for c in t.columns],
                        }
                        for t in tables
                    ],
                },
                indent=2,
            )
        )
        return
    typer.echo(f"{where}:")
    for t in tables:
        label = f"{t.schema}.{t.name}" if t.schema else t.name
        typer.echo(f"  {label}")
        for cname, ctype in t.columns:
            typer.echo(f"    {cname:28} {ctype.lower()}")


def _require_file_paths(*paths: Path | None) -> None:
    for path in paths:
        if path is None:
            continue
        if path.is_dir():
            typer.echo(f"error: {path} is a directory — pass a file", err=True)
            raise typer.Exit(1)
        ancestor = next((parent for parent in path.parents if parent.exists()), None)
        if ancestor is not None and not ancestor.is_dir():
            typer.echo(f"error: {ancestor} is not a directory — cannot write {path}", err=True)
            raise typer.Exit(1)


def _write_output(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    except OSError as exc:
        typer.echo(f"error: could not write {path}: {exc.strerror or exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"wrote {path}", err=True)


def _write_metrics(doc: dict, warnings: list[str], out: Path | None) -> None:
    from sqldash.semantics.cortex import render_yaml
    from sqldash.semantics.layer import SemanticError, parse_metrics_file

    text = render_yaml(doc)
    for warning in warnings:
        typer.echo(f"warning: {warning}", err=True)
    # Both importers always emit `account: <your-account>`, so gating this on the
    # placeholder's absence meant the check never ran and an import could write a
    # file the next `sqldash lint` was the first to reject (#337).
    try:
        parse_metrics_file(text)
    except SemanticError as exc:
        typer.echo(
            f"error: the import produced a metrics.yaml sqldash cannot load: {exc}", err=True
        )
        raise typer.Exit(1) from exc
    if "<your-account>" in text:
        typer.echo("note: fill in the placeholder account/warehouse in source:", err=True)
    if out:
        _write_output(out, text)
    else:
        typer.echo(text, nl=False)


@import_app.command("cortex")
def import_cortex(
    file: Path = typer.Argument(..., help="Snowflake semantic-view YAML file"),
    out: Path = typer.Option(None, "--out", "-o", help="Write metrics.yaml here instead of stdout"),
) -> None:
    """Convert a Snowflake semantic view into a sqldash metrics.yaml."""
    from sqldash.semantics import SemanticError
    from sqldash.semantics.cortex import parse_semantic_view

    if not file.exists():
        typer.echo(f"error: {file} does not exist", err=True)
        raise typer.Exit(1)
    _require_file_paths(file, out)
    try:
        doc, warnings = parse_semantic_view(file.read_text())
    except SemanticError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    _write_metrics(doc, warnings, out)


@import_app.command("lookml")
def import_lookml_cmd(
    path: Path = typer.Argument(..., help="A .lkml view file or a directory of them"),
    out: Path = typer.Option(None, "--out", "-o", help="Write metrics.yaml here instead of stdout"),
) -> None:
    """Convert LookML views/measures into a sqldash metrics.yaml."""
    from sqldash.semantics import SemanticError
    from sqldash.semantics.lookml import import_lookml

    if not path.exists():
        typer.echo(f"error: {path} does not exist", err=True)
        raise typer.Exit(1)
    _require_file_paths(out)
    try:
        doc, warnings = import_lookml(path)
    except SemanticError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    _write_metrics(doc, warnings, out)


def _resolve_target(target: str, branch: str | None) -> Path:
    from sqldash.gitrepo import GitError, clone_or_pull, is_git_url

    if is_git_url(target):
        typer.echo(f"syncing {mask_url_userinfo(target)} ...", err=True)
        try:
            return clone_or_pull(target, branch=branch)
        except GitError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(1) from exc
    path = Path(target)
    if not path.exists():
        typer.echo(f"error: {path} does not exist", err=True)
        raise typer.Exit(1)
    return path


@export_app.command("lookml")
def export_lookml_cmd(
    target: str = typer.Argument(".", help="Project dir, dashboard .yaml, or git URL"),
    out: Path = typer.Option(None, "--out", "-o", help="Write to a file instead of stdout"),
    branch: str = typer.Option(None, "--branch", "-b"),
) -> None:
    """Export the semantic layer as LookML views.

    Derived, cumulative, window, and non-Looker-aggregate metrics emit a plain
    measure and a warning — LookML cannot keep those semantics."""
    from sqldash.project.store import DashboardStore
    from sqldash.semantics import SemanticError, SemanticLayer
    from sqldash.semantics.lookml import export_lookml

    _require_file_paths(out)
    path = _resolve_target(target, branch)
    store = DashboardStore(path.resolve())
    try:
        text, warnings = export_lookml(SemanticLayer(store), store)
    except SemanticError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    for warning in warnings:
        typer.echo(f"warning: {warning}", err=True)
    if out:
        _write_output(out, text)
    else:
        typer.echo(text, nl=False)


@export_app.command("cortex")
def export_cortex(
    target: str = typer.Argument(".", help="Project dir, dashboard .yaml, or git URL"),
    out: Path = typer.Option(None, "--out", "-o", help="Write to a file instead of stdout"),
    name: str = typer.Option(None, "--name", help="Semantic view name (default: project dir name)"),
    description: str = typer.Option(None, "--description"),
    branch: str = typer.Option(None, "--branch", "-b"),
) -> None:
    """Export the semantic layer as a Snowflake semantic-view YAML for Cortex Analyst.

    Feed the output to SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML."""
    import re

    from sqldash.params import ParamError
    from sqldash.project.store import DashboardStore
    from sqldash.semantics import SemanticError, SemanticLayer
    from sqldash.semantics.cortex import build_semantic_view, render_yaml

    _require_file_paths(out)
    path = _resolve_target(target, branch)
    store = DashboardStore(path.resolve())
    layer = SemanticLayer(store)
    project = store.root.parent if store.root.name == ".sqldash" else store.root
    view_name = name or re.sub(r"\W+", "_", project.name).strip("_") or "sqldash"
    try:
        doc, warnings = build_semantic_view(layer, store, view_name, description)
    except (SemanticError, ParamError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    for warning in warnings:
        typer.echo(f"warning: {warning}", err=True)
    text = render_yaml(doc)
    if out:
        _write_output(out, text)
    else:
        typer.echo(text, nl=False)


@export_app.command("cortex-agent")
def export_cortex_agent(
    agent: str = typer.Argument(..., help="Agent name from agents.yaml"),
    target: str = typer.Argument(".", help="Project dir, dashboard .yaml, or git URL"),
    out: Path = typer.Option(None, "--out", "-o", help="Write to a file instead of stdout"),
    schema: str = typer.Option(None, "--schema", help="Destination DATABASE.SCHEMA"),
    model: str = typer.Option("auto", "--model", help="Cortex orchestration model"),
    spec_only: bool = typer.Option(False, "--spec-only", help="Emit agent YAML instead of SQL"),
    view_out: Path = typer.Option(
        None, "--view-out", help="Also write the scoped semantic-view YAML"
    ),
    branch: str = typer.Option(None, "--branch", "-b"),
) -> None:
    """Export a Cortex Agent and its semantic view. Generates files; never deploys."""
    from sqldash.params import ParamError
    from sqldash.semantics import SemanticError
    from sqldash.semantics.cortex import render_yaml
    from sqldash.semantics.cortex_agent import build_cortex_agent

    _require_file_paths(out, view_out)
    if out and view_out and out.resolve() == view_out.resolve():
        raise typer.BadParameter("--out and --view-out must be different files")
    store, layer = _open_project(target, branch)
    try:
        spec, view, sql, warnings = build_cortex_agent(
            layer, store, agent, schema=schema, model=model
        )
    except (SemanticError, ParamError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    for warning in warnings:
        typer.echo(f"warning: {warning}", err=True)
    if spec_only:
        view_name = spec["tool_resources"]["Analyst"]["semantic_view"]
        typer.echo(
            f"note: create semantic view {view_name} before using this spec; "
            "pass --view-out PATH to write its view YAML",
            err=True,
        )
    text = render_yaml(spec) if spec_only else sql
    if view_out:
        _write_output(view_out, render_yaml(view))
    if out:
        _write_output(out, text)
    else:
        typer.echo(text, nl=False)


@export_app.command("context")
def export_context_cmd(
    target: str = typer.Argument(".", help="Project dir, dashboard .yaml, or git URL"),
    out: Path = typer.Option(None, "--out", "-o", help="Write to a file instead of stdout"),
    branch: str = typer.Option(None, "--branch", "-b"),
) -> None:
    """Generate agent-readable markdown (for CLAUDE.md / llms.txt) describing the
    project's metrics, dashboards, and how to query them."""
    from sqldash.semantics import SemanticError
    from sqldash.semantics.agents import agent_layer_for
    from sqldash.semantics.context import export_context

    _require_file_paths(out)
    store, layer = _open_project(target, branch)
    try:
        if (
            not store.discover()
            and not layer.all_metrics()
            and not agent_layer_for(store, layer).all_agents()
        ):
            typer.echo(
                "error: nothing to export — no dashboards, metrics, or agents "
                "(add a dashboard, metrics.yaml, or agents.yaml)",
                err=True,
            )
            raise typer.Exit(1)
        text = export_context(layer, store)
    except SemanticError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    _report_problems(layer, agent_layer_for(store, layer))
    if out:
        _write_output(out, text)
    else:
        typer.echo(text, nl=False)


@app.command()
def init(
    directory: Path = typer.Argument(
        Path("."), help="Repo or directory to initialize (dashboards land in .sqldash/)"
    ),
    demo: bool = typer.Option(
        False, "--demo", help="Scaffold the sample dashboard, metrics, and orders CSV"
    ),
    force: bool = typer.Option(
        False, "--force", help="With --demo, overwrite existing demo.yaml / metrics.yaml"
    ),
) -> None:
    """Initialize a project: creates .sqldash/. Pass --demo for the sample dashboard."""
    from sqldash.scaffold import ScaffoldError, ScaffoldExists, create_demo, init_project

    if force and not demo:
        typer.echo("error: --force only applies with --demo", err=True)
        raise typer.Exit(1)
    if not demo:
        dest = directory / ".sqldash" if directory.name != ".sqldash" else directory
        existed = dest.exists()
        try:
            path = init_project(directory)
        except ScaffoldError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(1) from exc
        typer.echo(f"{'ready' if existed else 'created'} {path}")
        typer.echo()
        typer.echo("next:")
        if directory != Path("."):
            typer.echo(f"  cd {directory}")
        typer.echo("  sqldash init --demo    # sample dashboard")
        typer.echo("  sqldash setup          # or a warehouse")
        typer.echo("  sqldash serve")
        return
    try:
        demo_path = create_demo(directory, force=force)
    except (ScaffoldExists, ScaffoldError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    sqldash_dir = demo_path.parent
    for created in (
        demo_path,
        sqldash_dir / "metrics.yaml",
        sqldash_dir / "agents.yaml",
        sqldash_dir / "data" / "orders.csv",
    ):
        typer.echo(f"created {created}")
    typer.echo()
    typer.echo("next:")
    if directory != Path("."):
        typer.echo(f"  cd {directory}")
    typer.echo("  sqldash serve")


@app.command()
def setup(
    directory: Path = typer.Argument(
        Path("."), help="Repo or directory to initialize (dashboards land in .sqldash/)"
    ),
    source_type: str = typer.Option(
        None, "--type", help="duckdb | postgres | snowflake | bigquery | databricks | mysql | url"
    ),
    profile: str = typer.Option(
        None, "--profile", help="Name written to ~/.config/sqldash/profiles.yaml"
    ),
    account: str = typer.Option(None, "--account", help="Snowflake account locator"),
    host: str = typer.Option(None, "--host"),
    port: int = typer.Option(None, "--port"),
    database: str = typer.Option(
        None,
        "--database",
        help="Database name; for duckdb an existing .duckdb file (relative to the project dir) "
        "or :memory: — a file that does not exist yet needs --skip-test",
    ),
    db_schema: str = typer.Option(None, "--schema"),
    warehouse: str = typer.Option(None, "--warehouse", help="Snowflake warehouse"),
    role: str = typer.Option(None, "--role", help="Snowflake role"),
    username: str = typer.Option(None, "--username", "--user"),
    authentication: str = typer.Option(
        None, "--auth", help="snowflake: externalbrowser | password | pat | keypair"
    ),
    password_env: str = typer.Option(
        None, "--password-env", help="Env var the profile's password: ${env:VAR} will name"
    ),
    token_env: str = typer.Option(
        None, "--token-env", help="Env var the profile's token: ${env:VAR} will name"
    ),
    private_key_path: str = typer.Option(None, "--private-key-path"),
    url: str = typer.Option(None, "--url", help="Raw SQLAlchemy URL (use ${env:VAR} for secrets)"),
    project: str = typer.Option(None, "--project", help="BigQuery project"),
    http_path: str = typer.Option(None, "--http-path", help="Databricks HTTP path"),
    catalog: str = typer.Option(None, "--catalog", help="Databricks catalog"),
    register: bool = typer.Option(
        False, "--register", help="Register this directory with 'sqldash repo add'"
    ),
    skip_test: bool = typer.Option(False, "--skip-test", help="Don't connect after writing"),
) -> None:
    """Write a local profile and a project source, then test the connection.

    Interactive with no flags. Pass --type (and the fields that type needs) to
    run non-interactively — passwords are always ${env:VAR} references, never
    literals, and they land in ~/.config/sqldash/profiles.yaml, not the repo.
    On a tty, missing flags are asked instead of exiting.
    """
    from sqldash.setup import (
        SOURCE_TYPES,
        SetupError,
        SetupPlan,
        apply_setup,
        missing_fields,
    )
    from sqldash.setup_prompt import print_setup_result, prompt_setup

    if source_type is None:
        if not _stdin_is_tty():
            typer.secho(
                f"error: pass --type for a non-interactive run (one of {', '.join(SOURCE_TYPES)})",
                fg="red",
                err=True,
            )
            raise typer.Exit(1)
        plan = prompt_setup(directory)
    else:
        plan = SetupPlan(
            source_type=source_type,
            profile=profile,
            account=account,
            host=host,
            port=port,
            database=database,
            db_schema=db_schema,
            warehouse=warehouse,
            role=role,
            username=username,
            authentication=authentication,
            password_env=password_env,
            token_env=token_env,
            private_key_path=private_key_path,
            url=url,
            project=project,
            http_path=http_path,
            catalog=catalog,
        )
        needed = missing_fields(plan)
        if needed and _stdin_is_tty():
            plan = prompt_setup(directory, plan)
        elif needed:
            typer.secho(
                f"error: --type {plan.source_type} needs "
                + ", ".join(f"--{name}" for name in needed),
                fg="red",
                err=True,
            )
            raise typer.Exit(1)

    try:
        result = apply_setup(directory, plan, skip_test=skip_test, register=register)
    except SetupError as exc:
        typer.secho(f"error: {exc}", fg="red", bold=True, err=True)
        raise typer.Exit(1) from exc

    print_setup_result(result, directory)
    if result.test_ok is False:
        raise typer.Exit(1)


def _stdin_is_tty() -> bool:
    return sys.stdin.isatty()


@app.command()
def serve(
    target: str = typer.Argument(
        None,
        help="A dashboard .yaml file, a directory, or a git URL. Omit to serve the "
        "current directory — or, when it has no dashboards, every registered repo "
        "(see 'sqldash repo add')",
    ),
    port: int = typer.Option(8400, help="Port to listen on"),
    host: str = typer.Option("127.0.0.1", help="Host to bind"),
    branch: str = typer.Option(None, "--branch", "-b", help="Branch to check out (git URLs only)"),
    no_browser: bool = typer.Option(False, "--no-browser", help="Don't open the browser"),
    row_limit: int = typer.Option(10_000, help="Max rows returned per query"),
    all_repos: bool = typer.Option(
        False, "--all", help="Serve every repo registered with 'sqldash repo add'"
    ),
    studio: bool | None = typer.Option(
        None,
        "--studio/--no-studio",
        help="Local coding-agent editing (enabled by default on loopback hosts)",
    ),
) -> None:
    """Serve dashboards from a file, a directory, a git repo, or every registered repo."""
    from sqldash.gitrepo import GitError, clone_or_pull, is_git_url
    from sqldash.server import create_app
    from sqldash.workspace import load_registry

    loopback = host in {"127.0.0.1", "localhost", "::1"}
    if studio is None:
        studio = loopback
    if studio and not loopback:
        raise typer.BadParameter("Studio requires a loopback host")

    url_host = f"[{host}]" if ":" in host else host

    if target is None and not all_repos:
        cwd = Path(".")
        has_local = (cwd / ".sqldash").is_dir() or any(cwd.glob("*.yaml")) or any(cwd.glob("*.yml"))
        if has_local:
            target = "."
        elif load_registry():
            all_repos = True
        else:
            target = "."

    if all_repos:
        workspace = _resolve_all_repos(announce=True)
        for name, root in workspace:
            typer.echo(f"  {name}: {root}")
        application = create_app(
            row_limit=row_limit, allowed_hosts=[url_host], workspace=workspace, studio=studio
        )
        url = f"http://{url_host}:{port}"
        typer.echo(f"sqldash serving {len(workspace)} repos at {url}")
        typer.echo(
            f"api token (for scripts; sent automatically by the UI): {application.state.api_token}"
        )
        if not no_browser:
            threading.Timer(0.8, webbrowser.open, args=(url,)).start()
        uvicorn.run(application, host=host, port=port, log_level="warning")
        return

    if is_git_url(target):
        typer.echo(f"syncing {mask_url_userinfo(target)} ...")
        try:
            path = clone_or_pull(target, branch=branch)
        except GitError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(1) from exc
        typer.echo(f"local clone: {path}")
        typer.echo(
            "(edits made in the UI land there — commit & push from that directory to share them)"
        )
    else:
        path = Path(target)
        if not path.exists():
            typer.echo(f"error: {path} does not exist", err=True)
            nested = path.parent / ".sqldash" / path.name
            if nested.exists():
                typer.echo(
                    f"hint: found {nested} — or serve the project dir: sqldash serve {path.parent}",
                    err=True,
                )
            raise typer.Exit(1)

    application = create_app(
        path.resolve(),
        row_limit=row_limit,
        allowed_hosts=[url_host],
        serve_label=str(path.resolve()),
        studio=studio,
    )
    url = f"http://{url_host}:{port}"
    typer.echo(f"sqldash serving {path.resolve()} at {url}")
    typer.echo(
        f"api token (for scripts; sent automatically by the UI): {application.state.api_token}"
    )
    if not no_browser:
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()
    uvicorn.run(application, host=host, port=port, log_level="warning")


def _lint_checked_files(store, layer) -> set[str]:
    from sqldash.semantics.agents import AgentLayer

    checked = {p.name for p in store.discover().values()}
    metrics_path = layer.metrics_path()
    if metrics_path is not None:
        checked.add(metrics_path.name)
    agents_path = AgentLayer(store, layer).agents_path()
    if agents_path is not None:
        checked.add(agents_path.name)
    return checked


def _print_lint(findings, checked_files: set[str], strict: bool) -> None:
    by_file: dict[str, list] = {}
    for finding in findings:
        by_file.setdefault(finding.file, []).append(finding)
    checked = sorted(checked_files | set(by_file))
    for file in checked:
        items = by_file.get(file, [])
        if not items:
            typer.echo(f"✓ {file}")
            continue
        typer.echo(file)
        for finding in items:
            typer.echo(f"  {finding.level}: {finding.message}")
    errors = sum(1 for f in findings if f.level == "error")
    warnings = sum(1 for f in findings if f.level == "warning")
    typer.echo(f"\n{len(checked)} file(s) checked — {errors} error(s), {warnings} warning(s)")
    if errors or (strict and warnings):
        raise typer.Exit(1)


@app.command()
def lint(
    target: str = typer.Argument(".", help="Project dir, dashboard .yaml, or git URL"),
    strict: bool = typer.Option(
        False,
        "--strict",
        help="Probe sql tools and metrics against the warehouse; also exit non-zero on warnings",
    ),
    branch: str = typer.Option(None, "--branch", "-b"),
) -> None:
    """Validate dashboards and the semantic layer; designed for CI."""
    from sqldash.lint import Finding, lint_project
    from sqldash.project.store import WorkspaceStore

    store, layer = _open_project(target, branch)
    if isinstance(store, WorkspaceStore):
        findings = []
        checked_files: set[str] = set()
        for repo, repo_store in store.repos.items():
            repo_layer = layer.layers[repo]
            repo_findings = lint_project(repo_store, repo_layer, check_sql=strict)
            findings.extend(Finding(f"{repo}/{f.file}", f.level, f.message) for f in repo_findings)
            checked_files.update(
                f"{repo}/{name}" for name in _lint_checked_files(repo_store, repo_layer)
            )
    else:
        findings = lint_project(store, layer, check_sql=strict)
        checked_files = _lint_checked_files(store, layer)
    _print_lint(findings, checked_files, strict)


def _mcp_open(target: str, branch: str | None = None) -> dict:
    """Resolve a non-`--all` mcp target to create_mcp_server kwargs.

    Git URLs clone first. Anything else goes through `_open_project` so `.`
    from an empty cwd falls back to registered repos, matching list/serve.
    """
    from sqldash.gitrepo import GitError, clone_or_pull, is_git_url
    from sqldash.project.store import WorkspaceStore

    if is_git_url(target):
        typer.echo(f"syncing {mask_url_userinfo(target)} ...", err=True)
        try:
            path = clone_or_pull(target, branch=branch)
        except GitError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(1) from exc
        typer.echo(f"local clone: {path}", err=True)
        return {"path": path.resolve()}

    path = Path(target)
    if not path.exists():
        typer.echo(f"error: {path} does not exist", err=True)
        raise typer.Exit(1)
    store, _ = _open_project(target, branch)
    if isinstance(store, WorkspaceStore):
        return {"workspace": [(name, s.root) for name, s in store.repos.items()]}
    return {"path": path.resolve()}


@app.command()
def mcp(
    target: str = typer.Argument(
        ".", help="A dashboards/metrics dir, a dashboard .yaml, or a git URL"
    ),
    allow_sql: bool = typer.Option(
        False,
        "--allow-sql",
        help=(
            "Expose a raw run_sql tool. A keyword check refuses writing statements and "
            "only returned rows are capped; the credential's grants are the real guardrail"
        ),
    ),
    row_limit: int = typer.Option(1000, help="Max rows returned per tool call"),
    branch: str = typer.Option(None, "--branch", "-b", help="Branch to check out (git URLs only)"),
    all_repos: bool = typer.Option(
        False,
        "--all",
        help="Serve every repo registered with 'sqldash repo add' (metrics namespaced repo/name)",
    ),
) -> None:
    """Serve the semantic layer to agents over MCP (stdio)."""
    from sqldash.mcp_server import create_mcp_server, registry_for
    from sqldash.semantics.evals import TRACE_ENV

    trace = Path(os.environ[TRACE_ENV]) if os.environ.get(TRACE_ENV) else None

    if all_repos:
        workspace = _resolve_all_repos()
        for name, root in workspace:
            typer.echo(f"serving {name}: {root}", err=True)
        server = create_mcp_server(
            allow_sql=allow_sql, row_limit=row_limit, workspace=workspace, trace=trace
        )
        try:
            server.run(transport="stdio")
        finally:
            registry_for(server).shutdown()
        return

    opened = _mcp_open(target, branch)
    server = create_mcp_server(allow_sql=allow_sql, row_limit=row_limit, trace=trace, **opened)
    try:
        server.run(transport="stdio")
    finally:
        registry_for(server).shutdown()


@app.command()
def snapshot(
    target: str = typer.Argument(".", help="Project dir, dashboard .yaml, or git URL"),
    out: Path = typer.Option(Path("snapshots"), "--out", "-o", help="Output directory"),
    dashboard: list[str] = typer.Option(
        None, "--dashboard", "-d", help="Only these dashboards (default: all)"
    ),
    theme: str = typer.Option("dark", "--theme", help="dark | light"),
    width: int = typer.Option(1440, help="Viewport width in px (rendered at 2x)"),
    branch: str = typer.Option(None, "--branch", "-b", help="Branch to check out (git URLs only)"),
    all_repos: bool = typer.Option(
        False, "--all", help="Snapshot every repo registered with 'sqldash repo add'"
    ),
) -> None:
    """Render dashboards to static PNGs + an index.html — wallboards and
    stakeholders without warehouse credentials. Needs the 'snapshot' extra."""
    from sqldash.server import create_app
    from sqldash.snapshot import SnapshotError, snapshot_dashboards

    if theme not in ("dark", "light"):
        typer.echo("error: --theme must be dark or light", err=True)
        raise typer.Exit(1)
    if all_repos:
        application = create_app(workspace=_resolve_all_repos())
    else:
        path = _resolve_target(target, branch)
        application = create_app(path.resolve(), serve_label=str(path.resolve()))
    try:
        entries = snapshot_dashboards(
            application, out.resolve(), names=dashboard or None, theme=theme, width=width
        )
    except SnapshotError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    rendered = [e for e in entries if not e["error"]]
    failed = [e for e in entries if e["error"]]
    for entry in rendered:
        note = "" if entry["settled"] else "  (some tiles still loading at capture)"
        typer.echo(f"  {entry['file']:40} {entry['title']}{note}")
    for entry in failed:
        typer.echo(
            f"error: {entry['name']} failed to load, not snapshotted: {entry['error']}", err=True
        )
    typer.echo(f"{len(rendered)} snapshot(s) + index.html in {out.resolve()}")
    if failed:
        typer.echo(
            f"error: {len(failed)} dashboard(s) failed to load; "
            "index.html lists them, 'sqldash lint' has the details",
            err=True,
        )
        raise typer.Exit(1)


@app.command("query")
def query_cmd(
    target: str = typer.Argument(..., help="Dashboard .yaml, a project dir, or a git URL"),
    name: str = typer.Argument(
        ..., help="A query or metric name (use dashboard.query to disambiguate)"
    ),
    dashboard_opt: str = typer.Option(
        None, "--dashboard", help="Dashboard to resolve the query in"
    ),
    param: list[str] = typer.Option(
        [], "--param", "-p", help="Parameter as name=value (repeatable)"
    ),
    dimension: list[str] = typer.Option(
        [], "--dimension", "-d", help="Metric dimension to group by (repeatable)"
    ),
    grain: str = typer.Option(
        None, "--grain", "-g", help="Metric time grain (hour|day|week|month|quarter|year)"
    ),
    fmt: str = typer.Option("table", "--format", "-f", help="Output: table | csv | json"),
    source_name: str = typer.Option(
        None,
        "--source",
        help=(
            "A picker key: this dashboard's sources: name, metrics.yaml, or another "
            "dashboard's other.source / other.sources.prod"
        ),
    ),
    start: str = typer.Option(
        None,
        "--start",
        help="Metric only. ISO date, or a token: -30d, last_30_days, mtd, ytd, today",
    ),
    end: str = typer.Option(
        None,
        "--end",
        help="Metric only. ISO date. A token here means today — a window ends now",
    ),
    compare: str = typer.Option(
        None,
        "--compare",
        help="Metric only. previous_period | yoy — second window + delta, matching the tile",
    ),
    row_limit: int = typer.Option(10_000, help="Max rows returned"),
) -> None:
    """Run a dashboard query or a semantic-layer metric headlessly (for scripts and CI)."""
    from sqldash import query_command

    fmt = query_command.require_fmt(fmt)
    if row_limit < 0:
        typer.echo("error: row_limit must be >= 0", err=True)
        raise typer.Exit(1)
    store, layer, workspace_dashboard = _query_project(target)
    scope = query_command.query_scope(
        store, layer, workspace_dashboard, target, name, dashboard_opt, source_name
    )
    bound, compare, compare_kwargs = query_command.bind_query(
        scope, param, dimension, grain, fmt, start, end, compare
    )
    result, extra = query_command.run_query(bound, row_limit, compare, compare_kwargs)
    query_command.print_result(result, fmt, compare=extra)


def _query_project(target: str):
    # `dashboard list` prints `acme/demo`, so that is what a user or an agent
    # reaches for first. Accept it as the target rather than reporting that a
    # path by that name does not exist.
    workspace_dashboard = None
    if "/" in target and not Path(target).exists():
        candidate_store, candidate_layer = None, None
        try:
            candidate_store, candidate_layer = _open_project(".")
        except SystemExit:
            candidate_store = None
        if candidate_store is not None and target in candidate_store.discover():
            workspace_dashboard = target
            store, layer = candidate_store, candidate_layer
    if workspace_dashboard is None:
        store, layer = _open_project(target)
    return store, layer, workspace_dashboard


studio_app = typer.Typer(help="Configure custom coding-agent entrypoints")
app.add_typer(studio_app, name="studio")


@studio_app.command("add")
def studio_add(
    name: str = typer.Argument(..., help="Display name, e.g. Custom agent"),
    command: list[str] = typer.Argument(..., help="Command and arguments; include {prompt}"),
    shell: str = typer.Option(None, "--shell", help="Absolute bash/zsh path for aliases/functions"),
    env: list[str] = typer.Option(None, "--env", help="Local environment override KEY=VALUE"),
) -> None:
    """Save an agent entrypoint. Use -- before command arguments."""
    from pydantic import ValidationError

    from sqldash.project.store import plain_validation_message
    from sqldash.studio.entrypoints import (
        AgentEntrypoint,
        StudioError,
        entrypoints_path,
        save_entrypoint,
    )

    try:
        values = {}
        for item in env or []:
            key, separator, value = item.partition("=")
            if not separator:
                raise StudioError("Environment overrides must be KEY=VALUE")
            values[key] = value
        entrypoint = AgentEntrypoint(name=name, command=command, shell=shell, env=values)
        save_entrypoint(entrypoint)
    except ValidationError as exc:
        messages = dict.fromkeys(
            plain_validation_message(error)
            for error in exc.errors(include_input=False, include_context=False, include_url=False)
        )
        typer.echo("Invalid entrypoint: " + "; ".join(messages), err=True)
        raise typer.Exit(1) from exc
    except StudioError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Saved {name} in {entrypoints_path()}")


@studio_app.command("list")
def studio_list():
    """List discovered and saved entrypoint names and commands (never environment values)."""
    from sqldash.studio.entrypoints import (
        NO_ENTRYPOINTS,
        StudioError,
        available_entrypoints,
        entrypoints_path,
    )

    try:
        entrypoints = available_entrypoints()
    except StudioError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(str(entrypoints_path()))
    for entrypoint in entrypoints.values():
        typer.echo(f"{entrypoint.name}: {entrypoint.command[0]}")
    if not entrypoints:
        typer.echo(NO_ENTRYPOINTS, err=True)


@studio_app.command("check")
def studio_check(name: str):
    """Check that an entrypoint resolves without starting the coding agent."""
    from sqldash.studio.entrypoints import StudioError, resolve_entrypoint

    try:
        resolve_entrypoint(name).check()
    except StudioError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"{name}: entrypoint found")


if __name__ == "__main__":
    app()
