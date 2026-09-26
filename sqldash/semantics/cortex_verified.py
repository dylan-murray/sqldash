"""Project approved bundle invocations onto Snowflake's native semantic SQL."""

import math
import re
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal

from sqldash.models.semantics import RelationDef
from sqldash.semantics.agents import prepare_verified
from sqldash.semantics.bind import bind_resolved
from sqldash.semantics.compiler import bucket_input, exported_sql
from sqldash.semantics.layer import SemanticError

_SQL_TOKEN = re.compile(r""""(?:[^"]|"")*"|'(?:[^']|'')*'|:(\d+)""")


class UnsupportedExample(SemanticError):
    """A valid invocation whose meaning the exported view cannot preserve."""


def _ident(value):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", value):
        raise UnsupportedExample(f"logical name '{value}' requires a quoted identifier")
    return value


def _value_sql(value, literal):
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float, Decimal)):
        if (isinstance(value, float) and not math.isfinite(value)) or (
            isinstance(value, Decimal) and not value.is_finite()
        ):
            raise SemanticError("verified arguments must be finite numbers")
        return str(value)
    if isinstance(value, (date, datetime)):
        value = value.isoformat()
    if not isinstance(value, str):
        raise SemanticError("verified argument values must be strings, numbers, dates or booleans")
    return literal(value)


def _inline(bound, literal):
    return _SQL_TOKEN.sub(
        lambda match: _value_sql(bound.bind[int(match[1]) - 1], literal) if match[1] else match[0],
        bound.sql,
    )


def _project_metric(resolved, view, destination):
    definition = resolved.definition
    entries = [t for t in view["tables"] if any(m["name"] == resolved.name for m in t["metrics"])]
    if len(entries) != 1:
        raise UnsupportedExample(f"metric '{resolved.name}' is absent from the exported view")
    table = entries[0]
    if definition.cumulative or definition.window:
        raise UnsupportedExample(
            f"metric '{resolved.name}' has cumulative/window behavior "
            "that native semantic SQL cannot preserve in this export"
        )
    dimensions = {
        d["name"]: d["expr"]
        for d in [*table.get("dimensions", []), *table.get("time_dimensions", [])]
    }
    qualifier = _ident(table["name"])

    def project_dimension(dim, exported=None):
        # Against the *exported* expr: the view carries author SQL as the export
        # seam wrote it, so a dimension using the truncation macro compares equal
        # to its own definition only after the same conversion. Comparing raw
        # text dropped every such example as "changed", with a warning that
        # blamed the view.
        if dimensions.get(dim.name) != exported_sql("snowflake", exported or dim.sql_expr):
            raise UnsupportedExample(f"dimension '{dim.name}' changed or was omitted in the view")
        return dim.model_copy(update={"expr": f"{qualifier}.{_ident(dim.name)}"})

    # Only dimensions retained by the scoped view are exposed to the compiler.
    projected = definition.model_copy(
        update={
            "expr": f"AGG({qualifier}.{_ident(resolved.name)})",
            # The view's metric expr already carries them, folded in by the
            # export; applying them again as a WHERE would read a column the
            # view does not expose and splice arguments into author quoting.
            "filters": [],
            "dimensions": [
                project_dimension(d) for d in definition.dimensions if d.name in dimensions
            ],
            "time_dimension": (
                project_dimension(
                    definition.time_dimension,
                    bucket_input("snowflake", definition.time_dimension),
                )
                if definition.time_dimension and definition.time_dimension.name in dimensions
                else None
            ),
        }
    )
    return replace(resolved, definition=projected, relation=RelationDef(table=destination))


def export_verified(agent, view, schema, literal):
    """Return whole verified examples, warning when the view is a lossy projection."""
    verified, warnings = [], []
    destination = f"{schema}.{view['name']}"
    for example in agent.definition.verified:
        prepared = prepare_verified(agent, example)
        ctes, results = [], []
        try:
            for index, (bound, kwargs, previous) in enumerate(prepared):
                projected = _project_metric(bound.resolved, view, destination)
                current = bind_resolved(projected, **kwargs)
                name = f"sqldash_verified_{index}"
                ctes.append(f"{name} AS (\n{_inline(current, literal)}\n)")

                def rows(cte):
                    return (
                        "(SELECT COALESCE(ARRAY_AGG(OBJECT_CONSTRUCT_KEEP_NULL(*)), "
                        f"ARRAY_CONSTRUCT()) FROM {cte})"
                    )

                fields = f"'metric', {literal(bound.resolved.name)}, 'rows', {rows(name)}"
                if previous is not None:
                    start, end = previous.time_range
                    prior = bind_resolved(projected, **{**kwargs, "start": start, "end": end})
                    ctes.append(f"{name}_previous AS (\n{_inline(prior, literal)}\n)")
                    fields += f", 'previous_rows', {rows(name + '_previous')}"
                results.append(f"OBJECT_CONSTRUCT_KEEP_NULL({fields})")
        except UnsupportedExample as exc:
            warnings.append(f"verified '{example.name}' omitted: {exc}")
            continue
        sql = (
            "WITH "
            + ",\n".join(ctes)
            + "\nSELECT ARRAY_CONSTRUCT(\n  "
            + ",\n  ".join(results)
            + "\n) AS results"
        )
        item = {"name": f"{agent.name}_{example.name}", "question": example.question, "sql": sql}
        for key in ("verified_by", "verified_at"):
            value = getattr(example, key)
            if value is not None:
                item[key] = value
        verified.append(item)
    return verified, warnings
