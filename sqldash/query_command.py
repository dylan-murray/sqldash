"""`sqldash query` and `sqldash metric query`: resolve, bind, run and print.

cli.py imports this only when one of the two commands runs, so `sqldash --help`
does not pay for the execution and semantic-layer stack.
"""

import csv
import io
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import typer

from sqldash.connectors.wire import flat_text
from sqldash.csv_safe import spreadsheet_safe
from sqldash.execution import ExecutionRegistry
from sqldash.models.dashboard import Dashboard
from sqldash.models.source import Source
from sqldash.params import ParamError
from sqldash.period import COMPARE_MODES
from sqldash.project.sources import resolve_picker_source
from sqldash.project.store import InvalidDashboardError, NotFoundError, Store, WorkspaceStore
from sqldash.semantics import MetricNotFoundError, SemanticError, SemanticLayer
from sqldash.semantics.bind import bind_metric, bind_named_query, scope_note
from sqldash.semantics.compare import compare_metric
from sqldash.semantics.layer import WorkspaceLayer


def metric_query(
    store: Store,
    layer: SemanticLayer | WorkspaceLayer,
    name: str,
    dimension: list[str],
    grain: str | None,
    param: list[str],
    start: str | None,
    end: str | None,
    fmt: str,
    dashboard: str | None,
    compare: str | None,
    row_limit: int,
) -> None:
    try:
        filters = {}
        for item in param:
            key, _, value = item.partition("=")
            filters[key] = value
        dash_obj = None
        if dashboard:
            dash_obj, _, _ = store.load(dashboard)
        bound = bind_metric(
            layer,
            name,
            scope=dashboard,
            dash=dash_obj,
            params=filters if dash_obj is not None else None,
            dimensions=dimension,
            grain=grain,
            filters=filters,
            start=start,
            end=end,
            limit=None,
        )
    except (
        MetricNotFoundError,
        SemanticError,
        NotFoundError,
        InvalidDashboardError,
        ParamError,
    ) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    try:
        compare = _require_compare(compare)
    except ValueError as extra_exc:
        typer.echo(f"error: {extra_exc}", err=True)
        raise typer.Exit(1) from extra_exc
    if compare and fmt == "csv":
        typer.echo(
            "error: --compare is not a csv shape — use -f json (windows + delta) or -f table",
            err=True,
        )
        raise typer.Exit(1)
    _note_scope(bound, dashboard)
    registry = ExecutionRegistry(max_workers=1)
    try:
        result = registry.run_bound(bound, row_limit, timeout=None)
        extra = None
        if compare:
            extra = _compare_payload(
                compare,
                bound,
                result,
                registry,
                row_limit,
                bind_metric,
                {
                    "layer": layer,
                    "name": name,
                    "scope": dashboard,
                    "dash": dash_obj,
                    "params": filters if dash_obj is not None else None,
                    "dimensions": dimension,
                    "grain": grain,
                    "filters": filters,
                    "limit": None,
                },
            )
    except Exception as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    finally:
        registry.shutdown()
    print_result(result, fmt, compare=extra)


@dataclass
class QueryScope:
    store: Store
    layer: SemanticLayer | WorkspaceLayer
    name: str
    dashboard: Dashboard | None
    dash_name: str | None
    dash_named: bool
    source_name: str | None
    picker_source: Source | None = None
    picker_base: Path | None = None


def query_scope(
    store: Store,
    layer: SemanticLayer | WorkspaceLayer,
    workspace_dashboard: str | None,
    target: str,
    name: str,
    dashboard_opt: str | None,
    source_name: str | None,
) -> QueryScope:
    path = Path(target)

    requested = dashboard_opt or workspace_dashboard
    if "." in name and requested is None:
        requested, name = name.split(".", 1)

    # Naming the dashboard (--dashboard, a dotted name, a workspace id, or the
    # .yaml file itself) asks for its filter defaults; inferring it because the
    # project happens to hold exactly one does not. A bare metric name must mean
    # the same thing whether or not a second dashboard exists. #640
    dash_named = requested is not None or path.is_file()
    dashboard, dash_name = _query_dashboard(store, requested, path)
    scope = QueryScope(store, layer, name, dashboard, dash_name, dash_named, source_name)
    if source_name is not None:
        if not dash_name:
            typer.echo(
                "error: --source needs a dashboard — pass --dashboard "
                "or run against a project with one dashboard",
                err=True,
            )
            raise typer.Exit(1)
        scope.picker_source, scope.picker_base = _picker_source_or_exit(scope, source_name)
    return scope


def _query_dashboard(store, requested: str | None, path: Path):
    dashboard = None
    dash_name = None
    discovered = store.discover()
    if requested is not None:
        if requested not in discovered:
            typer.echo(
                f"error: no dashboard named '{requested}' (available: "
                f"{', '.join(discovered) or '(none)'})",
                err=True,
            )
            raise typer.Exit(1)
        dash_name = requested
    elif path.is_file() or len(discovered) == 1:
        dash_name = next(iter(discovered), None)
    if dash_name:
        try:
            dashboard, _, _ = store.load(dash_name)
        except InvalidDashboardError as exc:
            # The model rejects an unknown tile source at parse time, so this is
            # where that lands — a traceback until now, and the third command
            # with this gap after `dashboard show`.
            typer.echo(f"error: {dash_name} does not parse — {exc}", err=True)
            raise typer.Exit(1) from exc
    return dashboard, dash_name


def _picker_source_or_exit(scope: QueryScope, key: str):
    try:
        return resolve_picker_source(
            scope.store, scope.layer, scope.dash_name, key, dashboard=scope.dashboard
        )
    except KeyError as exc:
        known = exc.args[1] if len(exc.args) > 1 else []
        named = ", ".join(known) or "(none defined)"
        typer.echo(f"error: no source named '{key}' — known sources: {named}", err=True)
        raise typer.Exit(1) from exc


def bind_query(scope: QueryScope, param, dimension, grain, fmt, start, end, compare):
    overrides = {}
    for item in param:
        key, _, value = item.partition("=")
        overrides[key] = value

    try:
        compare = _require_compare(compare)
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    asked_compare = compare

    dashboard = scope.dashboard
    requested = name = scope.name
    tile = None
    if dashboard is not None:
        tile = next((item for item in dashboard.tiles if item.id == requested), None)
    # An exact metric-tile id wins over a query of the same name: addressing a
    # tile by id must return what the tile shows, never a same-named query's raw
    # SQL (silent-wrong-result). SQL tiles have metric=None, so query addressing
    # is unaffected.
    tile_claimed = tile is not None and tile.metric is not None
    # A tile id only exists on a dashboard, so addressing one asks for that
    # dashboard's window the same way --dashboard does — inferring the dashboard
    # to find the tile stays a convenience.
    dash_filters = dashboard if (scope.dash_named or tile_claimed) else None
    picked = scope.source_name is not None
    if tile_claimed:
        name = tile.metric.name
        if grain is None and tile.metric.grain:
            grain = tile.metric.grain
        if not dimension and tile.metric.dimensions:
            # Inherit the tile's grouping too, or an authored grouped tile
            # (dimensions: [region]) would run ungrouped here and hand back a
            # confident big-number delta the browser never shows — it renders
            # grouped rows with no pct. Grouped → delta_from_results yields None.
            dimension = list(tile.metric.dimensions)
        if compare is None:
            compare = tile.metric.compare
        if not picked and tile.source:
            scope.picker_source, scope.picker_base = _picker_source_or_exit(scope, tile.source)
            picked = True

    if asked_compare and fmt == "csv":
        typer.echo(
            "error: --compare is not a csv shape — use -f json (windows + delta) or -f table",
            err=True,
        )
        raise typer.Exit(1)
    if fmt == "csv":
        compare = None

    def metric_kwargs() -> dict:
        return {
            "layer": scope.layer,
            "name": name,
            "scope": scope.dash_name,
            "dash": dash_filters,
            "params": overrides,
            "dimensions": dimension,
            "grain": grain,
            "filters": dict(overrides.items()),
            "limit": None,
            "source": scope.picker_source,
            "base_dir": scope.picker_base if picked else None,
        }

    if dashboard is not None and requested in dashboard.queries and not tile_claimed:
        bound = _bind_named_query(scope, requested, overrides, start, end, compare)
    else:
        bound = _bind_metric(scope, metric_kwargs(), start, end)
    _note_scope(bound, scope.dash_name)
    return bound, compare, metric_kwargs()


def _bind_named_query(scope: QueryScope, requested, overrides, start, end, compare):
    dashboard = scope.dashboard
    if start is not None or end is not None or compare is not None:
        # Same shape as --source on the metric branch: a flag that does not
        # apply here must not silently no-op. Named queries already take
        # -p dates_start=-30d for their own params.
        flag = (
            "--compare"
            if compare is not None and start is None and end is None
            else "--start/--end"
        )
        typer.echo(
            f"error: {flag} apply to metrics; '{requested}' resolved as a "
            "query — pass -p on its date params, or run a metric",
            err=True,
        )
        raise typer.Exit(1)
    source = None
    base_dir = scope.store.path_for(scope.dash_name).parent
    if scope.source_name is not None:
        source, base_dir = scope.picker_source, scope.picker_base
    else:
        try:
            owner = dashboard.query_owner_source(requested)
        except ValueError as exc:
            typer.echo(f"error: {exc} — pass --source to choose", err=True)
            raise typer.Exit(1) from exc
        source = dashboard.named_source(owner)
    try:
        return bind_named_query(
            dashboard,
            dashboard.queries[requested],
            params=overrides,
            source=source,
            base_dir=base_dir,
        )
    except ParamError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc


def _bind_metric(scope: QueryScope, kwargs: dict, start, end):
    try:
        return bind_metric(**kwargs, start=start, end=end)
    except MetricNotFoundError as exc:
        dashboard, store = scope.dashboard, scope.store
        if dashboard is not None:
            queries = ", ".join(dashboard.queries) or "(none)"
        else:
            # Without a dashboard chosen, every query needs its prefix — and
            # saying "(none)" here was simply wrong in a workspace.
            qualified = [
                f"{dash_id}.{query}"
                for dash_id, loaded in store.iter_loaded()
                for query in loaded.queries
            ]
            queries = ", ".join(sorted(qualified)) or "(none)"
        hint = ""
        if dashboard is None and len(store.discover()) > 1:
            if isinstance(store, WorkspaceStore):
                hint = (
                    " — metrics live in repos; use repo/metric or "
                    "repo/dashboard.query, or sqldash metric list"
                )
            else:
                hint = " — this project has multiple dashboards; use dashboard.query or --dashboard"
        typer.echo(
            f"error: '{kwargs['name']}' is not a query (available: {queries}) and {exc}{hint}",
            err=True,
        )
        raise typer.Exit(1) from exc
    except (SemanticError, ParamError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc


def run_query(bound, row_limit: int, compare: str | None, compare_kwargs: dict):
    registry = ExecutionRegistry(max_workers=1)
    extra = None
    try:
        result = registry.run_bound(bound, row_limit, timeout=None)
        if compare and bound.resolved is not None:
            extra = _compare_payload(
                compare, bound, result, registry, row_limit, bind_metric, compare_kwargs
            )
    except Exception as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    finally:
        registry.shutdown()
    return result, extra


def _note_scope(bound, dash_name: str | None) -> None:
    """Say on stderr what naming a dashboard narrowed that the caller did not.

    A dashboard-scoped number is a windowed and filtered number, and every
    format printed it bare — so a script could not tell 60 days of one region's
    revenue from all of it. stderr keeps json/csv on stdout parseable.
    #640, #676, #680.
    """
    note = scope_note(bound.scope, dash_name)
    if note is not None:
        typer.echo(f"note: {note}", err=True)


def require_fmt(fmt: str) -> str:
    if fmt not in ("table", "csv", "json"):
        typer.echo(f"error: unknown format {fmt!r} — use table, csv, or json", err=True)
        raise typer.Exit(1)
    return fmt


def _require_compare(value: str | None) -> str | None:
    if value is None:
        return None
    if value not in COMPARE_MODES:
        raise ValueError(f"unknown compare '{value}' — use {' or '.join(COMPARE_MODES)}")
    return value


def _compare_payload(mode, bound, current, registry, row_limit, bind_metric, kwargs):
    """The shared second window, in the CLI's JSON shape: rows as dicts, no SQL."""
    return compare_metric(
        mode,
        bound,
        current,
        rebind=lambda start, end: bind_metric(**kwargs, start=start, end=end),
        run=lambda previous: registry.run_bound(previous, row_limit, timeout=None),
        grain=kwargs.get("grain"),
        dimensions=kwargs.get("dimensions"),
        dash=kwargs.get("dash"),
    ).payload(rows_as_dicts=True, include_sql=False)


def _table_cell(value) -> str:
    """A cell for the human table. A double prints at 15 significant digits, the
    most it holds exactly, so a SUM reads 172117.82 rather than its binary tail;
    json and csv keep the full value."""
    if isinstance(value, float) and math.isfinite(value):
        return format(value, ".15g")
    return str(flat_text(value))


def print_result(result, fmt: str, compare: dict | None = None) -> None:
    names = [c.name for c in result.columns]
    if fmt == "json":
        rows = [dict(zip(names, row, strict=True)) for row in result.rows]
        payload = {"rows": rows, "compare": compare} if compare else rows
        typer.echo(json.dumps(payload, indent=2, allow_nan=False))
    elif fmt == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([spreadsheet_safe(name) for name in names])
        writer.writerows([spreadsheet_safe(flat_text(v)) for v in row] for row in result.rows)
        sys.stdout.write(buffer.getvalue())
    else:
        cells = [[_table_cell(v) for v in row] for row in result.rows]
        widths = [
            max(len(str(name)), *(len(row[i]) for row in cells)) if cells else len(str(name))
            for i, name in enumerate(names)
        ]
        typer.echo("  ".join(name.ljust(widths[i]) for i, name in enumerate(names)))
        typer.echo("  ".join("-" * widths[i] for i in range(len(names))))
        for row in cells:
            typer.echo("  ".join(v.ljust(widths[i]) for i, v in enumerate(row)))
        if result.truncated:
            typer.echo(f"({result.row_count} rows shown, truncated)")
        if compare and compare.get("delta"):
            delta = compare["delta"]
            arrow = "▲" if delta["pct"] >= 0 else "▼"
            typer.echo(f"{arrow} {abs(delta['pct']) * 100:.1f}% vs {compare['label']}")
        elif compare:
            window = compare["window"]
            typer.echo(
                f"compare {compare['label']} {window['start']}..{window['end']} "
                "(grouped — no single delta; use -f json for both windows)"
            )
    if result.truncated and fmt in ("csv", "json"):
        typer.echo(f"({result.row_count} rows shown, truncated)", err=True)
