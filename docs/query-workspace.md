# Query workspace

**+ Explore** on a dashboard opens the query workspace: browser-local query tabs, a
project library of saved queries, a schema browser, and a chart builder that adds
results to the dashboard as tiles. The tile pencil still provides direct
SQL/text/metric editing.

## Try it on a seeded project

```sh
uv run python scripts/seed_query_workspace.py /tmp/sqldash-workspace
uv run sqldash serve /tmp/sqldash-workspace
```

The script writes a disposable synthetic dataset with four physical DuckDB tables,
two dashboards, and one saved library query. Open the demo dashboard and choose
**+ Explore**. Use a separate directory for each running server; two DuckDB writers
cannot share one database file.

## Try one complete workflow

1. Open **Saved queries → Revenue by region** from the seeded library, or create a query tab and enter SQL. Example:

   ```sql
   SELECT region, ROUND(SUM(amount), 2) AS revenue
   FROM orders
   GROUP BY region
   ORDER BY revenue DESC
   ```

2. **Save query** stores it in the project library without creating a tile. Saved queries are grouped by connection above **Database**, which browses source tables and columns.
3. **Run**, inspect **Results**, then use the adjacent chart builder and **Add to dashboard** to add a table. Name it “Revenue table”. The tab stays open.
4. Choose **Bar** in the chart builder, and **Add another tile**. Name it “Revenue chart”. These two tiles reference one SQL definition, with separate chart settings.
5. Open another query, switch back, and refresh the page. SQL/source/chart drafts recover; query results do not auto-run.
6. Rename or delete the library query. Existing dashboard copies and open drafts stay intact. Editing shared dashboard SQL lists its consumers and offers a copy for the current tile.
7. Open Queries from another dashboard in the same project to find the library. Its required source and parameter definitions must be compatible before opening. Adding copies SQL/source configuration into the destination dashboard rather than linking the tile to a library file.

Use the sidebar and SQL separators with a pointer or arrow keys. Collapse the sidebar for more room. Drafts can be renamed, downloaded, or explicitly discarded; closing a running tab cancels its query.

## Scope

SQL work starts from a dashboard, which supplies its source and filter definitions and
the destination for new tiles. A dashboard-independent workspace with an explicit
destination picker is not implemented.

The library currently stores SQL queries. Text and governed metrics remain supported as
tiles and local drafts. Live collaboration, scheduling, data-catalog features and
cross-project library search are not part of the workspace.

## Persistence boundaries

- Browser drafts: localStorage, versioned and scoped by the resolved project/dashboard path. Results and executions stay in memory. Twenty live tabs maximum. Export is available if storage is unavailable or another window changes drafts.
- Library: `.sqldash/queries/<stable-id>.yaml` (or `queries/` in a flat project), with readable SQL, display title, source reference and required filter definitions.
- Dashboard: existing YAML `queries` plus `tile.query`; a library edit does not update these copies.

