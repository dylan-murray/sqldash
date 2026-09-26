# The semantic layer

Define each metric once in `metrics.yaml`. The same definition powers dashboard
tiles, the CLI, agents over MCP, and exports to other BI tools. The README has the
short version; this page is the reference.

## Defining metrics

```yaml
# metrics.yaml
source: {type: snowflake, account: acme-xy12345, database: ANALYTICS, schema: PUBLIC,
         authentication: externalbrowser, username: "${env:SNOWFLAKE_USER}"}
relations:
  orders: {table: ORDERS}
metrics:
  revenue:
    title: Revenue
    description: Total order revenue in USD
    relation: orders
    expr: SUM(amount)
    synonyms: [sales, turnover]
    time_dimension: {name: order_date, grain: day}
    dimensions: [{name: region, description: Sales region}, {name: category}]
  order_count:
    relation: orders
    expr: COUNT(*)
    time_dimension: {name: order_date, grain: day}
    dimensions: [{name: region}, {name: category}]
  avg_order_value:
    derived: "{revenue} / NULLIF({order_count}, 0)"   # ratios of governed metrics
    format: currency
  cumulative_revenue:
    relation: orders
    expr: SUM(amount)
    cumulative: true                                  # running total over time
    time_dimension: {name: order_date, grain: day}
  trailing_28d_revenue:
    relation: orders
    expr: SUM(amount)
    window: 28 days                                   # trailing aggregate per bucket
    time_dimension: {name: order_date, grain: day}
```

A metric is one base (`relation`, `table`, or `sql`), an aggregate `expr`, optional
`dimensions`, an optional `time_dimension` with a default grain, optional static
`filters`, and metadata (title, description, format, synonyms, owners).

On Snowflake, a `TIMESTAMP_TZ` time dimension needs `timezone: session`:

```yaml
    time_dimension: {name: created_at, grain: month, timezone: session}
```

Snowflake truncates a `TIMESTAMP_TZ` at each row's own offset, so without it a month
of rows written from three time zones comes back as three buckets for that month.
With it, sqldash reads the column as `TIMESTAMP_LTZ` before bucketing, so every row
is cut in the session time zone. Postgres and DuckDB already bucket a `timestamptz`
that way, so the option changes nothing there. Leave it off for `DATE`,
`TIMESTAMP_NTZ` and `TIMESTAMP_LTZ` columns, which bucket correctly as they are.
`sqldash lint --strict` warns about a `TIMESTAMP_TZ` column time dimension that
doesn't set it.
A `derived` metric references plain metrics on one relation; a component that
carries `filters`, `window` or `cumulative` is refused, because only its `expr` is
inlined.

When an expression needs a time bucket inside it, write
`SQLDASH_TRUNC('<grain>', <expr>)` rather than a warehouse's own function:

```yaml
  active_weeks:
    relation: orders
    expr: "COUNT(DISTINCT SQLDASH_TRUNC('week', order_date))"
```

sqldash spells that for whatever source the file is pointed at, the same way it
spells a query's grain, so the metric still runs after you re-point `source:` at a
different warehouse. It works in `expr`, `filters`, dimension and time_dimension
`expr`, and a relation's `sql:`; the grains are the query grains (hour, day, week,
month, quarter, year), and `sqldash lint` reports a grain it does not know or a
call it cannot read. `sqldash import lookml` emits it for a dimension_group
timeframe. It is sqldash's own spelling, not a warehouse function, so it will not
run if you paste it into a tile or `run_sql` — the exports resolve it for you:
`export lookml` and `export cortex` write the target's real function, while
`export context` and the metric page show the definition as you wrote it and say
it is not runnable SQL.

## In dashboards

A tile says `metric: revenue`, or `metric: {name: revenue, grain: day}`, instead of
raw SQL. Dashboard filters bind to metric dimensions by name, and a `daterange` filter
becomes the metric's time range. Dashboards can also define `metrics:` inline to stay
single-file portable.

An inline metric is self-contained on purpose: it runs on the dashboard's own
`source:`, so its base has to be written in the dashboard file too. `relation:`
resolves against the dashboard's own `relations:` and never against metrics.yaml,
whose relations belong to that file's source. So a metric copied out of metrics.yaml
needs its base rewritten:

```yaml
# a dashboard file
relations:
  orders: {table: orders}      # either declare the relation here...
metrics:
  revenue_inline:
    relation: orders
    expr: SUM(amount)
    time_dimension: {name: order_date, grain: day}
  order_count_inline:
    table: orders              # ...or skip relations: and name the table directly
    expr: COUNT(*)
    time_dimension: {name: order_date, grain: day}
```

A metric that should reuse a project relation belongs in metrics.yaml; a tile
references it by name from any dashboard, with nothing inline at all.

## From the terminal

```bash
sqldash metric list [path]                       # the semantic layer at a glance
sqldash metric show revenue                      # full definition: expr, relation, dimensions
sqldash metric query revenue -d region -g month  # evaluate a governed metric
sqldash metric query revenue --compare previous_period
sqldash metric show revenue --dashboard sales    # scope a name two dashboards define inline
```

Every `list` and `show` takes `--json`.

## For agents (MCP)

`sqldash mcp .` (or a git URL) serves the layer to Claude Code and other MCP clients
over stdio:

```bash
claude mcp add acme-metrics -- uvx sqldash mcp git@github.com:acme/dashboards.git
```

Tools: `list_metrics`, `get_metric`, `query_metric(name, dimensions, grain, filters,
start, end, compare, limit)`, `list_sources` (credentials redacted), `get_dashboards`,
`get_schema` (tables and columns for a source), and `validate_metrics` /
`validate_dashboard` for checking candidate YAML before writing a file. Pass
`validate_dashboard`'s `name` (the dashboard the YAML will be saved as, or
`repo/dashboard` in a workspace) whenever you know it: it is how the validator
tells an edit of a stored dashboard from a new file, and how relative source
paths find the right repo.

`query_metric` also takes `dashboard=`, which applies that dashboard's filter
defaults. Whatever those narrowed that you did not ask for comes back in a
`scope_note` field, the same sentence the CLI puts on stderr.
Agents pass names and values. sqldash compiles safe, bound SQL, so callers never
supply SQL fragments. Raw `run_sql` is off unless you pass `--allow-sql`, and even
then it is a single statement with a row cap.

`run_sql` takes an optional `source`, any key from `list_sources`, and its result
says which source it ran on. Without one it follows the same rule as `get_schema`:
the metrics.yaml source when there is exactly one, else the dashboard's source when
there is exactly one dashboard, and otherwise an error listing the sources to pass.
That holds for a single project and a workspace alike.

Every result carries `row_limit`, the cap that was applied, and `truncated`, which
says whether that cap clipped the answer. `query_metric`'s `limit` is the caller's
own cap; omitted, the only cap is `sqldash mcp --row-limit` (1000 by default), the
same kind of cap the CLI and `POST /api/run` apply to the same query. A clipped
result always says so, so a short series is never mistaken for a complete one.

An agent without MCP can self-orient with `sqldash dashboard list --json` and
`sqldash metric list --json`, or read the markdown that `sqldash export context`
writes for a CLAUDE.md or llms.txt.

## Interop with other BI tools

`sqldash export cortex` emits a Snowflake semantic-view YAML for
`SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML`. `sqldash export lookml` emits LookML views.
The other way, `sqldash import cortex <view.yaml>` and `sqldash import lookml <views/>`
convert existing definitions to metrics.yaml (`pip install 'sqldash[lookml]'` for
LookML parsing). Snowflake stores an unquoted name uppercased, so a view read back
with `SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW` says `REVENUE`; `import cortex` folds
those names back to lowercase (Snowflake treats them as the same identifier) and
keeps mixed-case names, which only a quoted identifier can produce, as written.
A `timezone: session` time dimension exports as `CAST(<expr> AS TIMESTAMP_LTZ)`,
since Cortex Analyst does its own bucketing, and `import cortex` reads that cast back
as `timezone: session`.
Derived and non-Looker-aggregate metrics warn on export
because LookML has no measure type for them. Cumulative and window metrics warn
too, since Looker itself computes the per-bucket sum, but they do round-trip: the
exported measure carries the semantics on `tags:` and says what Looker will
compute in its `description:`, and `import lookml` reads it back. A metric's `title` and `format` export
as LookML `label:` and `value_format_name:`, and dimension descriptions and
`synonyms` carry across on the LookML fields of the same name, which `import lookml`
reads back; `owners` and formats Looker has no name for (`compact`, `date`, most
currencies) are dropped with a warning.

## One namespace per project

Metric names live in one namespace per project. `metrics.yaml` is canonical, so a
dashboard defining the same name inline overrides it within that dashboard only. A
name that two dashboards each define inline belongs to neither, and sqldash refuses it
rather than picking one. `--dashboard` on the CLI (and the `dashboard` argument on the
MCP metric tools) says which one you mean, and `sqldash lint` fails on the collision so
CI tells you before a query does.
