# Privacy Policy

Last updated September 26, 2026.

sqldash is open source software that runs on your own machine. There is no
sqldash service, account or server behind it. This page explains what sqldash
reads, what it connects to, and what it keeps.

## What sqldash reads

When you run `sqldash` (the CLI, `sqldash serve`, or `sqldash mcp`), it reads:

- **Dashboard and metric files** in the project or repos you point it at
  (`*.yaml`, `.sqldash/`, `metrics.yaml`, LookML files you import)
- **Git metadata** for those files (author and dates from `git log`), when the
  project is a git repository
- **Connection profiles** from `profiles.yaml` in your user config directory,
  and the environment variables that `${env:VAR}` references in them point at
- **Query results** returned by the warehouses you configure

## What sqldash connects to

sqldash makes network connections only to places you configure:

- **Your own warehouses and databases.** Queries go straight from your machine
  to the sources in your profiles (DuckDB, Postgres, Snowflake, BigQuery,
  Redshift, Databricks, Athena, MySQL, Trino, ClickHouse), using each vendor's
  own driver. Those drivers may talk to their vendor's endpoints as part of
  connecting (for example Snowflake's certificate checks, or a single sign-on
  page your identity provider serves), and each follows its vendor's policy.
  DuckDB may also download one of its own extensions from DuckDB's extension
  repository the first time a query needs it.
- **Git remotes you add to a workspace.** Remote repos are cloned and updated
  with your local `git`, using your own git credentials.
- **The local web UI.** `sqldash serve` listens on `127.0.0.1` by default and
  opens your browser at that address (skip it with `--no-browser`). The pages
  load every script, style and font from the local server, and their content
  security policy blocks connections to any other origin.

## Agents you choose

sqldash does not include or call any AI model itself.

- **AI Studio** runs a coding agent CLI that is already installed on your
  machine (Claude Code or Codex when found on your `PATH`, or a command you
  save). It runs only when you send it a request. It receives your request and
  annotations, a screenshot of the dashboard when you attach one, and works in
  your project directory. What that agent sends to its model provider is
  governed by that agent and provider.
- **The MCP server** (`sqldash mcp`) answers the MCP client you connect to it,
  over stdio on your machine. Metric results it returns reach whatever model
  that client uses.
- **`sqldash agent eval`** runs the agent command you configure for the eval.

## What sqldash does not do

- Does not collect analytics, telemetry, crash reports or usage data
- Does not phone home, check for updates, or contact any sqldash-operated server
- Does not send your queries, results or files anywhere you did not configure
- Does not write credentials into your project files; secrets stay in
  environment variables or your per-user profiles file
- Does not sell, share, or monetize your data in any way

## What sqldash keeps on your machine

| Location | Contains |
|---|---|
| Your project (`*.yaml`, `.sqldash/`) | Dashboards and metrics, which you edit and commit yourself |
| User config directory, `profiles.yaml` | Connection profiles; the file is written with owner-only permissions |
| User config directory, `repos.yaml` | The workspace repos you added |
| User config directory, `studio.json` | AI Studio agent entrypoints you saved |
| User cache directory, `repos/` | Clones of your remote workspace repos |

The user config and cache directories are the standard per-user locations for
your operating system (for example `~/Library/Application Support/sqldash` and
`~/Library/Caches/sqldash` on macOS). Deleting them removes everything sqldash
stored outside your projects.

## Contact

For privacy questions, open an issue at
https://github.com/dylan-murray/sqldash/issues. For anything sensitive, use
private vulnerability reporting as described in [SECURITY.md](SECURITY.md).
