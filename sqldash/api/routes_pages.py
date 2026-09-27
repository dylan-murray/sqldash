import hashlib
import json
import re
import subprocess
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from markdown_it import MarkdownIt

from sqldash.api.helpers import client_payload
from sqldash.api.routes_config import profile_health, repo_rows
from sqldash.api.routes_events import semantic_layer_event
from sqldash.models.source import source_label
from sqldash.params import filter_ui_default, select_choices
from sqldash.project.drill import dashboard_href
from sqldash.project.sources import distinct_picker_sources, picker_sources
from sqldash.project.store import InvalidDashboardError, NotFoundError, compute_etag
from sqldash.secrets import profiles_path
from sqldash.semantics.compiler import MACRO_NOTE, describes_macro
from sqldash.semantics.layer import MetricNotFoundError, SemanticError, metric_summary

router = APIRouter()
# html: False — markdown: keys come from dashboard YAML, which may be a cloned
# third-party repo; raw HTML here would run script with the viewer's API token.
md = MarkdownIt("commonmark", {"typographer": True, "html": False})

_SCRIPT_JSON_ESCAPES = str.maketrans({"<": "\\u003c", ">": "\\u003e", "&": "\\u0026"})


def script_json(data) -> str:
    """JSON for a `<script type="application/json">` body that no string value can end or
    re-state: `<!--<script>` in a title otherwise swallows every script after the tag."""
    return json.dumps(data).translate(_SCRIPT_JSON_ESCAPES)


def _hue_slot(name: str) -> int:
    h = 2166136261
    for ch in name:
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    return (h % 8) + 1


def _age(mtime: float) -> str:
    seconds = max(0.0, time.time() - mtime)
    if seconds < 90:
        return "just now"
    minutes = seconds / 60
    if minutes < 90:
        return f"{round(minutes)}m ago"
    hours = minutes / 60
    if hours < 36:
        return f"{round(hours)}h ago"
    days = hours / 24
    if days < 60:
        return f"{round(days)}d ago"
    months = days / 30
    if months < 18:
        return f"{round(months)}mo ago"
    return f"{round(months / 12)}y ago"


def _index_metrics(layer) -> tuple[list[dict], list[dict]]:
    """Metric rows, plus one error row per semantic layer that failed to load.

    Per repo in a workspace, so one broken metrics.yaml hides only its own
    repo's metrics, and says why instead of dropping the section.
    """
    repo_layers = getattr(layer, "layers", None)
    parts = list(repo_layers.items()) if repo_layers is not None else [(None, layer)]
    metrics, errors = [], []
    for repo, sub in parts:
        try:
            resolved = sub.all_metrics()
        except Exception as exc:
            path = sub.metrics_path()
            message = str(exc).strip() or "the semantic layer failed to load"
            errors.append(
                {
                    "repo": repo,
                    "file": path.name if path is not None else "",
                    "error": message.splitlines()[0],
                }
            )
            continue
        for m in resolved:
            if repo is not None:
                m = layer._prefixed(repo, m)
            metrics.append(
                {
                    "name": m.name,
                    "repo": repo,
                    "title": m.definition.title or m.name,
                    "description": m.definition.description,
                    "source_type": source_label(m.source),
                    "origin": m.origin,
                    "dashboard": m.dashboard,
                    "ambiguous_with": list(m.ambiguous_with),
                }
            )
    return metrics, errors


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    store = request.app.state.store
    paths = store.discover()
    if store.single_file is not None and len(paths) == 1:
        return RedirectResponse(url=f"/d/{next(iter(paths))}")
    workspace = getattr(store, "repos", None)
    dashboards = []
    broken = []
    for name, path in paths.items():
        repo = name.partition("/")[0] if workspace else None
        try:
            dashboard, _, etag = store.load(name)
        except Exception as exc:
            message = str(exc).strip().splitlines()[0] if str(exc).strip() else "invalid file"
            try:
                etag = compute_etag(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                etag = ""
            broken.append(
                {"name": name, "repo": repo, "file": path.name, "error": message, "etag": etag}
            )
            continue
        dashboards.append(
            {
                "name": name,
                "repo": repo,
                "title": dashboard.title,
                "description": dashboard.description,
                "source_type": source_label(dashboard.source),
                "tile_count": len(dashboard.tiles),
                "file": path.name,
                "etag": etag,
                "hue": _hue_slot(name),
                "age": _age(path.stat().st_mtime),
            }
        )
    metrics, metric_errors = _index_metrics(request.app.state.layer)
    return request.app.state.templates.TemplateResponse(
        request,
        "index.html",
        {
            "dashboards": dashboards,
            "broken": broken,
            "metrics": metrics,
            "metric_errors": metric_errors,
            "serve_label": request.app.state.serve_label,
            "repos": sorted(workspace) if workspace else [],
        },
    )


@router.get("/settings/panel", response_class=HTMLResponse)
async def settings_panel(request: Request):
    store = request.app.state.store
    workspace = getattr(store, "repos", None)
    return request.app.state.templates.TemplateResponse(
        request,
        "_settings.html",
        {
            "workspace_mode": workspace is not None,
            "repo_rows": repo_rows(workspace or {}),
            "profile_rows": profile_health(store, request.app.state.layer),
            "profiles_path": str(profiles_path()),
            "serve_label": request.app.state.serve_label,
        },
    )


def _all_dashboards(store) -> list[dict]:
    workspace = getattr(store, "repos", None) is not None
    items = []
    for name in store.discover():
        try:
            title = store.load(name)[0].title
        except Exception:
            continue
        items.append(
            {"name": name, "repo": name.partition("/")[0] if workspace else None, "title": title}
        )
    return items


def _repo_of(name: str) -> str:
    return name.partition("/")[0] if "/" in name else ""


def _git_file_meta(path: Path) -> dict:
    """Author and dates from git. Empty when the file is not in a repo.

    Newest commit is last-modified; oldest after --follow is creation.
    One process — two `git log`s used to cost ~20ms per render.
    """
    meta = {"author": None, "created": None, "modified_by": None, "modified": None}
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(path.parent),
                "log",
                "--follow",
                "--format=%aN\t%aI",
                "--",
                path.name,
            ],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return meta
    lines = [line for line in result.stdout.splitlines() if "\t" in line]
    if not lines:
        return meta
    try:
        meta["author"], meta["created"] = lines[-1].split("\t", 1)
        if len(lines) > 1:
            meta["modified_by"], meta["modified"] = lines[0].split("\t", 1)
    except ValueError:
        return meta
    return meta


def _relation_key(resolved) -> str | None:
    relation = getattr(resolved, "relation", None)
    if relation is None:
        return None
    if relation.table:
        return f"table:{relation.table}"
    if relation.sql:
        return "sql:" + re.sub(r"\s+", " ", relation.sql.strip())
    return None


def _sibling_dashboards(store, name: str, dash_repo: str):
    """Dashboards in the same project/repo, already parsed. Skip other repos."""
    repos = getattr(store, "repos", None)
    if dash_repo and repos and dash_repo in repos:
        for other_name, other in repos[dash_repo].iter_loaded():
            full = f"{dash_repo}/{other_name}"
            if full != name:
                yield full, other
        return
    for dash_name, other in store.iter_loaded():
        if dash_name != name:
            yield dash_name, other


def _catalog_metrics(layer, dash_repo: str) -> list:
    """Metrics for this repo only. A broken metrics.yaml must not 422 the page."""
    try:
        layers = getattr(layer, "layers", None)
        if layers is not None:
            sub = layers.get(dash_repo) if dash_repo else None
            if sub is None:
                return []
            return [layer._prefixed(dash_repo, metric) for metric in sub.all_metrics()]
        return list(layer.all_metrics())
    except Exception:
        return []


def _dashboard_info(store, layer, name, dashboard) -> dict:
    """Metrics used, git author, and metric-level lineage. No SQL parsing."""
    info = _git_file_meta(store.path_for(name))
    dash_repo = _repo_of(name)
    also_by_metric: dict[str, list[dict]] = {}
    for dash_name, other in _sibling_dashboards(store, name, dash_repo):
        seen_on_dash: set[str] = set()
        for tile in other.tiles:
            if not tile.metric or tile.metric.name in seen_on_dash:
                continue
            seen_on_dash.add(tile.metric.name)
            also_by_metric.setdefault(tile.metric.name, []).append(
                {"name": dash_name, "title": other.title}
            )
    kin_by_rel: dict[str, list[dict]] = {}
    for other in _catalog_metrics(layer, dash_repo):
        rel_key = _relation_key(other)
        if not rel_key:
            continue
        kin_by_rel.setdefault(rel_key, []).append(
            {
                "name": other.name,
                "title": other.definition.title or other.name,
                "href": f"/m/{other.name}",
            }
        )
    seen: list[str] = []
    metrics = []
    for tile in dashboard.tiles:
        if not tile.metric or tile.metric.name in seen:
            continue
        seen.append(tile.metric.name)
        metric_name = tile.metric.name
        try:
            resolved = layer.resolve(metric_name, dashboard=name)
        except Exception:
            resolved = None
        if resolved is not None:
            href_name = resolved.name
            title = resolved.definition.title or href_name
            href = f"/m/{href_name}"
            rel_key = _relation_key(resolved)
            skip_names = {href_name, metric_name}
        else:
            title, href, rel_key, skip_names = metric_name, None, None, {metric_name}
        kin = [entry for entry in kin_by_rel.get(rel_key, ()) if entry["name"] not in skip_names]
        metrics.append(
            {
                "name": metric_name,
                "title": title,
                "href": href,
                "also_on": also_by_metric.get(metric_name, []),
                "kin": kin[:8],
            }
        )
    info["metrics"] = metrics
    return info


def _nav_context(store, name: str | None = None) -> dict:
    """Switcher label + current repo. Workspace shows the repo you are in;
    a single project shows the directory name, not the word 'dashboards'."""
    workspace = getattr(store, "repos", None) is not None
    current_repo = name.partition("/")[0] if workspace and name and "/" in name else None
    if current_repo:
        label = current_repo
    else:
        root = getattr(store, "root", None)
        if root is not None:
            label = root.parent.name if root.name == ".sqldash" else root.name
        else:
            label = "dashboards"
    return {
        "all_dashboards": _all_dashboards(store),
        "switcher_label": label or "dashboards",
        "current_repo": current_repo,
    }


@router.get("/m/{metric_name:dname}", response_class=HTMLResponse)
async def metric_page(request: Request, metric_name: str):
    layer = request.app.state.layer
    store = request.app.state.store
    try:
        resolved = layer.resolve(metric_name)
    except MetricNotFoundError as exc:
        return _error_page(request, metric_name, exc, status=404)
    except SemanticError as exc:
        return _error_page(request, metric_name, exc, file=_metrics_file_name(layer, metric_name))
    summary = metric_summary(resolved)
    summary["relation"] = (
        {"table": resolved.relation.table}
        if resolved.relation.table
        else {"sql": resolved.relation.sql}
    )
    summary["default_filters"] = resolved.definition.filters
    # The page is where a human copies an expression into the query workspace,
    # so an expr sqldash resolves rather than the warehouse has to say so.
    definition = resolved.definition
    if describes_macro(
        definition.expr,
        *definition.filters,
        *(dim.expr for dim in definition.dimensions),
        definition.time_dimension.expr if definition.time_dimension else None,
        resolved.relation.sql,
    ):
        summary["expr_note"] = f"this definition {MACRO_NOTE}"

    # In a workspace the URL carries the namespaced name (`repo/revenue`) while a
    # tile stores the bare name it was authored with, so an exact match never
    # fires. Match the bare name, but only within the metric's own repo, or a
    # `revenue` in one repo would claim another repo's tiles.
    metric_repo, _, bare_name = metric_name.rpartition("/")
    used_by = {}
    for dash_name, dashboard in store.iter_loaded():
        dash_repo = dash_name.partition("/")[0] if "/" in dash_name else ""
        for w in dashboard.tiles:
            if not w.metric:
                continue
            same_repo_bare = bool(metric_repo) and dash_repo == metric_repo
            if w.metric.name == metric_name or (same_repo_bare and w.metric.name == bare_name):
                entry = used_by.setdefault(
                    dash_name,
                    {"dashboard": dash_name, "dashboard_title": dashboard.title, "tiles": []},
                )
                entry["tiles"].append(w.title or w.id)

    payload = script_json(summary)
    return request.app.state.templates.TemplateResponse(
        request,
        "metric.html",
        {
            "metric": summary,
            "used_by": list(used_by.values()),
            "metric_json": payload,
            **_nav_context(store, metric_name),
        },
    )


def _metrics_file_name(layer, metric_name: str) -> str | None:
    repo_layers = getattr(layer, "layers", None)
    if repo_layers is not None:
        layer = repo_layers.get(metric_name.partition("/")[0]) if "/" in metric_name else None
    path = layer.metrics_path() if layer is not None else None
    return path.name if path is not None else None


def _error_page(
    request: Request, name: str, exc: Exception, status: int = 422, file: str | None = None
):
    store = request.app.state.store
    if file is None:
        try:
            file = store.path_for(name).name
        except Exception:
            file = None
    return request.app.state.templates.TemplateResponse(
        request,
        "error.html",
        {
            "heading": f"'{name}' can't be loaded",
            "file": file,
            "message": str(exc),
        },
        status_code=status,
    )


@router.get("/d/{name:dname}/workspace", response_class=HTMLResponse)
async def query_workspace(request: Request, name: str):
    store = request.app.state.store
    try:
        dashboard, _, _ = store.load(name)
    except InvalidDashboardError as exc:
        return _error_page(request, name, exc)
    except NotFoundError as exc:
        return _error_page(request, name, exc, status=404)
    identity = hashlib.sha256(str(store.path_for(name).resolve()).encode()).hexdigest()
    return request.app.state.templates.TemplateResponse(
        request,
        "workspace.html",
        {
            "name": name,
            "dashboard": dashboard,
            "workspace_identity": identity,
            **_nav_context(store, name),
        },
    )


@router.get("/d/{name:dname}/query", response_class=HTMLResponse)
async def query_page(request: Request, name: str, tile: str | None = None):
    store = request.app.state.store
    try:
        dashboard, _, etag = store.load(name)
    except InvalidDashboardError as exc:
        return _error_page(request, name, exc)
    except NotFoundError as exc:
        return _error_page(request, name, exc, status=404)
    payload = script_json(client_payload(name, dashboard, etag, request.app.state.layer))
    editing_tile = next((t for t in dashboard.tiles if t.id == tile), None) if tile else None
    missing_tile = tile if tile and editing_tile is None else None
    source_choices = [
        {"key": e.key, "label": e.label, "kind": e.kind}
        for e in distinct_picker_sources(
            picker_sources(store, request.app.state.layer, name, dashboard=dashboard)
        )
    ]
    selected_source = (editing_tile.source or "") if editing_tile else ""
    # A tile may name the default connection by its own name; the picker spells
    # that option "" (dashboard default), so an unmatched value would blank it.
    if selected_source and selected_source == dashboard.default_source_name:
        selected_source = ""
    return request.app.state.templates.TemplateResponse(
        request,
        "query.html",
        {
            "name": name,
            "dashboard": dashboard,
            "source_label": source_label(dashboard.source),
            "dashboard_json": payload,
            "editing_tile": editing_tile,
            "missing_tile": missing_tile,
            "source_choices": source_choices,
            "selected_source": selected_source,
            **_nav_context(store, name),
        },
    )


def _drill_origin(store, name: str, origin: str | None) -> dict | None:
    """The dashboard a drill came from, for the breadcrumb back to it. Only a name the
    store serves counts, so the query string cannot put arbitrary text or links there."""
    if not origin or origin == name or origin not in store.discover():
        return None
    try:
        title = store.load(origin)[0].title
    except Exception:
        return None
    return {"name": origin, "title": title, "href": dashboard_href(origin)}


@router.get("/d/{name:dname}", response_class=HTMLResponse)
async def dashboard_page(request: Request, name: str):
    store = request.app.state.store
    try:
        dashboard, _, etag = store.load(name)
    except InvalidDashboardError as exc:
        return _error_page(request, name, exc)
    except NotFoundError as exc:
        return _error_page(request, name, exc, status=404)

    filters = []
    for f in dashboard.filters:
        item = f.model_dump()
        item["resolved_default"] = filter_ui_default(f)
        if f.type == "select":
            item["options"] = select_choices(f)
        filters.append(item)

    tiles = []
    for w in dashboard.tiles:
        item = w.model_dump()
        if w.type == "text":
            item["rendered_markdown"] = md.render(w.markdown or "")
        else:
            chart = w.chart
            multi = chart is not None and (
                len(chart.y or []) > 1 or bool(chart.group_by) or chart.type == "pie"
            )
            item["hue"] = 1 if multi else _hue_slot(w.id or "")
        tiles.append(item)

    view = {
        "title": dashboard.title,
        "description": dashboard.description,
        "filters": filters,
        "tiles": tiles,
        "layout": dashboard.layout,
    }
    data = client_payload(name, dashboard, etag, request.app.state.layer, store)
    data["metrics_etag"] = request.app.state.watcher.revision(semantic_layer_event(name))
    payload = script_json(data)
    style = dashboard.page_style()
    dash_css = style.dashboard.replace("</", "<\\/") if style.dashboard else None
    dash_page = style.page.replace("</", "<\\/") if style.page else None
    return request.app.state.templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "name": name,
            "drill_from": _drill_origin(store, name, request.query_params.get("from")),
            "dashboard": view,
            "dashboard_json": payload,
            "dash_css": dash_css,
            "dash_page": dash_page,
            "dash_info": _dashboard_info(store, request.app.state.layer, name, dashboard),
            **_nav_context(store, name),
        },
    )
