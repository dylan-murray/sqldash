"""Resolve a tile's `drill:` block against the project: the destination dashboard,
its filters, and how each one is filled from a click.

The browser builds links from the plan this returns and never from the YAML, and
`sqldash lint` reports the same plan's errors, so a link the page would build and
a link lint would pass are the same link."""

from typing import Any
from urllib.parse import quote

from sqldash.models.dashboard import Dashboard, FilterDef, Tile
from sqldash.models.drill import CurrentFilter, DrillSpec
from sqldash.params import option_value, select_choices
from sqldash.project.store import InvalidDashboardError, NotFoundError, Store

SCALAR_ACCEPTS = {
    "text": {"text", "select", "number", "date"},
    "select": {"text", "select", "number", "date"},
    "number": {"number"},
    "date": {"date"},
}


def is_workspace(store: Store) -> bool:
    return getattr(store, "repos", None) is not None


def drill_target_name(store: Store, source_name: str | None, spec: DrillSpec) -> str | None:
    """The store name a drill lands on. A bare name stays inside the source's repo in
    a workspace, so a repo's links keep working under whatever name it is mounted as."""
    if spec.dashboard is None:
        return source_name
    if is_workspace(store) and "/" not in spec.dashboard and source_name and "/" in source_name:
        return f"{source_name.partition('/')[0]}/{spec.dashboard}"
    return spec.dashboard


def dashboard_href(name: str) -> str:
    return f"/d/{quote(name, safe='/')}"


def _filter_label(f: FilterDef) -> str:
    return f"'{f.name}' ({f.type})"


def _load_target(
    store: Store, source_name: str | None, source: Dashboard, spec: DrillSpec
) -> tuple[str | None, Dashboard | None, str | None]:
    target = drill_target_name(store, source_name, spec)
    if spec.dashboard is None or target == source_name:
        return target, source, None
    if "/" in (target or "") and not is_workspace(store):
        return (
            target,
            None,
            f"drill dashboard '{spec.dashboard}' names a repo, but this project is not a "
            "workspace; use the dashboard's own name",
        )
    try:
        dashboard, _, _ = store.load(target)
    except NotFoundError:
        names = sorted(store.discover())
        if source_name and "/" in source_name and is_workspace(store):
            repo = source_name.partition("/")[0]
            names = [n.partition("/")[2] for n in names if n.startswith(f"{repo}/")]
        listed = ", ".join(names) or "none"
        return (
            target,
            None,
            f"drill dashboard '{spec.dashboard}' does not exist (dashboards: {listed})",
        )
    except InvalidDashboardError as exc:
        return target, None, f"drill dashboard '{spec.dashboard}' does not load: {exc}"
    return target, dashboard, None


def _options(target_filter: FilterDef) -> dict[str, list[str]]:
    """The choices a destination select offers, when they are known before it runs."""
    if target_filter.type != "select" or target_filter.options is None:
        return {}
    return {"options": [option_value(o) for o in select_choices(target_filter)]}


def _map_current(
    target_filter: FilterDef, value: CurrentFilter, source: Dashboard
) -> tuple[list[dict[str, Any]], str | None]:
    own = next((f for f in source.filters if f.name == value.filter), None)
    if own is None:
        names = ", ".join(f.name for f in source.filters) or "none"
        return [], (
            f"drill filter '{target_filter.name}' reads {{filter: {value.filter}}}, which is "
            f"not a filter on this dashboard (filters: {names})"
        )
    if (own.type == "daterange") != (target_filter.type == "daterange"):
        return [], (
            f"drill filter {_filter_label(target_filter)} cannot take this dashboard's "
            f"filter {_filter_label(own)}; a date range only carries into a date range"
        )
    if own.type == "daterange":
        return [
            {"param": target_filter.bind[edge], "type": "date", "current": own.bind[edge]}
            for edge in ("start", "end")
        ], None
    if own.type not in SCALAR_ACCEPTS[target_filter.type]:
        return [], (
            f"drill filter {_filter_label(target_filter)} cannot take this dashboard's "
            f"filter {_filter_label(own)}; the value would not fit the destination"
        )
    entry = {"param": target_filter.name, "type": target_filter.type, "current": own.name}
    return [entry | _options(target_filter)], None


def plan_drill(
    store: Store, source_name: str | None, source: Dashboard, tile: Tile
) -> dict[str, Any] | None:
    """What the browser needs to turn one click on this tile into a URL, plus the
    problems that would make that URL wrong. None when the tile has no drill."""
    spec = tile.drill
    if spec is None:
        return None
    target, dashboard, problem = _load_target(store, source_name, source, spec)
    errors: list[str] = [problem] if problem else []
    warnings: list[str] = []
    params: list[dict[str, Any]] = []
    if dashboard is not None:
        declared = {f.name: f for f in dashboard.filters}
        for name, value in spec.filters.items():
            target_filter = declared.get(name)
            if target_filter is None:
                listed = ", ".join(declared) or "none"
                errors.append(
                    f"drill filter '{name}' is not a filter on "
                    f"'{spec.dashboard or 'this dashboard'}' (filters: {listed})"
                )
                continue
            if isinstance(value, CurrentFilter):
                mapped, problem = _map_current(target_filter, value, source)
                if problem:
                    errors.append(problem)
                params.extend(mapped)
                continue
            if target_filter.type == "daterange":
                errors.append(
                    f"drill filter {_filter_label(target_filter)} is a date range; carry one "
                    "with {filter: <this dashboard's daterange>} instead of a column"
                )
                continue
            params.append(
                {"param": name, "type": target_filter.type, "column": value}
                | _options(target_filter)
            )
        if not spec.filters:
            warnings.append(
                f"drill to '{spec.dashboard or 'this dashboard'}' maps no filters, so every "
                "click opens the same unfiltered dashboard"
            )
    return {
        "target": target,
        "href": dashboard_href(target) if target else None,
        "title": dashboard.title if dashboard is not None else spec.dashboard,
        "new_tab": spec.new_tab,
        "column": spec.link_column(),
        "params": params,
        "errors": errors,
        "warnings": warnings,
    }


def plan_drills(store: Store, source_name: str | None, dashboard: Dashboard) -> dict[str, dict]:
    """Every drill plan on a dashboard, keyed by tile id."""
    plans = {}
    for tile in dashboard.tiles:
        plan = plan_drill(store, source_name, dashboard, tile)
        if plan is not None:
            plans[tile.id] = plan
    return plans
