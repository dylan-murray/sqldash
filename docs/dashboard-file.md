# The dashboard file

One YAML file is one dashboard: the source connection, the filters, the queries, and
the tiles. This page is the reference for what goes in it. The README has the short
version.

## Layout

Tiles flow in file order, so there are no coordinates to hand-author. `size: WxH`
hints the footprint (grid units), and dragging a tile in the UI pins an exact
`position`, which is the only key that diff touches.

Tile ids derive from titles. Inline `sql:` on a tile skips the named-query
indirection; a shared `queries:` block still works when several tiles reuse one
query. `chart: area` (and the other chart types) infer their encodings from the
result columns.

## Parameters and filters

Parameters (`{{ name }}`) are bound as native query parameters, never string
interpolation. Write the placeholder unquoted: `c = {{ region }}`, not
`c = '{{ region }}'`. A quoted placeholder is refused by name, because binding cannot
reach inside a string literal and sqldash will not interpolate text into one. For a
`LIKE` pattern, build it in SQL instead: `c LIKE '%' || {{ region }} || '%'`.

Optional filters use conditional blocks. The block is included only when the filter
has a value (a select sitting on `all` counts as off), and the inner parameter is
still safely bound:

```sql
WHERE order_date BETWEEN {{ dates_start }} AND {{ dates_end }}
  {% if region %}AND region = {{ region }}{% endif %}
```

A block can have `{% elif other_param %}` branches and an `{% else %}` fallback. The
first branch whose param has a value wins, `{% else %}` runs when none of them do, and
every branch binds its own parameters the same way:

```sql
{% if region %}SELECT * FROM orders WHERE region = {{ region }}
{% elif country %}SELECT * FROM orders WHERE country = {{ country }}
{% else %}SELECT * FROM orders{% endif %}
```

The condition is always a bare parameter name: no comparisons, no `not`, no nesting one
block inside another, and no other Jinja tags. Anything else is refused by name, before
the warehouse sees it.

A `daterange` filter exposes `<name>_start` and `<name>_end`. A `select` filter can
take static `options:` or `options_sql:`.

## Chart types

`line`, `bar`, `area`, `scatter`, `pie`, `histogram`, `heatmap`, `big_number`, `table`, plus
markdown tiles (just a `markdown:` key).

### Histograms

A histogram counts how many rows fall in each range of one numeric column, so a
distribution needs no bucket SQL. The query returns the raw values:

```yaml
- title: Order values
  format: currency
  chart: {type: histogram, x: amount, bin_width: 25}
  sql: SELECT amount FROM orders
```

| Key | Meaning |
|---|---|
| `x` | The numeric column to bin. Defaults to the first numeric column. |
| `bins` | Exactly this many equal-width bins from the smallest value to the largest (1 to 200). |
| `bin_width` | Bins this wide, with edges on multiples of the width. |
| `bin_start` | Shifts the `bin_width` edges so one lands on this value. Needs `bin_width`. |
| `measure` | `count` (the default) or `percent`, each bin's share of the binned values. |

Set `bins` or `bin_width`, not both. With neither, sqldash picks a rounded width
(1, 2, 2.5 or 5 times a power of ten) for about log2(n) + 1 bins. A `bin_width`
that would need more than 200 bins over the data's range falls back to automatic
bins, and the chart says so.

Every bin includes its lower edge and excludes its upper one, except the last,
which includes both, so the largest value always has a bin. With `bin_width: 10`,
a 10 lands in 10 to 20, not 0 to 10, and negative values bin the same way
(-0.5 lands in -10 to 0). A column where every value is the same draws one bar at
that value. Nulls and values that are not finite numbers are left out, never
counted as zero, and the line above the chart says how many rows were binned and
how many were left out. The bin counts always add up to that number.

Binning happens in the browser, over the rows the query returned. When the row
cap cut the result short, the note under the chart says so, and each bar's
tooltip adds that the bins cover only those first rows, not the full distribution. To bin a table bigger than the cap, raise it with
`sqldash serve --row-limit`, or bucket in SQL and draw a `bar` chart. The CSV
download is always the raw rows, not the bins.

`bins`, `bin_width`, `bin_start` and `measure` are histogram keys, and
`sqldash lint` rejects them on any other chart type. A histogram takes no `y` or
`group_by`, since the count is its value.

### Heatmaps

A heatmap shades a grid of two categorical columns by a number, for patterns like
weekday by hour or region by product:

```yaml
- title: Orders by weekday and hour
  chart:
    type: heatmap
    x: hour
    y: weekday
    value: orders
    y_order: [Mon, Tue, Wed, Thu, Fri, Sat, Sun]
  sql: |
    SELECT dayname(ordered_at)[:3] AS weekday, hour(ordered_at) AS hour,
           COUNT(*) AS orders
    FROM orders GROUP BY 1, 2

- title: Revenue by region and category
  format: currency
  chart: {type: heatmap, x: category, y: region, value: amount, aggregate: sum}
  sql: SELECT region, category, amount FROM orders
```

| Key | Meaning |
|---|---|
| `x`, `y` | The two columns that make the grid. Default to the first text columns, then integer ones. |
| `value` | The numeric column that shades each cell. Not needed with `aggregate: count`. |
| `aggregate` | `sum`, `avg`, `count`, `min` or `max` over the rows that share a cell. |
| `palette` | `sequential` (the default, light to the tile's color) or `diverging`. |
| `midpoint` | Where a `diverging` palette turns from one color to the other. Defaults to 0. |
| `x_order`, `y_order` | Categories to put first, in this order, whether or not the result has them. Quote integers larger than 9007199254740991. |

Without `aggregate`, each cell must come from exactly one row, as a query that
already groups by `x` and `y` returns. If two rows land in one cell the heatmap
draws nothing and says how many cells have more than one row; it never keeps one
and drops the other. `sum`, `avg`, `min` and `max` skip null values, the way SQL
does, and `count` counts rows. The tooltip names the aggregation ("Sum of
amount") and how many rows made the cell.

A cell no row reaches is drawn hatched, so a missing combination never reads as
zero, and a zero is shaded like any other value. A cell whose rows are all null
is hatched too, and its tooltip says there is no value. A null `x` or `y` becomes
its own `null` category, placed last.

Categories keep the order the query returned them in, so an `ORDER BY` sets it;
numeric and date columns sort ascending, and timestamps with a UTC offset sort
by the instant they name, to the millisecond (two within the same millisecond
keep the order of their text). `x_order` and `y_order` pin any order
you want, like weekdays. A diverging palette is symmetric around its midpoint,
so the darkest color on each side means the same distance from it.

Each axis shows at most 60 categories, in that order; the line above the chart
says how many were left out, and a result cut short by the row cap gets the
tile's usual note under the chart.
Long labels are shortened on the axis and shown in full in the tooltip. Clicking
a cell does not cross-filter the dashboard: a cell is two values at once, and a
filter takes one.

`aggregate`, `palette`, `midpoint`, `x_order` and `y_order` are heatmap keys, and
`sqldash lint` rejects them on any other chart type, as it does a heatmap with
more than one `y` column.

## Reference lines

`references:` draws targets, thresholds and markers over a `line`, `bar`, `area` or
`scatter` chart. Each entry takes exactly one position:

```yaml
chart:
  type: bar
  format: currency
  references:
    - {y: 150000, label: Goal}                        # a line at a value
    - {y: [40000, 60000], label: Healthy range}       # a band between two values
    - {x: 2026-09-01, label: Pricing change}          # a marker at a date or category
    - {x: [2026-08-10, 2026-08-24], label: Promo}     # a span across the x axis
    - {metric: avg_order_value, label: Average}       # a line at a scalar metric
```

`y` is always the value axis and `x` the category or time axis, so a horizontal bar
chart draws `y` references as vertical lines. A `metric` reference runs that metric
with no dimensions under the dashboard's current filters, the way a big number would,
so the line moves when the date range or a select filter changes. It takes the bare
metric name, as a tile's `metric:` does. A metric the dashboard also shows as a big
number is queried once for both. If the warehouse refuses a metric reference, the chart
draws the rest and a note at the bottom of the tile names the reference and the error.

Optional keys on every entry:

| Key | Values | Default |
|---|---|---|
| `label` | text shown beside the line | the value, or the metric's name |
| `color` | `ink`, `muted`, `accent`, `good`, `bad`, `series-1` to `series-8` | `ink` |
| `style` | `dashed`, `solid`, `dotted` (lines and markers) | `dashed` |
| `format` | any format name or currency code | the chart's value format, or the metric's |

Colors are theme tokens rather than hex values, so a reference reads the same in the
light and dark themes and under a custom theme. Lines are drawn thin and dashed with
no points, and they never enter the legend or the tooltip, so a target does not read
as another observed series.

A value past the data widens the axis to fit it, so a goal the series has not reached
yet is still on the chart. A category marker that the result does not contain is left
off rather than drawn in the wrong place. It matches a category exactly, except that
a date finds its day on a timestamp column a bar chart draws as categories. A number
marks the category with that value, not the position. On a narrow tile a line keeps
its label and drops the number beside it. `sqldash lint` rejects references on `pie`, `histogram`, `heatmap`, `big_number` and
`table` tiles,
a metric that does not exist, a trailing-window metric on a dashboard with a date range
(a window is one value as of a day, not a value over a range), a band that is not a
pair, and an entry with no position or more than one. The chart builder has the same
controls under References, and switching to a chart type without axes sets the
references aside until you switch back.

## Relative dates

`-30d` is the same as `last_30_days`. `mtd` and `ytd` are accepted everywhere a date
is. A token names a window, so it resolves to that window's edge for the position it
is in: `--start -30d` is 30 days ago, `--end -30d` is today. End a range in the past
with an ISO date.

A token resolves against the date on the machine running sqldash, never the date on
the viewer's laptop, so a dashboard tile, the API, the CLI and MCP all run the same
window for the same token.

## Period comparison

`compare: previous_period` or `compare: yoy` on a metric tile. Big numbers grow a
delta with a direction arrow, and time-series charts overlay the prior window as a
dashed series. The prior window is the dashboard's daterange shifted back, so the
dashboard needs a daterange filter: without one `sqldash lint` errors and the tile
shows that error instead of a number with no delta. The same window math applies to
`sqldash query --compare`, `sqldash metric query --compare`, and the MCP
`query_metric(compare=)` call, and when you run the tile by id (json returns
`{rows, compare}`, csv returns the main window only). No SQL to write.

## Formatting

Locale-aware via the browser's `Intl`. `format: currency` uses the dashboard's
`currency:` (default USD). Any ISO 4217 code works (`format: EUR`,
`format: {revenue: JPY}`). A dashboard `locale:` (for example `de-DE`) overrides the
viewer's. A metric's `format:` in metrics.yaml flows through to its tiles
automatically.

## Custom CSS

An optional top-level `css: |` block styles the dashboard canvas. Use `:scope` for
the canvas itself, `.tile` for cards, and `.tile[data-tile-id="revenue"]` for one
stable tile ID. The stylesheet is scoped to `main.container`, so its selectors never
reach the app's topbar or AI Studio; page tokens at the top of the block set the
colours they are drawn with. See [custom themes](themes.md) for a complete example,
current boundaries, and three runnable designs.

## Sources

`source:` is the only connection key. It is either one connection:

```yaml
source: {type: duckdb, database: analytics.duckdb}
```

or a name per connection, which is what a dashboard reading two databases writes:

```yaml
source:
  warehouse:
    type: snowflake
    account: acme-xy12345
    default: true
  app_db:
    type: postgres
    host: db.internal
```

A tile picks one by name (`source: app_db`); a tile that names none runs against the
default. **The default is the entry marked `default: true`.** A mapping with a single
entry needs no mark. Anything else is an error — with several connections and no mark
there is nothing to guess from, and inferring one from file order would let a
reordering silently repoint every tile.

Naming the default (`source: warehouse` on a tile) is allowed and means the same as
naming nothing. Each entry of the map is a connection written as a mapping, so a
connection given as a raw URL is `events: {url: "duckdb:///events.duckdb"}` — that
is what keeps a misspelled field (`typ: duckdb`) a misspelled field instead of a
connection named "typ".

`sources:` was the older spelling of the named map, as a sibling of a single
`source:`. Files written that way still load, unchanged and without a warning — but
`source:` is what the docs teach and what the query page writes, so a new named
connection lands there.

Every major warehouse takes flat config and has an install extra
(`pip install 'sqldash[bigquery]'` and so on):

| Warehouse | Fields |
|---|---|
| Snowflake | `account`, `warehouse`, `role`, `authentication: externalbrowser`, `pat`, `keypair`, or `password`; `secondary_roles: true` to keep the user's secondary roles under `role` |
| BigQuery | `project`, `database` (dataset); auth via ADC or `options: {credentials_path: ...}` |
| Databricks | `host`, `http_path`, `token`, `catalog` and `schema` |
| Redshift | `host`, `database`, `username`, `password` |
| Athena | `host` (region), `schema`, `options: {s3_staging_dir: ...}` |
| Postgres, MySQL, Trino, ClickHouse, SQLite | `host`, `database`, `username`, `password` as the driver needs |
| DuckDB | `database` (a file or `:memory:`), `attach_files: true` to expose local CSV and Parquet files in `base_dir` as tables, `external_access: true` to let SQL read files outside the project |

DuckDB's `attach_files` scans a local directory only. Remote object storage is not
attached automatically.

### DuckDB reads only the project directory

A warehouse source is bounded by the credential it connects with. A DuckDB source
has no credential: whatever SQL it runs reads files as the user running `sqldash
serve`. Since anyone who can load a served dashboard can also type SQL into the
query workspace, sqldash confines a DuckDB source to its own directories: the folder
holding its `database:` file, and the folder its files resolve against, which is
`base_dir` when the source sets one and the dashboard's own folder otherwise.
Subdirectories of those are included. `read_text`, `read_csv`, `read_blob` and `glob`
outside them fail with a permission error naming what they are confined to.

So `database: warehouse/w.duckdb` with csvs at the project root reads both. A source
with `base_dir: ../warehouse` reads the warehouse, and a csv left beside the dashboard
is *outside* its reach: `base_dir` says where this source's files live, so move the
file there or point `base_dir` at the directory holding both.

If a project genuinely reads files outside itself (a shared drive, a `.duckdb` in
another tree, an httpfs extension), say so on the source:

```yaml
source: {type: duckdb, database: app.duckdb, external_access: true}
```

That switch is off by default and it is not narrow: it hands every viewer of every
dashboard on that source the full file access of the user running the server. Prefer
pointing `base_dir` at the directory you want read.

DuckDB gives one database file one configuration per process, so sources that share a
`.duckdb` file share their reach. Two dashboards over the same warehouse file in the
same project is the ordinary case and works, and so is a warehouse file with sibling
project directories beside it: each one's reach sits inside the other's, so neither sees
anything the other would not have seen. A source whose reach is not mutual with the one
already on the file cannot be served: a dashboard nested inside another project sharing
its warehouse file, or a mix of confined and `external_access: true`. The second one to
connect is then refused with a message naming the conflict rather than quietly taking the
first one's reach, and pointing both at the same `base_dir` is the way out.

You can always paste a raw SQLAlchemy URL instead
(`source: "trino://user@host:8080/hive"`). `sqldash lint` validates the config: typo'd
types, missing required fields, fields the dialect ignores, and which extra to install.

### A Snowflake role turns secondary roles off

Snowflake users get `DEFAULT_SECONDARY_ROLES = ALL` unless an admin changed it, and with
secondary roles active every role granted to the user contributes its privileges, not
just the primary one. Left that way, `role: REPORTING_READER` would narrow nothing: a
table only the user's owner role can read would still be readable through the source,
from the query workspace and from MCP `run_sql`.

When a source sets `role:`, or a role is picked in the query workspace, sqldash runs
`USE SECONDARY ROLES NONE` on every pooled connection it hands out, right after
`USE ROLE`, so the source gets exactly that role's grants. A source with no role keeps
the user's defaults, and the role picker says which secondary roles are active. If the
source relies on them (a primary role that only grants the warehouse, with data access
coming from other roles), keep them explicitly:

```yaml
source: {type: snowflake, account: acme-xy12345, role: ANALYST, secondary_roles: true}
```

`secondary_roles: false` turns them off even without a `role:`.

## Credential profiles

Use `${env:VAR}` references in a source block, or name a per-user profile to keep
credential values outside committed dashboard files:

```yaml
# in the dashboard
source:
  type: snowflake
  account: acme-prod
  profile: acme-prod
```

```yaml
# in ~/.config/sqldash/profiles.yaml, per teammate, not committed
acme-prod:
  username: ada@acme.com
  authentication: externalbrowser
```

Like AWS named profiles, the dashboard says which profile it needs and each teammate
defines that name locally with their own credentials. Profiles work for every
database type. `sqldash setup` writes the profile and project source and tests the
connection.

## Custom CSS

`css:` is an author stylesheet for the dashboard. It is injected scoped to the dashboard
area, so it can restyle tiles, headings and charts (`.tile[data-tile-id=revenue]` targets
one tile) but never the top bar, AI Studio or the page around the dashboard.

Anything written at page level in it means the page instead: the app's design tokens
set at the top of the block, inside `:root`, `html`, `body` or `:scope`, or a plain
`background` or `color` on `body`. Those are applied page-wide in both light and dark
mode, so the background, glow, top bar and accent follow the dashboard from edge to edge:

```yaml
css: |
  --page: #0c0918;
  --page-glow: radial-gradient(ellipse at 15% 0%, #702fc950, transparent 55%);
  --glass: #120e22d9;
  --accent: #be91ff;
  .tile { border-radius: 20px; }
```

Page tokens: `page`, `page-glow`, `surface`, `surface-raised`, `glass` (the top bar),
`ink-1`, `ink-2`, `ink-muted`, `grid-line`, `baseline`, `border`, `border-strong`,
`accent`, `accent-soft`, `accent-glow`, `accent-ink`, and `series-1` to `series-8` for
chart colours. Values are colours or gradients; anything else at page level is dropped,
`url()` and anything that could end a declaration included, and `sqldash lint` names
what was dropped. A token set inside a narrower selector, such as one tile, stays
scoped to it.

## Extras

Install only what your warehouses need: `sqldash[snowflake]`, `[postgres]`,
`[bigquery]`, `[databricks]`, `[redshift]`, `[athena]`, `[mysql]`, `[trino]`,
`[clickhouse]`, and `[lookml]` for LookML import. `sqldash[all]` exists but pulls every
driver (pyarrow, google-cloud libs, and more), which is fine for a dev box and heavy for
CI. `sqldash lint` names the exact extra to install when a dashboard needs a missing
driver.
