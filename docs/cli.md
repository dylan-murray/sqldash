# CLI reference

`sqldash --help` and `sqldash <command> --help` are always current. This page groups
the commands by what they are for.

## Project

```bash
sqldash init [dir] [--demo]         # .sqldash/; --demo adds the sample dashboard and metrics
sqldash setup [dir]                 # first run: pick a warehouse, write a profile, test the connection
sqldash serve [path | git-url]      # serve a directory of dashboards, a single file, or the workspace
sqldash lint [path] [--strict]      # validate dashboards and metrics (CI-friendly exit codes)
```

Plain `lint` never opens a warehouse. `--strict` does: it compiles each metrics.yaml
metric with all its dimensions and its grain, and each read sql tool, and runs them
wrapped in `WHERE 1 = 0`, so an expr the warehouse can't parse (`MAX(order)`) or a
column it can't resolve fails CI with the warehouse's own error. No rows come back.
On Snowflake it also reads the column types, and warns about a time dimension over a
`TIMESTAMP_TZ` column that has no `timezone: session`.

## AI Studio

```bash
sqldash serve                          # includes Studio on loopback hosts
sqldash serve --no-studio              # disable the local coding-agent editor
sqldash studio list                    # discover installed agents and saved entrypoints
sqldash studio add "Custom agent" --shell /bin/zsh -- claude-custom -p '{prompt}'
sqldash studio check "Custom agent"     # check command lookup without running an edit
```

Open **AI Studio** on a dashboard to pin requests and send them to an agent.
See the [Studio guide](studio.md) for entrypoints, permissions, and undo.

## Queries and metrics

```bash
sqldash query file.yaml daily_revenue \
  -p start_date=2026-06-01 -f csv   # run a query headlessly: table | csv | json
sqldash query . dash.daily_revenue  # dotted name (or --dashboard) picks the dashboard
sqldash metric list [path]          # the semantic layer at a glance
sqldash metric show revenue         # full definition: expr, relation, dimensions
sqldash metric query revenue -d region -g month   # evaluate a governed metric
sqldash metric show revenue --dashboard sales     # scope a name two dashboards define inline
sqldash agent list [path]           # the agents served over MCP
sqldash agent show finance_analyst --prompt      # exactly what a host receives
sqldash agent eval finance_analyst --runner 'claude -p ...'   # answer each eval, grade it against the real result
```

A metric reached by a bare name is all-time: `sqldash query . revenue` means the same
thing in a one-dashboard project as in a ten-dashboard one. Name a dashboard and you
get its filter defaults too, so `query . demo.revenue`, `--dashboard demo`, a `.yaml`
target, and a tile id all run the dashboard's date window — and say so on stderr:

```
note: windowed 2026-07-24..2026-09-22 by dashboard 'demo' — pass --start/--end for your own window
```

The note names every default the dashboard applied that you did not pass yourself,
not only the window, and the remedy is the one that works on the path you are on:

```
note: windowed 2026-07-24..2026-09-22 and filtered region=us by dashboard 'dflt' — pass --start/--end for your own window, -p region=all for your own filters
note: windowed 2026-07-24..2026-09-22 by dashboard 'demo' — pass -p dates_start=<date> -p dates_end=<date> for your own window
```

The second is a **named query**: `--start/--end` are metric flags, so a query takes
its own date params with `-p`. MCP `query_metric` gets the same sentence in a
`scope_note` field.

Pass one endpoint and the dashboard still supplies the other, so the note names that
half alone rather than going quiet:

```
$ sqldash query . demo.revenue_by_category -p dates_end=2027-01-01
note: windowed from 2026-07-24 by dashboard 'demo' — pass -p dates_start=<date> for your own window
```

## Introspection

Everything a dashboard or metric knows is available headlessly, built for scripts and
AI agents. Every `list` and `show` takes `--json`.

```bash
sqldash dashboard list [path]       # every dashboard: title, source, tiles, metrics used
sqldash dashboard show <name>       # one dashboard in full (filters, tiles, queries)
sqldash source list [path]          # every data source, credentials redacted
sqldash source test [path]          # connect and SELECT 1 on each source, report latency
sqldash source describe [path]      # tables and columns, the raw material for metrics
```

## Workspace, agents, and exports

```bash
sqldash repo add|list|remove …      # the multi-repo workspace registry
sqldash mcp [path | git-url | --all]   # serve the semantic layer to agents over stdio
sqldash snapshot [path] -o out      # render dashboards to static PNGs plus a gallery
sqldash export cortex-agent NAME …    # Cortex Agent + scoped view deployment SQL
sqldash export cortex|lookml|context …  # Snowflake semantic view, LookML, agent markdown
sqldash import cortex|lookml …          # convert existing definitions to metrics.yaml
```

See [semantic-layer.md](semantic-layer.md) for the MCP tool surface and
[workspaces.md](workspaces.md) for the registry and snapshots.
