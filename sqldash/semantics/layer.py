"""Resolves metric names to compilable definitions across metrics.yaml and
dashboard-inline declarations."""

import io
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from sqldash.models.dashboard import Dashboard
from sqldash.models.semantics import MetricDef, MetricsFile, RelationDef
from sqldash.models.source import source_label
from sqldash.project.store import DashboardStore, format_validation_error, yaml, yaml_error


class SemanticError(Exception):
    pass


class MetricNotFoundError(SemanticError):
    pass


class AmbiguousMetricError(MetricNotFoundError):
    """A name more than one dashboard defines inline. Carries the dashboards
    structurally so a workspace can repo-qualify them: the escape hatch the
    message recommends is only usable if the names in it are."""

    def __init__(self, metric: str, dashboards: tuple[str, ...]) -> None:
        self.metric = metric
        self.dashboards = dashboards
        super().__init__(
            f"'{metric}' is defined inline by more than one dashboard "
            f"({', '.join(dashboards)}) — resolve it against one of those "
            f"dashboards, or move the definition to metrics.yaml"
        )

    def requalified(self, repo: str) -> "AmbiguousMetricError":
        return AmbiguousMetricError(
            f"{repo}/{self.metric}", tuple(f"{repo}/{d}" for d in self.dashboards)
        )


class MetricNotInDashboardError(MetricNotFoundError):
    """A name that exists, but not in the dashboard it was scoped to. Same
    requalifying contract as the ambiguity it usually follows."""

    def __init__(self, metric: str, dashboard: str, dashboards: tuple[str, ...]) -> None:
        self.metric = metric
        self.dashboard = dashboard
        self.dashboards = dashboards
        super().__init__(
            f"no metric named '{metric}' in '{dashboard}' — it is defined "
            f"inline by {', '.join(dashboards)}"
        )

    def requalified(self, repo: str) -> "MetricNotInDashboardError":
        return MetricNotInDashboardError(
            f"{repo}/{self.metric}",
            f"{repo}/{self.dashboard}",
            tuple(f"{repo}/{d}" for d in self.dashboards),
        )


@dataclass
class ResolvedMetric:
    """A metric ready for the compiler: its definition (any derived expr already
    expanded to plain aggregates), the relation it aggregates over, and the
    source/base_dir it executes against."""

    name: str
    definition: MetricDef
    source: object
    base_dir: Path
    relation: RelationDef
    origin: Literal["project", "dashboard"]
    dashboard: str | None = None
    # Every dashboard defining this name inline, when more than one does. A
    # listing that shows such a name as ordinary is claiming a uniqueness the
    # resolve path refuses, so the fact rides on the metric itself rather than
    # being rediscovered by each of the six surfaces that list metrics: CLI
    # `metric list`, MCP `list_metrics`, `/api/metrics`, the index page,
    # `export context` and `export cortex`.
    ambiguous_with: tuple[str, ...] = ()


def resolve_relation(metric: MetricDef, relations: dict[str, RelationDef]) -> RelationDef:
    if metric.relation:
        return relations[metric.relation]
    if metric.table:
        return RelationDef(table=metric.table)
    return RelationDef(sql=metric.sql)


DERIVED_REF = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_derived(
    name: str,
    definition: MetricDef,
    peers: dict[str, MetricDef],
    relations: dict[str, RelationDef],
) -> tuple[MetricDef, RelationDef]:
    """Inline each ``{ref}``'s aggregate expr into the derived expression; all refs
    must be plain metrics sharing one relation, with no filters, window or cumulative."""
    refs = DERIVED_REF.findall(definition.derived)
    if not refs:
        raise SemanticError(
            f"derived metric '{name}' references no metrics — wrap names in braces, "
            'e.g. derived: "{revenue} / NULLIF({order_count}, 0)"'
        )
    relation: RelationDef | None = None
    for ref in refs:
        peer = peers.get(ref)
        if peer is None:
            available = ", ".join(sorted(n for n in peers if peers[n].derived is None))
            raise SemanticError(
                f"derived metric '{name}' references unknown metric '{{{ref}}}' "
                f"— available: {available or '(none)'}"
            )
        if peer.derived is not None:
            raise SemanticError(
                f"derived metric '{name}' references derived metric '{{{ref}}}' — "
                "derived metrics may only reference plain metrics"
            )
        if peer.filters:
            # Only the ref's `expr` is inlined below, so its `filters` would be
            # dropped — and the result stays plausible while being wrong: a
            # rate over two differently-filtered components compiled to
            # ABS(SUM(x)) / SUM(x) and reported exactly 1.0. Refuse instead of
            # answering with a number nobody can tell is wrong.
            conditions = " AND ".join(peer.filters)
            raise SemanticError(
                f"derived metric '{name}' references '{{{ref}}}', which carries "
                f"filters ({conditions}) — inlining would silently drop them. "
                f"Fold the condition into that metric's expr instead, e.g. "
                f"SUM(CASE WHEN {peer.filters[0]} THEN <column> END)"
            )
        if peer.window or peer.cumulative:
            kind = f"window: {peer.window}" if peer.window else "cumulative: true"
            raise SemanticError(
                f"derived metric '{name}' references '{{{ref}}}', which carries {kind}, "
                "and inlining would silently drop it and compute the plain per-bucket "
                f"aggregate instead. Query '{ref}' on its own; a derived expression "
                "cannot carry a trailing window or running total"
            )
        peer_relation = resolve_relation(peer, relations)
        if relation is None:
            relation = peer_relation
        elif relation != peer_relation:
            raise SemanticError(
                f"derived metric '{name}' mixes metrics from different relations "
                f"({relation.table or relation.sql} vs {peer_relation.table or peer_relation.sql})"
            )
    expr = DERIVED_REF.sub(lambda m: f"({peers[m.group(1)].expr})", definition.derived)
    return definition.model_copy(update={"expr": expr, "derived": None}), relation


def resolve_definitions(
    definitions: dict[str, MetricDef], relations: dict[str, RelationDef]
) -> dict[str, tuple[MetricDef, RelationDef]]:
    """Pair every definition with its relation, expanding derived metrics along the way."""
    resolved: dict[str, tuple[MetricDef, RelationDef]] = {}
    for metric_name, definition in definitions.items():
        if definition.derived is not None:
            resolved[metric_name] = expand_derived(metric_name, definition, definitions, relations)
        else:
            resolved[metric_name] = (definition, resolve_relation(definition, relations))
    return resolved


def parse_metrics_file(text: str) -> MetricsFile:
    """Parse metrics.yaml text, folding YAML and schema errors into SemanticError."""
    try:
        data = yaml.load(io.StringIO(text))
    except Exception as exc:
        raise SemanticError(f"metrics.yaml: {yaml_error(text, exc)}") from exc
    if not isinstance(data, dict):
        raise SemanticError("metrics.yaml must be a YAML mapping")
    try:
        return MetricsFile.model_validate(data)
    except ValidationError as exc:
        errors = "; ".join(format_validation_error(e) for e in exc.errors())
        raise SemanticError(f"metrics.yaml: {errors}") from exc


class SemanticLayer:
    """One project's metrics: metrics.yaml is canonical; dashboard-inline metrics
    win by name within their own dashboard and are visible globally only when the
    name is otherwise free. Project metrics run on the metrics.yaml source, inline
    ones on their dashboard's."""

    def __init__(self, store: DashboardStore) -> None:
        self.store = store

    def metrics_path(self) -> Path | None:
        for name in ("metrics.yaml", "metrics.yml"):
            path = self.store.root / name
            if path.is_file():
                return path
        return None

    def metrics_file(self) -> MetricsFile | None:
        path = self.metrics_path()
        if path is None:
            return None
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise SemanticError(f"metrics.yaml: file is not valid UTF-8: {exc}") from exc
        return parse_metrics_file(text)

    def project_metrics(self) -> dict[str, ResolvedMetric]:
        mf = self.metrics_file()
        if mf is None:
            return {}
        return {
            name: ResolvedMetric(
                name=name,
                definition=definition,
                source=mf.source,
                base_dir=self.store.root,
                relation=relation,
                origin="project",
            )
            for name, (definition, relation) in resolve_definitions(
                mf.metrics, mf.relations
            ).items()
        }

    def _inline_metrics(self, dashboard: Dashboard, name: str) -> dict[str, ResolvedMetric]:
        base_dir = self.store.path_for(name).parent
        return {
            metric_name: ResolvedMetric(
                name=metric_name,
                definition=definition,
                source=dashboard.source,
                base_dir=base_dir,
                relation=relation,
                origin="dashboard",
                dashboard=name,
            )
            for metric_name, (definition, relation) in resolve_definitions(
                dashboard.metrics, dashboard.relations
            ).items()
        }

    def metrics_for_dashboard(self, name: str) -> dict[str, ResolvedMetric]:
        dashboard, _, _ = self.store.load(name)
        merged = self.project_metrics()
        merged.update(self._inline_metrics(dashboard, name))
        return merged

    def _inline_by_name(self) -> dict[str, list[ResolvedMetric]]:
        """Every dashboard's inline metrics, grouped by name so collisions show."""
        grouped: dict[str, list[ResolvedMetric]] = {}
        for name, dashboard in self.store.iter_loaded():
            for metric_name, resolved in self._inline_metrics(dashboard, name).items():
                grouped.setdefault(metric_name, []).append(resolved)
        return grouped

    def all_metrics(self) -> list[ResolvedMetric]:
        merged = self.project_metrics()
        for metric_name, resolved in self._inline_by_name().items():
            if metric_name in merged:
                continue  # metrics.yaml is canonical, so this is not a collision
            first = resolved[0]
            if len(resolved) > 1:
                first = replace(
                    first,
                    ambiguous_with=tuple(sorted(m.dashboard for m in resolved if m.dashboard)),
                )
            merged[metric_name] = first
        return list(merged.values())

    def resolve(self, metric: str, dashboard: str | None = None) -> ResolvedMetric:
        """Look up one metric, scoped to a dashboard's merged view when given."""
        if dashboard is not None:
            metrics = self.metrics_for_dashboard(dashboard)
        else:
            # A metrics.yaml definition is canonical and wins by design, so it is
            # not a collision. Two dashboards defining the same inline name is:
            # answering with either one returns that dashboard's number under a
            # name the caller did not scope.
            project = self.project_metrics()
            inline = self._inline_by_name()
            metrics = dict(project)
            for metric_name, resolved in inline.items():
                metrics.setdefault(metric_name, resolved[0])
            if metric not in project and len(inline.get(metric, ())) > 1:
                where = sorted(m.dashboard for m in inline[metric] if m.dashboard)
                raise AmbiguousMetricError(metric, tuple(where))
        if metric not in metrics:
            if dashboard is not None:
                # Scoping is what the ambiguity refusal tells you to do, so this
                # miss is reachable by following that advice to the wrong
                # dashboard. "No such metric" would contradict the message that
                # sent the caller here.
                defined_by = sorted(m.dashboard for m in self._inline_by_name().get(metric, ()))
                if defined_by:
                    raise MetricNotInDashboardError(metric, dashboard, tuple(defined_by))
            available = ", ".join(sorted(metrics)) or "(none defined)"
            raise MetricNotFoundError(
                f"no metric named '{metric}' — available metrics: {available}"
            )
        return metrics[metric]


def repo_problem(repo: str, exc: Exception) -> str:
    """A repo's load failure as a workspace listing reports it: a message that
    already names its file reads as that repo's path."""
    message = str(exc)
    if message.startswith(("metrics.yaml", "agents.yaml")):
        return f"{repo}/{message}"
    return f"{repo}: {message}"


class WorkspaceLayer:
    """Semantic layers of several repos; metric names are 'repo/metric' (bare
    names still resolve when unambiguous across the workspace)."""

    def __init__(self, layers: dict[str, SemanticLayer]) -> None:
        self.layers = layers

    def metrics_file(self) -> None:
        return None

    def _prefixed(self, repo: str, resolved: ResolvedMetric) -> ResolvedMetric:
        return replace(
            resolved,
            name=f"{repo}/{resolved.name}",
            dashboard=f"{repo}/{resolved.dashboard}" if resolved.dashboard else None,
            ambiguous_with=tuple(f"{repo}/{d}" for d in resolved.ambiguous_with),
        )

    def _enumerate(self) -> tuple[list[ResolvedMetric], list[str]]:
        metrics: list[ResolvedMetric] = []
        problems: list[str] = []
        for repo, layer in self.layers.items():
            try:
                resolved = layer.all_metrics()
            except SemanticError as exc:
                problems.append(repo_problem(repo, exc))
                continue
            metrics.extend(self._prefixed(repo, m) for m in resolved)
        return metrics, problems

    def all_metrics(self) -> list[ResolvedMetric]:
        """Every metric of every repo that resolves. A repo whose metrics do not
        is skipped, not fatal, so one broken file cannot hide the rest of the
        workspace; `problems()` names what was skipped."""
        return self._enumerate()[0]

    def problems(self) -> list[str]:
        return self._enumerate()[1]

    def metrics_for_dashboard(self, name: str) -> dict[str, ResolvedMetric]:
        repo, _, rest = name.partition("/")
        if repo not in self.layers or not rest:
            raise MetricNotFoundError(f"unknown dashboard '{name}'")
        return self.layers[repo].metrics_for_dashboard(rest)

    def _available(self) -> str:
        return ", ".join(sorted(m.name for m in self.all_metrics())) or "(none defined)"

    def _unknown(self, metric: str) -> MetricNotFoundError:
        return MetricNotFoundError(
            f"no metric named '{metric}' — available metrics: {self._available()}"
        )

    def _resolve_in(self, repo: str, metric: str, dashboard: str | None = None) -> ResolvedMetric:
        try:
            return self._prefixed(repo, self.layers[repo].resolve(metric, dashboard))
        except (AmbiguousMetricError, MetricNotInDashboardError) as exc:
            raise exc.requalified(repo) from exc
        except MetricNotFoundError as exc:
            # Inner SemanticLayer lists bare names. Those are not what
            # `metric list` prints from a workspace cwd, and a colliding
            # bare name does not resolve there. AmbiguousMetricError and
            # MetricNotInDashboardError already requalify; this is the
            # remaining leak (#463).
            raise self._unknown(f"{repo}/{metric}") from exc

    def resolve(self, metric: str, dashboard: str | None = None) -> ResolvedMetric:
        if dashboard is not None:
            repo, _, rest = dashboard.partition("/")
            if repo not in self.layers or not rest:
                raise MetricNotFoundError(f"unknown dashboard '{dashboard}'")
            # Listings print metrics repo-prefixed, so the natural call pairs a
            # prefixed name with a prefixed dashboard. The inner layer knows
            # neither prefix; a name pointing at a different repo than the
            # dashboard does is a contradiction, not something to strip.
            owner, slash, bare = metric.partition("/")
            if slash:
                if owner != repo:
                    raise MetricNotFoundError(
                        f"'{metric}' is not in repo '{repo}', which is where "
                        f"dashboard '{dashboard}' lives"
                    )
                metric = bare
            return self._resolve_in(repo, metric, rest)
        if "/" in metric:
            repo, _, rest = metric.partition("/")
            if repo not in self.layers:
                known = ", ".join(sorted(self.layers))
                raise MetricNotFoundError(
                    f"no repo named '{repo}' in this workspace (repos: {known})"
                )
            return self._resolve_in(repo, rest)
        metrics, problems = self._enumerate()
        matches = [m for m in metrics if m.name.split("/", 1)[1] == metric]
        if problems and len(matches) < 2:
            raise SemanticError(
                f"cannot resolve '{metric}' across the workspace while a repo does not "
                f"load ({'; '.join(problems)}); name it as repo/{metric}"
            )
        if len(matches) == 1:
            # Re-resolve through the owning repo rather than returning the match:
            # all_metrics() is already first-wins per repo, so a name two of that
            # repo's dashboards define looks unambiguous from out here. Only the
            # inner resolve can tell.
            repo = matches[0].name.split("/", 1)[0]
            return self._resolve_in(repo, metric)
        if not matches:
            raise self._unknown(metric)
        options = ", ".join(sorted(m.name for m in matches))
        raise MetricNotFoundError(
            f"'{metric}' exists in more than one repo — use one of: {options}"
        )


def metric_summary(resolved: ResolvedMetric) -> dict:
    """The serialized shape of a metric shared by the HTTP API, CLI, and MCP."""
    d = resolved.definition
    return {
        "name": resolved.name,
        "title": d.title or resolved.name,
        "description": d.description,
        "expr": d.expr,
        "format": d.format,
        "synonyms": d.synonyms,
        "owners": d.owners,
        "dimensions": [
            {"name": dim.name, "description": dim.description, "synonyms": dim.synonyms}
            for dim in d.dimensions
        ],
        "time_dimension": (
            {"name": d.time_dimension.name, "grain": d.time_dimension.grain}
            if d.time_dimension
            else None
        ),
        "origin": resolved.origin,
        "dashboard": resolved.dashboard,
        "ambiguous_with": list(resolved.ambiguous_with),
        "source_type": source_label(resolved.source),
        **({"window": d.window} if d.window else {}),
        **({"cumulative": True} if d.cumulative else {}),
    }
