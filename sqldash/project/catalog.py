"""The one walk over dashboards and metric listings, shared by CLI, HTTP, and MCP.

Adapters format their own wire shapes from these records. Broken dashboards
are entries with ``error`` set — never omitted. ``iter_loaded`` is for
callers that must skip them on purpose.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqldash.models.source import redact_source
from sqldash.semantics.compiler import MACRO_NOTE, describes_macro
from sqldash.semantics.layer import metric_summary

if TYPE_CHECKING:
    from sqldash.models.dashboard import Dashboard
    from sqldash.project.store import Store
    from sqldash.semantics import SemanticLayer
    from sqldash.semantics.layer import ResolvedMetric


@dataclass(frozen=True)
class DashboardRecord:
    """One discovered dashboard file, loaded or not."""

    name: str
    path: Path
    dashboard: "Dashboard | None"
    etag: str | None = None
    error: str | None = None


def list_dashboards(store: "Store") -> list[DashboardRecord]:
    """Every discovered file, including ones that fail to parse."""
    records = []
    for name, path in store.discover().items():
        try:
            dashboard, _, etag = store.load(name)
            records.append(DashboardRecord(name, path, dashboard, etag=etag))
        except Exception as exc:
            records.append(DashboardRecord(name, path, None, error=str(exc)))
    return records


def list_metrics(layer: "SemanticLayer", dashboard: str | None = None) -> list[dict[str, Any]]:
    """The canonical metric_summary for every metric the layer can resolve.

    Pass ``dashboard`` for the names a tile on that file can save. Workspace
    listings are repo-prefixed (`acme/revenue`) and ``MetricRef.name`` rejects
    ``/``, so the query-page picker must use this scoped list, not the global one.
    """
    if dashboard is None:
        resolved = layer.all_metrics()
    else:
        resolved = layer.metrics_for_dashboard(dashboard).values()
    return [metric_summary(m) for m in resolved]


def metric_detail(resolved: "ResolvedMetric") -> dict[str, Any]:
    """Full definition of one resolved metric. Adapters add wire aliases."""
    payload = metric_summary(resolved)
    payload.update(
        expr=resolved.definition.expr,
        relation=(
            {"table": resolved.relation.table}
            if resolved.relation.table
            else {"sql": resolved.relation.sql}
        ),
        default_filters=resolved.definition.filters,
        source=redact_source(resolved.source),
    )
    # Agents read this payload and go on to write SQL, so an expr they cannot
    # run has to say so. Only when one is actually present: a note on every
    # metric is noise that teaches nothing.
    definition = resolved.definition
    if describes_macro(
        definition.expr,
        *definition.filters,
        *(dim.expr for dim in definition.dimensions),
        definition.time_dimension.expr if definition.time_dimension else None,
        resolved.relation.sql,
    ):
        payload["expr_note"] = f"this definition {MACRO_NOTE}"
    return payload
