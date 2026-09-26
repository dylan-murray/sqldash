"""Snowflake Cortex Analyst interop: exports the layer as semantic-view YAML (for
SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML) and imports such YAML back into metrics.yaml."""

import getpass
import io
import re
import time
from typing import Any

from ruamel.yaml import YAML

from sqldash.models.semantics import SQL_RESERVED, MetricDef
from sqldash.params import (
    PLACEHOLDER,
    ParamError,
    extract_params,
    inactive_params,
    param_values,
    render_conditionals,
)
from sqldash.project.store import DashboardStore
from sqldash.semantics.compiler import bucket_input, expand_metric_sql, exported_sql
from sqldash.semantics.layer import ResolvedMetric, SemanticError, SemanticLayer
from sqldash.semantics.naming import import_name, source_column

_yaml = YAML(typ="rt")
_yaml.width = 4096
_yaml.indent(mapping=2, sequence=4, offset=2)
_yaml.default_flow_style = False
# A converter reuses one list for every metric that shares a dimension, and the
# round-trip dumper renders the second use as an alias (`synonyms: *id001`).
# The file still loads, but a generated file is meant to be read and edited by
# hand, and an anchor pointing into another metric is not what anyone wrote.
_yaml.representer.ignore_aliases = lambda *_: True


def render_yaml(doc: dict) -> str:
    buffer = io.StringIO()
    _yaml.dump(doc, buffer)
    return buffer.getvalue()


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def _sql_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _inline_params(sql: str, values: dict) -> str:
    return PLACEHOLDER.sub(lambda m: _sql_literal(values[m.group(1)]), sql)


def _base_table(resolved: ResolvedMetric, warnings: list[str]) -> dict | None:
    source = resolved.source
    table = resolved.relation.table or ""
    parts = table.split(".")
    database = parts[-3] if len(parts) >= 3 else getattr(source, "database", None)
    schema = parts[-2] if len(parts) >= 2 else getattr(source, "db_schema", None)
    if not database or not schema:
        warnings.append(
            f"skipped metric '{resolved.name}': cannot fully qualify '{table}' — "
            "set database/schema on the snowflake source or use a dotted table name"
        )
        return None
    return {"database": database, "schema": schema, "table": parts[-1]}


def _authored_metrics(layer: SemanticLayer, store: DashboardStore) -> dict:
    authored = {}
    for _, dashboard in store.iter_loaded():
        authored.update(dashboard.metrics)
    mf = layer.metrics_file()
    if mf is not None:
        authored.update(mf.metrics)
    return authored


def _skip_reason(resolved: ResolvedMetric) -> str | None:
    if resolved.ambiguous_with:
        # This document is executed by SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML
        # and then answered from, so emitting one of two conflicting
        # definitions under a name sqldash itself refuses would publish a
        # claim the project cannot back. Skip-with-warning is what this
        # exporter already does for anything it cannot represent faithfully.
        return (
            f"skipped metric '{resolved.name}' — defined inline by "
            f"{', '.join(resolved.ambiguous_with)}, so it has no single definition; "
            f"rename one or move it to metrics.yaml"
        )
    if resolved.source.type != "snowflake":
        return (
            f"skipped metric '{resolved.name}' ({resolved.source.type} source — "
            "Cortex semantic views are Snowflake-only)"
        )
    return None


def _table_entry(
    resolved: ResolvedMetric, metric: MetricDef, tables: dict[object, dict], warnings: list[str]
) -> tuple[object, dict] | None:
    if resolved.relation.table:
        base = _base_table(resolved, warnings)
        if base is None:
            return None
        # Grouped by the physical table, not by whatever name the metric
        # used to reach it. A derived metric has no `relation:` — expansion
        # clears it — so keying on the name put it in a second logical table
        # over the same base_table as its own peers, and the round trip then
        # produced two relations for one table.
        table_key = (base["database"], base["schema"], base["table"])
        entry = tables.setdefault(
            table_key,
            {
                "name": metric.relation or base["table"],
                "base_table": base,
                "dimensions": [],
                "time_dimensions": [],
                "metrics": [],
            },
        )
        # A relation name is the better label, and only some metrics carry
        # one — so take it from whichever does.
        if metric.relation:
            entry["name"] = metric.relation
        return table_key, entry
    # Keyed by the definition, for the same reason base tables are keyed
    # by physical identity: expansion clears `relation:` on a derived
    # metric, so keying on the metric's own name split a derived metric
    # over a sql relation from the peers it was derived from — two
    # tables with identical definitions, the split this fix is about.
    relation_sql = exported_sql("snowflake", resolved.relation.sql)
    table_key = ("sql", relation_sql)
    entry = tables.setdefault(
        table_key,
        {
            "name": f"{resolved.name}_base",
            "base_table": {"definition": relation_sql},
            "dimensions": [],
            "time_dimensions": [],
            "metrics": [],
        },
    )
    return table_key, entry


def _warn_round_trip_losses(resolved: ResolvedMetric, authored: dict, warnings: list[str]) -> None:
    src = authored.get(resolved.name) or authored.get(resolved.name.rsplit("/", 1)[-1])
    if src is None:
        return
    if src.cumulative:
        warnings.append(
            f"metric '{resolved.name}' is cumulative — Cortex has no running total, "
            "so a round-trip import will be the per-bucket sum"
        )
    if src.window:
        warnings.append(
            f"metric '{resolved.name}' has window: {src.window} — Cortex has no "
            "trailing window, so a round-trip import will be the per-bucket sum"
        )
    if src.derived is not None:
        warnings.append(
            f"metric '{resolved.name}' is derived — Cortex inlines the expr, "
            "so a round-trip import will not track its peers"
        )


def _export_dimensions(
    entry: dict, metric: MetricDef, lossy_merges: list[tuple[dict, str]]
) -> None:
    existing = {d["name"]: d["expr"] for d in entry["dimensions"]}
    for dim in metric.dimensions:
        if dim.name in existing:
            # The dimension half of the same merge loss the time dimensions
            # report: two relations over one table can spell one dimension
            # name over different columns, and only the first survives.
            if existing[dim.name] != dim.sql_expr:
                lossy_merges.append(
                    (
                        entry,
                        f"defines dimension '{dim.name}' over both "
                        f"{existing[dim.name]} and {dim.sql_expr} — only the "
                        f"first is exported",
                    )
                )
            continue
        item: dict[str, Any] = {"name": dim.name, "expr": dim.sql_expr}
        if dim.description:
            item["description"] = dim.description
        if dim.synonyms:
            item["synonyms"] = list(dim.synonyms)
        entry["dimensions"].append(item)


def _export_time_dimension(
    entry: dict, metric: MetricDef, lossy_merges: list[tuple[dict, str]]
) -> None:
    # Grouping by physical table merges relations that disagree here,
    # and `import cortex` keeps only a table's first time dimension per
    # metric — so the second one comes back changed. Reported on the way
    # out, where the definitions that disagree are still in hand.
    # Compared by expr as well as name: two relations can spell one name
    # over different columns, and that drop is the quieter of the two.
    existing_times = {t["name"]: (t["expr"], t.get("grain")) for t in entry["time_dimensions"]}
    # Cortex Analyst truncates the time dimension itself, so `timezone: session`
    # has to travel in the expr: DATE_TRUNC over a raw TIMESTAMP_TZ keeps each
    # row's offset and splits one month into a bucket per offset.
    td_name, td_expr = metric.time_dimension.name, bucket_input("snowflake", metric.time_dimension)
    td_grain = metric.time_dimension.grain
    td_description = metric.time_dimension.description
    if td_name in existing_times:
        shared = next(t for t in entry["time_dimensions"] if t["name"] == td_name)
        if td_description and not shared.get("description"):
            shared["description"] = td_description
        elif td_description and shared.get("description") != td_description:
            lossy_merges.append(
                (
                    entry,
                    f"has metrics with different descriptions for time dimension "
                    f"'{td_name}'; only the first is exported",
                )
            )
        prev_expr, prev_grain = existing_times[td_name]
        if (prev_expr, prev_grain) != (td_expr, td_grain):
            lossy_merges.append(
                (
                    entry,
                    f"defines time dimension '{td_name}' over both "
                    f"{prev_expr}/{prev_grain} and {td_expr}/{td_grain} — "
                    f"only the first is exported",
                )
            )
        return
    if existing_times:
        lossy_merges.append(
            (
                entry,
                f"has metrics with different time dimensions "
                f"({', '.join(sorted(existing_times))}, {td_name}) — the "
                f"table carries them all, but importing one back keeps "
                f"only the first",
            )
        )
    td_item = {"name": td_name, "expr": td_expr}
    if td_grain:
        td_item["grain"] = td_grain
    if td_description:
        td_item["description"] = td_description
    entry["time_dimensions"].append(td_item)


def _export_metric(
    resolved: ResolvedMetric, metric: MetricDef, warnings: list[str]
) -> dict[str, Any]:
    metric_item: dict[str, Any] = {"name": resolved.name, "expr": metric.expr}
    if metric.description:
        metric_item["description"] = metric.description
    if metric.synonyms:
        metric_item["synonyms"] = list(metric.synonyms)
    uncarried = [
        f"format: {metric.format}" if field == "format" else field
        for field in ("title", "format", "owners")
        if getattr(metric, field) and not (field == "format" and metric.format == "number")
    ]
    if uncarried:
        warnings.append(
            f"metric '{resolved.name}' has {_join(uncarried)}, which a Cortex semantic "
            f"view cannot carry; {'it was' if len(uncarried) == 1 else 'they were'} dropped"
        )
    return metric_item


_FOLDABLE_AGGREGATE = re.compile(
    r"\b(SUM|COUNT|AVG|MIN|MAX|MEDIAN|MODE|ANY_VALUE|KURTOSIS|SKEW"
    r"|STDDEV|STDDEV_POP|STDDEV_SAMP|VARIANCE|VARIANCE_POP|VARIANCE_SAMP|VAR_POP|VAR_SAMP)\s*\(",
    re.IGNORECASE,
)
_DISTINCT = re.compile(r"^\s*DISTINCT\b", re.IGNORECASE)


def _masked(text: str) -> str:
    """The text with every quoted literal's contents blanked, same length, so
    positions found in it index the original."""
    return _QUOTED.sub(lambda m: m.group(0)[0] + " " * (len(m.group(0)) - 2) + m.group(0)[-1], text)


def _closing_paren(masked: str, start: int) -> int | None:
    depth = 0
    for i in range(start, len(masked)):
        if masked[i] == "(":
            depth += 1
        elif masked[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return None


def _filtered_argument(argument: str, masked: str, condition: str) -> str | None:
    if _KNOWN_AGGREGATE.search(masked) or _AGGREGATE_FAMILY.search(masked):
        return None
    depth = 0
    for char in masked:
        depth += {"(": 1, ")": -1}.get(char, 0)
        if char == "," and depth == 0:
            return None
    if argument.strip() == "*":
        return f"CASE WHEN {condition} THEN 1 END"
    distinct = _DISTINCT.match(masked)
    if distinct:
        rest = argument[distinct.end() :].strip()
        return f"DISTINCT CASE WHEN {condition} THEN {rest} END"
    return f"CASE WHEN {condition} THEN {argument.strip()} END"


def _fold_filters(name: str, metric: MetricDef, warnings: list[str]) -> MetricDef | None:
    """The metric with its filters moved inside every aggregate it calls.

    A semantic view has no per-metric filter. Its table `filters` are named
    conditions Cortex Analyst may choose to apply to a question; a query over the
    view never applies them to a metric (checked live: a metric exported with
    `region = 'us'` as a table filter returned the unfiltered total). Guarding
    each aggregate's input with the condition is the same number, because every
    aggregate folded here ignores NULLs. Anything else, a window, a multi-argument
    aggregate, one this cannot see into, is skipped rather than published
    unfiltered.
    """
    expr = metric.expr or ""
    masked = _masked(expr)
    condition = " AND ".join(f"({snippet})" for snippet in metric.filters)
    pieces: list[str] = []
    last = 0
    for call in _FOLDABLE_AGGREGATE.finditer(masked):
        close = _closing_paren(masked, call.end() - 1)
        guarded = (
            None
            if close is None
            else _filtered_argument(expr[call.end() : close], masked[call.end() : close], condition)
        )
        if guarded is None:
            break
        pieces.append(f"{expr[last : call.end()]}{guarded}")
        last = close
    else:
        rest = _KNOWN_AGGREGATE.search(masked[last:]) or _AGGREGATE_FAMILY.search(masked[last:])
        if pieces and rest is None:
            return metric.model_copy(update={"expr": "".join(pieces) + expr[last:], "filters": []})
    warnings.append(
        f"skipped metric '{name}': a semantic view cannot filter one metric, and its "
        f"filters could not be folded into '{expr}' (only single-argument aggregates like "
        f"SUM, COUNT or AVG can be); write the condition into the expr as "
        f"SUM(CASE WHEN ... THEN ... END)"
    )
    return None


def _verified_queries(store: DashboardStore, warnings: list[str]) -> list[dict]:
    verified: list[dict] = []
    for dash_name, dashboard in store.iter_loaded():
        if dashboard.source.type != "snowflake":
            continue
        titles = {w.query: w.title for w in dashboard.tiles if w.query and w.title}
        for query_name, sql in dashboard.queries.items():
            try:
                values, _ = param_values(dashboard, extract_params(sql), {})
                sql = render_conditionals(sql, values, inactive_params(dashboard, values))
                values, missing = param_values(dashboard, extract_params(sql), {})
            except ParamError as exc:
                raise ParamError(f"verified query '{dash_name}.{query_name}': {exc}") from exc
            if missing:
                warnings.append(
                    f"skipped verified query '{dash_name}.{query_name}' — "
                    f"no default for param(s): {', '.join(missing)}"
                )
                continue
            verified.append(
                {
                    "name": f"{dash_name}_{query_name}",
                    "question": titles.get(query_name) or f"{dashboard.title}: {query_name}",
                    "sql": _inline_params(sql, values).strip(),
                    "verified_at": int(time.time()),
                    "verified_by": getpass.getuser(),
                    "use_as_onboarding_question": False,
                }
            )
    return verified


def _qualify_shared_labels(tables: dict[object, dict], warnings: list[str]) -> set[str]:
    # Two groups can want the same label: a bare table name is not unique across
    # schemas, and only some metrics carry a relation name to use instead. The
    # importer keys relations by name, so a duplicate silently rebound one
    # group's metrics to the other group's physical table — a different
    # warehouse table, in valid-looking YAML.
    claimed: dict[str, list[dict]] = {}
    for entry in tables.values():
        claimed.setdefault(entry["name"], []).append(entry)
    taken = set(claimed)
    for label, entries in claimed.items():
        if len(entries) == 1:
            continue
        warnings.append(
            f"'{label}' names more than one physical table — qualifying each so the "
            f"view does not bind one table's metrics to another"
        )
        # One qualification level for the whole group, and the level has to
        # separate every member of it. Qualifying each entry independently at
        # the first level that cleared the *original* names left two tables in
        # different databases both called SCHEMA_TABLE — the very collision this
        # pass exists to remove, reintroduced by the pass itself.
        for parts in (("schema", "table"), ("database", "schema", "table")):
            candidates = [
                "_".join(str(base[p]) for p in parts if base.get(p))
                for base in (entry.get("base_table") or {} for entry in entries)
            ]
            if not all(candidates) or len(set(candidates)) != len(candidates):
                continue
            if any(c in taken - {label} for c in candidates):
                continue
            for entry, candidate in zip(entries, candidates, strict=True):
                entry["name"] = candidate
                taken.add(candidate)
            # Only free the group's label if nothing kept it. The first entry
            # often qualifies to the label it already had, and discarding it
            # then let a later group take a name still in use — this pass
            # handing out the collision it exists to remove, for the second
            # time.
            if label not in candidates:
                taken.discard(label)
            break
        else:
            # Nothing to qualify with: a sql relation's base_table is a
            # `definition`, not a database/schema/table, so no level separates a
            # group containing one. Unique labels are the invariant the importer
            # depends on, and the warning above promises them — leaving the
            # duplicate in place bound a real warehouse table's metrics to a
            # SELECT.
            for entry in entries[1:]:
                suffix = 2
                while f"{entry['name']}_{suffix}" in taken:
                    suffix += 1
                entry["name"] = f"{entry['name']}_{suffix}"
                taken.add(entry["name"])
    return taken


def _untimed_merges(
    tables: dict[object, dict],
    untimed_metrics: dict[object, list[str]],
    lossy_merges: list[tuple[dict, str]],
) -> None:
    # `import cortex` gives every metric on a table that table's first time
    # dimension, so a metric authored without one comes back able to be queried
    # by time against a column its author never associated with it — a time
    # range that used to raise "has no time_dimension" now silently filters.
    # Grouping by physical table is what puts an untimed metric on a timed
    # table; before it, they were separate tables and the round trip was
    # lossless.
    for table_key, names in untimed_metrics.items():
        entry = tables[table_key]
        if not entry["time_dimensions"]:
            continue
        lossy_merges.append(
            (
                entry,
                f"has metrics with no time dimension ({', '.join(sorted(names))}) beside one "
                f"that has {entry['time_dimensions'][0]['name']} — importing the view back "
                f"gives them that time dimension too",
            )
        )


def _ensure_unique_labels(tables: dict[object, dict], taken: set[str]) -> None:
    # Unique labels are what the importer binds on, and two bookkeeping slips in
    # this pass have already shipped duplicates that read as valid YAML. The
    # invariant is cheap to enforce outright, so it does not depend on the
    # bookkeeping above being right.
    seen: set[str] = set()
    for entry in tables.values():
        if entry["name"] in seen:
            suffix = 2
            while f"{entry['name']}_{suffix}" in seen or f"{entry['name']}_{suffix}" in taken:
                suffix += 1
            entry["name"] = f"{entry['name']}_{suffix}"
        seen.add(entry["name"])


def build_semantic_view(
    layer: SemanticLayer,
    store: DashboardStore,
    name: str,
    description: str | None = None,
    *,
    metrics: list[ResolvedMetric] | None = None,
    include_verified_queries: bool = True,
) -> tuple[dict, list[str]]:
    """Assemble a semantic-view document from snowflake-source metrics, grouping by
    relation and emitting dashboard queries as verified_queries with parameter
    defaults inlined as SQL literals; non-snowflake metrics are skipped with warnings."""
    warnings: list[str] = []
    # Keyed by physical identity: a (database, schema, table) triple for a base
    # table, ("sql", definition) for a sql relation. The two key spaces never
    # collide, which is why one dict holds both.
    tables: dict[object, dict] = {}
    lossy_merges: list[tuple[dict, str]] = []
    untimed_metrics: dict[object, list[str]] = {}
    authored = _authored_metrics(layer, store)

    for resolved in layer.all_metrics() if metrics is None else metrics:
        skipped = _skip_reason(resolved)
        if skipped:
            warnings.append(skipped)
            continue
        # Everything below is written into a semantic view Snowflake executes,
        # so every author SQL body goes out through the export seam. Only
        # Snowflake sources reach here (the check above), so the dialect is not
        # a guess.
        metric = expand_metric_sql(resolved.definition, "snowflake")
        if metric.filters:
            metric = _fold_filters(resolved.name, metric, warnings)
            if metric is None:
                continue
        keyed = _table_entry(resolved, metric, tables, warnings)
        if keyed is None:
            continue
        table_key, entry = keyed
        _warn_round_trip_losses(resolved, authored, warnings)
        _export_dimensions(entry, metric, lossy_merges)
        if metric.time_dimension is None:
            untimed_metrics.setdefault(table_key, []).append(resolved.name)
        else:
            _export_time_dimension(entry, metric, lossy_merges)
        entry["metrics"].append(_export_metric(resolved, metric, warnings))

    verified = _verified_queries(store, warnings) if include_verified_queries else []
    taken = _qualify_shared_labels(tables, warnings)
    _untimed_merges(tables, untimed_metrics, lossy_merges)
    _ensure_unique_labels(tables, taken)

    # Named after qualification: a warning that quotes the pre-rename label
    # points at a table that does not appear in the output.
    for entry, detail in lossy_merges:
        warnings.append(f"'{entry['name']}' {detail}")

    if not any(t["metrics"] for t in tables.values()):
        # The warnings are the whole explanation when everything was skipped —
        # saying "no snowflake-source metrics" to someone whose metrics are all
        # snowflake, but ambiguous, sends them to fix the wrong thing.
        detail = "".join(f"\n  - {w}" for w in warnings)
        raise SemanticError(
            "no exportable metrics — Cortex semantic views require metrics whose "
            f"source is snowflake and whose name resolves to one definition{detail}"
        )

    for entry in tables.values():
        for key in ("dimensions", "time_dimensions"):
            if not entry[key]:
                del entry[key]

    doc: dict[str, Any] = {"name": name}
    if description:
        doc["description"] = description
    doc["tables"] = list(tables.values())
    doc["relationships"] = []
    if verified:
        doc["verified_queries"] = verified
    return doc, warnings


# Aggregates we can name. Matching one is certain, so nothing is said about it.
# Omitting a real aggregate here is not silent — it falls through to the wrap
# path, which warns — but SUM(CORR(a, b)) is still a query no engine accepts, so
# the list is worth keeping current.
_KNOWN_AGGREGATE = re.compile(
    r"\b("
    r"SUM|COUNT|AVG|MIN|MAX|MEDIAN|MODE|ANY_VALUE|LISTAGG|GROUPING|GROUPING_ID"
    r"|STDDEV|STDDEV_POP|STDDEV_SAMP|VARIANCE|VARIANCE_POP|VARIANCE_SAMP|VAR_POP|VAR_SAMP"
    r"|CORR|COVAR_POP|COVAR_SAMP|KURTOSIS|SKEW|REGR_\w+"
    r"|PERCENTILE_CONT|PERCENTILE_DISC|MIN_BY|MAX_BY"
    r")\s*\(|\bOVER\s*\(",
    re.IGNORECASE,
)

# Shapes Snowflake spells with a suffix or prefix. These catch the aggregates we
# have not enumerated (COUNT_IF, BOOLAND_AGG, APPROX_PERCENTILE) — but a scalar
# UDF can wear the same shape, and treating one as already-aggregated emits a
# metric with no aggregate at all. Matching here is a guess, so it is reported.
_AGGREGATE_FAMILY = re.compile(
    r"\b(\w+_AGG|\w+_IF|APPROX_\w+|HLL\w*)\s*\(",
    re.IGNORECASE,
)

# A bare column reference, optionally qualified. Wrapping one of these in the
# declared aggregate is the case we are actually confident about.
_SIMPLE_COLUMN = re.compile(r"^[A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)*$")

# Any call at all. An expr that calls something we did not classify as an
# aggregate still gets wrapped — SUM(COALESCE(x, 0)) is right — but the caller
# is told, because that is the guess most likely to be wrong.
_FUNCTION_CALL = re.compile(r"[A-Za-z_]\w*\s*\(")

# A key, quoted or not. Recognizing only the bare form meant `"sql": |` was not
# seen as a block header and its SQL got rewritten as if it were YAML.
_KEY = r"(?:[\w.-]+|\"[^\"]*\"|'[^']*')"

# A `key: |` / `key: >` header. Everything indented under it is literal content,
# not YAML, and rewriting inside it corrupts whatever it holds — usually SQL.
_BLOCK_SCALAR = re.compile(rf"^(?P<indent>\s*)(?:-\s+)?{_KEY}\s*:\s*[|>][-+]?\d*\s*$")

_PLACEHOLDER_LINE = re.compile(rf"^(?P<indent>\s*(?:-\s+)?{_KEY}\s*:\s+)(?P<rest>\S.*?)\s*$")
_PLACEHOLDER = re.compile(r"\{\{[^{}]*?\}\}")


def _split_comment(value: str) -> tuple[str, str]:
    """Separate a YAML inline comment from the value it follows.

    Cutting the value at the first `#` treats one inside a quoted string as a
    comment, which drops the rest of a flow mapping and leaves its placeholder
    unquoted — the exact parse failure this whole function removes. A comment
    starts at a `#` that is preceded by whitespace and sits outside quotes.
    """
    spans = [m.span() for m in _QUOTED.finditer(value)]
    for i, char in enumerate(value):
        if char != "#" or i == 0 or not value[i - 1].isspace():
            continue
        if any(start <= i < end for start, end in spans):
            continue
        return value[:i].rstrip(), value[i:]
    return value, ""


def _quote_placeholders(text: str) -> tuple[str, int]:
    """Quote unquoted `{{PLACEHOLDER}}` values so the document parses.

    Snowflake's own published views are deployment templates: `database:
    {{DATABASE}}` is not a mapping, but YAML reads `{{...}}` as a flow mapping
    whose key is itself a mapping, and fails with "found unhashable key" —
    which says nothing about the real problem. Quoting keeps the placeholder as
    a string so the rest of the view imports and the user can substitute it.

    What breaks is an unquoted placeholder in a value position: on its own, or
    inside a flow mapping or sequence (`base_table: {database: {{X}}, ...}`,
    `tags: [{{X}}, prod]`). Everything else is left exactly as written — a
    quoted scalar already parses, and injecting quotes inside it corrupts the
    value, which is what a broader rule did to the `sql:` of every verified
    query in the real file.
    """
    count = 0
    block_indent: int | None = None
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        stripped = line.rstrip("\r\n")
        # Anchoring to a `key:` is not enough on its own: a block scalar's
        # content can itself look like `config: {{ env }}`. Track the block and
        # skip everything indented under it.
        if block_indent is not None:
            if not stripped.strip() or len(stripped) - len(stripped.lstrip()) > block_indent:
                continue
            block_indent = None
        block = _BLOCK_SCALAR.match(stripped)
        if block:
            block_indent = len(block["indent"])
            continue
        match = _PLACEHOLDER_LINE.match(stripped)
        if not match:
            continue
        value, comment = _split_comment(match["rest"])
        if value.startswith(('"', "'")) or "{{" not in value:
            continue
        if value.startswith("{{"):
            quoted, n = f'"{value}"', 1
        elif value.startswith(("{", "[")):
            # A flow sequence (`tags: [{{X}}, prod]`) fails to parse for exactly
            # the same reason a flow mapping does. Outside quotes only: either
            # may already quote its own placeholder, and wrapping that again
            # yields `""{{X}}""`.
            quoted, n = _outside_quotes(
                value, lambda segment: _PLACEHOLDER.subn(lambda m: f'"{m.group(0)}"', segment)
            )
        else:
            continue  # a plain scalar merely containing `{{` parses as-is
        if not n:
            continue
        newline = line[len(stripped) :]
        separator = "  " if comment else ""
        lines[i] = f"{match['indent']}{quoted}{separator}{comment}{newline}"
        count += n
    return "".join(lines), count


_AGGREGATIONS = {
    "sum": "SUM(X)",
    "avg": "AVG(X)",
    "average": "AVG(X)",
    "min": "MIN(X)",
    "max": "MAX(X)",
    "count": "COUNT(X)",
    "count_distinct": "COUNT(DISTINCT X)",
    "median": "MEDIAN(X)",
}


# A single- or double-quoted literal, doubled-quote escapes included. Nothing
# inside one is an identifier, so no rewrite here may reach into it: quoting a
# placeholder inside an already-quoted scalar produces `""{{X}}""`, and swapping
# a fact name inside a string literal silently changes what a metric compares
# against.
_QUOTED = re.compile(r"\'(?:[^\']|\'\')*\'|\"(?:[^\"]|\"\")*\"")


def _unquoted(text: str) -> str:
    """The text with quoted literals blanked, for classification only.

    `CASE WHEN f('SUM(x)') THEN 1 ELSE 0 END` contains no aggregate — but the
    literal does, and reading it made the importer treat the whole expression as
    already aggregated and emit a metric with no aggregate at all."""
    return _QUOTED.sub("''", text)


def _outside_quotes(text: str, rewrite) -> tuple[str, int]:
    """Apply `rewrite` to the parts of `text` that sit outside quoted literals."""
    parts: list[str] = []
    count = 0
    last = 0
    for match in _QUOTED.finditer(text):
        piece, n = rewrite(text[last : match.start()])
        parts.append(piece)
        count += n
        parts.append(match.group(0))
        last = match.end()
    piece, n = rewrite(text[last:])
    parts.append(piece)
    return "".join(parts), count + n


def _inline_aliases(expr: str, aliases: dict[str, str], table: str | None = None) -> str:
    """Replace fact/measure names in a metric expr with what they are defined as.

    A semantic view's facts are aliases scoped to the view, not columns of the
    base table, so a metric written over them only binds once they are inlined.
    Names followed by `(` are left alone — a fact called `count` must not rewrite
    the COUNT function.

    Snowflake requires a semantic view's metrics to spell a fact `<table>.<fact>`,
    and reads that reference case-insensitively, so `SUM(orders.amount_usd)` over
    a fact it reads back as `AMOUNT_USD` is the ordinary shape. The `table.`
    prefix is consumed with the name: left in place, `orders.(amount * 100)` does
    not parse. A bare name stays case-sensitive, since `AUM` beside a fact `aum`
    may well be the base column, and a name qualified by any other table is not
    this table's fact.
    """
    if not aliases:
        return expr
    folded = {name.lower(): value for name, value in aliases.items()}
    alternation = "|".join(re.escape(n) for n in sorted(aliases, key=len, reverse=True))
    qualified = rf"(?i:\b{re.escape(table)}\s*\.\s*(?P<qualified>{alternation}))|" if table else ""
    pattern = re.compile(rf"(?<![.\w])(?:{qualified}(?P<bare>{alternation}))\b(?!\s*\()")

    def swap(match: "re.Match[str]") -> str:
        if match["bare"] is not None:
            value = aliases[match["bare"]]
        else:
            value = folded[match["qualified"].lower()]
        return value if _SIMPLE_COLUMN.match(value) else f"({value})"

    return _outside_quotes(expr, lambda segment: pattern.subn(swap, segment))[0]


def _resolve_alias_chain(
    aliases: dict[str, str], tname: str, warnings: list[str]
) -> dict[str, str]:
    """Expand aliases defined over other aliases, so one pass over a metric expr
    is enough.

    A fact may be written over another fact. Substituting once leaves the inner
    name dangling against the base table — an import that lints clean and fails
    at query time, which is the whole failure this inlining exists to prevent.
    """
    resolved = dict(aliases)
    for _ in range(len(resolved) + 1):
        changed = False
        for name, expr in list(resolved.items()):
            others = {k: v for k, v in resolved.items() if k != name}
            expanded = _inline_aliases(expr, others, tname)
            if expanded != expr:
                resolved[name] = expanded
                changed = True
        if not changed:
            return resolved
    warnings.append(
        f"table '{tname}': facts reference each other in a cycle — left as written, "
        f"so the metrics over them will not bind"
    )
    return resolved


_UNQUOTED_UPPER = re.compile(r"^[A-Z_][A-Z0-9_$]*$")


def _fold_case(name: str) -> str:
    """The name as the author most likely wrote it.

    Snowflake stores an unquoted identifier uppercased, and
    SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW hands names back the way they are
    stored, so a view created from `revenue` reads back as `REVENUE`. The
    identifier is case-insensitive in Snowflake, but a metrics.yaml key is not:
    every dashboard reference to `revenue` failed to resolve. Only a name that
    could have been written unquoted folds; mixed case can only have come from a
    quoted identifier and means exactly what it says.
    """
    if _UNQUOTED_UPPER.match(name) and any(c.isalpha() for c in name):
        return name.lower()
    return name


def _column_expr(expr: str | None, original: str, name: str) -> str | None:
    """The `expr` a dimension needs, or None when its name already reads the
    column. A folded name still reads the column it came from, since only
    unquoted identifiers fold."""
    expr = expr or source_column(original)
    if expr == name or (expr == original and _fold_case(original) == name):
        return None
    return expr


def _mapping_entries(items: Any, where: str) -> list[dict]:
    """Raise SemanticError instead of AttributeError when a list of mappings
    contains a bare string — the shape a generator emits for `tables: [ORDERS]`
    or `dimensions: [REGION]` (#336)."""
    if not items:
        return []
    if not isinstance(items, list):
        raise SemanticError(f"semantic-view YAML: '{where}' must be a list")
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            raise SemanticError(f"semantic-view YAML: each entry in '{where}' must be a mapping")
        out.append(item)
    return out


def _metric_key(
    name: str, tname: str, metrics: dict, owners: dict[str, str], warnings: list[str]
) -> str:
    """The metrics.yaml key for a table's metric: its own name, or qualified by
    the table when another table already took it.

    Facts and metrics are namespaced per table in a semantic view, so two tables
    exposing `AMOUNT` is ordinary input. Keying on the bare name let the second
    silently replace the first, and the survivor aggregated the wrong table.
    The first keeps the bare name and later ones are qualified `<table>_<name>`,
    the naming `import lookml` uses. lookml has no fallback beyond that and
    overwrites when the qualified name is itself taken, so this is stricter,
    not identical.
    """
    if name not in metrics:
        owners[name] = tname
        return name
    qualified = f"{tname}_{name}"
    key = qualified
    suffix = 2
    while key in metrics:
        key = f"{qualified}_{suffix}"
        suffix += 1
    taken = (
        f", and '{qualified}' by table '{owners.get(qualified, qualified)}'"
        if key != qualified
        else ""
    )
    warnings.append(
        f"table '{tname}': metric '{name}' is already defined by table '{owners[name]}'"
        f"{taken}, so it was imported as '{key}'"
    )
    owners[key] = tname
    return key


def _load_view(text: str, warnings: list[str]) -> tuple[dict, list[dict]]:
    text, placeholders = _quote_placeholders(text)
    if placeholders:
        warnings.append(
            f"{placeholders} deployment placeholder(s) like {{{{DATABASE}}}} kept verbatim "
            f"— substitute them before pointing sqldash at the warehouse"
        )
    try:
        doc = YAML(typ="safe").load(io.StringIO(text))
    except Exception as exc:
        raise SemanticError(f"invalid semantic-view YAML: {exc}") from exc
    if not isinstance(doc, dict) or not doc.get("tables"):
        raise SemanticError("semantic-view YAML must be a mapping with a 'tables' list")
    return doc, _mapping_entries(doc["tables"], "tables")


def _placeholder_source(tables: list[dict]) -> dict[str, Any]:
    first_base = next(
        (t.get("base_table", {}) for t in tables if isinstance(t.get("base_table"), dict)),
        {},
    )
    return {
        "type": "snowflake",
        "account": "<your-account>",
        "warehouse": "<your-warehouse>",
        "database": first_base.get("database", "<database>"),
        "schema": first_base.get("schema", "<schema>"),
        "authentication": "externalbrowser",
        "username": "${env:SNOWFLAKE_USER}",
    }


def _warn_unsupported_sections(doc: dict, warnings: list[str]) -> None:
    if doc.get("relationships"):
        names = [
            str(r.get("name") or "(unnamed)")
            for r in _mapping_entries(doc["relationships"], "relationships")
        ]
        warnings.append(
            f"relationships {', '.join(names)} skipped: a sqldash metric reads one relation, "
            f"so nothing can be grouped by another table's dimensions"
        )
    if doc.get("verified_queries"):
        warnings.append(
            f"{len(doc['verified_queries'])} verified_queries skipped — "
            f"copy any you want into a dashboard's queries"
        )


def _warn_unmapped_fields(table: dict, tname: str, warnings: list[str]) -> None:
    """Name what a sqldash relation and dimension have no field for, rather than
    dropping it silently: table synonyms, dimension sample_values and time
    dimension synonyms. Cortex Analyst reads them; sqldash has nowhere to put
    them."""
    if table.get("synonyms"):
        warnings.append(
            f"table '{tname}': synonyms ({', '.join(map(str, table['synonyms']))}) dropped; "
            f"a sqldash relation has no synonyms"
        )
    sampled = [
        str(d["name"])
        for d in _mapping_entries(table.get("dimensions"), "dimensions")
        if d.get("sample_values") and d.get("name")
    ]
    if sampled:
        warnings.append(
            f"table '{tname}': sample_values on {', '.join(sampled)} dropped; "
            f"a sqldash dimension has no sample values"
        )
    timed = [
        str(t["name"])
        for t in _mapping_entries(table.get("time_dimensions"), "time_dimensions")
        if t.get("synonyms") and t.get("name")
    ]
    if timed:
        warnings.append(
            f"table '{tname}': synonyms on time dimension {', '.join(timed)} dropped; "
            f"a sqldash time dimension has no synonyms"
        )


def _import_relation(table: dict, relations: dict, warnings: list[str]) -> str | None:
    tname = table.get("name")
    base = table.get("base_table") or {}
    if not tname:
        warnings.append("skipped a table with no name")
        return None
    if not isinstance(base, dict):
        # Placeholder quoting turns a `{{...}}` flow mapping into a string,
        # so a malformed base_table would reach .get() and raise an
        # AttributeError traceback instead of the CLI's clean error path.
        warnings.append(f"skipped table '{tname}': base_table is not a mapping")
        return None
    rname = import_name(
        _fold_case(str(tname)),
        "table",
        warnings,
        kind="relation",
        allow_reserved=True,
        taken=relations,
    )
    if base.get("definition"):
        relations[rname] = {"sql": base["definition"]}
    elif base.get("table"):
        qualified = ".".join(
            p for p in (base.get("database"), base.get("schema"), base.get("table")) if p
        )
        relations[rname] = {"table": qualified}
    else:
        warnings.append(f"skipped table '{tname}': no base_table")
        return None
    return rname


def _import_dimensions(table: dict, tname: str, warnings: list[str]) -> list[dict]:
    dims = []
    for d in _mapping_entries(table.get("dimensions"), "dimensions"):
        if not d.get("name"):
            continue
        dname = import_name(
            _fold_case(str(d["name"])),
            f"table '{tname}': dimension",
            warnings,
            taken=[existing["name"] for existing in dims],
        )
        item = {"name": dname}
        expr = _column_expr(d.get("expr"), str(d["name"]), dname)
        if expr:
            item["expr"] = expr
        if d.get("description"):
            item["description"] = d["description"]
        if d.get("synonyms"):
            item["synonyms"] = list(d["synonyms"])
        dims.append(item)
    return dims


_SESSION_CAST = re.compile(r"(?is)^\s*CAST\s*\((?P<inner>.+)\s+AS\s+TIMESTAMP_LTZ\s*\)\s*$")


def _session_cast(expr: Any) -> tuple[Any, bool]:
    """The column under an export's `CAST(.. AS TIMESTAMP_LTZ)`, and whether it was
    there, so `timezone: session` round trips instead of coming back as a cast the
    compiler would wrap in DATE_TRUNC with no idea why it is there."""
    match = _SESSION_CAST.match(expr) if isinstance(expr, str) else None
    if match is None:
        return expr, False
    masked = _masked(expr)
    opening = masked.index("(")
    if _closing_paren(masked, opening) != len(masked.rstrip()) - 1:
        return expr, False
    return match["inner"].strip(), True


def _import_time_dimension(table: dict, tname: str, warnings: list[str]) -> dict | None:
    time_dims = _mapping_entries(table.get("time_dimensions"), "time_dimensions")
    if time_dims and not time_dims[0].get("name"):
        warnings.append(f"table '{tname}': skipped a time dimension with no name")
        time_dims = time_dims[1:]
    if not time_dims:
        return None
    td = time_dims[0]
    tdname = import_name(_fold_case(str(td["name"])), f"table '{tname}': time dimension", warnings)
    time_dimension: dict[str, Any] = {"name": tdname, "grain": td.get("grain") or "day"}
    raw_expr, session = _session_cast(td.get("expr"))
    expr = _column_expr(raw_expr, str(td["name"]), tdname)
    if expr:
        time_dimension["expr"] = expr
    if session:
        time_dimension["timezone"] = "session"
    if td.get("description"):
        time_dimension["description"] = td["description"]
    if len(time_dims) > 1:
        warnings.append(
            f"table '{tname}': only the first time dimension "
            f"('{td.get('name')}') was kept per metric"
        )
    return time_dimension


def _warn_named_filters(table: dict, tname: str, warnings: list[str]) -> None:
    """A table's `filters` are named conditions Cortex Analyst may choose for a
    question; a query over the view never applies them to a metric (checked
    live: ORDER_COUNT over a table with `amount > 150` is every row). Importing
    them as metric filters changed every number on the table, and sqldash has no
    optional filter to hold them, so they are named and left out."""
    for f in _mapping_entries(table.get("filters"), "filters"):
        if not f.get("expr"):
            continue
        label = f"'{f['name']}' ({f['expr']})" if f.get("name") else f"({f['expr']})"
        warnings.append(
            f"table '{tname}': named filter {label} skipped; Snowflake only offers it to "
            f"Cortex Analyst and never applies it to a metric, so importing it would change "
            f"every metric's numbers. Add it to a metric's filters to always apply it"
        )


def _fact_aliases(
    table: dict, tname: str, declares_metrics: bool, warnings: list[str]
) -> dict[str, str]:
    # Facts and measures are names scoped to the view, and anything written
    # over them refers to them by name — a metric expr, or another fact.
    # They are not columns of the base table, so whatever references them has
    # to be inlined or it cannot bind. This is built for every table, not
    # only ones declaring metrics: a facts-only table turns those same names
    # into metrics, and a fact defined over another fact is dangling there
    # too. An identity fact (`amount: amount`) is kept: its bare name already
    # reads the column, but `<table>.amount` names the logical table, which is
    # not a relation alias once imported.
    aliases: dict[str, str] = {}
    for column in list(table.get("facts", [])) + list(table.get("measures", [])):
        if not isinstance(column, dict):
            if declares_metrics:
                # The raw-column loop below does not run for such a table,
                # so this is the only place it would be heard.
                warnings.append(f"table '{tname}': skipped a fact that is not a mapping")
            continue
        cname, cexpr = column.get("name"), column.get("expr")
        if cname and cexpr:
            aliases[cname] = cexpr
    return _resolve_alias_chain(aliases, tname, warnings)


def _measure_expr(
    column: dict, cname: str, tname: str, aliases: dict[str, str], warnings: list[str]
) -> str:
    # Excluding its own name: a fact is not defined in terms of itself.
    source_expr = _inline_aliases(
        str(column.get("expr") or cname),
        {k: v for k, v in aliases.items() if k != cname},
        tname,
    )
    classifiable = _unquoted(source_expr)
    known = _KNOWN_AGGREGATE.search(classifiable)
    family = _AGGREGATE_FAMILY.search(classifiable)
    if not known and family:
        warnings.append(
            f"table '{tname}': measure '{cname}' calls "
            f"'{family.group(1)}', which is shaped like an aggregate, so it was "
            f"left unwrapped — check it aggregates, or the metric returns a row "
            f"value instead of a number"
        )
    elif (
        not known and not _SIMPLE_COLUMN.match(classifiable) and _FUNCTION_CALL.search(classifiable)
    ):
        warnings.append(
            f"table '{tname}': measure '{cname}' is an expression calling a "
            f"function this importer does not recognize as an aggregate, so it "
            f"was wrapped — check '{source_expr}' is not already aggregated"
        )
    if known or family:
        # Measures are usually raw columns, but not always: real views
        # carry `expr: COUNT(DISTINCT PM_ID)` under `measures`. Wrapping
        # that gives SUM(COUNT(...)), which every target engine rejects
        # as a nested aggregate — an import that lints clean and dies at
        # query time.
        if column.get("default_aggregation"):
            warnings.append(
                f"table '{tname}': measure '{cname}' already aggregates, so its "
                f"default_aggregation '{column['default_aggregation']}' was not applied"
            )
        return source_expr
    agg = str(column.get("default_aggregation") or "sum").strip().lower()
    template = _AGGREGATIONS.get(agg)
    if template is None:
        warnings.append(
            f"table '{tname}': measure '{cname}' has unsupported "
            f"default_aggregation '{agg}' — imported as SUM, review it"
        )
        template = "SUM(X)"
    # Not str.format: a measure expr may legitimately contain braces.
    return template.replace("X", source_expr, 1)


def _metric_entry(
    rname: str,
    expr: str,
    spec: dict,
    time_dimension: dict | None,
    dims: list[dict],
) -> dict[str, Any]:
    entry: dict[str, Any] = {"relation": rname, "expr": expr}
    if spec.get("description"):
        entry["description"] = spec["description"]
    if spec.get("synonyms"):
        entry["synonyms"] = list(spec["synonyms"])
    if time_dimension:
        entry["time_dimension"] = dict(time_dimension)
    if dims:
        entry["dimensions"] = [dict(d) for d in dims]
    return entry


def _add_metric(
    out: dict,
    owners: dict[str, str],
    name: str,
    tname: str,
    rname: str,
    entry: dict,
    warnings: list[str],
) -> str:
    key = _metric_key(
        import_name(_fold_case(str(name)), f"table '{tname}': metric", warnings, kind="metric"),
        rname,
        out["metrics"],
        owners,
        warnings,
    )
    out["metrics"][key] = entry
    return key


def _import_table(
    table: dict,
    out: dict,
    owners: dict[str, str],
    warnings: list[str],
    table_metrics: dict[tuple[str, str], str],
) -> None:
    if table.get("name") and not any(table.get(k) for k in ("metrics", "facts", "measures")):
        # Every sqldash metric carries its own dimensions, so a table with
        # nothing to aggregate has nowhere for its dimensions to go; in the view
        # they are reached through a relationship, which sqldash cannot follow.
        dims = [
            str(d.get("name"))
            for d in _mapping_entries(table.get("dimensions"), "dimensions")
            + _mapping_entries(table.get("time_dimensions"), "time_dimensions")
        ]
        warnings.append(
            f"table '{table['name']}' skipped: it has no metrics or facts, so its dimensions "
            f"({', '.join(dims) or 'none'}) are only reachable through a relationship, "
            f"which a sqldash metric cannot follow"
        )
        return
    rname = _import_relation(table, out["relations"], warnings)
    if rname is None:
        return
    tname = table["name"]
    _warn_unmapped_fields(table, tname, warnings)
    dims = _import_dimensions(table, tname, warnings)
    time_dimension = _import_time_dimension(table, tname, warnings)
    if time_dimension:
        dims = [d for d in dims if d["name"] != time_dimension["name"]]
    _warn_named_filters(table, tname, warnings)

    # A view carries aggregates under `metrics`, but the two shapes Snowflake
    # actually emits carry raw numeric columns instead: `measures` in a Cortex
    # Analyst semantic model, `facts` in a semantic view. Reading only
    # `metrics` meant every real file imported as "no importable metrics".
    # Facts are the inputs to a table's metrics, so they only become metrics
    # themselves when the table declares none.
    raw_columns = []
    declares_metrics = bool(table.get("metrics"))
    if declares_metrics:
        for key in ("facts", "measures"):
            if table.get(key):
                warnings.append(
                    f"table '{tname}': {key} are inputs to its metrics, so they were "
                    f"not imported as metrics of their own"
                )
    else:
        raw_columns = list(table.get("measures", [])) + list(table.get("facts", []))
    aliases = _fact_aliases(table, tname, declares_metrics, warnings)

    for column in raw_columns:
        if not isinstance(column, dict):
            # A shorthand entry like `facts: [AMOUNT]` is a plausible thing
            # for a generator to emit, and .get() on it raises past the CLI's
            # SemanticError handling into a traceback.
            warnings.append(f"table '{tname}': skipped a measure that is not a mapping")
            continue
        cname = column.get("name")
        if not cname:
            warnings.append(f"skipped a measure on '{tname}' with no name")
            continue
        expr = _measure_expr(column, cname, tname, aliases, warnings)
        entry = _metric_entry(rname, expr, column, time_dimension, dims)
        _add_metric(out, owners, cname, tname, rname, entry, warnings)

    for metric in table.get("metrics", []):
        if not isinstance(metric, dict):
            warnings.append(f"table '{tname}': skipped a metric that is not a mapping")
            continue
        mname = metric.get("name")
        if not mname or not metric.get("expr"):
            warnings.append(f"skipped a metric on '{tname}' missing name or expr")
            continue
        expr = _inline_aliases(metric["expr"], aliases, tname)
        entry = _metric_entry(rname, expr, metric, time_dimension, dims)
        key = _add_metric(out, owners, mname, tname, rname, entry, warnings)
        table_metrics[(str(tname).lower(), str(mname).lower())] = key


_QUALIFIED_REF = re.compile(r"(?<![.\w])([A-Za-z_][\w$]*)\s*\.\s*([A-Za-z_][\w$]*)\b(?!\s*[.(])")
_BARE_NAME = re.compile(r"(?<![.\w])[A-Za-z_]\w*\b(?!\s*\()")


def _derived_from(
    expr: str, table_metrics: dict[tuple[str, str], str], metrics: dict
) -> tuple[str, list[str]] | str:
    """The view-level metric as a sqldash `derived:` string and the metrics it
    reads, or the reason it cannot be one."""
    masked = _unquoted(expr)
    keys = []
    for table, name in _QUALIFIED_REF.findall(masked):
        key = table_metrics.get((table.lower(), name.lower()))
        if key is None:
            return f"'{table}.{name}' is not a metric of a table in the view"
        keys.append(key)
    if not keys:
        return "it does not reference a table's metric"
    stray = [
        name
        for name in _BARE_NAME.findall(_QUALIFIED_REF.sub("", masked))
        if name.lower() not in SQL_RESERVED
    ]
    if stray:
        return f"'{stray[0]}' is not a table's metric"
    relations = sorted({metrics[k]["relation"] for k in keys})
    if len(relations) > 1:
        return f"it combines metrics of {_join(relations)}, and a derived metric reads one relation"
    filtered = sorted({k for k in keys if metrics[k].get("filters")})
    if filtered:
        return f"{_join(filtered)} carries filters, which a derived metric cannot inline"
    derived = _outside_quotes(
        expr,
        lambda segment: _QUALIFIED_REF.subn(
            lambda m: "{" + table_metrics[(m[1].lower(), m[2].lower())] + "}", segment
        ),
    )[0]
    return derived, keys


def _import_view_metrics(
    doc: dict,
    out: dict,
    owners: dict[str, str],
    warnings: list[str],
    table_metrics: dict[tuple[str, str], str],
) -> None:
    """View-level metrics combine table metrics (`orders.revenue /
    orders.order_count`), which is what a sqldash derived metric is when they
    all read one relation. Anything else is named, not dropped silently."""
    for metric in _mapping_entries(doc.get("metrics"), "metrics"):
        mname, expr = metric.get("name"), metric.get("expr")
        if not mname or not expr:
            warnings.append("skipped a view-level metric missing name or expr")
            continue
        converted = _derived_from(str(expr), table_metrics, out["metrics"])
        if isinstance(converted, str):
            warnings.append(f"view-level metric '{mname}' ({expr}) skipped: {converted}")
            continue
        derived, keys = converted
        peer = out["metrics"][keys[0]]
        entry: dict[str, Any] = {"derived": derived}
        if metric.get("description"):
            entry["description"] = metric["description"]
        if metric.get("synonyms"):
            entry["synonyms"] = list(metric["synonyms"])
        if peer.get("time_dimension"):
            entry["time_dimension"] = dict(peer["time_dimension"])
        if peer.get("dimensions"):
            entry["dimensions"] = [dict(d) for d in peer["dimensions"]]
        _add_metric(out, owners, mname, "view", peer["relation"], entry, warnings)


def parse_semantic_view(text: str) -> tuple[dict, list[str]]:
    """Invert the export: tables become relations and per-table metrics, the source
    gets placeholder credentials, and relationships/verified_queries are skipped
    with warnings."""
    warnings: list[str] = []
    owners: dict[str, str] = {}
    doc, tables = _load_view(text, warnings)
    out: dict[str, Any] = {
        "source": _placeholder_source(tables),
        "relations": {},
        "metrics": {},
    }
    _warn_unsupported_sections(doc, warnings)
    table_metrics: dict[tuple[str, str], str] = {}
    for table in tables:
        _import_table(table, out, owners, warnings, table_metrics)
    _import_view_metrics(doc, out, owners, warnings, table_metrics)
    if not out["metrics"]:
        raise SemanticError("no importable metrics found in the semantic view")
    return out, warnings
