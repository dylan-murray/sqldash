"""Renders the semantic layer as markdown for agent context files (CLAUDE.md,
llms.txt): canonical metrics, dashboards, and YAML authoring rules."""

from sqldash.models.source import redact_source, source_label
from sqldash.project.store import Store
from sqldash.semantics.agents import agent_layer_for, allowed_metrics, tool_signature
from sqldash.semantics.compiler import MACRO_NOTE, describes_macro


def export_context(layer, store: Store) -> str:
    lines: list[str] = []
    out = lines.append

    out("# Data context: sqldash semantic layer")
    out("")
    out(
        "This project defines governed BI metrics and dashboards as code (sqldash). "
        "Metric names, dimensions, and SQL below are the canonical definitions — "
        "prefer them over improvising your own aggregations."
    )
    out("")

    metrics = layer.all_metrics()
    if metrics:
        out("## Metrics")
        out("")
        for resolved in metrics:
            d = resolved.definition
            title = d.title or resolved.name
            out(f"### `{resolved.name}` — {title}")
            if resolved.ambiguous_with:
                out("")
                out(
                    f"**Ambiguous — not resolvable by this name.** Defined inline by "
                    f"{', '.join(f'`{w}`' for w in resolved.ambiguous_with)}; the "
                    f"definition below is one of them. Scope it to the one you mean, "
                    f"e.g. `sqldash metric query {resolved.name} "
                    f"--dashboard {resolved.ambiguous_with[0]}`."
                )
            if d.description:
                out("")
                out(d.description)
            out("")
            base = (
                f"table `{resolved.relation.table}`"
                if resolved.relation.table
                else "inline SQL relation"
            )
            out(f"- definition: `{d.expr}` over {base} ({source_label(resolved.source)})")
            # This file is read by agents that then write SQL of their own, and
            # the definition above is the author's text, not compiled SQL. Say so
            # where it matters, rather than rewriting it to one dialect here.
            if describes_macro(
                d.expr,
                *d.filters,
                *(dim.expr for dim in d.dimensions),
                d.time_dimension.expr if d.time_dimension else None,
            ):
                out(f"- note: this definition {MACRO_NOTE}")
            if d.synonyms:
                out(f"- synonyms: {', '.join(d.synonyms)}")
            if d.dimensions:
                dims = ", ".join(
                    f"`{dim.name}`" + (f" ({dim.description})" if dim.description else "")
                    for dim in d.dimensions
                )
                out(f"- dimensions: {dims}")
            if d.time_dimension:
                out(
                    f"- time dimension: `{d.time_dimension.name}` "
                    f"(default grain: {d.time_dimension.grain})"
                )
            if d.cumulative:
                out("- cumulative: a running total, each bucket includes every earlier one")
            if d.window:
                out(f"- window: trailing {d.window}, each bucket covers the window ending there")
            if d.filters:
                out(f"- always-applied filters: {'; '.join(f'`{f}`' for f in d.filters)}")
            if d.owners:
                out(f"- owners: {', '.join(d.owners)}")
            out("")

    loaded = list(store.iter_loaded())
    if loaded:
        out("## Dashboards")
        out("")
        for name, dashboard in loaded:
            out(f"### {dashboard.title} (`{name}.yaml`)")
            if dashboard.description:
                out("")
                out(dashboard.description)
            out("")
            out(f"- source: {redact_source(dashboard.source).get('type')}")
            if dashboard.filters:
                out(f"- filters: {', '.join(f.name for f in dashboard.filters)}")
            metric_tiles = [w for w in dashboard.tiles if w.metric]
            if metric_tiles:
                out(
                    "- metric tiles: "
                    + ", ".join(f"{w.id} → `{w.metric.name}`" for w in metric_tiles)
                )
            out("")
            for query_name, sql in dashboard.queries.items():
                out(f"#### query `{query_name}`")
                out("")
                out("```sql")
                out(sql.strip())
                out("```")
                out("")

    agents = agent_layer_for(store, layer).all_agents()
    if agents:
        out("## Agents")
        out("")
        out(
            "Agents defined in `agents.yaml`. `sqldash mcp` serves each one as an MCP "
            "prompt and each of its tools as an MCP tool; pick the prompt to take on the "
            "role. sqldash never runs a model."
        )
        out("")
        for agent in agents:
            d = agent.definition
            out(f"### `{agent.name}` — {d.title or agent.name}")
            out("")
            out(d.description)
            out("")
            out("Instructions:")
            out("")
            out(d.instructions.strip())
            if d.response:
                out("")
                out(f"How to answer: {d.response.strip()}")
            out("")
            names = ", ".join(f"`{m.name}`" for m in allowed_metrics(agent)) or "none"
            scope = "" if d.metrics else " (every metric in the layer)"
            out(f"- metrics: {names}{scope}")
            if d.dimensions:
                out(f"- dimensions: {', '.join(d.dimensions)}")
            for tool in agent.tools:
                out(f"- tool `{tool_signature(tool)}`: {tool.definition.description}")
            out(f"- raw SQL: {'allowed' if d.sql else 'off'}")
            for question in d.sample_questions:
                out(f"- example: {question}")
            out("")

    out("## Authoring sqldash YAML (for generators)")
    out("")
    out("If you write or edit dashboard files for this project, the dialect is")
    out("deliberately small — near-misses from other tools' conventions are the")
    out("most common failure. The rules:")
    out("")
    out("- **Source config is flat.** `type:` names the database (snowflake,")
    out("  postgres, duckdb, ...), credentials are flat fields (`username:`,")
    out("  `password:`, `authentication:`) or `profile: <name>` for local")
    out("  credentials. There is no `connection:` key and no nested `auth:` block.")
    out("- **Conditionals are `{% if param %}`, `{% elif param %}`, `{% else %}`,")
    out("  `{% endif %}` only** — bare param names, no comparisons, no nesting, no")
    out("  other Jinja. A select filter sitting on its `all` value already counts")
    out("  as inactive, so `{% if model %}` does what `!= 'all'` intends.")
    out("- **Params are `{{ name }}`** and always bind as native query parameters.")
    out("- **Tiles flow in file order**; `size: WxH` hints the footprint (12")
    out("  columns). Inline `sql:` on a tile is fine; `metric: <name>` uses the")
    out("  semantic layer; `compare: previous_period|yoy` adds period comparison")
    out("  (headless: `query` / `metric query --compare`, MCP `query_metric(compare=)`).")
    out("- **Validate before writing**: with MCP, call `validate_dashboard` /")
    out("  `validate_metrics` on candidate YAML, passing the name the file will be")
    out("  saved as when you know it; with a shell, write the file and run")
    out("  `sqldash lint`.")
    out("")
    out("## Querying these metrics")
    out("")
    out(
        "Agents with MCP access: run `sqldash mcp <project-or-git-url>` and use "
        "`list_metrics` / `query_metric` — pass metric and dimension names plus filter "
        "values; SQL is compiled and executed with safe binding. Humans: `sqldash serve` "
        "for dashboards, `sqldash query <file> <name>` for headless results."
    )
    out("")
    return "\n".join(lines)
