import csv
import io
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from sqldash import query_command
from sqldash.cli import app
from sqldash.scaffold import create_demo
from sqldash.semantics import SemanticError

runner = CliRunner()

SPREADSHEET_CORPUS = json.loads(
    (Path(__file__).parent / "spreadsheet_safe.json").read_text(encoding="utf-8")
)["cases"]


@pytest.fixture(scope="module")
def demo_dir(tmp_path_factory):
    path = tmp_path_factory.mktemp("cli-demo")
    create_demo(path)
    return path


@pytest.fixture(scope="module")
def multi_dir(tmp_path_factory):
    path = tmp_path_factory.mktemp("cli-multi")
    create_demo(path)
    folder = path / ".sqldash"
    shutil.copy(folder / "demo.yaml", folder / "second.yaml")
    return path


def test_dashboard_list(demo_dir):
    result = runner.invoke(app, ["dashboard", "list", str(demo_dir), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    names = [d["name"] for d in payload["dashboards"]]
    assert names == ["demo"]
    assert payload["dashboards"][0]["tiles"] > 0
    assert "revenue" in payload["dashboards"][0]["metrics_used"]


def test_dashboard_show(demo_dir):
    result = runner.invoke(app, ["dashboard", "show", "demo", "--target", str(demo_dir), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["title"]
    assert payload["source"].get("password") in (None, "•••")
    assert any(w["metric"] for w in payload["tiles"])


def test_lint_metrics_yaml_by_path(demo_dir):
    result = runner.invoke(app, ["lint", str(demo_dir / ".sqldash" / "metrics.yaml")])
    assert result.exit_code == 0, result.output
    assert "title: Field required" not in result.output
    assert "metrics.yaml" in result.output


def test_lint_invalid_utf8_file_reports_not_tracebacks(tmp_path):
    (tmp_path / "ok.yaml").write_text(
        "title: Ok\nsource: {type: duckdb, database: ':memory:'}\ntiles:\n"
        '  - title: A\n    sql: "SELECT 1 AS a"\n'
    )
    (tmp_path / "broken.yaml").write_bytes(b"title: Broken\n\xff\xfe bad\n")
    (tmp_path / "metrics.yaml").write_bytes(b"metrics:\n\xff\xfe bad\n")
    result = runner.invoke(app, ["lint", str(tmp_path)])
    assert result.exit_code == 1, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "broken.yaml" in result.output
    assert "metrics.yaml" in result.output
    assert "not valid UTF-8" in result.output
    assert "✓ ok.yaml" in result.output
    assert "3 file(s) checked — 2 error(s)" in result.output


def test_metric_list(demo_dir):
    result = runner.invoke(app, ["metric", "list", str(demo_dir), "--json"])
    assert result.exit_code == 0
    names = [m["name"] for m in json.loads(result.stdout)["metrics"]]
    assert "revenue" in names


def test_metric_show(demo_dir):
    result = runner.invoke(app, ["metric", "show", "revenue", "--target", str(demo_dir), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["expr"] == "SUM(amount)"
    assert payload["relation"] == {"table": "orders"}


def test_metric_show_text_prints_the_table_relation_by_name(demo_dir):
    result = runner.invoke(app, ["metric", "show", "revenue", "--target", str(demo_dir)])
    assert result.exit_code == 0, result.output
    assert "  relation: orders\n    table: orders\n" in result.stdout
    assert "{'table'" not in result.stdout


def test_metric_show_text_names_the_relation_apart_from_its_table(tmp_path):
    folder = tmp_path / ".sqldash"
    folder.mkdir()
    (folder / "metrics.yaml").write_text(
        "source: {type: duckdb, attach_files: true}\n"
        "relations:\n  sales: {table: orders}\n"
        "metrics:\n"
        "  revenue: {relation: sales, expr: SUM(amount)}\n"
        "  inline_revenue: {table: orders, expr: SUM(amount)}\n"
    )
    named = runner.invoke(app, ["metric", "show", "revenue", "-t", str(tmp_path)])
    assert named.exit_code == 0, named.output
    assert "  relation: sales\n    table: orders\n  dimensions:" in named.stdout
    inline = runner.invoke(app, ["metric", "show", "inline_revenue", "-t", str(tmp_path)])
    assert inline.exit_code == 0, inline.output
    assert "  expr: SUM(amount)\n  table: orders\n  dimensions:" in inline.stdout
    assert "relation:" not in inline.stdout
    as_json = runner.invoke(app, ["metric", "show", "revenue", "-t", str(tmp_path), "--json"])
    assert json.loads(as_json.stdout)["relation"] == {"table": "orders"}


def test_metric_show_text_prints_a_sql_relation_as_an_indented_block(tmp_path):
    """The text line used to be the payload dict's repr, newlines escaped."""
    sql = "SELECT order_date, region, amount\nFROM orders\nWHERE region <> 'test'\n"
    folder = tmp_path / ".sqldash"
    folder.mkdir()
    (folder / "metrics.yaml").write_text(
        "source: {type: duckdb, attach_files: true}\n"
        "relations:\n  paid_orders:\n    sql: |\n"
        + "".join(f"      {line}\n" for line in sql.splitlines())
        + "metrics:\n  revenue:\n    relation: paid_orders\n    expr: SUM(amount)\n"
    )
    text = runner.invoke(app, ["metric", "show", "revenue", "-t", str(tmp_path)])
    assert text.exit_code == 0, text.output
    assert (
        "  relation: paid_orders\n"
        "    sql:\n"
        "      SELECT order_date, region, amount\n"
        "      FROM orders\n"
        "      WHERE region <> 'test'\n"
    ) in text.stdout
    as_json = runner.invoke(app, ["metric", "show", "revenue", "-t", str(tmp_path), "--json"])
    assert as_json.exit_code == 0, as_json.output
    assert json.loads(as_json.stdout)["relation"] == {"sql": sql}


def test_metric_show_unknown(demo_dir):
    result = runner.invoke(app, ["metric", "show", "nope", "--target", str(demo_dir)])
    assert result.exit_code == 1


def test_metric_query(demo_dir):
    result = runner.invoke(
        app,
        ["metric", "query", "revenue", "--target", str(demo_dir), "-d", "region", "-f", "json"],
    )
    assert result.exit_code == 0
    rows = json.loads(result.stdout)
    assert {row["region"] for row in rows} == {"us", "eu", "apac"}


def test_metric_query_bad_dimension(demo_dir):
    result = runner.invoke(
        app,
        ["metric", "query", "revenue", "--target", str(demo_dir), "-d", "nope; DROP"],
    )
    assert result.exit_code == 1


def test_source_list(demo_dir):
    result = runner.invoke(app, ["source", "list", str(demo_dir), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert "metrics.yaml" in payload["sources"]
    assert "demo.source" in payload["sources"]


def test_source_test(demo_dir):
    result = runner.invoke(app, ["source", "test", str(demo_dir)])
    assert result.exit_code == 0
    assert "ok" in result.stdout
    assert "FAIL" not in result.stdout


def test_source_test_unknown_only(demo_dir):
    result = runner.invoke(app, ["source", "test", str(demo_dir), "--only", "nope"])
    assert result.exit_code == 1


def test_query_ambiguous_multi_dashboard(multi_dir):
    result = runner.invoke(app, ["query", str(multi_dir), "revenue_by_category"])
    assert result.exit_code == 1
    assert "--dashboard" in result.output
    assert "this project has multiple dashboards" in result.output
    assert "metrics live in repos" not in result.output


def test_query_dotted(multi_dir):
    result = runner.invoke(
        app, ["query", str(multi_dir), "second.revenue_by_category", "-f", "json"]
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout)


def test_query_dashboard_flag(multi_dir):
    result = runner.invoke(
        app,
        ["query", str(multi_dir), "revenue_by_category", "--dashboard", "demo", "-f", "json"],
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout)


def test_query_unknown_dashboard(multi_dir):
    result = runner.invoke(
        app, ["query", str(multi_dir), "revenue_by_category", "--dashboard", "nope"]
    )
    assert result.exit_code == 1
    assert "second" in result.output


def test_source_describe(demo_dir):
    result = runner.invoke(
        app, ["source", "describe", str(demo_dir), "--only", "metrics.yaml", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    orders = next(t for t in payload["tables"] if t["name"] == "orders")
    columns = {c["name"] for c in orders["columns"]}
    assert {"region", "category", "amount"} <= columns


def test_source_describe_single_source_no_flag(demo_dir):
    result = runner.invoke(app, ["source", "describe", str(demo_dir)])
    assert result.exit_code == 0, result.output
    assert "orders" in result.stdout


def test_source_describe_unknown(demo_dir):
    result = runner.invoke(app, ["source", "describe", str(demo_dir), "--only", "nope"])
    assert result.exit_code == 1


def test_version_flag_prints_package_version():
    from sqldash import __version__

    for flag in ("--version", "-V"):
        result = runner.invoke(app, [flag])
        assert result.exit_code == 0, result.output
        assert __version__ in result.output


def test_dashboard_show_missing_name_is_a_clean_error(demo_dir):
    """It used to print a full traceback, while `metric show` printed one line.

    Assert on result.exception, not on the absence of "Traceback" in the output:
    CliRunner captures the exception instead of printing it, so a crashing
    command yields empty output and a "Traceback" check passes either way.
    A clean exit raises SystemExit (typer.Exit); a crash raises the domain error.
    """
    result = runner.invoke(app, ["dashboard", "show", "no-such-dashboard", "-t", str(demo_dir)])
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit), result.exception
    assert "no dashboard named 'no-such-dashboard'" in result.output
    assert "available:" in result.output


def test_dashboard_show_malformed_file_is_a_clean_error(tmp_path):
    """The same symptom via the likelier route: `dashboard list` shows broken
    files as ERROR rows, so showing one is the natural next step."""
    from sqldash.scaffold import create_demo

    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "broken.yaml").write_text(
        "title: X\nsource: {type: duckdb\ntiles: []\n"
    )
    result = runner.invoke(app, ["dashboard", "show", "broken", "-t", str(tmp_path / ".sqldash")])
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit), result.exception
    assert "does not parse" in result.output


def test_query_honors_a_tile_source_override(tmp_path):
    """The UI and the HTTP API run a tile against its `source:`; the headless
    path ran everything against the dashboard default and silently returned
    another database's numbers."""
    import duckdb

    data = tmp_path / ".sqldash" / "data"
    data.mkdir(parents=True)
    for name, amount in (("a.db", 300.0), ("b.db", 500.0)):
        conn = duckdb.connect(str(data / name))
        conn.execute("CREATE TABLE orders (amount DOUBLE)")
        conn.execute(f"INSERT INTO orders VALUES ({amount})")
        conn.close()
    (tmp_path / ".sqldash" / "ms.yaml").write_text(
        "title: Multi\n"
        "source: {type: duckdb, database: data/a.db}\n"
        "sources:\n"
        "  src_b: {type: duckdb, database: data/b.db}\n"
        "tiles:\n"
        "  - title: From B\n"
        "    chart: big_number\n"
        "    source: src_b\n"
        '    sql: "SELECT SUM(amount) AS total FROM orders"\n'
    )
    result = runner.invoke(
        app, ["query", str(tmp_path / ".sqldash" / "ms.yaml"), "from_b", "-f", "json"]
    )
    assert result.exit_code == 0, result.output
    assert "500" in result.output, result.output
    assert "300" not in result.output, result.output


def test_query_relative_database_uses_source_base_dir_not_dashboard_dir(tmp_path):
    """CLI `query --source` used to open `app.duckdb` at the dashboard root
    when the named source had `base_dir: data/csv`. #339."""
    import duckdb

    root = tmp_path / ".sqldash"
    nested = root / "data" / "csv"
    nested.mkdir(parents=True)
    for path, value in ((nested / "app.duckdb", 42), (root / "app.duckdb", 999)):
        conn = duckdb.connect(str(path))
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute(f"INSERT INTO t VALUES ({value})")
        conn.close()
    path = root / "ms.yaml"
    path.write_text(
        "title: Nested db\n"
        "source: {type: duckdb, attach_files: true}\n"
        "sources:\n"
        "  alt:\n"
        "    type: duckdb\n"
        "    attach_files: true\n"
        "    base_dir: data/csv\n"
        "    database: app.duckdb\n"
        'queries:\n  q: "SELECT x FROM t"\n'
        "tiles: [{title: T, query: q, source: alt}]\n"
    )
    for args in (
        ["query", str(path), "q", "--source", "alt", "-f", "json"],
        ["query", str(path), "q", "-f", "json"],
    ):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.output
        assert "42" in result.output, result.output
        assert "999" not in result.output, result.output


def test_query_uses_named_source_files_dir_for_attach_files(tmp_path):
    """#257: CLI query walked the tile's named source but attached CSV from the
    dashboard directory, so a second attach_files source silently summed the
    wrong files."""
    dash_data = tmp_path / ".sqldash" / "data"
    other = tmp_path / "other" / "data"
    dash_data.mkdir(parents=True)
    other.mkdir(parents=True)
    (dash_data / "orders.csv").write_text("amount\n2.0\n")
    (other / "orders.csv").write_text("amount\n1998.0\n")
    (tmp_path / ".sqldash" / "ms.yaml").write_text(
        "title: Multi\n"
        "source: {type: duckdb, attach_files: true}\n"
        "sources:\n"
        "  alt: {type: duckdb, attach_files: true, base_dir: ../other}\n"
        "tiles:\n"
        "  - title: From alt\n"
        "    chart: big_number\n"
        "    source: alt\n"
        '    sql: "SELECT SUM(amount) AS total FROM orders"\n'
    )
    result = runner.invoke(
        app, ["query", str(tmp_path / ".sqldash" / "ms.yaml"), "from_alt", "-f", "json"]
    )
    assert result.exit_code == 0, result.output
    assert "1998" in result.output, result.output
    assert "2.0" not in result.output, result.output


def test_query_negative_row_limit_is_an_error(demo_dir):
    """A negative cap used to print '(0 rows shown, truncated)' and exit 0. #440."""
    result = runner.invoke(
        app, ["query", str(demo_dir), "demo.revenue_by_category", "--row-limit", "-1"]
    )
    assert result.exit_code == 1, result.output
    assert "row_limit must be >= 0" in result.output
    assert "truncated" not in result.output


def test_query_json_and_csv_report_truncation(demo_dir):
    """Table printed '(N rows shown, truncated)'; json/csv said nothing. #289."""
    json_run = runner.invoke(
        app, ["query", str(demo_dir), "demo.revenue_by_category", "-f", "json", "--row-limit", "1"]
    )
    csv_run = runner.invoke(
        app, ["query", str(demo_dir), "demo.revenue_by_category", "-f", "csv", "--row-limit", "1"]
    )
    assert json_run.exit_code == 0, json_run.output
    assert csv_run.exit_code == 0, csv_run.output
    assert "truncated" in (json_run.stderr or "") + json_run.output
    assert "truncated" in (csv_run.stderr or "") + csv_run.output
    assert len(json.loads(json_run.stdout)) == 1


@pytest.mark.parametrize("case", SPREADSHEET_CORPUS, ids=lambda c: repr(c["value"]))
def test_query_csv_quotes_cells_like_the_shared_corpus(case, capsys):
    """The CLI wrote its own csv raw, so `=HYPERLINK(...)` ran when `sqldash query
    -f csv > out.csv` was opened in a spreadsheet. It answers the server export's corpus."""
    result = SimpleNamespace(
        columns=[SimpleNamespace(name=case["value"] if isinstance(case["value"], str) else "c")],
        rows=[[case["value"]]],
        truncated=False,
        row_count=1,
    )
    query_command.print_result(result, "csv")
    header, row = list(csv.reader(io.StringIO(capsys.readouterr().out)))
    want = "" if case["want"] is None else str(case["want"])
    assert row == [want]
    if isinstance(case["value"], str):
        assert header == [want]


def test_query_csv_neutralizes_formula_headers_and_cells_end_to_end(tmp_path):
    (tmp_path / ".sqldash").mkdir()
    (tmp_path / ".sqldash" / "cells.yaml").write_text(
        "title: Cells\n"
        "source: {type: duckdb}\n"
        "tiles:\n"
        "  - title: Cells\n"
        "    chart: table\n"
        "    sql: >-\n"
        '      SELECT \'=HYPERLINK("http://example.invalid","x")\' AS "=head",\n'
        "      '@cmd' AS c, 'plain' AS p, -2.5::DECIMAL(10,2) AS d, 42 AS n\n"
    )
    result = runner.invoke(app, ["query", str(tmp_path), "cells.cells", "-f", "csv"])
    assert result.exit_code == 0, result.output
    assert list(csv.reader(io.StringIO(result.stdout))) == [
        ["'=head", "c", "p", "d", "n"],
        ['\'=HYPERLINK("http://example.invalid","x")', "'@cmd", "plain", "-2.50", "42"],
    ]


def test_a_metric_query_reports_the_row_cap_that_clipped_it(demo_dir):
    """The metric paths compiled the cap into the SQL as `LIMIT <row_limit>` and
    then asked the connector for the same number of rows, so `truncated` (which
    only sees rows the cap did not fit) was false for every clipped answer. The
    cap is applied once now, where the rows are fetched. Sibling of #674, which
    is the same bug on MCP with a tighter default."""
    window = ["--start", "-365d", "--end", "today"]
    full = runner.invoke(
        app, ["query", str(demo_dir), "order_count", "-g", "day", *window, "-f", "csv"]
    )
    clipped = runner.invoke(
        app,
        [
            "query",
            str(demo_dir),
            "order_count",
            "-g",
            "day",
            *window,
            "-f",
            "csv",
            "--row-limit",
            "5",
        ],
    )
    assert full.exit_code == 0, full.output
    assert clipped.exit_code == 0, clipped.output
    assert len(full.stdout.strip().splitlines()) - 1 > 5
    assert len(clipped.stdout.strip().splitlines()) - 1 == 5
    assert "truncated" in (clipped.stderr or "") + clipped.output
    assert "truncated" not in (full.stderr or "") + full.output


def test_query_unknown_format_is_an_error(demo_dir):
    """A typo'd --format used to render a table with exit 0. #292."""
    result = runner.invoke(app, ["query", str(demo_dir), "demo.revenue", "-f", "bogus"])
    assert result.exit_code == 1, result.output
    assert "unknown format" in result.output
    metric = runner.invoke(
        app, ["metric", "query", "revenue", "--target", str(demo_dir), "-f", "jso"]
    )
    assert metric.exit_code == 1, metric.output
    assert "unknown format" in metric.output


def test_query_metric_uses_dashboard_daterange_default(demo_dir):
    """Headless metric ignored the dashboard daterange default, so
    `query demo.revenue` returned all-time while the tile showed last_60_days."""
    all_time = runner.invoke(
        app, ["metric", "query", "revenue", "--target", str(demo_dir), "-f", "json"]
    )
    scoped = runner.invoke(app, ["query", str(demo_dir), "demo.revenue", "-f", "json"])
    via_flag = runner.invoke(
        app,
        [
            "metric",
            "query",
            "revenue",
            "--target",
            str(demo_dir),
            "--dashboard",
            "demo",
            "-f",
            "json",
        ],
    )
    assert all_time.exit_code == 0, all_time.output
    assert scoped.exit_code == 0, scoped.output
    assert via_flag.exit_code == 0, via_flag.output
    all_val = json.loads(all_time.stdout)[0]["revenue"]
    scoped_val = json.loads(scoped.stdout)[0]["revenue"]
    flag_val = json.loads(via_flag.stdout)[0]["revenue"]
    assert scoped_val == flag_val
    assert scoped_val < all_val


def test_query_bare_metric_ignores_an_inferred_dashboard_window(demo_dir, multi_dir):
    """A one-dashboard project auto-selected its dashboard and handed the bare
    metric that dashboard's daterange default, so `query <proj> revenue` was
    last-60-days here and all-time in the same project with a second dashboard
    added. Inferring a dashboard resolves names; it does not window them. #640"""
    bare = runner.invoke(app, ["query", str(demo_dir), "revenue", "-f", "json"])
    two_dashboards = runner.invoke(app, ["query", str(multi_dir), "revenue", "-f", "json"])
    unscoped = runner.invoke(
        app, ["metric", "query", "revenue", "--target", str(demo_dir), "-f", "json"]
    )
    assert bare.exit_code == 0, bare.output
    assert two_dashboards.exit_code == 0, two_dashboards.output
    assert unscoped.exit_code == 0, unscoped.output
    bare_val = json.loads(bare.stdout)[0]["revenue"]
    assert bare_val == json.loads(unscoped.stdout)[0]["revenue"]
    assert bare_val == json.loads(two_dashboards.stdout)[0]["revenue"]
    assert "windowed" not in (bare.stderr or "")


def test_query_names_the_window_a_dashboard_applied(demo_dir):
    """Every scoped number printed bare, so a script could not tell 60 days of
    revenue from all of it. Naming the dashboard is not naming its window. #640"""
    dotted = runner.invoke(app, ["query", str(demo_dir), "demo.revenue", "-f", "json"])
    by_flag = runner.invoke(
        app,
        [
            "metric",
            "query",
            "revenue",
            "--target",
            str(demo_dir),
            "--dashboard",
            "demo",
            "-f",
            "json",
        ],
    )
    assert dotted.exit_code == 0, dotted.output
    assert by_flag.exit_code == 0, by_flag.output
    assert "windowed" in (dotted.stderr or "")
    assert "windowed" in (by_flag.stderr or "")
    assert json.loads(dotted.stdout)[0]["revenue"] == json.loads(by_flag.stdout)[0]["revenue"]


def test_query_tile_id_keeps_the_inferred_dashboards_window(demo_dir):
    """Tile auto-selection is the convenience worth keeping: a tile id only
    exists on a dashboard, so it still runs what the tile shows. #640"""
    tile = runner.invoke(app, ["query", str(demo_dir), "total_revenue", "-f", "json"])
    bare = runner.invoke(app, ["query", str(demo_dir), "revenue", "-f", "json"])
    assert tile.exit_code == 0, tile.output
    assert bare.exit_code == 0, bare.output
    tile_val = json.loads(tile.stdout)["rows"][0]["revenue"]
    assert tile_val < json.loads(bare.stdout)[0]["revenue"]
    assert "windowed" in (tile.stderr or "")


def test_query_grainless_trailing_does_not_inherit_daterange_start(demo_dir):
    """A dashboard daterange default injected start onto a grainless windowed
    metric, which the compiler rejects. The window is as-of the end."""
    result = runner.invoke(app, ["query", str(demo_dir), "demo.trailing_28d_revenue", "-f", "json"])
    assert result.exit_code == 0, result.output
    assert "omit start" not in result.output
    row = json.loads(result.stdout)[0]
    assert next(iter(row.values())) > 0


def test_query_metric_p_overrides_daterange_without_unknown_dimension(demo_dir):
    """-p dates_start used to be re-added as a metric filter after the
    daterange consumed it, so compile_metric raised unknown filter dimension."""
    result = runner.invoke(
        app,
        ["query", str(demo_dir), "demo.revenue", "-p", "dates_start=2026-07-01", "-f", "json"],
    )
    assert result.exit_code == 0, result.output
    assert "unknown filter dimension" not in result.output
    assert json.loads(result.stdout)[0]["revenue"] > 0


def test_query_compare_returns_the_prior_window_and_delta(demo_dir):
    """Compare used to be browser-only; headless returned the main window. #275."""
    main = runner.invoke(
        app,
        [
            "query",
            str(demo_dir),
            "demo.revenue",
            "--start",
            "2026-06-27",
            "--end",
            "2026-08-26",
            "-f",
            "json",
        ],
    )
    compared = runner.invoke(
        app,
        [
            "query",
            str(demo_dir),
            "demo.revenue",
            "--start",
            "2026-06-27",
            "--end",
            "2026-08-26",
            "--compare",
            "previous_period",
            "-f",
            "json",
        ],
    )
    assert main.exit_code == 0, main.output
    assert compared.exit_code == 0, compared.output
    main_val = json.loads(main.stdout)[0]["revenue"]
    body = json.loads(compared.stdout)
    assert body["rows"][0]["revenue"] == main_val
    assert body["compare"]["mode"] == "previous_period"
    assert body["compare"]["window"] == {"start": "2026-04-27", "end": "2026-06-26"}
    delta = body["compare"]["delta"]
    assert delta["current"] == main_val
    assert delta["previous"] == body["compare"]["rows"][0]["revenue"]
    assert delta["previous"] > 0
    assert delta["pct"] == pytest.approx(
        (delta["current"] - delta["previous"]) / abs(delta["previous"])
    )


def test_query_metric_tile_id_does_not_run_a_query_named_like_the_metric(tmp_path):
    """A SQL tile titled Revenue hoists queries.revenue. Addressing the
    metric tile by id used to rewrite name to revenue then take that SQL. #275."""
    create_demo(tmp_path)
    demo = tmp_path / ".sqldash" / "demo.yaml"
    demo.write_text(
        demo.read_text()
        + "\n  - title: Revenue\n"
        + "    chart: table\n"
        + "    sql: SELECT SUM(amount) * 2 AS revenue FROM orders\n"
    )
    metric = runner.invoke(
        app,
        [
            "metric",
            "query",
            "revenue",
            "--target",
            str(tmp_path),
            "--dashboard",
            "demo",
            "--start",
            "2026-06-27",
            "--end",
            "2026-08-26",
            "-f",
            "json",
        ],
    )
    tile = runner.invoke(
        app,
        [
            "query",
            str(tmp_path),
            "demo.total_revenue",
            "--start",
            "2026-06-27",
            "--end",
            "2026-08-26",
            "-f",
            "json",
        ],
    )
    sql = runner.invoke(app, ["query", str(tmp_path), "demo.revenue", "-f", "json"])
    assert metric.exit_code == 0, metric.output
    assert tile.exit_code == 0, tile.output
    assert sql.exit_code == 0, sql.output
    metric_val = json.loads(metric.stdout)[0]["revenue"]
    assert json.loads(tile.stdout)["rows"][0]["revenue"] == metric_val
    assert json.loads(sql.stdout)[0]["revenue"] != metric_val


def test_query_metric_tile_id_honors_tile_source(tmp_path):
    """The browser sends tile.source on /api/run. query <tile-id> used to bind
    the metric against the dashboard default warehouse. #275."""
    import duckdb

    data = tmp_path / ".sqldash" / "data"
    data.mkdir(parents=True)
    for name, amount in (("a.db", 300.0), ("b.db", 500.0)):
        conn = duckdb.connect(str(data / name))
        conn.execute("CREATE TABLE orders (order_date DATE, amount DOUBLE)")
        conn.execute(f"INSERT INTO orders VALUES ('2026-07-01', {amount})")
        conn.close()
    (tmp_path / ".sqldash" / "metrics.yaml").write_text(
        "source: {type: duckdb, database: data/a.db}\n"
        "metrics:\n"
        "  revenue:\n"
        "    table: orders\n"
        "    expr: SUM(amount)\n"
        "    time_dimension: {name: order_date, grain: day}\n"
    )
    (tmp_path / ".sqldash" / "d.yaml").write_text(
        "title: D\n"
        "source: {type: duckdb, database: data/a.db}\n"
        "sources:\n"
        "  src_b: {type: duckdb, database: data/b.db}\n"
        "tiles:\n"
        "  - title: Total revenue\n"
        "    metric: revenue\n"
        "    source: src_b\n"
        "    compare: previous_period\n"
    )
    result = runner.invoke(
        app,
        [
            "query",
            str(tmp_path),
            "d.total_revenue",
            "--start",
            "2026-06-27",
            "--end",
            "2026-08-26",
            "-f",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["rows"][0]["revenue"] == 500.0


def test_query_compare_tile_id_csv_is_the_main_window(demo_dir):
    """Inherited compare: plus -f csv used to error even though the caller
    never passed --compare."""
    result = runner.invoke(
        app,
        [
            "query",
            str(demo_dir),
            "demo.total_revenue",
            "--start",
            "2026-06-27",
            "--end",
            "2026-08-26",
            "-f",
            "csv",
        ],
    )
    assert result.exit_code == 0, result.output
    # csv is the main window only: a header plus one numeric row, no compare
    # block. The exact total is date-relative (the demo CSV is generated from
    # date.today()), so assert the shape, not a magic number.
    lines = [ln for ln in result.output.splitlines() if ln.strip()]
    assert lines[0] == "revenue"
    assert len(lines) == 2
    assert float(lines[1]) > 0
    assert "compare" not in result.output.lower()


def test_query_compare_table_names_a_grouped_window(demo_dir):
    result = runner.invoke(
        app,
        [
            "query",
            str(demo_dir),
            "demo.revenue",
            "--start",
            "2026-06-27",
            "--end",
            "2026-08-26",
            "--compare",
            "previous_period",
            "-g",
            "month",
            "-f",
            "table",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "grouped" in result.output
    assert "-f json" in result.output


def test_query_compare_tile_id_applies_the_tile_compare(demo_dir):
    result = runner.invoke(
        app,
        [
            "query",
            str(demo_dir),
            "demo.total_revenue",
            "--start",
            "2026-06-27",
            "--end",
            "2026-08-26",
            "-f",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["compare"]["mode"] == "previous_period"
    assert "delta" in body["compare"]


def test_query_compare_without_a_range_is_an_error(demo_dir):
    result = runner.invoke(
        app,
        ["metric", "query", "revenue", "--target", str(demo_dir), "--compare", "yoy", "-f", "json"],
    )
    assert result.exit_code == 1, result.output
    assert "time range" in result.output
    assert "pass start/end" in result.output


@pytest.mark.parametrize(
    ("bound", "given", "missing"),
    [(["--start", "-30d"], "a start", "an end"), (["--end", "today"], "an end", "a start")],
)
def test_query_compare_with_one_side_of_the_range_names_the_missing_side(
    demo_dir, bound, given, missing
):
    """A start alone is an open-ended range with no length to shift. The error
    used to say "pass start/end" to a caller who had passed a start."""
    result = runner.invoke(
        app,
        ["metric", "query", "revenue", "-t", str(demo_dir), *bound, "--compare", "yoy"],
    )
    assert result.exit_code == 1, result.output
    assert f"got only {given}: pass {missing}, e.g." in result.output
    assert "pass start/end" not in result.output


def test_query_compare_on_grainless_window_does_not_blame_daterange(tmp_path):
    """A grainless windowed metric drops the daterange start, so compare has
    nothing to shift — even on a dashboard that already has a daterange. #479."""
    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "trailing.yaml").write_text(
        "title: Trailing\n"
        "source: {type: duckdb, attach_files: true}\n"
        "filters:\n"
        "  - {name: dates, type: daterange, default: last_60_days}\n"
        "tiles:\n"
        "  - title: Trailing compare\n"
        "    id: trailing_compare\n"
        "    metric: trailing_28d_revenue\n"
        "    compare: previous_period\n"
    )
    result = runner.invoke(
        app,
        ["query", str(tmp_path), "trailing.trailing_compare", "-f", "json"],
    )
    assert result.exit_code == 1, result.output
    assert "dashboard with a daterange filter" not in result.output
    assert "grain" in result.output or "window" in result.output


def test_query_compare_on_a_dashboard_without_a_daterange_names_the_dashboard(tmp_path):
    """#516: scoped to a dashboard with no daterange filter, the error used to
    say "run inside a dashboard with a daterange filter", and lint was clean."""
    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "bad4.yaml").write_text(
        "title: Bad four\n"
        "source: {type: duckdb, attach_files: true}\n"
        "tiles:\n"
        "  - title: T\n"
        "    metric: {name: revenue, compare: previous_period}\n"
    )
    for args in (
        [
            "metric",
            "query",
            "revenue",
            "-t",
            str(tmp_path),
            "--compare",
            "previous_period",
            "--dashboard",
            "bad4",
        ],
        ["query", str(tmp_path), "bad4.t", "-f", "json"],
    ):
        result = runner.invoke(app, args)
        assert result.exit_code == 1, (args, result.output)
        assert "the dashboard has no daterange filter" in result.output, result.output
        assert "run inside a dashboard" not in result.output, result.output
    linted = runner.invoke(app, ["lint", str(tmp_path)])
    assert linted.exit_code == 1, linted.output
    assert "no daterange filter" in linted.output, linted.output


def test_query_compare_delta_is_null_when_grained(demo_dir):
    """A month grain has no single pct — the browser overlays series, it does
    not delta the first bucket of each window. #275 review."""
    result = runner.invoke(
        app,
        [
            "query",
            str(demo_dir),
            "demo.revenue",
            "--start",
            "2026-06-27",
            "--end",
            "2026-08-26",
            "--compare",
            "previous_period",
            "-g",
            "month",
            "-f",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["compare"]["delta"] is None
    assert body["compare"]["rows"]


def test_query_compare_delta_is_null_when_grouped(demo_dir):
    result = runner.invoke(
        app,
        [
            "query",
            str(demo_dir),
            "demo.revenue",
            "--start",
            "2026-06-27",
            "--end",
            "2026-08-26",
            "--compare",
            "previous_period",
            "-d",
            "region",
            "-f",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["compare"]["delta"] is None
    assert body["compare"]["rows"]


def test_query_compare_csv_is_an_error(demo_dir):
    result = runner.invoke(
        app,
        [
            "query",
            str(demo_dir),
            "demo.revenue",
            "--start",
            "2026-06-27",
            "--end",
            "2026-08-26",
            "--compare",
            "previous_period",
            "-f",
            "csv",
        ],
    )
    assert result.exit_code == 1, result.output
    assert "csv" in result.output
    assert "--compare" in result.output


def test_query_compare_on_sql_is_an_error(demo_dir):
    result = runner.invoke(
        app,
        [
            "query",
            str(demo_dir),
            "demo.recent_orders",
            "--compare",
            "previous_period",
            "-f",
            "json",
        ],
    )
    assert result.exit_code == 1, result.output
    assert "--compare" in result.output
    assert "metric" in result.output


def test_query_metric_unknown_p_is_still_an_error(demo_dir):
    """Scoped overlay used to drop non-dimension keys, so a typo like
    `regoin` returned the unfiltered number instead of the compiler error."""
    result = runner.invoke(
        app, ["query", str(demo_dir), "demo.revenue", "-p", "regoin=us", "-f", "json"]
    )
    assert result.exit_code == 1, result.output
    assert "unknown filter dimension" in result.output
    assert "regoin" in result.output


def test_query_named_query_unknown_p_is_an_error(demo_dir):
    """#626, the residual half of #394: `prepare_sql` only ever looked up the
    names the rendered SQL mentions, so a typo on a SQL tile dropped the filter
    and returned every region as a success."""
    typo = runner.invoke(
        app, ["query", str(demo_dir), "demo.revenue_by_category", "-p", "regoin=eu", "-f", "csv"]
    )
    assert typo.exit_code == 1, typo.output
    assert "unknown parameter 'regoin'" in typo.output
    assert "valid parameters: dates, dates_end, dates_start, region" in typo.output
    good = runner.invoke(
        app, ["query", str(demo_dir), "demo.revenue_by_category", "-p", "region=eu", "-f", "csv"]
    )
    assert good.exit_code == 0, good.output
    assert "electronics" in good.output


def test_query_param_only_inside_an_untaken_block_is_still_a_real_name(tmp_path):
    """A `{{ tier }}` that lives inside `{% if region %}` is absent from the
    rendered SQL whenever the block is dropped, but it is a name this query
    defines — refusing on the rendered SQL alone would break it. #626."""
    path = tmp_path / ".sqldash" / "cond.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        "title: Conditional\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "filters:\n"
        "  - {name: region, type: select, options: [all, eu]}\n"
        "queries:\n"
        "  q: |\n"
        "    SELECT 1 AS n\n"
        "    {% if region %}WHERE {{ region }} = {{ tier }}{% endif %}\n"
        "tiles: [{title: T, query: q}]\n"
    )
    unrendered = runner.invoke(app, ["query", str(path), "q", "-p", "tier=gold", "-f", "json"])
    assert unrendered.exit_code == 0, unrendered.output
    rendered = runner.invoke(
        app, ["query", str(path), "q", "-p", "region=eu", "-p", "tier=gold", "-f", "json"]
    )
    assert rendered.exit_code == 0, rendered.output
    typo = runner.invoke(app, ["query", str(path), "q", "-p", "teir=gold", "-f", "json"])
    assert typo.exit_code == 1, typo.output
    assert "unknown parameter 'teir'" in typo.output
    assert "valid parameters: region, tier" in typo.output


def test_query_grainless_trailing_explicit_dates_start_is_an_error(demo_dir):
    """The dashboard default's start is dropped for a grainless window; an
    explicit -p dates_start must still hit the compiler's omit-start error."""
    result = runner.invoke(
        app,
        ["query", str(demo_dir), "demo.trailing_28d_revenue", "-p", "dates_start=2026-07-01"],
    )
    assert result.exit_code == 1, result.output
    assert "omit start" in result.output


def test_query_metric_p_all_does_not_bind_the_sentinel(demo_dir):
    scoped = runner.invoke(app, ["query", str(demo_dir), "demo.revenue", "-f", "json"])
    all_opt = runner.invoke(
        app, ["query", str(demo_dir), "demo.revenue", "-p", "region=all", "-f", "json"]
    )
    assert scoped.exit_code == 0, scoped.output
    assert all_opt.exit_code == 0, all_opt.output
    assert json.loads(scoped.stdout)[0]["revenue"] == json.loads(all_opt.stdout)[0]["revenue"]


def test_query_metric_path_accepts_start(demo_dir):
    """`sqldash query` used to build no time_range on its metric fallback, so
    a metric could not be filtered by time the way `metric query --start` can."""
    result = runner.invoke(app, ["query", str(demo_dir), "revenue", "--start", "-30d", "-f", "csv"])
    assert result.exit_code == 0, result.output
    assert "invalid date" not in result.output.lower(), result.output


def test_query_start_on_a_named_query_is_an_error(demo_dir):
    """--start on a dashboard query used to be dropped with no message, so
    `query demo.recent_orders --start -30d` returned the unfiltered series."""
    result = runner.invoke(
        app, ["query", str(demo_dir), "demo.revenue_by_category", "--start", "-30d"]
    )
    assert result.exit_code == 1, result.output
    assert "--start/--end apply to metrics" in result.output
    assert "revenue_by_category" in result.output


def test_query_refuses_a_write_tile(tmp_path):
    """CLI used to execute a DELETE named query the same way the dashboard did. #500."""
    import duckdb

    root = tmp_path / ".sqldash"
    root.mkdir()
    db = root / "wh.duckdb"
    conn = duckdb.connect(str(db))
    conn.execute("CREATE TABLE orders AS SELECT 1 AS n")
    conn.close()
    (root / "cleanup.yaml").write_text(
        "title: DML\n"
        "source: {type: duckdb, database: wh.duckdb}\n"
        "tiles:\n"
        "  - title: cleanup\n"
        "    chart: table\n"
        "    sql: DELETE FROM orders\n"
    )
    result = runner.invoke(app, ["query", str(tmp_path), "cleanup", "-f", "json"])
    assert result.exit_code == 1, result.output
    assert "write statement" in result.output
    conn = duckdb.connect(str(db), read_only=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
    finally:
        conn.close()


def test_query_resolves_relative_date_tokens(demo_dir):
    """README: relative dates are accepted everywhere. They were resolved only
    for a filter's declared default, so a caller passing one got the literal
    string bound into SQL."""
    for token in ("-30d", "last_30_days", "mtd", "ytd"):
        result = runner.invoke(
            app,
            ["query", str(demo_dir), "demo.revenue_by_category", "-p", f"dates_start={token}"],
        )
        assert result.exit_code == 0, f"{token}: {result.output}"
        assert "Conversion Error" not in result.output, f"{token}: {result.output}"


def test_init_creates_an_empty_project(tmp_path):
    result = runner.invoke(app, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output
    sqldash = tmp_path / ".sqldash"
    assert sqldash.is_dir()
    assert not (sqldash / "demo.yaml").exists()
    assert not (sqldash / "metrics.yaml").exists()
    assert "init --demo" in result.output
    demo_at = result.output.index("init --demo")
    setup_at = result.output.index("sqldash setup")
    assert demo_at < setup_at
    again = runner.invoke(app, ["init", str(tmp_path)])
    assert again.exit_code == 0, again.output
    assert "ready" in again.output


def test_init_unwritable_dir_says_not_writable(tmp_path):
    tmp_path.chmod(0o555)
    try:
        result = runner.invoke(app, ["init", str(tmp_path)])
    finally:
        tmp_path.chmod(0o755)
    assert result.exit_code == 1, result.output
    assert "not writable" in result.output
    assert "not a directory" not in result.output
    assert "Traceback" not in result.output


def test_init_on_a_file_is_an_error_not_a_traceback(tmp_path):
    target = tmp_path / "dash.yaml"
    target.write_text("title: x\n")
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 1, result.output
    assert "not a directory" in result.output
    assert "Traceback" not in result.output
    demo = runner.invoke(app, ["init", str(target), "--demo"])
    assert demo.exit_code == 1, demo.output
    assert "not a directory" in demo.output
    assert "Traceback" not in demo.output


def test_init_force_without_demo_is_an_error(tmp_path):
    result = runner.invoke(app, ["init", str(tmp_path), "--force"])
    assert result.exit_code == 1
    assert "--force only applies with --demo" in result.output


def test_init_demo_refuses_to_overwrite_an_existing_project(tmp_path):
    """`--demo` used to be the default and clobbered demo.yaml with no warning."""
    (tmp_path / ".sqldash").mkdir()
    mine = tmp_path / ".sqldash" / "demo.yaml"
    mine.write_text("# MY CUSTOM DASHBOARD\n")

    result = runner.invoke(app, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert mine.read_text() == "# MY CUSTOM DASHBOARD\n"

    blocked = runner.invoke(app, ["init", str(tmp_path), "--demo"])
    assert blocked.exit_code == 1
    assert "refusing to overwrite" in blocked.output
    assert mine.read_text() == "# MY CUSTOM DASHBOARD\n"

    forced = runner.invoke(app, ["init", str(tmp_path), "--demo", "--force"])
    assert forced.exit_code == 0, forced.output
    assert "MY CUSTOM DASHBOARD" not in mine.read_text()
    assert (tmp_path / ".sqldash" / "metrics.yaml").exists()


def test_init_demo_lists_agents_yaml(tmp_path):
    result = runner.invoke(app, ["init", str(tmp_path), "--demo"])
    assert result.exit_code == 0, result.output
    sqldash = tmp_path / ".sqldash"
    created = (
        sqldash / "demo.yaml",
        sqldash / "metrics.yaml",
        sqldash / "agents.yaml",
        sqldash / "data" / "orders.csv",
    )
    for path in created:
        assert path.exists(), path
        assert f"created {path}" in result.output


def test_query_reports_a_broken_dashboard_without_a_traceback(tmp_path):
    """An unknown tile source is rejected at parse time, so it surfaces from
    store.load() — the third CLI command to hit that gap."""
    (tmp_path / ".sqldash").mkdir()
    (tmp_path / ".sqldash" / "bad.yaml").write_text(
        "title: Bad\n"
        'source: {type: duckdb, database: ":memory:"}\n'
        "tiles: [{title: T, chart: big_number, source: not_defined, "
        'sql: "SELECT 1 AS n"}]\n'
    )
    result = runner.invoke(app, ["query", str(tmp_path / ".sqldash" / "bad.yaml"), "t"])
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit), result.exception
    assert "does not parse" in result.output
    assert "unknown source" in result.output


def test_query_refuses_a_shared_query_spanning_sources(tmp_path):
    """The browser runs each tile against its own source; the CLI can only run
    one, so it must not pick silently."""
    import duckdb

    data = tmp_path / ".sqldash" / "data"
    data.mkdir(parents=True)
    for name, amount in (("a.db", 300.0), ("b.db", 500.0)):
        conn = duckdb.connect(str(data / name))
        conn.execute("CREATE TABLE orders (amount DOUBLE)")
        conn.execute(f"INSERT INTO orders VALUES ({amount})")
        conn.close()
    (tmp_path / ".sqldash" / "amb.yaml").write_text(
        "title: Ambiguous\n"
        "source: {type: duckdb, database: data/a.db}\n"
        "sources:\n  src_b: {type: duckdb, database: data/b.db}\n"
        'queries:\n  shared: "SELECT SUM(amount) AS total FROM orders"\n'
        "tiles:\n"
        "  - {title: On default, chart: big_number, query: shared}\n"
        "  - {title: On B, chart: big_number, query: shared, source: src_b}\n"
    )
    path = str(tmp_path / ".sqldash" / "amb.yaml")
    result = runner.invoke(app, ["query", path, "shared"])
    assert result.exit_code == 1
    assert "different sources" in result.output

    chosen = runner.invoke(app, ["query", path, "shared", "--source", "src_b", "-f", "json"])
    assert chosen.exit_code == 0, chosen.output
    assert "500" in chosen.output

    unknown = runner.invoke(app, ["query", path, "shared", "--source", "nope"])
    assert unknown.exit_code == 1
    assert "no source named 'nope'" in unknown.output


def test_query_source_accepts_the_source_list_key(demo_dir):
    """source list prints demo.source; query --source used to only list
    metrics.yaml because the current dashboard's label is skipped in the
    picker. #421."""
    listed = runner.invoke(app, ["source", "list", str(demo_dir), "--json"])
    assert "demo.source" in json.loads(listed.stdout)["sources"]
    result = runner.invoke(
        app,
        ["query", str(demo_dir), "revenue_by_category", "--source", "demo.source", "-f", "json"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)
    aliased = runner.invoke(
        app,
        [
            "query",
            str(demo_dir),
            "revenue_by_category",
            "--source",
            "dashboard:demo",
            "-f",
            "json",
        ],
    )
    assert aliased.exit_code == 0, aliased.output
    nope = runner.invoke(app, ["query", str(demo_dir), "revenue_by_category", "--source", "nope"])
    assert nope.exit_code == 1
    assert "demo.source" in nope.output


def test_query_source_accepts_a_project_picker_key(demo_dir):
    result = runner.invoke(
        app,
        ["query", str(demo_dir), "revenue_by_category", "--source", "metrics.yaml", "-f", "json"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)


def test_query_metric_source_accepts_a_project_picker_key(demo_dir):
    result = runner.invoke(
        app, ["query", str(demo_dir), "revenue", "--source", "metrics.yaml", "-f", "json"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)


def test_source_flag_rejects_an_unknown_picker_key_on_a_metric(demo_dir):
    result = runner.invoke(app, ["query", str(demo_dir), "revenue", "--source", "bogus"])
    assert result.exit_code == 1
    assert "no source named 'bogus'" in result.output


BROKEN_SOURCE = (
    "source: {type: sqlite, path: /tmp/nope.sqlite}\n"
    "relations:\n  orders: {table: orders}\n"
    "metrics:\n  revenue: {relation: orders, expr: SUM(amount)}\n"
)


def test_source_test_fails_loudly_on_a_metrics_yaml_it_cannot_read(tmp_path):
    """Enumeration skips an unparseable metrics.yaml so one bad file cannot hide
    every other source. `source test` inherited that silence and exited 0 with
    no output at all — a "verify your connection" command reporting success for
    a project whose source config is invalid.
    """
    (tmp_path / "metrics.yaml").write_text(BROKEN_SOURCE)
    result = CliRunner().invoke(app, ["source", "test", str(tmp_path)])
    assert result.exit_code == 1, result.output
    assert "unknown source field 'path'" in result.output, result.output


@pytest.mark.parametrize("command", ["test", "describe"])
def test_source_commands_report_an_unparseable_url_without_the_password(tmp_path, command):
    (tmp_path / "metrics.yaml").write_text(
        "source: {url: 'bad url//admin:HUNTER2_URL_SECRET@host/db'}\n"
        "relations:\n  orders: {table: orders}\n"
        "metrics:\n  m1: {relation: orders, expr: SUM(1)}\n"
    )
    result = CliRunner().invoke(app, ["source", command, str(tmp_path)])
    assert result.exit_code == 1, result.output
    assert "cannot resolve dialect for source:" in result.output, result.output
    assert "HUNTER2_URL_SECRET" not in result.output


def test_source_test_fails_when_attach_base_dir_is_missing(tmp_path):
    """SELECT 1 against :memory: used to report ok for a missing scan dir. #339."""
    create_demo(tmp_path)
    demo = tmp_path / ".sqldash" / "demo.yaml"
    text = demo.read_text()
    text = text.replace(
        "source: {type: duckdb, attach_files: true}",
        "source: {type: duckdb, attach_files: true}\n"
        "sources:\n  alt:\n    type: duckdb\n    attach_files: true\n"
        "    base_dir: nonexistent\n",
        1,
    )
    demo.write_text(text)
    result = runner.invoke(app, ["source", "test", str(tmp_path)])
    assert result.exit_code == 1, result.output
    assert "nonexistent" in result.output
    assert "demo.sources.alt" in result.output


def test_source_test_fails_when_attach_dir_has_no_files(tmp_path):
    """SELECT 1 against :memory: used to report ok for an empty scan dir. #478."""
    path = tmp_path / "probe.yaml"
    path.write_text(
        "title: No files probe\n"
        "source: {type: duckdb, attach_files: true}\n"
        "tiles:\n"
        "  - title: n\n"
        "    chart: big_number\n"
        "    sql: SELECT COUNT(*) AS n FROM orders\n"
    )
    result = runner.invoke(app, ["source", "test", str(path)])
    assert result.exit_code == 1, result.output
    assert str(tmp_path) in result.output or str(tmp_path.resolve()) in result.output
    assert "0 files" in result.output
    assert "csv" in result.output
    assert "parquet" in result.output


def test_source_test_accepts_a_nested_relative_base_dir(tmp_path):
    create_demo(tmp_path)
    nested = tmp_path / ".sqldash" / "data" / "csv"
    nested.mkdir(parents=True)
    (nested / "orders.csv").write_text("n\n7\n")
    demo = tmp_path / ".sqldash" / "demo.yaml"
    text = demo.read_text()
    text = text.replace(
        "source: {type: duckdb, attach_files: true}",
        "source: {type: duckdb, attach_files: true}\n"
        "sources:\n  alt:\n    type: duckdb\n    attach_files: true\n"
        "    base_dir: data/csv\n",
        1,
    )
    demo.write_text(text)
    result = runner.invoke(app, ["source", "test", str(tmp_path), "--only", "demo.sources.alt"])
    assert result.exit_code == 0, result.output
    assert "FAIL" not in result.output


def test_source_test_fails_when_duckdb_file_is_missing(tmp_path):
    """DuckDB creates a missing database on connect, so SELECT 1 used to report ok
    against a brand-new empty file and leave it in the project. #303."""
    metrics = tmp_path / ".sqldash"
    metrics.mkdir()
    (metrics / "metrics.yaml").write_text("source: {type: duckdb, database: ghost.duckdb}\n")
    result = runner.invoke(app, ["source", "test", str(tmp_path)])
    assert result.exit_code == 1, result.output
    assert "FAIL" in result.output
    assert "database file not found: ghost.duckdb" in result.output
    assert not list(tmp_path.rglob("*.duckdb"))


def _typo_duckdb_project(tmp_path):
    folder = tmp_path / ".sqldash"
    folder.mkdir()
    (folder / "metrics.yaml").write_text(
        "source: {type: duckdb, database: typo.duckdb}\n"
        "relations:\n  orders: {table: orders}\n"
        "metrics:\n  revenue: {title: Revenue, relation: orders, expr: SUM(amount)}\n"
    )
    return folder


def test_source_describe_refuses_a_missing_duckdb_file(tmp_path):
    """describe connected first, so DuckDB created typo.duckdb and the result was a
    green empty schema while `source test` said FAIL. #515."""
    folder = _typo_duckdb_project(tmp_path)
    result = runner.invoke(app, ["source", "describe", str(tmp_path)])
    assert result.exit_code == 1, result.output
    assert "error: database file not found: typo.duckdb (resolved to " in result.output
    assert str((folder / "typo.duckdb").resolve()) in result.output
    assert not list(tmp_path.rglob("*.duckdb"))


def test_query_and_lint_refuse_a_missing_duckdb_file(tmp_path):
    """A query used to plant typo.duckdb too, after which every surface ran against
    an empty database; lint never looked. #515."""
    _typo_duckdb_project(tmp_path)
    result = runner.invoke(app, ["query", str(tmp_path), "revenue"])
    assert result.exit_code == 1, result.output
    assert "database file not found: typo.duckdb" in result.output
    assert not list(tmp_path.rglob("*.duckdb"))
    result = runner.invoke(app, ["lint", str(tmp_path)])
    assert result.exit_code == 1, result.output
    assert "database file not found: typo.duckdb" in result.output
    assert not list(tmp_path.rglob("*.duckdb"))


def test_export_context_empty_dir_is_an_error(tmp_path):
    """An empty dir used to print dialect boilerplate with zero ### sections. #442."""
    result = runner.invoke(app, ["export", "context", str(tmp_path)])
    assert result.exit_code == 1, result.output
    assert "error:" in result.output
    assert "nothing to export" in result.output
    assert "# Data context" not in result.output


def test_export_context_agents_only_project_is_not_empty(tmp_path):
    """agents.yaml with no dashboards/metrics is a real project, not 'no project'. #442."""
    agents = tmp_path / ".sqldash"
    agents.mkdir()
    (agents / "agents.yaml").write_text(
        "agents:\n"
        "  helper:\n"
        "    description: A helper agent\n"
        "    instructions: Be helpful.\n"
        "    sample_questions: [q]\n"
    )
    result = runner.invoke(app, ["export", "context", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "### `helper`" in result.output


def test_export_context_demo_still_emits_metrics(demo_dir):
    result = runner.invoke(app, ["export", "context", str(demo_dir)])
    assert result.exit_code == 0, result.output
    assert "###" in result.output
    assert "revenue" in result.output


def test_export_context_broken_metrics_is_error_not_traceback(tmp_path):
    """export context was the only export command that dumped a traceback. #335."""
    metrics = tmp_path / ".sqldash"
    metrics.mkdir()
    (metrics / "metrics.yaml").write_text(
        "source: {type: duckdb, database: ':memory:'}\n"
        "metrics:\n  revenue: {table: ORDERS, expr: SUM(amount), bogus: true}\n"
    )
    result = runner.invoke(app, ["export", "context", str(tmp_path)])
    assert result.exit_code == 1
    assert "error:" in result.output
    assert "bogus" in result.output
    assert "Traceback" not in result.output
    lookml = runner.invoke(app, ["export", "lookml", str(tmp_path)])
    assert lookml.exit_code == 1
    assert lookml.output.startswith("error:") or "error:" in lookml.output


CORTEX_VIEW = (
    "name: V\ntables:\n"
    "  - name: orders\n"
    "    base_table: {database: DB, schema: S, table: ORDERS}\n"
    "    facts: [{name: AMOUNT, expr: AMOUNT}]\n"
)
LOOKML_VIEW = "view: v {\n  sql_table_name: t ;;\n  measure: c { type: count }\n}\n"
SNOWFLAKE_METRICS = (
    "source: {type: snowflake, account: a, database: DB, schema: PUBLIC, username: u}\n"
    "metrics:\n  revenue: {table: ORDERS, expr: SUM(amount)}\n"
)
SNOWFLAKE_AGENTS = (
    "agents:\n  finance:\n    description: Finance\n    instructions: Use metrics\n"
    "    metrics: [revenue]\n    sample_questions: [What is revenue?]\n"
)


FILE_OUTPUTS = [
    ["import", "cortex", "{view}", "-o", "{out}"],
    ["import", "lookml", "{lkml}", "-o", "{out}"],
    ["export", "context", "{demo}", "-o", "{out}"],
    ["export", "lookml", "{demo}", "-o", "{out}"],
    ["export", "cortex", "{sf}", "-o", "{out}"],
    ["export", "cortex-agent", "finance", "{sf}", "-o", "{out}"],
    ["export", "cortex-agent", "finance", "{sf}", "--out", "{other}", "--view-out", "{out}"],
    ["export", "cortex-agent", "finance", "{sf}", "--view-out", "{other}", "--out", "{out}"],
]


def _invoke_with_output(argv, tmp_path, demo_dir, out):
    (tmp_path / "v.yaml").write_text(CORTEX_VIEW)
    (tmp_path / "v.view.lkml").write_text(LOOKML_VIEW)
    snowflake = tmp_path / "sf"
    snowflake.mkdir()
    (snowflake / "metrics.yaml").write_text(SNOWFLAKE_METRICS)
    (snowflake / "agents.yaml").write_text(SNOWFLAKE_AGENTS)
    values = {
        "out": str(out),
        "other": str(tmp_path / "other.yaml"),
        "view": str(tmp_path / "v.yaml"),
        "lkml": str(tmp_path / "v.view.lkml"),
        "demo": str(demo_dir),
        "sf": str(snowflake),
    }
    return runner.invoke(app, [part.format(**values) for part in argv])


@pytest.mark.parametrize("argv", [["import", "cortex", "{out}"], *FILE_OUTPUTS])
def test_a_directory_where_a_file_is_expected_is_an_error_not_a_traceback(demo_dir, tmp_path, argv):
    """A directory passed `exists()` and died in read_text/write_text with a raw
    IsADirectoryError. #509."""
    folder = tmp_path / "outdir"
    folder.mkdir()
    result = _invoke_with_output(argv, tmp_path, demo_dir, folder)
    assert result.exit_code == 1, result.output
    assert f"error: {folder} is a directory — pass a file" in result.output
    assert isinstance(result.exception, SystemExit), result.exception
    assert not (tmp_path / "other.yaml").exists()
    assert not list(folder.iterdir())


@pytest.mark.parametrize("argv", FILE_OUTPUTS)
def test_a_missing_parent_directory_is_created_like_snapshot_and_init_do(demo_dir, tmp_path, argv):
    """`-o new/dir/file` died with a raw FileNotFoundError. #509."""
    out = tmp_path / "new" / "deep" / "out.txt"
    result = _invoke_with_output(argv, tmp_path, demo_dir, out)
    assert result.exit_code == 0, result.output
    assert out.read_text()
    assert f"wrote {out}" in result.output
    assert (tmp_path / "other.yaml").exists() == ("{other}" in argv)


@pytest.mark.parametrize("argv", FILE_OUTPUTS)
def test_a_parent_that_is_a_file_is_an_error_before_anything_is_written(demo_dir, tmp_path, argv):
    """cortex-agent used to write --view-out and then die on --out. #509."""
    blocker = tmp_path / "afile"
    blocker.write_text("")
    out = blocker / "out.txt"
    result = _invoke_with_output(argv, tmp_path, demo_dir, out)
    assert result.exit_code == 1, result.output
    assert f"error: {blocker} is not a directory — cannot write {out}" in result.output
    assert isinstance(result.exception, SystemExit), result.exception
    assert not (tmp_path / "other.yaml").exists()
    assert blocker.read_text() == ""


def test_import_lookml_still_accepts_a_directory_of_views(tmp_path):
    views = tmp_path / "views"
    views.mkdir()
    (views / "v.view.lkml").write_text(LOOKML_VIEW)
    out = tmp_path / "metrics.yaml"
    result = runner.invoke(app, ["import", "lookml", str(views), "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert "metrics:" in out.read_text()


@pytest.mark.parametrize("command", ["context", "lookml"])
def test_export_to_a_file_path_still_writes_it(demo_dir, tmp_path, command):
    out = tmp_path / f"out.{command}"
    result = runner.invoke(app, ["export", command, str(demo_dir), "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert out.read_text()
    assert f"wrote {out}" in result.output


def test_source_list_says_why_a_source_is_missing(tmp_path):
    """ "(no sources)" for a project that plainly declares one is the same lie in
    a quieter voice."""
    (tmp_path / "metrics.yaml").write_text(BROKEN_SOURCE)
    result = CliRunner().invoke(app, ["source", "list", str(tmp_path)])
    assert "unknown source field 'path'" in result.output, result.output


def test_a_healthy_project_is_unaffected(tmp_path):
    create_demo(tmp_path)
    result = CliRunner().invoke(app, ["source", "test", str(tmp_path / ".sqldash")])
    assert result.exit_code == 0, result.output
    assert "FAIL" not in result.output, result.output


BROKEN_DERIVED = (
    "source: {type: duckdb, database: ':memory:'}\n"
    "relations:\n  t: {sql: 'SELECT 1 AS amount'}\n"
    "metrics:\n"
    "  revenue: {relation: t, expr: SUM(amount)}\n"
    "  broken: {derived: '{revenue} / {nope}'}\n"
)


@pytest.mark.parametrize(
    "argv",
    [
        ["metric", "list"],
        ["metric", "show", "broken", "-t"],
        ["metric", "query", "broken", "-t"],
    ],
)
def test_a_broken_metric_definition_is_an_error_not_a_traceback(tmp_path, argv):
    """`metric query` reported this as one line of text while `metric list` and
    `metric show` printed a full rich traceback for the same file. Author error
    in YAML is not an internal fault, and the three commands disagreeing about
    that is the disagreement, not the formatting.
    """
    (tmp_path / "metrics.yaml").write_text(BROKEN_DERIVED)
    result = CliRunner().invoke(app, [*argv, str(tmp_path)])
    # `exit_code == 1` and `"Traceback" not in output` both pass pre-fix: the
    # runner captures the exception, so the output is empty and the exit code is
    # 1 either way. What changed is that the error no longer escapes the command
    # (SemanticError before, a clean SystemExit now) and that it is printed.
    assert not isinstance(result.exception, SemanticError), result.exception
    assert result.exit_code == 1, result.output
    assert "unknown metric '{nope}'" in result.output, result.output


def test_a_healthy_project_still_lists_its_metrics(tmp_path):
    """The guard must not swallow the normal listing."""
    create_demo(tmp_path)
    result = CliRunner().invoke(app, ["metric", "list", str(tmp_path / ".sqldash")])
    assert result.exit_code == 0, result.output
    assert "revenue" in result.output


def test_snapshot_help_names_the_extra():
    """click/typer strips `[...]` from command docs, so `Needs the [snapshot]
    extra.` rendered as `Needs the  extra.` with no name."""
    result = runner.invoke(app, ["snapshot", "--help"])
    assert result.exit_code == 0, result.output
    assert "Needs the 'snapshot' extra" in result.output, result.output


def test_query_grouped_compare_tile_by_id_inherits_dimensions(tmp_path):
    """A tile authored with dimensions + compare must run grouped when queried
    by id — otherwise it returns one ungrouped total with a confident delta the
    browser (which renders grouped rows with no pct) never shows."""
    create_demo(tmp_path)
    folder = tmp_path / ".sqldash"
    with (folder / "demo.yaml").open("a") as f:
        f.write(
            "\n  - title: Revenue by region\n"
            "    id: rev_by_region\n"
            "    metric: {name: revenue, dimensions: [region], compare: previous_period}\n"
            "    chart: {type: bar, group_by: region}\n"
            "    size: 6x4\n"
        )
    result = runner.invoke(
        app,
        [
            "query",
            str(folder),
            "demo.rev_by_region",
            "--start",
            "2026-06-27",
            "--end",
            "2026-08-26",
            "-f",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload["rows"]) > 1
    assert all("region" in row for row in payload["rows"])
    assert payload["compare"]["delta"] is None
    assert "rows" in payload["compare"]


def test_query_metric_tile_by_id_wins_over_a_same_named_query(tmp_path):
    """Addressing a metric tile by id must return the tile's metric, never a
    query that happens to share the name — the same silent-wrong-result the
    tile-id path exists to close."""
    create_demo(tmp_path)
    folder = tmp_path / ".sqldash"
    with (folder / "demo.yaml").open("a") as f:
        f.write(
            "\n  - title: Total collide\n"
            "    id: total_collide\n"
            "    metric: revenue\n"
            "    chart: big_number\n"
            "    size: 4x2\n"
            "\nqueries:\n"
            "  total_collide: |\n"
            "    SELECT SUM(amount) * 2 AS revenue FROM orders\n"
        )
    result = runner.invoke(app, ["query", str(folder), "demo.total_collide", "-f", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    metric_total = payload[0]["revenue"]
    # the query would be SUM(amount)*2 all-time; the metric is the governed def
    plain = runner.invoke(app, ["query", str(folder), "demo.revenue", "-f", "json"])
    assert json.loads(plain.stdout)[0]["revenue"] == metric_total


def test_metric_query_refuses_an_inverted_window(demo_dir):
    """#361: `--start 2026-09-01 --end 2026-01-01` printed `null` and exited 0."""
    base = ["metric", "query", "-t", str(demo_dir / ".sqldash"), "revenue", "-f", "json"]
    inverted = runner.invoke(app, [*base, "--start", "2026-09-01", "--end", "2026-01-01"])
    assert inverted.exit_code == 1, inverted.output
    assert "date range is inverted" in inverted.output
    assert "'2026-09-01' is after end '2026-01-01'" in inverted.output
    assert "null" not in inverted.output
    tokens = runner.invoke(app, [*base, "--start", "today", "--end", "2026-01-01"])
    assert tokens.exit_code == 1, tokens.output
    assert "(from 'today' and '2026-01-01')" in tokens.output
    one_day = runner.invoke(app, [*base, "--start", "2026-01-01", "--end", "2026-01-01"])
    assert one_day.exit_code == 0, one_day.output


def test_query_refuses_an_inverted_daterange_on_sql_and_metric_tiles(demo_dir):
    """The dashboard path binds `dates_start`/`dates_end` straight into author
    SQL, so a lint-level guard on the filter would not have caught this."""
    for name in ("demo.revenue_by_category", "demo.total_revenue"):
        params = ["-p", "dates_start=2026-09-01", "-p", "dates_end=2026-01-01"]
        result = runner.invoke(app, ["query", str(demo_dir), name, *params])
        assert result.exit_code == 1, (name, result.output)
        assert "Traceback" not in result.output, result.output
        assert "dates_start '2026-09-01' is after dates_end '2026-01-01'" in result.output


def _strict_json(text):
    def refuse(constant):
        raise ValueError(f"non-finite literal {constant} is not JSON")

    return json.loads(text, parse_constant=refuse)


@pytest.fixture
def nan_dashboard(tmp_path):
    path = tmp_path / "nan.yaml"
    path.write_text(
        "title: NaN\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "tiles: []\n"
        "queries:\n"
        "  q_nan: \"SELECT 'NaN'::DOUBLE AS v, '-Infinity'::DOUBLE AS i, 1.5::DOUBLE AS ok\"\n"
    )
    return path


def test_query_json_spells_non_finite_floats_as_strict_json_text(nan_dashboard):
    """`json.dumps` defaults to allow_nan=True and wrote a bare `NaN`, which node
    rejects and jq silently turns into null (and Infinity into 1.8e308). They then
    became null, which read as SQL NULL, so they are JSON strings. #359"""
    result = runner.invoke(app, ["query", str(nan_dashboard), "q_nan", "-f", "json"])
    assert result.exit_code == 0, result.output
    assert _strict_json(result.stdout) == [{"v": "NaN", "i": "-Infinity", "ok": 1.5}]


def test_query_csv_and_table_show_non_finite_floats(nan_dashboard):
    result = runner.invoke(app, ["query", str(nan_dashboard), "q_nan", "-f", "csv"])
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == ["v,i,ok", "NaN,'-Infinity,1.5"]
    result = runner.invoke(app, ["query", str(nan_dashboard), "q_nan", "-f", "table"])
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0].split() == ["v", "i", "ok"]
    assert lines[2].split() == ["NaN", "-Infinity", "1.5"]


def test_query_table_rounds_float_noise_but_json_and_csv_keep_it(tmp_path):
    """The demo's first metric query printed 172117.81999999998 in the table."""
    path = tmp_path / "f.yaml"
    path.write_text(
        "title: F\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "tiles: []\n"
        "queries:\n"
        "  q: SELECT 0.1::DOUBLE + 0.2::DOUBLE AS s, 1.5::DOUBLE AS h, 12::BIGINT AS n\n"
    )
    table = runner.invoke(app, ["query", str(path), "q", "-f", "table"])
    assert table.exit_code == 0, table.output
    assert table.stdout.splitlines()[2].split() == ["0.3", "1.5", "12"]
    as_json = runner.invoke(app, ["query", str(path), "q", "-f", "json"])
    assert json.loads(as_json.stdout)[0]["s"] == 0.1 + 0.2
    as_csv = runner.invoke(app, ["query", str(path), "q", "-f", "csv"])
    assert as_csv.stdout.splitlines()[1].split(",")[0] == repr(0.1 + 0.2)


def test_query_csv_and_table_print_json_cells_as_one_line_json(tmp_path):
    """A list or struct cell printed as a Python repr (`[1, None]`, `{'k': 'v'}`)."""
    path = tmp_path / "j.yaml"
    path.write_text(
        "title: J\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "tiles: []\n"
        "queries:\n"
        "  q: \"SELECT [1, NULL] AS l, {'k': 'v'} AS s, 'ab'::BLOB AS b\"\n"
    )
    result = runner.invoke(app, ["query", str(path), "q", "-f", "csv"])
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == ["l,s,b", '"[1,null]","{""k"":""v""}",6162']
    result = runner.invoke(app, ["query", str(path), "q", "-f", "table"])
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[2].split("  ") == ["[1,null]", '{"k":"v"}', "6162"]


@pytest.mark.parametrize("source_type", ["", "type: duckdb, "])
def test_url_source_ignores_unused_attach_directory(tmp_path, source_type):
    path = tmp_path / "probe.yaml"
    path.write_text(
        "title: URL probe\nsource: {"
        + source_type
        + "url: 'duckdb:///:memory:', attach_files: true, base_dir: missing}\n"
        "tiles: [{title: n, sql: SELECT 7 AS n}]\n"
    )
    tested = runner.invoke(app, ["source", "test", str(path)])
    assert tested.exit_code == 0, tested.output
    linted = runner.invoke(app, ["lint", str(path)])
    assert linted.exit_code == 0, linted.output
    assert "no csv/parquet" not in linted.output
    assert "does not exist" not in linted.output
    assert "takes precedence" in linted.output


@pytest.mark.parametrize("target_kind", ["directory", "git", "workspace"])
@pytest.mark.parametrize(
    ("options", "enabled"),
    [
        ([], True),
        (["--host", "localhost"], True),
        (["--host", "::1"], True),
        (["--no-studio"], False),
        (["--studio"], True),
        (["--host", "0.0.0.0"], False),
        (["--host", "192.0.2.1", "--no-studio"], False),
    ],
)
def test_serve_studio_default_and_opt_out(demo_dir, monkeypatch, target_kind, options, enabled):
    from fastapi.testclient import TestClient

    target = [str(demo_dir)]
    dashboard = "demo"
    if target_kind == "git":
        target = ["https://github.com/example/dashboards.git"]
        monkeypatch.setattr("sqldash.gitrepo.clone_or_pull", lambda *a, **kw: demo_dir)
    elif target_kind == "workspace":
        target = ["--all"]
        dashboard = "example/demo"
        monkeypatch.setattr("sqldash.cli._resolve_all_repos", lambda **kw: [("example", demo_dir)])

    served = []

    def run(application, **kwargs):
        served.append(application)
        assert (application.state.studio is not None) is enabled
        host = kwargs["host"]
        url_host = f"[{host}]" if ":" in host else host
        assert application.state.studio is None or host in {"127.0.0.1", "localhost", "::1"}
        with TestClient(
            application,
            base_url="http://localhost",
            headers={"Host": f"{url_host}:8400"},
            client=("::1" if host == "::1" else "127.0.0.1", 1234),
        ) as client:
            page = client.get(f"/d/{dashboard}")
            assert page.status_code == 200
            assert ('id="studio-open"' in page.text) is enabled
            response = client.get("/api/studio/entrypoints")
            assert response.status_code == (403 if enabled else 404)

    monkeypatch.setattr("sqldash.cli.uvicorn.run", run)
    result = runner.invoke(app, ["serve", *target, "--no-browser", *options])
    assert result.exit_code == 0, result.output or str(result.exception)
    assert len(served) == 1


@pytest.mark.parametrize("host", ["0.0.0.0", "192.0.2.1", "::"])
def test_serve_explicit_studio_rejects_non_loopback(demo_dir, monkeypatch, host):
    def unexpected_run(*args, **kwargs):
        pytest.fail("Non-loopback Studio must be refused before starting the server")

    monkeypatch.setattr("sqldash.cli.uvicorn.run", unexpected_run)
    result = runner.invoke(
        app, ["serve", str(demo_dir), "--host", host, "--studio", "--no-browser"]
    )
    assert result.exit_code == 2
    assert "Studio requires a loopback host" in result.output


def test_metric_list_and_show_flag_a_running_total(tmp_path):
    """A running total and a trailing window share revenue's expr, so a listing
    that drops the flag prints the same row twice (#605)."""
    create_demo(tmp_path)
    target = str(tmp_path / ".sqldash")
    listed = runner.invoke(app, ["metric", "list", target])
    assert listed.exit_code == 0, listed.output
    rows = {line.split()[0]: line for line in listed.output.splitlines() if line.strip()}
    assert "cumulative: running total" in rows["cumulative_revenue"], listed.output
    assert "window: 28 days" in rows["trailing_28d_revenue"], listed.output
    assert "cumulative" not in rows["revenue"]
    assert "window" not in rows["revenue"]

    as_json = runner.invoke(app, ["metric", "list", target, "--json"])
    metrics = {m["name"]: m for m in json.loads(as_json.output)["metrics"]}
    assert metrics["cumulative_revenue"]["cumulative"] is True

    shown = runner.invoke(app, ["metric", "show", "cumulative_revenue", "-t", target])
    assert "cumulative: running total" in shown.output, shown.output
    shown = runner.invoke(app, ["metric", "show", "trailing_28d_revenue", "-t", target])
    assert "window: 28 days" in shown.output, shown.output


DEFAULTS_DASHBOARD = (
    "title: Defaults\n"
    "source: {type: duckdb, attach_files: true}\n"
    "filters:\n"
    "  - {name: dates, type: daterange, label: Date range, default: last_60_days}\n"
    "  - {name: region, type: select, label: Region, options: [us, eu, apac], default: us}\n"
    "tiles:\n"
    "  - title: Rev\n"
    "    metric: revenue\n"
    "  - title: Rev sql\n"
    "    sql: |\n"
    "      SELECT ROUND(SUM(amount), 2) AS revenue FROM orders\n"
    "      WHERE order_date BETWEEN {{ dates_start }} AND {{ dates_end }}\n"
    "        {% if region %}AND region = {{ region }}{% endif %}\n"
)

MACRO_METRIC = (
    "\n  first_week_revenue:\n"
    "    title: First week revenue\n"
    "    relation: orders\n"
    "    expr: \"SUM(CASE WHEN SQLDASH_TRUNC('week', order_date) = order_date"
    ' THEN amount ELSE 0 END)"\n'
    "    time_dimension: {name: order_date, grain: day}\n"
)


def test_named_query_says_the_dashboard_windowed_it(tmp_path):
    """#676: every dashboard-scoped form runs the daterange default, and only the
    metric path said so — a script asking for a saved query by name got a number
    bounded by an unrelated filter default with nothing on stderr."""
    create_demo(tmp_path)
    forms = (
        ["query", str(tmp_path), "demo.revenue_by_category"],
        ["query", str(tmp_path), "revenue_by_category", "--dashboard", "demo"],
        ["query", str(tmp_path / ".sqldash" / "demo.yaml"), "revenue_by_category"],
        ["query", str(tmp_path), "revenue_by_category"],
    )
    for form in forms:
        result = runner.invoke(app, [*form, "-f", "json"])
        assert result.exit_code == 0, result.output
        assert "note: windowed " in result.stderr, (form, result.stderr)
        assert "by dashboard 'demo'" in result.stderr, form
        # --start/--end are refused on this path, so the note must not offer them.
        assert "--start/--end" not in result.stderr, form
        assert "-p dates_start=" in result.stderr, form
        # The note is stderr-only: stdout stays parseable for the scripts it is for.
        json.loads(result.stdout)
        assert "note:" not in result.stdout


def test_scope_note_names_the_half_window_the_caller_did_not_pass(tmp_path):
    """One endpoint from the caller still leaves the dashboard's other endpoint in
    force. Dropping the whole clause there hid exactly the narrowing #676 is about;
    claiming `lo..hi` would credit the dashboard with the caller's own date."""
    create_demo(tmp_path)
    end_only = runner.invoke(
        app, ["query", str(tmp_path), "demo.revenue_by_category", "-p", "dates_end=2027-01-01"]
    )
    assert end_only.exit_code == 0, end_only.output
    assert "note: windowed from " in end_only.stderr, end_only.stderr
    assert "-p dates_start=<date> for your own window" in end_only.stderr
    assert "dates_end" not in end_only.stderr

    start_only = runner.invoke(
        app, ["query", str(tmp_path), "demo.revenue_by_category", "-p", "dates_start=2020-01-01"]
    )
    assert start_only.exit_code == 0, start_only.output
    assert "note: windowed to " in start_only.stderr, start_only.stderr
    assert "-p dates_end=<date> for your own window" in start_only.stderr

    metric = runner.invoke(
        app, ["query", str(tmp_path), "demo.revenue", "-p", "dates_end=2027-01-01", "-f", "json"]
    )
    assert metric.exit_code == 0, metric.output
    assert "note: windowed from " in metric.stderr, metric.stderr


def test_named_query_windowed_by_the_caller_gets_no_note(tmp_path):
    """The note reports what the dashboard supplied, never the caller's own dates."""
    create_demo(tmp_path)
    result = runner.invoke(
        app,
        [
            "query",
            str(tmp_path),
            "demo.revenue_by_category",
            "-p",
            "dates_start=2020-01-01",
            "-p",
            "dates_end=2030-01-01",
            "-f",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "note:" not in result.stderr, result.stderr


def test_scope_note_names_the_applied_filter_defaults(tmp_path):
    """#680: a dashboard-scoped metric also applies the select defaults, and the
    note named only the window — so following its --start/--end advice still
    returned one region's rows with nothing saying so."""
    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "dflt.yaml").write_text(DEFAULTS_DASHBOARD)

    scoped = runner.invoke(app, ["query", str(tmp_path), "dflt.rev", "-f", "json"])
    assert scoped.exit_code == 0, scoped.output
    assert "note: windowed " in scoped.stderr
    assert "and filtered region=us by dashboard 'dflt'" in scoped.stderr, scoped.stderr
    assert "-p region=all" in scoped.stderr

    own_window = runner.invoke(
        app,
        [
            "query",
            str(tmp_path),
            "dflt.rev",
            "--start",
            "2020-01-01",
            "--end",
            "2030-01-01",
            "-f",
            "json",
        ],
    )
    assert own_window.exit_code == 0, own_window.output
    assert "note: filtered region=us by dashboard 'dflt'" in own_window.stderr, own_window.stderr
    assert "windowed" not in own_window.stderr
    assert "-p region=all" in own_window.stderr

    cleared = runner.invoke(
        app,
        [
            "query",
            str(tmp_path),
            "dflt.rev",
            "--start",
            "2020-01-01",
            "--end",
            "2030-01-01",
            "-p",
            "region=all",
            "-f",
            "json",
        ],
    )
    assert cleared.exit_code == 0, cleared.output
    assert "note:" not in cleared.stderr, cleared.stderr

    asked = runner.invoke(
        app, ["query", str(tmp_path), "dflt.rev", "-p", "region=eu", "-f", "json"]
    )
    assert asked.exit_code == 0, asked.output
    assert "region=" not in asked.stderr, asked.stderr


def test_scope_note_wording_for_a_window_alone_is_unchanged(tmp_path):
    """docs/cli.md quotes this line verbatim; a dashboard with no select default
    must keep saying exactly what it said before #676/#680."""
    create_demo(tmp_path)
    result = runner.invoke(app, ["query", str(tmp_path), "demo.revenue", "-f", "json"])
    assert result.exit_code == 0, result.output
    note = next(line for line in result.stderr.splitlines() if line.startswith("note:"))
    assert note.endswith("by dashboard 'demo' — pass --start/--end for your own window"), note


def test_named_query_scope_note_names_a_select_default(tmp_path):
    """A named query gets the select default too, not only the window."""
    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "dflt.yaml").write_text(DEFAULTS_DASHBOARD)
    result = runner.invoke(app, ["query", str(tmp_path), "dflt.rev_sql", "-f", "json"])
    assert result.exit_code == 0, result.output
    assert "filtered region=us by dashboard 'dflt'" in result.stderr, result.stderr


def test_named_query_scope_note_sees_a_filter_that_only_gates_a_branch(tmp_path):
    """A select that decides a `{% if %}` binds no value, so reading the rendered
    SQL's params alone missed it: the clause narrowed the answer and the note said
    nothing. Found in review of the first pass at #680."""
    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "gated.yaml").write_text(
        "title: Gated\n"
        "source: {type: duckdb, attach_files: true}\n"
        "filters:\n"
        "  - {name: dates, type: daterange, default: last_60_days}\n"
        "  - {name: region, type: select, options: [us, eu, apac], default: us}\n"
        "tiles:\n"
        "  - title: Rev sql\n"
        "    sql: |\n"
        "      SELECT ROUND(SUM(amount), 2) AS revenue FROM orders\n"
        "      WHERE order_date BETWEEN {{ dates_start }} AND {{ dates_end }}\n"
        "        {% if region %}AND region = 'us'{% endif %}\n"
    )
    scoped = runner.invoke(app, ["query", str(tmp_path), "gated.rev_sql", "-f", "json"])
    assert scoped.exit_code == 0, scoped.output
    assert "filtered region=us by dashboard 'gated'" in scoped.stderr, scoped.stderr
    assert "-p region=all" in scoped.stderr
    narrowed = json.loads(scoped.stdout)[0]["revenue"]

    cleared = runner.invoke(
        app, ["query", str(tmp_path), "gated.rev_sql", "-p", "region=all", "-f", "json"]
    )
    assert cleared.exit_code == 0, cleared.output
    assert "region=" not in cleared.stderr, cleared.stderr
    assert json.loads(cleared.stdout)[0]["revenue"] > narrowed


def test_metric_show_prints_the_macro_note(tmp_path):
    """#677: the human reader is the one most likely to paste the expr, and was
    the only reader not told it will not run — --json, the metric page, MCP
    get_metric and export context all carry expr_note."""
    create_demo(tmp_path)
    metrics = tmp_path / ".sqldash" / "metrics.yaml"
    metrics.write_text(metrics.read_text() + MACRO_METRIC)
    target = str(tmp_path)

    shown = runner.invoke(app, ["metric", "show", "first_week_revenue", "-t", target])
    assert shown.exit_code == 0, shown.output
    as_json = runner.invoke(app, ["metric", "show", "first_week_revenue", "-t", target, "--json"])
    expected = json.loads(as_json.stdout)["expr_note"]
    assert f"  note: {expected}" in shown.stdout, shown.stdout

    plain = runner.invoke(app, ["metric", "show", "revenue", "-t", target])
    assert "note:" not in plain.stdout, plain.stdout


@pytest.mark.parametrize(
    "token", ["-99999999d", "-2739000y", "last_99999999999_days", "-9999999999999999999999w"]
)
def test_query_huge_relative_date_is_an_error_line_not_a_traceback(demo_dir, token):
    result = runner.invoke(app, ["query", str(demo_dir), "revenue", "--start", token])
    assert result.exit_code == 1
    assert f"unrecognized date {token!r}" in result.output
    assert not isinstance(result.exception, OverflowError)
