"""Lossy, offline projection of agents.yaml onto Snowflake Cortex Agents."""

import json
import re
from dataclasses import replace

from sqldash.models.semantics import SQL_RESERVED
from sqldash.project.store import DashboardStore, WorkspaceStore
from sqldash.semantics.agents import AgentLayer
from sqldash.semantics.cortex import build_semantic_view, render_yaml
from sqldash.semantics.cortex_verified import export_verified
from sqldash.semantics.layer import SemanticError, SemanticLayer


def _identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", value):
        raise SemanticError(f"expected an unquoted Snowflake identifier, got {value!r}")
    if value.lower() in SQL_RESERVED:
        raise SemanticError(f"Snowflake export identifier {value!r} is a reserved word")
    return value


def _literal(value: str) -> str:
    if "$$" not in value and not value.endswith("$"):
        return "$$" + value + "$$"
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def build_cortex_agent(
    layer: SemanticLayer,
    store: DashboardStore,
    name: str,
    *,
    schema: str | None = None,
    model: str = "auto",
) -> tuple[dict, dict, str, list[str]]:
    """Return the agent spec, scoped view, deployment SQL, and projection warnings."""
    if isinstance(store, WorkspaceStore):
        raise SemanticError(
            "Cortex Agent export requires a single project; pass a project path "
            "after the agent name"
        )
    agent = AgentLayer(store, layer).resolve(name)
    definition = agent.definition
    _identifier(name)
    metrics = (
        [layer.resolve(n) for n in definition.metrics]
        if definition.metrics
        else layer.all_metrics()
    )
    scoped = bool(definition.metrics or definition.dimensions)
    accounts = {m.source.account for m in metrics if m.source.type == "snowflake"}
    if not scoped:
        accounts.update(
            dashboard.source.account
            for _, dashboard in store.iter_loaded()
            if dashboard.source.type == "snowflake" and dashboard.queries
        )
    if len(accounts) > 1:
        raise SemanticError(
            "Cortex Agent export requires metrics and verified queries from one Snowflake "
            "account; narrow the agent's metric allow-list or separate the projects"
        )
    dimensions = set(definition.dimensions)
    declared = {d.name for m in metrics for d in m.definition.dimensions} | {
        m.definition.time_dimension.name for m in metrics if m.definition.time_dimension
    }
    if unknown := dimensions - declared:
        raise SemanticError(f"agent '{name}' has unknown dimensions: {', '.join(sorted(unknown))}")
    if dimensions:
        metrics = [
            replace(
                m,
                definition=m.definition.model_copy(
                    update={
                        "dimensions": [d for d in m.definition.dimensions if d.name in dimensions],
                        "time_dimension": (
                            m.definition.time_dimension
                            if m.definition.time_dimension
                            and m.definition.time_dimension.name in dimensions
                            else None
                        ),
                    }
                ),
            )
            for m in metrics
        ]
    view_name = f"{name}_metrics"
    view, warnings = build_semantic_view(
        layer,
        store,
        view_name,
        definition.description,
        metrics=metrics,
        include_verified_queries=not scoped,
    )
    if schema is None:
        schemas = {
            (m.source.database, m.source.db_schema) for m in metrics if m.source.type == "snowflake"
        }
        if len(schemas) != 1 or any(not part for pair in schemas for part in pair):
            raise SemanticError("pass --schema DATABASE.SCHEMA for the exported objects")
        schema = ".".join(next(iter(schemas)))
    parts = schema.split(".")
    if len(parts) != 2:
        raise SemanticError("--schema must be DATABASE.SCHEMA")
    schema = ".".join(_identifier(part) for part in parts)
    verified, verification_warnings = export_verified(agent, view, schema, _literal)
    if verified:
        existing = view.setdefault("verified_queries", [])
        names = {q["name"] for q in existing}
        for query in verified:
            if query["name"] in names:
                raise SemanticError(f"duplicate exported verified query name: {query['name']}")
            existing.append(query)
            names.add(query["name"])
    warnings.extend(verification_warnings)
    if scoped:
        warnings.append(
            "dashboard verified queries omitted: authored SQL cannot be checked against "
            "the agent's metric/dimension allow-list"
        )
    for tool in agent.tools:
        warnings.append(
            f"tool '{tool.name}' omitted as a callable: Cortex requires a hosted tool; "
            "only explicitly verified invocations are exported as verified_queries"
        )
    if definition.uses:
        warnings.append("external MCP dependencies (uses) omitted; configure them in the host")
    if definition.evals:
        warnings.append("evals omitted: expectations are tests, not verified SQL queries")
    if definition.sql:
        warnings.append("sql: true omitted: no raw-SQL tool is exported")
    else:
        warnings.append(
            "sql: false has no Cortex equivalent: Cortex Analyst generates SQL; "
            "warehouse grants remain the access boundary"
        )
    instructions: dict = {"orchestration": definition.instructions}
    if definition.response:
        instructions["response"] = definition.response
    if definition.sample_questions:
        instructions["sample_questions"] = [
            {"question": question} for question in definition.sample_questions
        ]
    spec = {
        "models": {"orchestration": model},
        "instructions": instructions,
        "tools": [
            {
                "tool_spec": {
                    "type": "cortex_analyst_text_to_sql",
                    "name": "Analyst",
                    "description": definition.description,
                }
            }
        ],
        "tool_resources": {"Analyst": {"semantic_view": f"{schema}.{view_name}"}},
    }
    profile = json.dumps({"display_name": definition.title or name})
    sql = (
        "CALL SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML(\n"
        f"  {_literal(schema)},\n  {_literal(render_yaml(view))}\n);\n\n"
        f"CREATE OR REPLACE AGENT {schema}.{name}\n"
        f"  COMMENT = {_literal(definition.description)}\n"
        f"  PROFILE = {_literal(profile)}\n"
        f"  FROM SPECIFICATION\n{_literal(render_yaml(spec))};\n"
    )
    return spec, view, sql, warnings
