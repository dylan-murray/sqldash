# Examples

A runnable mini-project: inline DuckDB data, no credentials, no files to download.

```bash
sqldash serve examples             # from the repo root — opens the dashboard
sqldash lint examples              # validate everything
sqldash metric query revenue -d region     # evaluate a governed metric (run inside examples/)
```

- `metrics.yaml` — the semantic layer: a `sql:` relation with inline data, plain
  metrics, a derived ratio, and a cumulative running total.
- `orders.yaml` — a dashboard where every tile is a `metric:` reference; the
  region filter binds to metric dimensions by name. No SQL in the dashboard at all.

Swap the `source:` for your warehouse and the same shapes work against real tables.

For AI Studio and custom CSS, try the separate [Studio theme showcase](studio/README.md):
three designs over the same metrics, plus a starting dashboard to edit.
