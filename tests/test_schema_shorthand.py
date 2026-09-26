import pytest

from sqldash.project.store import DashboardStore, InvalidDashboardError, parse_dashboard

CLEAN = """
title: Clean
source: {type: duckdb, database: ':memory:'}
metrics:
  revenue: {table: orders, expr: SUM(amount),
            time_dimension: {name: order_date}, dimensions: [{name: region}]}
tiles:
  - title: Total revenue
    metric: revenue
    size: 3x2

  - title: Daily revenue
    metric: revenue
    grain: day
    size: 6x4

  - title: By region
    chart: bar
    format: currency
    size: 6x4
    sql: |
      SELECT region, SUM(amount) AS revenue FROM orders GROUP BY 1

  - type: text
    size: 6x2
    markdown: A note.
"""


def test_ids_derived_from_titles():
    d = parse_dashboard(CLEAN)
    assert [w.id for w in d.tiles] == ["total_revenue", "daily_revenue", "by_region", "tile_4"]


def test_auto_layout_flows_in_file_order():
    d = parse_dashboard(CLEAN)
    positions = [(w.position.x, w.position.y, w.position.w, w.position.h) for w in d.tiles]
    assert positions == [(0, 0, 3, 2), (3, 0, 6, 4), (0, 4, 6, 4), (6, 4, 6, 2)]


def test_inline_sql_hoisted_to_named_query():
    d = parse_dashboard(CLEAN)
    by_region = d.tiles[2]
    assert by_region.query == "by_region"
    assert by_region.sql is None
    assert "SELECT region" in d.queries["by_region"]


def test_grain_folds_into_metric_ref():
    d = parse_dashboard(CLEAN)
    assert d.tiles[1].metric.grain == "day"
    assert d.tiles[1].grain is None


def test_chart_string_and_flat_format():
    d = parse_dashboard(CLEAN)
    assert d.tiles[2].chart.type == "bar"
    assert d.tiles[2].chart.format == "currency"


def test_format_without_chart_uses_metric_default():
    d = parse_dashboard(
        """
title: T
source: {type: duckdb}
metrics:
  revenue: {table: orders, expr: SUM(amount)}
tiles:
  - {title: Rev, metric: revenue, format: currency}
"""
    )
    assert d.tiles[0].chart.type == "big_number"
    assert d.tiles[0].chart.format == "currency"


def test_bad_size_rejected():
    with pytest.raises(InvalidDashboardError, match="size"):
        parse_dashboard(
            "title: T\nsource: {type: duckdb}\n"
            "queries: {q: SELECT 1}\ntiles: [{title: W, query: q, size: banana}]\n"
        )


def test_duplicate_titles_get_unique_ids():
    d = parse_dashboard(
        "title: T\nsource: {type: duckdb}\nqueries: {q: SELECT 1}\n"
        "tiles:\n  - {title: Same, query: q}\n  - {title: Same, query: q}\n"
    )
    assert [w.id for w in d.tiles] == ["same", "same_2"]


def test_derived_tile_id_skips_an_explicit_tile_n():
    """An untitled tile at index 1 would derive `tile_2`. If a later tile
    already authored that id, setdefault used to keep both — parse clean,
    every UI mutation 422. #183."""
    d = parse_dashboard(
        "title: Collision probe\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - {title: Revenue, sql: 'SELECT 1 AS x'}\n"
        "  - {markdown: '## notes'}\n"
        "  - {id: tile_2, title: Explicit, sql: 'SELECT 2 AS y'}\n"
    )
    assert [w.id for w in d.tiles] == ["revenue", "tile_2_2", "tile_2"]
    assert len({w.id for w in d.tiles}) == 3


def test_explicit_duplicate_ids_still_rejected():
    with pytest.raises(InvalidDashboardError, match="duplicate tile ids: tile_2"):
        parse_dashboard(
            "title: T\n"
            "source: {type: duckdb, database: ':memory:'}\n"
            "tiles:\n"
            "  - {id: tile_2, sql: 'SELECT 1 AS x'}\n"
            "  - {id: tile_2, sql: 'SELECT 2 AS y'}\n"
        )


def test_explicit_positions_still_respected():
    d = parse_dashboard(
        "title: T\nsource: {type: duckdb}\nqueries: {q: SELECT 1}\n"
        "tiles:\n"
        "  - {title: Pinned, query: q, position: {x: 6, y: 0, w: 6, h: 3}}\n"
        "  - {title: Flowed, query: q, size: 4x2}\n"
    )
    assert (d.tiles[0].position.x, d.tiles[0].position.y) == (6, 0)
    assert (d.tiles[1].position.x, d.tiles[1].position.y) == (0, 3)


def test_store_mutations_work_with_derived_ids(tmp_path):
    (tmp_path / "clean.yaml").write_text(CLEAN)
    store = DashboardStore(tmp_path)

    store.update_positions("clean", {"total_revenue": {"x": 9, "y": 0, "w": 3, "h": 2}}, None)
    text = (tmp_path / "clean.yaml").read_text()
    assert "position: {x: 9, y: 0, w: 3, h: 2}" in text
    # A titled tile derives its id from the title, so writing one back would be
    # noise. The untitled tile's `tile_4` is positional — pinned on the first
    # mutation, or the next delete renumbers it onto a different tile.
    assert "id: tile_4" in text
    for derived in ("total_revenue", "daily_revenue", "by_region"):
        assert f"id: {derived}" not in text, text

    store.delete_tile("clean", "by_region", None)
    d, _, _ = store.load("clean")
    assert "by_region" not in [w.id for w in d.tiles]

    store.upsert_tile(
        "clean",
        {
            "id": "daily_revenue",
            "title": "Daily revenue",
            "metric": {"name": "revenue", "grain": "week"},
            "position": {"x": 3, "y": 0, "w": 6, "h": 4},
            "chart": {"type": "line"},
        },
        sql=None,
        if_match=None,
    )
    d, text, _ = store.load("clean")
    weekly = next(w for w in d.tiles if w.id == "daily_revenue")
    assert weekly.metric.grain == "week"
    assert "id: daily_revenue" not in text


def test_materialized_position_replaces_size_in_place(tmp_path):
    (tmp_path / "clean.yaml").write_text(CLEAN)
    store = DashboardStore(tmp_path)
    store.update_positions(
        "clean",
        {
            "total_revenue": {"x": 9, "y": 0, "w": 3, "h": 2},
            "daily_revenue": {"x": 0, "y": 2, "w": 6, "h": 4},
        },
        None,
    )
    text = (tmp_path / "clean.yaml").read_text()
    assert "size: 3x2" not in text
    lines = [line.strip() for line in text.splitlines()]
    total_idx = lines.index("- title: Total revenue")
    assert lines[total_idx + 1] == "metric: revenue"
    assert lines[total_idx + 2] == "position: {x: 9, y: 0, w: 3, h: 2}"
    d, _, _ = store.load("clean")
    assert d.tiles[0].position.x == 9


def test_text_type_inferred_from_markdown():
    d = parse_dashboard("title: T\nsource: {type: duckdb}\ntiles:\n  - {markdown: 'A note.'}\n")
    assert d.tiles[0].type == "text"


def test_source_renamed_field_hint():
    with pytest.raises(InvalidDashboardError, match="renamed — use 'attach_files'"):
        parse_dashboard("title: T\nsource: {type: duckdb, attach_csv: true}\ntiles: []")


def test_source_nested_auth_hint():
    with pytest.raises(InvalidDashboardError, match="nested 'auth:' block is gone"):
        parse_dashboard(
            "title: T\nsource: {type: snowflake, account: a, auth: {method: password}}\ntiles: []"
        )


def test_source_unknown_field_did_you_mean():
    with pytest.raises(InvalidDashboardError, match="did you mean 'warehouse'"):
        parse_dashboard("title: T\nsource: {type: snowflake, warehous: X}\ntiles: []")


def test_tile_compare_folds_into_metric_ref():
    d = parse_dashboard(
        "title: T\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "metrics:\n"
        "  revenue: {sql: SELECT 1 AS v, expr: SUM(v)}\n"
        "tiles:\n"
        "  - title: KPI\n"
        "    metric: revenue\n"
        "    compare: previous_period\n"
    )
    assert d.tiles[0].metric.compare == "previous_period"


def test_compare_requires_metric():
    with pytest.raises(InvalidDashboardError, match="'compare' requires a 'metric'"):
        parse_dashboard(
            "title: T\n"
            "source: {type: duckdb, database: ':memory:'}\n"
            "queries: {q: SELECT 1}\n"
            "tiles:\n"
            "  - {title: X, query: q, compare: yoy}\n"
        )


def test_compare_invalid_value_rejected():
    with pytest.raises(InvalidDashboardError):
        parse_dashboard(
            "title: T\n"
            "source: {type: duckdb, database: ':memory:'}\n"
            "metrics:\n"
            "  revenue: {sql: SELECT 1 AS v, expr: SUM(v)}\n"
            "tiles:\n"
            "  - {title: X, metric: revenue, compare: last_week}\n"
        )


def test_source_options_accept_a_numeric_timeout():
    """YAML `connect_timeout: 10` is an int and used to 422 the whole file. #293."""
    dash = parse_dashboard(
        "title: T\n"
        "source:\n"
        "  type: duckdb\n"
        "  attach_files: true\n"
        "  options:\n"
        "    connect_timeout: 10\n"
        "tiles:\n"
        "  - {title: A, sql: 'SELECT 1 AS a'}\n"
    )
    assert dash.source.options["connect_timeout"] == "10"


def test_css_block_is_accepted():
    d = parse_dashboard(
        "title: T\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "css: |\n"
        "  .tile { border-radius: 0; }\n"
        "tiles: [{title: W, sql: 'SELECT 1'}]\n"
    )
    assert ".tile { border-radius: 0; }" in d.css


def test_chart_orientation_horizontal():
    d = parse_dashboard(
        "title: T\nsource: {type: duckdb, database: ':memory:'}\n"
        "queries: {q: SELECT 1}\n"
        "tiles:\n"
        "  - {title: X, query: q, chart: {type: bar, orientation: horizontal}}\n"
    )
    assert d.tiles[0].chart.orientation == "horizontal"
    with pytest.raises(InvalidDashboardError):
        parse_dashboard(
            "title: T\nsource: {type: duckdb, database: ':memory:'}\n"
            "queries: {q: SELECT 1}\n"
            "tiles:\n"
            "  - {title: X, query: q, chart: {type: bar, orientation: sideways}}\n"
        )


def test_chart_color_by_value():
    d = parse_dashboard(
        "title: T\nsource: {type: duckdb, database: ':memory:'}\n"
        "queries: {q: SELECT 1}\n"
        "tiles:\n"
        "  - {title: X, query: q, chart: {type: bar, color_by: value}}\n"
    )
    assert d.tiles[0].chart.color_by == "value"
    with pytest.raises(InvalidDashboardError):
        parse_dashboard(
            "title: T\nsource: {type: duckdb, database: ':memory:'}\n"
            "queries: {q: SELECT 1}\n"
            "tiles:\n"
            "  - {title: X, query: q, chart: {type: bar, color_by: rainbow}}\n"
        )


def test_a_markdown_tile_defaults_to_a_band_not_a_chart_block(tmp_path):
    """A text tile is nearly always a section heading or a caption. Defaulting
    it to the chart footprint reserved 320px of grid for one line of prose."""
    d = parse_dashboard(
        "title: T\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - {type: text, markdown: '## Section'}\n"
        "  - {title: A, sql: 'SELECT 1 AS v'}\n"
    )
    assert (d.tiles[0].position.w, d.tiles[0].position.h) == (12, 1)
    assert (d.tiles[1].position.w, d.tiles[1].position.h) == (6, 4)


def test_an_explicit_size_still_wins_for_text(tmp_path):
    d = parse_dashboard(
        "title: T\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - {type: text, markdown: '## Section', size: 6x3}\n"
    )
    assert (d.tiles[0].position.w, d.tiles[0].position.h) == (6, 3)
