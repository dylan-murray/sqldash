"""Build a BoundQuery from caller names and values.

This is the only path that constructs a MetricQuery and calls
compile_metric. Callers pass names and values; SQL comes from the compiler.
Dry-runs of unpersisted YAML go through bind_resolved.
"""

from dataclasses import dataclass, replace
from typing import Any

from sqldash.connectors.engine import paramstyle_for
from sqldash.params import (
    ParamError,
    bind_sql,
    check_date_window,
    condition_params,
    extract_params,
    filter_param_names,
    inactive_params,
    missing_params_message,
    param_values,
    prepare_sql,
    resolve_date_token,
)
from sqldash.semantics.compiler import MetricQuery, compile_metric
from sqldash.semantics.layer import ResolvedMetric
from sqldash.sqlguard import read_only_violation


@dataclass(frozen=True)
class AppliedFilter:
    """One dashboard filter default that narrowed a call nobody narrowed by hand.

    ``off`` is the token that switches this filter back off where one exists
    (`all`, on a select); the caller has to supply their own value otherwise.
    """

    name: str
    value: Any
    off: str | None = None


@dataclass(frozen=True)
class DashboardScope:
    """What naming a dashboard applied on top of what the caller asked for.

    ``time_range`` alone cannot carry this. A select filter's default narrows an
    answer exactly as much as a window does (#680), and the named-query path
    binds the dashboard's dates into SQL without producing a time_range at all
    (#676). Everything here is what the *dashboard* supplied: a value the caller
    passed is theirs and is never reported back to them.
    """

    window: tuple[Any, Any] | None = None
    filters: tuple[AppliedFilter, ...] = ()
    window_params: tuple[str, str] | None = None
    window_parts: tuple[bool, bool] = (True, True)

    def __bool__(self) -> bool:
        return self.window is not None or bool(self.filters)


def _window_clause(scope: "DashboardScope") -> str | None:
    """How much of the effective window the dashboard supplied, and no more.

    A caller who passed one endpoint still gets the dashboard's other one, so
    dropping the whole clause there hides exactly the narrowing this note
    exists for — while claiming `lo..hi` would credit the dashboard with the
    endpoint the caller set.
    """
    if scope.window is None:
        return None
    lo, hi = scope.window
    own_start, own_end = scope.window_parts
    if own_start and own_end:
        return f"windowed {lo or 'all'}..{hi or 'today'}"
    if own_start and lo:
        return f"windowed from {lo}"
    if own_end and hi:
        return f"windowed to {hi}"
    return None


def scope_note(scope: "DashboardScope | None", dash_name: str | None) -> str | None:
    """The one sentence every surface uses to disclose dashboard scoping.

    Each surface frames it (`note: ` on stderr, its own field in JSON) the way
    `MACRO_NOTE` is framed, so the wording cannot drift per surface — which is
    how the window half came to be told on one path and not the others.
    """
    if not scope or not dash_name:
        return None
    applied: list[str] = []
    remedies: list[str] = []
    if (clause := _window_clause(scope)) is not None:
        applied.append(clause)
        if scope.window_params is None:
            remedies.append("--start/--end for your own window")
        else:
            own_start, own_end = scope.window_parts
            names = [
                name
                for name, own in zip(scope.window_params, (own_start, own_end), strict=True)
                if own
            ]
            flags = " ".join(f"-p {name}=<date>" for name in names)
            remedies.append(f"{flags} for your own window")
    if scope.filters:
        applied.append("filtered " + ", ".join(f"{f.name}={f.value}" for f in scope.filters))
        remedies.append(
            " ".join(f"-p {f.name}={f.off or '<value>'}" for f in scope.filters)
            + " for your own filters"
        )
    return f"{' and '.join(applied)} by dashboard '{dash_name}' — pass {', '.join(remedies)}"


@dataclass(frozen=True)
class BoundQuery:
    """Compiled SQL plus the source it should run against."""

    sql: str
    bind: list
    source: Any
    base_dir: Any
    resolved: ResolvedMetric | None = None
    time_range: tuple[Any, Any] | None = None
    scope: DashboardScope | None = None


def bind_metric(
    layer,
    name: str,
    *,
    scope: str | None = None,
    dimensions: tuple[str, ...] | list[str] = (),
    grain: str | None = None,
    filters: dict[str, Any] | None = None,
    start: Any = None,
    end: Any = None,
    dash=None,
    params: dict[str, Any] | None = None,
    limit: int | None = None,
    source: Any = None,
    base_dir: Any = None,
) -> BoundQuery:
    """Resolve a metric and compile it.

    Two input styles, one MetricQuery: ``dash`` + ``params`` is the dashboard
    filter bar; ``start`` / ``end`` / ``filters`` is the headless shape.
    A dashboard-scoped CLI/MCP call must pass ``dash`` so the daterange
    default applies; ``scope`` only disambiguates the metric name.
    ``metric_query_from_params`` dissolved into the first of those.
    ``source`` / ``base_dir`` override the definition's warehouse (query-page
    picker); compile uses that dialect.
    """
    resolved = layer.resolve(name, scope)
    if source is not None:
        resolved = replace(resolved, source=source, base_dir=base_dir or resolved.base_dir)
    return bind_resolved(
        resolved,
        dimensions=dimensions,
        grain=grain,
        filters=filters,
        start=start,
        end=end,
        dash=dash,
        params=params,
        limit=limit,
    )


def bind_resolved(
    resolved: ResolvedMetric,
    *,
    dimensions: tuple[str, ...] | list[str] = (),
    grain: str | None = None,
    filters: dict[str, Any] | None = None,
    start: Any = None,
    end: Any = None,
    dash=None,
    params: dict[str, Any] | None = None,
    limit: int | None = None,
    paramstyle: str | None = None,
) -> BoundQuery:
    """Compile an already-resolved metric. ``bind_metric`` is resolve + this.

    Dry-runs of candidate YAML that is not on disk yet build a ResolvedMetric
    by hand and come through here, so they do not grow another compile_metric
    call site. ``paramstyle`` lets a dry-run fall back to qmark when the
    dialect is not installed — runtime callers leave it unset.
    """
    scope: DashboardScope | None = None
    if dash is not None:
        query, scope = _query_from_dash(dash, resolved, params or {}, dimensions, grain, limit)
        overlays: dict[str, Any] = {}
        if start or end:
            overlays["time_range"] = (
                resolve_date_token(start),
                resolve_date_token(end, window_end=True),
            )
            scope = replace(scope, window=None, window_params=None)
        if filters:
            param_names = dashboard_param_names(dash)
            declared = {d.name for d in resolved.definition.dimensions}
            extra = {k: v for k, v in filters.items() if k in declared or k not in param_names}
            if extra:
                merged = {**query.filters, **extra}
                off = inactive_params(dash, {**(params or {}), **filters})
                overlays["filters"] = {k: v for k, v in merged.items() if k not in off}
        if overlays:
            query = replace(query, **overlays)
    else:
        query = MetricQuery(
            dimensions=tuple(dimensions),
            grain=grain,
            filters=filters or {},
            time_range=(
                (resolve_date_token(start), resolve_date_token(end, window_end=True))
                if (start or end)
                else None
            ),
            limit=limit,
        )
    if query.time_range is not None:
        check_date_window(*query.time_range, given=(start, end))
    style = paramstyle if paramstyle is not None else paramstyle_for(resolved.source)
    sql, bind = compile_metric(resolved, query, style)
    return BoundQuery(
        sql,
        bind,
        resolved.source,
        resolved.base_dir,
        resolved,
        time_range=query.time_range,
        scope=scope,
    )


def _asked_for(params: dict[str, Any], *names: str) -> bool:
    """Did the caller supply any of these param names themselves?"""
    return any(params.get(name) not in (None, "") for name in names)


def _query_from_dash(
    dashboard, resolved, params, dimensions, grain, limit
) -> tuple[MetricQuery, DashboardScope]:
    """Translate dashboard filter state into a MetricQuery, and say what it added.

    Daterange becomes time_range; select filters matching declared dimensions
    become equality filters; a select on 'all' applies no filter at all.
    A grainless windowed metric keeps only the daterange end.
    """
    query_filters: dict[str, Any] = {}
    time_range: tuple[Any, Any] | None = None
    scope_window: tuple[Any, Any] | None = None
    window_parts = (True, True)
    applied: list[AppliedFilter] = []
    declared = {d.name for d in resolved.definition.dimensions}

    for f in dashboard.filters:
        if f.type == "daterange":
            if resolved.definition.time_dimension is None:
                continue
            start_name, end_name = f.bind["start"], f.bind["end"]
            values, _ = param_values(dashboard, [start_name, end_name], params)
            start, end = values.get(start_name), values.get(end_name)
            if start or end:
                # A grainless window is "last N as of the end". The dashboard
                # default's start would 422 "omit start" on a caller who passed
                # none; an explicit dates_start still goes through so it errors.
                if resolved.definition.window and grain is None:
                    explicit = start_name in params and start not in (None, "")
                    time_range = (start if explicit else None, end)
                else:
                    time_range = (start, end)
                own = (not _asked_for(params, start_name), not _asked_for(params, end_name))
                if scope_window is None and any(own):
                    scope_window, window_parts = time_range, own
            continue
        if f.name not in declared:
            continue
        values, _ = param_values(dashboard, [f.name], params)
        value = values.get(f.name)
        if value in (None, "") or f.name in inactive_params(dashboard, values):
            continue
        query_filters[f.name] = value
        if not _asked_for(params, f.name):
            applied.append(AppliedFilter(f.name, value, "all" if f.type == "select" else None))

    query = MetricQuery(
        dimensions=tuple(dimensions),
        grain=grain,
        filters=query_filters,
        time_range=time_range,
        limit=limit,
    )
    return query, DashboardScope(
        window=scope_window, filters=tuple(applied), window_parts=window_parts
    )


def dashboard_param_names(dashboard) -> set[str]:
    """Every param name the dashboard itself defines: filter names plus daterange binds.

    A caller naming one of these is setting the dashboard's own filter, not
    naming a metric dimension; anything outside the set is a name the caller
    invented, so it must reach the compiler to be validated rather than be
    dropped.
    """
    names: set[str] = set()
    for f in dashboard.filters:
        names.add(f.name)
        if f.bind:
            names.update(f.bind.values())
    return names


def query_param_names(dashboard, sql: str) -> set[str]:
    """Every param name a run of this SQL may legitimately be given.

    The dashboard's own names (the filter bar posts all of them with every
    tile run) plus every name the *unrendered* SQL references. Unrendered is
    the load-bearing word: a `{{ tier }}` that only appears inside a
    `{% if region %}` block is a real name of this query even on a run where
    the block is dropped, so the rendered SQL is not the vocabulary — it is
    the subset of it that this one run happened to use.
    """
    return dashboard_param_names(dashboard) | set(extract_params(sql))


def refuse_invented_params(dashboard, sql: str, params: dict[str, Any]) -> None:
    """Refuse a param name neither the dashboard nor the query's SQL defines.

    `prepare_sql` only ever looks up the names the rendered SQL still
    mentions, so an unrecognised one was never consulted and the run returned
    the unfiltered answer to a question nobody asked (#626). The metric route
    already makes this split (#394); this is the same invariant on the
    query/SQL route, named for what a query has — parameters, not dimensions.
    """
    known = query_param_names(dashboard, sql)
    invented = [name for name in params if name not in known]
    if not invented:
        return
    valid = ", ".join(sorted(known)) or "(none declared)"
    raise ParamError(f"unknown parameter '{invented[0]}' — valid parameters: {valid}")


def bind_named_query(
    dashboard,
    sql: str,
    *,
    params: dict[str, Any] | None = None,
    source=None,
    base_dir=None,
    surface: str = "tile SQL",
    scan_body: bool = False,
) -> BoundQuery:
    """Prepare dashboard SQL and bind it.

    ``source`` is whatever the adapter already picked: an explicit choice, or
    the owning tile's source from ``Dashboard.query_owner_source``. Omitted,
    this is the dashboard default. Missing params raise ParamError, and so
    does a param name nobody defines. A write opener raises ParamError too —
    viewing a dashboard runs this SQL (#500).

    The guard reads the rendered text, the statement that actually runs. Ad-hoc
    SQL passes ``surface="ad-hoc sql", scan_body=True``: a `{% if %}` branch
    can drop a quote, and a body scan of the template alone then saw INTO as
    part of a string literal while Postgres ran SELECT INTO.
    """
    chosen = source if source is not None else dashboard.source
    refuse_invented_params(dashboard, sql, params or {})
    text, values, missing = prepare_sql(dashboard, sql, params or {})
    if missing:
        raise ParamError(missing_params_message(dashboard, missing))
    violation = read_only_violation(text, surface=surface, scan_body=scan_body)
    if violation is not None:
        raise ParamError(violation)
    bound, bind = bind_sql(text, values, paramstyle_for(chosen))
    return BoundQuery(
        bound, bind, chosen, base_dir, scope=_query_scope(dashboard, sql, values, params or {})
    )


def _query_scope(
    dashboard, sql: str, bound: dict[str, Any], params: dict[str, Any]
) -> DashboardScope:
    """What the dashboard supplied to a named query that the caller did not.

    The metric path reads this off the MetricQuery; a named query has none, only
    the params `prepare_sql` filled in — which is why the dashboard's dates
    reached the SQL with nothing upstream holding them (#676).

    A filter is in play when its value was bound *or* when it decided a `{% if %}`
    branch. Reading the bound values alone misses the second: a filter gating a
    clause leaves no placeholder behind, so `{% if region %}AND region = 'us'{%
    endif %}` narrowed the answer and the note said nothing (#680, PR review).
    Resolving against the unrendered vocabulary is what makes that value
    available at all — the rendered SQL no longer mentions the name.
    """
    vocabulary = extract_params(sql)
    values, _ = param_values(dashboard, vocabulary, params)
    gating = set(condition_params(sql))
    window: tuple[Any, Any] | None = None
    window_params: tuple[str, str] | None = None
    window_parts = (True, True)
    applied: list[AppliedFilter] = []
    off = inactive_params(dashboard, values)
    for f in dashboard.filters:
        names = filter_param_names(f)
        if not (any(name in bound for name in names) or gating.intersection(names)):
            continue
        if f.type == "daterange":
            start_name, end_name = f.bind["start"], f.bind["end"]
            own = (not _asked_for(params, start_name), not _asked_for(params, end_name))
            if window is None and any(own):
                window = (values.get(start_name), values.get(end_name))
                window_params = (start_name, end_name)
                window_parts = own
            continue
        if _asked_for(params, *names):
            continue
        value = values.get(f.name)
        if value in (None, "") or f.name in off:
            continue
        applied.append(AppliedFilter(f.name, value, "all" if f.type == "select" else None))
    return DashboardScope(
        window=window,
        filters=tuple(applied),
        window_params=window_params,
        window_parts=window_parts,
    )
