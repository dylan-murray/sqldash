from pathlib import Path

import pytest

from sqldash.connectors.base import ConnectorError, TableInfo
from sqldash.connectors.engine import EngineConnector
from sqldash.lint import lint_project
from sqldash.models.results import QueryResult
from sqldash.project.store import DashboardStore
from sqldash.scaffold import create_demo
from sqldash.semantics import SemanticLayer


def lint(tmp_path):
    store = DashboardStore(tmp_path)
    return lint_project(store, SemanticLayer(store))


def test_scaffold_demo_is_lint_clean(tmp_path):
    create_demo(tmp_path)
    findings = lint(tmp_path)
    assert findings == [], [f"{f.level}: {f.message}" for f in findings]


def test_linting_metrics_yaml_by_path_is_not_a_dashboard_error(tmp_path):
    create_demo(tmp_path)
    path = tmp_path / ".sqldash" / "metrics.yaml"
    store = DashboardStore(path)
    findings = lint_project(store, SemanticLayer(store))
    assert findings == [], [f"{f.level}: {f.file}: {f.message}" for f in findings]


def test_examples_lint_clean():
    """CI runs `sqldash lint examples --strict`. A new warning that the
    example dashboards trip fails the job (exit 1) after pytest is green."""
    root = Path(__file__).resolve().parents[1] / "examples"
    store = DashboardStore(root)
    findings = lint_project(store, SemanticLayer(store))
    assert findings == [], [f"{f.level}: {f.file}: {f.message}" for f in findings]


def test_write_tile_sql_is_an_error(tmp_path):
    """Viewing the page used to run a DELETE tile; lint was green. #500."""
    (tmp_path / "cleanup.yaml").write_text(
        "title: DML check\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - title: cleanup\n"
        "    chart: table\n"
        "    sql: |\n"
        "      DELETE FROM orders WHERE region = 'us'\n"
    )
    findings = lint(tmp_path)
    assert any(
        f.level == "error" and f.message == "tile 'cleanup' is a write statement" for f in findings
    )


def test_explain_analyze_delete_tile_is_a_write_statement(tmp_path):
    """DuckDB EXPLAIN ANALYZE DELETE used to pass opener-only and wipe rows. #500."""
    (tmp_path / "cleanup.yaml").write_text(
        "title: DML check\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - title: cleanup\n"
        "    chart: table\n"
        "    sql: EXPLAIN ANALYZE DELETE FROM orders\n"
    )
    findings = lint(tmp_path)
    assert any(
        f.level == "error" and f.message == "tile 'cleanup' is a write statement" for f in findings
    )


def test_use_statements_in_authored_sql_are_errors(tmp_path):
    """Lint was clean while a USE SCHEMA query moved the pooled session."""
    (tmp_path / "d.yaml").write_text(
        "title: T\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "queries:\n"
        "  hop: USE SCHEMA other\n"
        "tiles:\n"
        "  - title: roles\n"
        "    chart: table\n"
        "    sql: USE SECONDARY ROLES NONE\n"
    )
    errors = {f.message for f in lint(tmp_path) if f.level == "error"}
    suffix = "changes session state that later queries on the pooled connection would inherit"
    assert f"query 'hop' {suffix}; USE statements are refused" in errors
    assert f"tile 'roles' {suffix}; USE statements are refused" in errors


def test_replace_and_vacuum_tiles_are_errors(tmp_path):
    """Both wrote a sqlite source on every page view while lint reported clean."""
    (tmp_path / "d.yaml").write_text(
        "title: T\n"
        "source: {type: sqlite, database: data.db}\n"
        "tiles:\n"
        "  - title: replace a row\n"
        "    chart: table\n"
        "    sql: REPLACE INTO t VALUES (1, 'x')\n"
        "  - title: reclaim\n"
        "    chart: table\n"
        "    sql: VACUUM\n"
    )
    errors = {f.message for f in lint(tmp_path) if f.level == "error"}
    assert "tile 'replace_a_row' is a write statement" in errors
    assert "tile 'reclaim' is a write statement" in errors


def test_copy_column_tile_is_not_a_write_statement(tmp_path):
    """The body-keyword scan would refuse `AS copy`; opener-only must not. #500."""
    (tmp_path / "d.yaml").write_text(
        "title: T\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - title: Copy col\n"
        "    chart: table\n"
        '    sql: "SELECT k AS copy, v FROM orders"\n'
    )
    findings = lint(tmp_path)
    assert not any("write statement" in f.message for f in findings)


def test_unused_write_query_is_an_error(tmp_path):
    (tmp_path / "d.yaml").write_text(
        "title: T\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "queries:\n"
        "  cleanup: DELETE FROM orders\n"
        "tiles: []\n"
    )
    findings = lint(tmp_path)
    assert any(
        f.level == "error" and f.message == "query 'cleanup' is a write statement" for f in findings
    )


def test_if_only_filter_is_not_unused(tmp_path):
    (tmp_path / "d.yaml").write_text(
        "title: T\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "filters:\n"
        "  - {name: mode, type: select, default: all, options: [all, strict]}\n"
        "queries:\n"
        '  q: "SELECT 1 {% if mode %}WHERE x = 1{% endif %}"\n'
        "tiles: [{title: t, query: q}]\n"
    )
    findings = lint(tmp_path)
    assert not any("not used" in f.message for f in findings)


def test_select_default_not_in_options_is_an_error(tmp_path):
    (tmp_path / "d.yaml").write_text(
        "title: T\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "filters:\n"
        "  - {name: region, type: select, default: uk, options: [us, eu]}\n"
        "tiles: []\n"
    )
    findings = lint(tmp_path)
    assert any(
        f.level == "error" and "default 'uk' is not in options" in f.message for f in findings
    )


def test_select_default_all_omitted_from_options_is_clean(tmp_path):
    """#216: all is the off sentinel; omitting it from options is not an error."""
    (tmp_path / "d.yaml").write_text(
        "title: T\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "filters:\n"
        "  - {name: region, type: select, default: all, options: [us, eu]}\n"
        "tiles: []\n"
    )
    findings = lint(tmp_path)
    assert not any("default" in f.message and f.level == "error" for f in findings)


def test_select_default_all_without_options_is_not_an_empty_dropdown(tmp_path):
    """select_choices injects `all`, so the empty-options warning does not apply."""
    (tmp_path / "d.yaml").write_text(
        "title: T\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "filters:\n"
        "  - {name: region, type: select, default: all}\n"
        "tiles: []\n"
    )
    findings = lint(tmp_path)
    assert not any("dropdown will be empty" in f.message for f in findings)


def test_parse_error_reported(tmp_path):
    (tmp_path / "broken.yaml").write_text("title: Broken\n")
    findings = lint(tmp_path)
    assert any(f.level == "error" and f.file == "broken.yaml" for f in findings)


def test_tile_level_dimensions_names_the_metric_shape(tmp_path):
    """tile-level dimensions: used to lint as Extra inputs are not permitted
    with no hint that they belong inside metric:. #480."""
    (tmp_path / "d.yaml").write_text(
        "title: D\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "metrics:\n"
        "  revenue: {table: orders, expr: SUM(amount), dimensions: [{name: region}]}\n"
        "tiles:\n"
        "  - title: By region\n"
        "    metric: revenue\n"
        "    dimensions: [region]\n"
    )
    findings = lint(tmp_path)
    assert any(
        f.level == "error"
        and "dimensions" in f.message
        and "metric: {name, dimensions, grain}" in f.message
        for f in findings
    ), [f.message for f in findings]


def test_chart_dimensions_does_not_get_the_metric_hint(tmp_path):
    """tiles.N.chart.dimensions is not the tile-level key. #480."""
    (tmp_path / "d.yaml").write_text(
        "title: D\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - title: T\n"
        "    sql: SELECT 1 AS n\n"
        "    chart: {type: bar, dimensions: [n]}\n"
    )
    findings = lint(tmp_path)
    chart = [f.message for f in findings if f.level == "error" and "dimensions" in f.message]
    assert chart, [f.message for f in findings]
    assert not any("metric: {name, dimensions, grain}" in m for m in chart), chart


def test_unknown_metric_is_error(tmp_path):
    (tmp_path / "d.yaml").write_text(
        "title: D\nsource: {type: duckdb}\ntiles: [{title: W, metric: ghost}]\n"
    )
    findings = lint(tmp_path)
    assert any("unknown metric 'ghost'" in f.message and f.level == "error" for f in findings)


def test_bad_metric_dimension_is_error(tmp_path):
    (tmp_path / "d.yaml").write_text(
        """
title: D
source: {type: duckdb}
metrics:
  revenue: {table: orders, expr: SUM(amount), dimensions: [{name: region}]}
tiles:
  - {title: W, metric: {name: revenue, dimensions: [nope]}}
"""
    )
    findings = lint(tmp_path)
    assert any("no dimension 'nope'" in f.message and f.level == "error" for f in findings)


def test_grain_without_time_dimension_is_error(tmp_path):
    (tmp_path / "d.yaml").write_text(
        """
title: D
source: {type: duckdb}
metrics:
  revenue: {table: orders, expr: SUM(amount)}
tiles:
  - {title: W, metric: revenue, grain: day}
"""
    )
    findings = lint(tmp_path)
    assert any("no time_dimension" in f.message and f.level == "error" for f in findings)


def test_unbound_param_is_warning(tmp_path):
    (tmp_path / "d.yaml").write_text(
        "title: D\nsource: {type: duckdb}\ntiles: [{title: W, sql: 'SELECT {{ mystery }}'}]\n"
    )
    findings = lint(tmp_path)
    assert any("mystery" in f.message and f.level == "warning" for f in findings)


def test_unused_query_and_filter_are_warnings(tmp_path):
    (tmp_path / "d.yaml").write_text(
        """
title: D
source: {type: duckdb}
filters:
  - {name: ghost_filter, type: text}
queries:
  used: SELECT 1
  orphan: SELECT 2
tiles:
  - {title: W, query: used}
"""
    )
    findings = lint(tmp_path)
    messages = [f.message for f in findings if f.level == "warning"]
    assert any("orphan" in m for m in messages)
    assert any("ghost_filter" in m for m in messages)


def test_broken_metrics_yaml_is_error(tmp_path):
    (tmp_path / "metrics.yaml").write_text("source: {type: duckdb}\nmetrics:\n  bad: {expr: X}\n")
    findings = lint(tmp_path)
    assert any(f.file == "metrics.yaml" and f.level == "error" for f in findings)


def _source_findings(tmp_path, source_yaml):
    (tmp_path / "d.yaml").write_text(
        f"title: D\nsource:\n{source_yaml}\ntiles: [{{title: W, sql: 'SELECT 1'}}]\n"
    )
    return lint(tmp_path)


def test_typo_database_type_suggests(tmp_path):
    findings = _source_findings(tmp_path, "  type: postgress\n  database: x")
    assert any(f.level == "error" and "did you mean 'postgres'" in f.message for f in findings)


def test_unknown_type_is_error(tmp_path):
    findings = _source_findings(tmp_path, "  type: frobnicator\n  database: x")
    assert any(f.level == "error" and "unknown database type" in f.message for f in findings)


def _force_missing_dialect(monkeypatch):
    from sqlalchemy.exc import NoSuchModuleError

    def fake_create_engine(url, *args, **kwargs):
        raise NoSuchModuleError(f"Can't load plugin: sqlalchemy.dialects:{str(url).split(':')[0]}")

    monkeypatch.setattr("sqldash.lint.create_engine", fake_create_engine)


def test_known_external_dialect_warns_install(tmp_path, monkeypatch):
    _force_missing_dialect(monkeypatch)
    findings = _source_findings(tmp_path, "  type: trino\n  host: h\n  database: hive")
    assert any(f.level == "warning" and "dialect package installed" in f.message for f in findings)
    assert not any(f.level == "error" for f in findings)


def test_snowflake_requires_account(tmp_path):
    findings = _source_findings(tmp_path, "  type: snowflake\n  username: u")
    assert any(f.level == "error" and "'account'" in f.message for f in findings)


def test_plaintext_password_is_a_lint_warning(tmp_path):
    findings = _source_findings(
        tmp_path, "  type: postgres\n  host: h\n  database: d\n  password: SUPERSECRET\n"
    )
    assert any(
        f.level == "warning" and "plaintext secret" in f.message and "password" in f.message
        for f in findings
    )


def test_env_ref_password_is_not_a_plaintext_warning(tmp_path):
    findings = _source_findings(
        tmp_path,
        "  type: postgres\n  host: h\n  database: d\n  password: ${env:PGPASSWORD}\n",
    )
    assert not any("plaintext secret" in f.message for f in findings)


def test_plaintext_password_in_url_is_a_lint_warning(tmp_path):
    findings = _source_findings(tmp_path, "  url: 'postgres://u:hunter2@localhost/db'\n")
    assert any(f.level == "warning" and "url password" in f.message for f in findings)


def test_env_ref_password_in_url_is_not_a_plaintext_warning(tmp_path):
    findings = _source_findings(tmp_path, "  url: 'postgres://u:${env:PGPASSWORD}@localhost/db'\n")
    assert not any("plaintext secret" in f.message for f in findings)


def test_plaintext_token_and_passphrase_are_lint_warnings(tmp_path):
    findings = _source_findings(
        tmp_path,
        "  type: snowflake\n  account: a\n  token: s3cret\n  private_key_passphrase: hunter2\n",
    )
    warned = {
        f.message for f in findings if f.level == "warning" and "plaintext secret" in f.message
    }
    assert any("token" in m for m in warned), warned
    assert any("private_key_passphrase" in m for m in warned), warned


def test_username_is_not_a_plaintext_secret_warning(tmp_path):
    findings = _source_findings(
        tmp_path, "  type: postgres\n  host: h\n  database: d\n  username: ada\n"
    )
    assert not any("plaintext secret" in f.message for f in findings)


def test_plaintext_url_password_next_to_env_host_is_a_lint_warning(tmp_path):
    """make_url cannot parse ${env:HOST}; substituting first is what the dialect probe does."""
    findings = _source_findings(
        tmp_path, "  url: 'postgres://${env:USER}:hunter2@${env:HOST}/db'\n"
    )
    assert any(
        f.level == "warning" and "url password" in f.message and "plaintext secret" in f.message
        for f in findings
    )


def test_plaintext_password_in_url_query_is_a_lint_warning(tmp_path):
    findings = _source_findings(
        tmp_path, "  url: 'postgres://u:${env:PGPASSWORD}@localhost/db?password=hunter2'\n"
    )
    assert any("url query password" in f.message for f in findings)


def test_url_query_sslmode_is_not_a_plaintext_warning(tmp_path):
    findings = _source_findings(
        tmp_path, "  url: 'postgres://u:${env:PGPASSWORD}@localhost/db?sslmode=require'\n"
    )
    assert not any("plaintext secret" in f.message for f in findings)


def test_plaintext_token_in_connect_args_is_a_lint_warning(tmp_path):
    findings = _source_findings(
        tmp_path,
        "  type: postgres\n  host: h\n  database: d\n  connect_args: {token: supersecret}\n",
    )
    assert any("connect_args.token" in f.message for f in findings)


def test_bigquery_credentials_path_is_not_a_plaintext_warning(tmp_path):
    findings = _source_findings(
        tmp_path,
        "  type: bigquery\n  project: p\n  options: {credentials_path: /tmp/sa.json}\n",
    )
    assert not any(
        "credentials_path" in f.message and "plaintext secret" in f.message for f in findings
    )


def test_numeric_token_in_connect_args_is_a_lint_warning(tmp_path):
    findings = _source_findings(
        tmp_path,
        "  type: postgres\n  host: h\n  database: d\n  connect_args: {token: 12345}\n",
    )
    assert any("connect_args.token" in f.message for f in findings)


def test_plaintext_api_key_in_url_query_is_a_lint_warning(tmp_path):
    findings = _source_findings(tmp_path, "  url: 'postgres://u@localhost/db?api_key=hunter2'\n")
    assert any("url query api_key" in f.message or "api_key" in f.message for f in findings)


def test_plaintext_passwd_in_connect_args_is_a_lint_warning(tmp_path):
    findings = _source_findings(
        tmp_path,
        "  type: postgres\n  host: h\n  database: d\n  connect_args: {passwd: hunter2}\n",
    )
    assert any("connect_args.passwd" in f.message for f in findings)


def test_plaintext_password_on_duckdb_is_not_also_an_ignored_field(tmp_path):
    """A literal secret is the finding. Repeating it as 'ignored' is noise."""
    findings = _source_findings(tmp_path, "  type: duckdb\n  password: hunter2\n")
    assert any("plaintext secret" in f.message and "password" in f.message for f in findings)
    assert not any("ignored" in f.message and "password" in f.message for f in findings)


def test_irrelevant_fields_warn(tmp_path):
    findings = _source_findings(
        tmp_path, "  type: duckdb\n  attach_files: true\n  warehouse: WH\n  username: u"
    )
    messages = [f.message for f in findings if f.level == "warning"]
    assert any("warehouse" in m and "username" in m for m in messages)


def test_url_ignores_other_fields_warns(tmp_path):
    findings = _source_findings(tmp_path, "  url: 'sqlite:///x.db'\n  host: h\n  type: sqlite")
    assert any("'url' takes precedence" in f.message and "host" in f.message for f in findings)


def test_bad_url_is_error(tmp_path):
    findings = _source_findings(tmp_path, "  url: 'not a url at all'")
    assert any(f.level == "error" and "does not parse" in f.message for f in findings)


def test_env_refs_in_url_do_not_require_env(tmp_path, monkeypatch):
    monkeypatch.delenv("LINT_DB_PW", raising=False)
    findings = _source_findings(tmp_path, "  url: 'sqlite:///${env:LINT_DB_PW}.db'")
    assert not any(f.level == "error" for f in findings)


def test_named_sources_are_linted(tmp_path):
    (tmp_path / "d.yaml").write_text(
        "title: D\nsource: {type: duckdb}\n"
        "sources:\n  other: {type: postgress, database: x}\n"
        "tiles: [{title: W, sql: 'SELECT 1'}]\n"
    )
    findings = lint(tmp_path)
    assert any("sources.other" in f.message and "did you mean" in f.message for f in findings)


def test_bigquery_requires_project(tmp_path):
    findings = _source_findings(tmp_path, "  type: bigquery")
    assert any(f.level == "error" and "'project'" in f.message for f in findings)


def test_databricks_requirements(tmp_path):
    findings = _source_findings(tmp_path, "  type: databricks\n  host: dbx.cloud")
    messages = [f.message for f in findings if f.level == "error"]
    assert any("http_path" in m for m in messages)
    assert any("token" in m for m in messages)


def test_warehouse_install_hint_names_extra(tmp_path, monkeypatch):
    _force_missing_dialect(monkeypatch)
    findings = _source_findings(tmp_path, "  type: bigquery\n  project: my-proj")
    assert any("sqldash[bigquery]" in f.message and f.level == "warning" for f in findings)


def test_lint_catches_unsupported_template_tags(tmp_path):
    (tmp_path / "bad.yaml").write_text(
        "title: T\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "filters:\n"
        "  - {name: model, type: select, default: all, options: [all, a, b]}\n"
        "tiles:\n"
        "  - title: X\n"
        "    sql: \"SELECT 1 {% if model != 'all' %}WHERE m = {{ model }}{% endif %}\"\n"
    )
    from sqldash.project.store import DashboardStore
    from sqldash.semantics import SemanticLayer

    store = DashboardStore(tmp_path)
    findings = lint_project(store, SemanticLayer(store))
    errors = [f for f in findings if f.level == "error"]
    assert any("unsupported template tag" in f.message for f in errors)
    assert any("no comparison needed" in f.message for f in errors)


def _branching_findings(tmp_path, else_body):
    (tmp_path / "cond.yaml").write_text(
        "title: T\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "filters:\n"
        "  - {name: region, type: select, options: [all, eu]}\n"
        "queries:\n"
        '  q: "{% if region %}SELECT 1 AS n{% else %}' + else_body + '{% endif %}"\n'
        "tiles:\n"
        "  - {title: X, query: q}\n"
    )
    from sqldash.project.store import DashboardStore
    from sqldash.semantics import SemanticLayer

    store = DashboardStore(tmp_path)
    return lint_project(store, SemanticLayer(store))


def test_lint_accepts_else_blocks(tmp_path):
    """#657: `{% else %}` linted as a nesting error."""
    assert not [f for f in _branching_findings(tmp_path, "SELECT 2 AS n") if f.level == "error"]


def test_lint_names_the_tag_that_has_no_if(tmp_path):
    findings = _branching_findings(tmp_path, "SELECT 2 AS n{% endif %}")
    assert any(
        f.level == "error" and "{% endif %} has no {% if param %} to belong to" in f.message
        for f in findings
    )


def test_lint_warns_on_non_additive_cumulative(tmp_path):
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: duckdb, database: ':memory:'}\n"
        "relations:\n"
        "  ev: {sql: \"SELECT 1 AS amount, DATE '2026-01-01' AS day\"}\n"
        "metrics:\n"
        "  bad_running_avg:\n"
        "    relation: ev\n"
        "    expr: AVG(amount)\n"
        "    cumulative: true\n"
        "    time_dimension: {name: day, grain: day}\n"
        "  good_running_total:\n"
        "    relation: ev\n"
        "    expr: SUM(amount)\n"
        "    cumulative: true\n"
        "    time_dimension: {name: day, grain: day}\n"
        "  distinct_trap:\n"
        "    relation: ev\n"
        "    expr: COUNT(DISTINCT amount)\n"
        "    cumulative: true\n"
        "    time_dimension: {name: day, grain: day}\n"
    )
    from sqldash.project.store import DashboardStore
    from sqldash.semantics import SemanticLayer

    store = DashboardStore(tmp_path)
    findings = lint_project(store, SemanticLayer(store))
    warnings = [f.message for f in findings if f.level == "warning"]
    assert any("bad_running_avg" in w and "AVG" in w for w in warnings)
    assert any("distinct_trap" in w for w in warnings)
    assert not any("good_running_total" in w for w in warnings)


def test_lint_warns_on_non_additive_trailing_window(tmp_path):
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: duckdb, database: ':memory:'}\n"
        "relations:\n"
        "  ev: {sql: \"SELECT 1 AS amount, DATE '2026-01-01' AS day\"}\n"
        "metrics:\n"
        "  bad_avg:\n"
        "    relation: ev\n"
        "    expr: AVG(amount)\n"
        "    window: 28 days\n"
        "    time_dimension: {name: day, grain: day}\n"
        "  good_sum:\n"
        "    relation: ev\n"
        "    expr: SUM(amount)\n"
        "    window: 28 days\n"
        "    time_dimension: {name: day, grain: day}\n"
    )
    store = DashboardStore(tmp_path)
    findings = lint_project(store, SemanticLayer(store))
    warnings = [f.message for f in findings if f.level == "warning"]
    assert any("bad_avg" in w and "AVG" in w and "trailing-window" in w for w in warnings)
    assert not any("good_sum" in w and "trailing-window" in w for w in warnings)


def test_compare_without_time_dimension_is_an_error(tmp_path):
    """Both compare windows return the same number, so the delta always reads 0%
    — silently, until this check existed."""
    (tmp_path / "metrics.yaml").write_text(
        'source: {type: duckdb, database: ":memory:"}\n'
        'relations:\n  orders: {sql: "SELECT 1 AS amount"}\n'
        "metrics:\n  revenue: {relation: orders, expr: SUM(amount)}\n"
    )
    (tmp_path / "d.yaml").write_text(
        "title: D\n"
        'source: {type: duckdb, database: ":memory:"}\n'
        "tiles: [{title: T, metric: revenue, compare: previous_period}]\n"
    )
    store = DashboardStore(tmp_path)
    findings = lint_project(store, SemanticLayer(store))
    messages = [f.message for f in findings if f.level == "error"]
    assert any("compare 'previous_period'" in m and "no time_dimension" in m for m in messages), (
        messages
    )


def test_compare_on_grainless_windowed_metric_is_an_error(tmp_path):
    """A grainless window drops the daterange start, so compare has no range to
    shift — lint used to stay clean while the tile never rendered. #479."""
    (tmp_path / "metrics.yaml").write_text(
        'source: {type: duckdb, database: ":memory:"}\n'
        "relations:\n"
        "  orders: {sql: \"SELECT 1 AS amount, DATE '2026-01-01' AS day\"}\n"
        "metrics:\n"
        "  trailing_28d:\n"
        "    relation: orders\n"
        "    expr: SUM(amount)\n"
        "    window: 28 days\n"
        "    time_dimension: {name: day, grain: day}\n"
    )
    (tmp_path / "d.yaml").write_text(
        "title: D\n"
        'source: {type: duckdb, database: ":memory:"}\n'
        "tiles: [{title: T, metric: trailing_28d, compare: previous_period}]\n"
    )
    findings = lint(tmp_path)
    messages = [f.message for f in findings if f.level == "error"]
    assert any(
        "compare 'previous_period'" in m and ("window" in m or "grain" in m) for m in messages
    ), messages

    (tmp_path / "d.yaml").write_text(
        "title: D\n"
        'source: {type: duckdb, database: ":memory:"}\n'
        "tiles: [{title: T, metric: trailing_28d, grain: day, compare: previous_period}]\n"
    )
    findings = lint(tmp_path)
    messages = [f.message for f in findings if f.level == "error"]
    assert not any(
        "compare 'previous_period'" in m and ("window" in m or "grain" in m) for m in messages
    ), messages


def test_compare_on_a_dashboard_without_a_daterange_filter_is_an_error(tmp_path):
    """Compare shifts the dashboard's daterange; with no daterange filter every
    headless surface errored and the browser dropped the delta silently, while
    lint stayed clean. #516."""
    create_demo(tmp_path)
    folder = tmp_path / ".sqldash"
    for label, tile in (
        ("folded", "    metric: {name: revenue, compare: previous_period}\n"),
        ("tile-level", "    metric: revenue\n    compare: previous_period\n"),
    ):
        (folder / "bad.yaml").write_text(
            "title: Bad\nsource: {type: duckdb, attach_files: true}\n"
            f"tiles:\n  - title: T\n{tile}"
        )
        errors = [f.message for f in lint(folder) if f.file == "bad.yaml" and f.level == "error"]
        assert errors == [
            "tile 't': compare 'previous_period' needs a time range but the dashboard "
            "has no daterange filter — add one, or omit compare"
        ], (label, errors)


def test_compare_with_a_defaulted_daterange_filter_is_clean(tmp_path):
    create_demo(tmp_path)
    folder = tmp_path / ".sqldash"
    (folder / "ok.yaml").write_text(
        "title: Ok\nsource: {type: duckdb, attach_files: true}\n"
        "filters:\n  - {name: dates, type: daterange, default: last_60_days}\n"
        "tiles:\n  - title: T\n    metric: {name: revenue, compare: yoy}\n"
    )
    assert [f for f in lint(folder) if f.file == "ok.yaml"] == []


def test_compare_with_an_undefaulted_daterange_filter_is_a_warning(tmp_path):
    """The dashboard opens with no range to shift, so the tile errors until a
    viewer picks one — a warning, since picking a range is a real remedy."""
    create_demo(tmp_path)
    folder = tmp_path / ".sqldash"
    (folder / "nodefault.yaml").write_text(
        "title: No default\nsource: {type: duckdb, attach_files: true}\n"
        "filters:\n  - {name: dates, type: daterange}\n"
        "tiles:\n  - title: T\n    metric: {name: revenue, compare: previous_period}\n"
    )
    findings = [f for f in lint(folder) if f.file == "nodefault.yaml"]
    assert [(f.level, f.message) for f in findings] == [
        (
            "warning",
            "tile 't': compare 'previous_period' needs a time range but daterange filter "
            "'dates' has no default — the tile errors until a date range is picked",
        )
    ]


def test_compare_without_a_daterange_does_not_stack_on_the_existing_compare_errors(tmp_path):
    """No time_dimension and a grainless window already say why compare cannot
    run; a second error about the missing daterange would be one defect twice."""
    (tmp_path / "metrics.yaml").write_text(
        'source: {type: duckdb, database: ":memory:"}\n'
        "relations:\n"
        "  orders: {sql: \"SELECT 1 AS amount, DATE '2026-01-01' AS day\"}\n"
        "metrics:\n"
        "  flat: {relation: orders, expr: SUM(amount)}\n"
        "  trailing_28d:\n"
        "    relation: orders\n"
        "    expr: SUM(amount)\n"
        "    window: 28 days\n"
        "    time_dimension: {name: day, grain: day}\n"
    )
    (tmp_path / "d.yaml").write_text(
        "title: D\n"
        'source: {type: duckdb, database: ":memory:"}\n'
        "tiles:\n"
        "  - {title: A, metric: flat, compare: previous_period}\n"
        "  - {title: B, metric: trailing_28d, compare: previous_period}\n"
    )
    errors = [f.message for f in lint(tmp_path) if f.level == "error"]
    assert len([m for m in errors if "compare" in m]) == 2, errors
    assert not any("no daterange filter" in m for m in errors), errors


def test_metric_tile_that_cannot_apply_a_dashboard_filter_is_a_warning(tmp_path):
    """#476: a metric tile whose metric lacks time_dimension / the filter's
    dimension used to answer the unfiltered total while lint stayed clean
    whenever any other tile used the filter."""
    (tmp_path / "mixed.yaml").write_text(
        """
title: Mixed Filter Use
source: {type: duckdb}
metrics:
  revenue:
    table: orders
    expr: SUM(amount)
    time_dimension: {name: order_date, grain: day}
    dimensions: [{name: region}]
  revenue_no_time:
    table: orders
    expr: SUM(amount)
    dimensions: [{name: region}]
  revenue_no_dims:
    table: orders
    expr: SUM(amount)
    time_dimension: {name: order_date, grain: day}
filters:
  - {name: dates, type: daterange, default: last_30_days}
  - {name: region, type: select, options: [us, eu, apac], default: all}
tiles:
  - title: Filtered SQL
    sql: |
      SELECT region, SUM(amount) AS a FROM orders
      WHERE order_date BETWEEN {{ dates_start }} AND {{ dates_end }}
      GROUP BY 1
  - {title: Metric ignores dates, metric: revenue_no_time}
  - {title: Metric honors dates, metric: revenue, grain: day}
  - {title: Metric no dims ignores region, metric: revenue_no_dims}
"""
    )
    findings = lint(tmp_path)
    assert findings, "expected per-tile warnings, lint was clean"
    assert not any(f.level == "error" for f in findings), [
        f"{f.level}: {f.message}" for f in findings
    ]
    warnings = [f.message for f in findings if f.level == "warning"]
    dates = [m for m in warnings if "metric_ignores_dates" in m and "'dates'" in m]
    assert dates, warnings
    assert any(
        "revenue_no_time" in m and "no time_dimension" in m and "ignores the date filter" in m
        for m in dates
    ), dates
    region = [m for m in warnings if "metric_no_dims_ignores_region" in m and "'region'" in m]
    assert region, warnings
    assert any("revenue_no_dims" in m and "ignores the filter" in m for m in region), region
    honors = [m for m in warnings if "metric_honors_dates" in m]
    assert not any("'dates'" in m and "ignores" in m for m in honors), honors
    assert not any("'region'" in m and "ignores" in m for m in honors), honors
    assert not any("metric_ignores_dates" in m and "'region'" in m for m in warnings)
    assert not any("metric_no_dims_ignores_region" in m and "'dates'" in m for m in warnings)
    assert all(f.level == "warning" for f in findings if "ignores" in f.message)


def test_lone_metric_tile_without_time_dimension_still_warns_about_daterange(tmp_path):
    """#476: even when the filter is also unused by SQL, the per-tile warning
    must name the tile that shows the unfiltered total."""
    (tmp_path / "d.yaml").write_text(
        """
title: D
source: {type: duckdb}
metrics:
  revenue_no_time: {table: orders, expr: SUM(amount)}
filters:
  - {name: dates, type: daterange, default: last_30_days}
tiles:
  - {title: KPI, metric: revenue_no_time}
"""
    )
    findings = lint(tmp_path)
    tile = [
        f
        for f in findings
        if f.level == "warning"
        and "kpi" in f.message
        and "'dates'" in f.message
        and "revenue_no_time" in f.message
        and "no time_dimension" in f.message
        and "ignores the date filter" in f.message
    ]
    assert tile, [f"{f.level}: {f.message}" for f in findings]


def test_lint_rejects_a_malformed_refresh(tmp_path):
    """refresh: bananas used to lint clean and silently disable auto-refresh. #283."""
    (tmp_path / "d.yaml").write_text(
        "title: D\n"
        "refresh: bananas\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - {title: T, sql: 'SELECT 1 AS n'}\n"
    )
    errors = [f.message for f in lint(tmp_path) if f.level == "error"]
    assert any("refresh 'bananas'" in m for m in errors), errors


def test_lint_warns_when_refresh_is_below_the_floor(tmp_path):
    (tmp_path / "d.yaml").write_text(
        "title: D\n"
        "refresh: 1s\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - {title: T, sql: 'SELECT 1 AS n'}\n"
    )
    warnings = [f.message for f in lint(tmp_path) if f.level == "warning"]
    assert any("5s floor" in m for m in warnings), warnings


def test_lint_rejects_orientation_on_a_non_bar_chart(tmp_path):
    (tmp_path / "d.yaml").write_text(
        "title: D\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - {title: T, chart: {type: pie, orientation: horizontal}, "
        "sql: 'SELECT 1 AS n'}\n"
    )
    errors = [f.message for f in lint(tmp_path) if f.level == "error"]
    assert any("orientation is only valid on bar charts" in m for m in errors), errors


def test_lint_rejects_stacked_on_a_non_bar_or_area_chart(tmp_path):
    """orientation on line is an error; stacked on table/pie was silent. #287."""
    (tmp_path / "d.yaml").write_text(
        "title: D\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - {title: T, chart: {type: table, stacked: true}, sql: 'SELECT 1 AS n'}\n"
        "  - {title: P, chart: {type: pie, stacked: true}, sql: 'SELECT 1 AS n'}\n"
    )
    errors = [f.message for f in lint(tmp_path) if f.level == "error"]
    assert any("stacked is only valid on bar and area" in m and "table" in m for m in errors), (
        errors
    )
    assert any("pie" in m and "stacked" in m for m in errors), errors


def test_lint_reports_an_inline_metric_two_dashboards_define(tmp_path):
    """#87 refuses this at query time; the linter should say so in CI first.

    Only visible across the project, so it cannot live in lint_dashboard.
    """
    for name, mult in (("a", 2), ("b", 100)):
        (tmp_path / f"{name}.yaml").write_text(
            f"title: Dash {name}\n"
            "source: {type: duckdb, database: ':memory:'}\n"
            "metrics:\n"
            f"  inline_rev: {{sql: 'SELECT 1 AS amount', expr: 'SUM(amount) * {mult}'}}\n"
            "queries: {q: 'SELECT 1'}\n"
            "tiles: [{id: w, metric: inline_rev}]\n"
        )
    store = DashboardStore(tmp_path)
    findings = lint_project(store, SemanticLayer(store))
    errors = [f for f in findings if f.level == "error" and "inline_rev" in f.message]
    assert errors, [f.message for f in findings]
    assert "a.yaml" in errors[0].message
    assert "b.yaml" in errors[0].message


def test_lint_accepts_the_same_name_when_metrics_yaml_defines_it(tmp_path):
    """metrics.yaml is canonical and wins by design — not a collision."""
    for name in ("a", "b"):
        (tmp_path / f"{name}.yaml").write_text(
            f"title: Dash {name}\n"
            "source: {type: duckdb, database: ':memory:'}\n"
            "metrics:\n"
            "  inline_rev: {sql: 'SELECT 1 AS amount', expr: 'SUM(amount)'}\n"
            "queries: {q: 'SELECT 1'}\n"
            "tiles: [{id: w, metric: inline_rev}]\n"
        )
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: duckdb, database: ':memory:'}\n"
        "relations: {r: {sql: 'SELECT 1 AS amount'}}\n"
        "metrics:\n  inline_rev: {relation: r, expr: 'SUM(amount)'}\n"
    )
    store = DashboardStore(tmp_path)
    findings = lint_project(store, SemanticLayer(store))
    assert not [
        f for f in findings if f.level == "error" and "more than one dashboard" in f.message
    ]


def test_lint_warns_when_a_time_series_metric_has_dimensions_and_no_group_by(tmp_path):
    """A line/area over (time, dim, value) without group_by draws one interleaved series. #181."""
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: duckdb, database: ':memory:'}\n"
        "relations:\n"
        "  orders: {sql: \"SELECT DATE '2026-01-01' AS order_date, "
        "'us' AS region, 1.0 AS amount\"}\n"
        "metrics:\n"
        "  revenue:\n"
        "    relation: orders\n"
        "    expr: SUM(amount)\n"
        "    time_dimension: {name: order_date, grain: day}\n"
        "    dimensions: [{name: region}]\n"
    )
    cases = (
        (
            "  - {title: By region, metric: {name: revenue, grain: day, "
            "dimensions: [region]}, chart: line}\n",
            True,
        ),
        (
            "  - {title: Implicit area, metric: {name: revenue, grain: day, "
            "dimensions: [region]}}\n",
            True,
        ),
        (
            "  - {title: Grouped, metric: {name: revenue, grain: day, "
            "dimensions: [region]}, chart: {type: line, group_by: region}}\n",
            False,
        ),
        (
            "  - {title: Table, metric: {name: revenue, grain: day, "
            "dimensions: [region]}, chart: table}\n",
            False,
        ),
        (
            "  - {title: No grain, metric: {name: revenue, dimensions: [region]}, chart: line}\n",
            False,
        ),
    )
    for tile, expect_warn in cases:
        (tmp_path / "d.yaml").write_text(
            f"title: D\nsource: {{type: duckdb, database: ':memory:'}}\ntiles:\n{tile}"
        )
        warnings = [f.message for f in lint(tmp_path) if f.level == "warning"]
        warned = any("group_by" in w and "interleaved" in w for w in warnings)
        assert warned is expect_warn, (tile, warnings)


def test_lint_catches_a_typoed_daterange_default(tmp_path):
    """MCP validate_dashboard dry-runs and raised; lint never resolved defaults. #185."""
    (tmp_path / "d.yaml").write_text(
        "title: D\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "filters:\n"
        "  - {name: dates, type: daterange, default: last_30_day, "
        "bind: {start: start, end: end}}\n"
        "tiles: [{title: W, sql: 'SELECT 1 WHERE d >= {{ start }} AND d < {{ end }}'}]\n"
    )
    findings = lint(tmp_path)
    errors = [f.message for f in findings if f.level == "error"]
    assert any("last_30_day" in m and "last_30_days" in m for m in errors), errors


def test_lint_flags_a_daterange_default_that_is_a_single_date(tmp_path):
    """`default: today` is a valid date token but not a range. Lint used to
    stay clean and the tile then 422'd 'declare filter defaults'. #274."""
    (tmp_path / "d.yaml").write_text(
        "title: D\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "filters:\n"
        "  - {name: dates, type: daterange, default: today, "
        "bind: {start: start, end: end}}\n"
        "tiles: [{title: W, sql: 'SELECT 1 WHERE d >= {{ start }}'}]\n"
    )
    findings = lint(tmp_path)
    errors = [f.message for f in findings if f.level == "error"]
    assert any("today" in m and "single date" in m and "{start, end}" in m for m in errors), errors


def test_lint_flags_an_iso_date_as_a_daterange_default(tmp_path):
    (tmp_path / "d.yaml").write_text(
        "title: D\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "filters:\n"
        "  - {name: dates, type: daterange, default: '2026-04-28'}\n"
        "tiles: [{title: W, sql: 'SELECT 1 WHERE d >= {{ dates_start }}'}]\n"
    )
    findings = lint(tmp_path)
    errors = [f.message for f in findings if f.level == "error"]
    assert any("2026-04-28" in m and "single date" in m for m in errors), errors


def test_lint_catches_a_typoed_date_filter_default(tmp_path):
    (tmp_path / "d.yaml").write_text(
        "title: D\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "filters:\n"
        "  - {name: day, type: date, default: last_30_day}\n"
        "tiles: [{title: W, sql: 'SELECT 1 WHERE d = {{ day }}'}]\n"
    )
    findings = lint(tmp_path)
    errors = [f.message for f in findings if f.level == "error"]
    assert any("last_30_day" in m for m in errors), errors


def test_lint_catches_a_yaml_int_date_filter_default(tmp_path):
    """Unquoted 20260101 is a YAML int. Skipping non-strings left this lint-clean. #197."""
    (tmp_path / "d.yaml").write_text(
        "title: D\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "filters:\n"
        "  - {name: day, type: date, default: 20260101}\n"
        "tiles: [{title: W, sql: 'SELECT 1 WHERE d = {{ day }}'}]\n"
    )
    errors = [f.message for f in lint(tmp_path) if f.level == "error"]
    assert any("20260101" in m for m in errors), errors


def test_validate_dashboard_agrees_with_lint_on_a_typoed_default(tmp_path):
    """The disagreement was lint-clean vs validate-false. Both must fail without a warehouse."""
    from sqldash.lint import validate_dashboard
    from sqldash.semantics import SemanticLayer

    text = (
        "title: D\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "filters:\n"
        "  - {name: dates, type: daterange, default: last_30_day, "
        "bind: {start: start, end: end}}\n"
        "tiles: [{title: W, sql: 'SELECT 1 WHERE d >= {{ start }} AND d < {{ end }}'}]\n"
    )
    (tmp_path / "d.yaml").write_text(text)
    store = DashboardStore(tmp_path)
    payload = validate_dashboard(
        text, store=store, layer=SemanticLayer(store), registry=None, check_sql=False
    )
    assert payload["valid"] is False
    assert any("last_30_day" in e for e in payload["errors"])
    assert len([e for e in payload["errors"] if "last_30_day" in e]) == 1


def _daterange_dash(default: str) -> str:
    return (
        "title: D\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "filters:\n"
        f"  - {{name: dates, type: daterange, default: {default}, "
        "bind: {start: start, end: end}}\n"
        "tiles: [{title: W, sql: 'SELECT 1 AS d WHERE d >= {{ start }} AND d < {{ end }}'}]\n"
    )


def test_lint_catches_a_yaml_int_daterange_default(tmp_path):
    """Unquoted 20240101 is a YAML int. Runtime str()s it then rejects compact ISO."""
    (tmp_path / "d.yaml").write_text(_daterange_dash("{start: 20240101, end: 20240201}"))
    errors = [f.message for f in lint(tmp_path) if f.level == "error"]
    assert any("20240101" in m for m in errors), errors


def test_lint_catches_a_yaml_int_daterange_scalar_default(tmp_path):
    """The dict form was #185. The scalar `default: 20260101` was still skipped."""
    (tmp_path / "d.yaml").write_text(_daterange_dash("20260101"))
    errors = [f.message for f in lint(tmp_path) if f.level == "error"]
    assert any("20260101" in m for m in errors), errors


def test_lint_catches_a_daterange_default_missing_start(tmp_path):
    (tmp_path / "d.yaml").write_text(_daterange_dash("{begin: '2024-01-01', end: '2024-02-01'}"))
    errors = [f.message for f in lint(tmp_path) if f.level == "error"]
    assert any("no 'start'" in m for m in errors), errors


def test_lint_ignores_extra_keys_on_a_daterange_default(tmp_path):
    """Runtime only reads start/end. An extra last_30_day must not fail lint."""
    (tmp_path / "d.yaml").write_text(
        _daterange_dash("{start: '2024-01-01', end: '2024-02-01', extra: last_30_day}")
    )
    errors = [f.message for f in lint(tmp_path) if f.level == "error"]
    assert not any("last_30_day" in m for m in errors), errors


def test_validate_dashboard_does_not_bless_a_yaml_int_date_default(tmp_path):
    """Scalar `default: 20260101` must fail lint and validate, not a warehouse cast. #197."""
    from sqldash.lint import validate_dashboard
    from sqldash.semantics import SemanticLayer

    text = (
        "title: D\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "filters:\n"
        "  - {name: day, type: date, default: 20260101}\n"
        "tiles: [{title: W, sql: 'SELECT 1 WHERE d = {{ day }}'}]\n"
    )
    (tmp_path / "d.yaml").write_text(text)
    store = DashboardStore(tmp_path)
    payload = validate_dashboard(
        text, store=store, layer=SemanticLayer(store), registry=None, check_sql=False
    )
    assert payload["valid"] is False
    assert any("20260101" in e for e in payload["errors"]), payload["errors"]


def test_validate_dashboard_does_not_bless_a_yaml_int_daterange_default(tmp_path):
    """The skip-by-message-substring used to return valid=True here. #188 review."""
    from sqldash.execution import ExecutionRegistry
    from sqldash.lint import validate_dashboard
    from sqldash.semantics import SemanticLayer

    text = _daterange_dash("{start: 20240101, end: 20240201}")
    (tmp_path / "d.yaml").write_text(text)
    store = DashboardStore(tmp_path)
    registry = ExecutionRegistry(max_workers=1)
    try:
        payload = validate_dashboard(
            text, store=store, layer=SemanticLayer(store), registry=registry, check_sql=True
        )
    finally:
        registry.shutdown()
    assert payload["valid"] is False
    assert any("20240101" in e for e in payload["errors"]), payload["errors"]


def test_validate_dashboard_does_not_double_report_a_missing_daterange_role(tmp_path):
    """Lint names the missing start; dry-run used to repeat it as no value for start."""
    from sqldash.execution import ExecutionRegistry
    from sqldash.lint import validate_dashboard
    from sqldash.semantics import SemanticLayer

    text = _daterange_dash("{begin: '2024-01-01', end: '2024-02-01'}")
    (tmp_path / "d.yaml").write_text(text)
    store = DashboardStore(tmp_path)
    registry = ExecutionRegistry(max_workers=1)
    try:
        payload = validate_dashboard(
            text, store=store, layer=SemanticLayer(store), registry=registry, check_sql=True
        )
    finally:
        registry.shutdown()
    assert payload["valid"] is False
    assert any("no 'start'" in e for e in payload["errors"]), payload["errors"]
    assert not any("no value for" in e for e in payload["errors"]), payload["errors"]


def test_validate_dashboard_does_not_double_report_a_single_date_daterange(tmp_path):
    """Lint names the single-date default; dry-run used to also say no value for start/end."""
    from sqldash.execution import ExecutionRegistry
    from sqldash.lint import validate_dashboard
    from sqldash.semantics import SemanticLayer

    text = _daterange_dash("today")
    (tmp_path / "d.yaml").write_text(text)
    store = DashboardStore(tmp_path)
    registry = ExecutionRegistry(max_workers=1)
    try:
        payload = validate_dashboard(
            text, store=store, layer=SemanticLayer(store), registry=registry, check_sql=True
        )
    finally:
        registry.shutdown()
    assert payload["valid"] is False
    assert any("single date" in e for e in payload["errors"]), payload["errors"]
    assert not any("no value for" in e for e in payload["errors"]), payload["errors"]


def test_validate_dashboard_reports_a_typoed_default_once(tmp_path):
    from sqldash.execution import ExecutionRegistry
    from sqldash.lint import validate_dashboard
    from sqldash.semantics import SemanticLayer

    text = _daterange_dash("last_30_day")
    (tmp_path / "d.yaml").write_text(text)
    store = DashboardStore(tmp_path)
    registry = ExecutionRegistry(max_workers=1)
    try:
        payload = validate_dashboard(
            text, store=store, layer=SemanticLayer(store), registry=registry, check_sql=True
        )
    finally:
        registry.shutdown()
    assert payload["valid"] is False
    assert len([e for e in payload["errors"] if "last_30_day" in e]) == 1, payload["errors"]


def test_lint_still_flags_compare_after_the_editor_folds_it_into_the_metric(tmp_path):
    """#92 moves `compare:` from the tile into the metric on every save, and the
    check read only the tile-level key — so this guardrail vanished the first
    time anyone opened the dashboard, while rendering still read metric.compare
    and still drew the 0% delta it warns about.
    """
    from sqldash.lint import lint_project
    from sqldash.project.store import DashboardStore
    from sqldash.semantics import SemanticLayer

    (tmp_path / "metrics.yaml").write_text(
        "source: {type: duckdb, attach_files: true}\n"
        "relations:\n  t: {sql: 'SELECT 1 AS amount'}\n"
        "metrics:\n  revenue: {relation: t, expr: SUM(amount)}\n"
    )
    for label, tile in (
        ("tile-level", "  - {title: Rev, metric: revenue, compare: yoy}\n"),
        ("folded", "  - {title: Rev, metric: {name: revenue, compare: yoy}}\n"),
    ):
        (tmp_path / "d.yaml").write_text(
            f"title: D\nsource: {{type: duckdb, attach_files: true}}\ntiles:\n{tile}"
        )
        store = DashboardStore(tmp_path)
        errors = [f for f in lint_project(store, SemanticLayer(store)) if f.level == "error"]
        assert any("no time_dimension" in f.message for f in errors), (label, errors)


def test_non_utf8_file_is_an_error_finding_not_a_crash(tmp_path):
    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "oops.yaml").write_bytes(b"\xff\xfe invalid")
    findings = lint(tmp_path / ".sqldash")
    oops = [f for f in findings if f.file == "oops.yaml"]
    assert len(oops) == 1
    assert oops[0].level == "error"
    assert "UTF-8" in oops[0].message


def test_missing_attach_base_dir_is_an_error(tmp_path):
    """A typo'd base_dir used to lint clean and fail every query. #339."""
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
    errors = [f for f in lint(tmp_path) if f.level == "error"]
    assert any("base_dir" in f.message and "nonexistent" in f.message for f in errors), errors


def test_empty_attach_dir_is_an_error(tmp_path):
    """A dir that exists but holds no csv/parquet used to lint clean. #478."""
    (tmp_path / "probe.yaml").write_text(
        "title: No files probe\n"
        "source: {type: duckdb, attach_files: true}\n"
        "tiles:\n"
        "  - title: n\n"
        "    chart: big_number\n"
        "    sql: SELECT COUNT(*) AS n FROM orders\n"
    )
    errors = [f for f in lint(tmp_path) if f.level == "error"]
    assert any(
        "0 files" in f.message
        and "csv" in f.message
        and "parquet" in f.message
        and (str(tmp_path) in f.message or str(tmp_path.resolve()) in f.message)
        for f in errors
    ), errors


def test_invalid_utf8_dashboard_is_one_finding_and_siblings_still_lint(tmp_path):
    """The issue's repro: lint used to die in a UnicodeDecodeError traceback before
    printing anything, ok.yaml included. #290."""
    (tmp_path / "ok.yaml").write_text(
        "title: Ok\nsource: {type: duckdb, database: ':memory:'}\ntiles:\n"
        '  - title: A\n    sql: "SELECT 1 AS a"\n'
    )
    (tmp_path / "broken.yaml").write_bytes(b"title: Broken\n\xff\xfe bad\n")
    findings = lint(tmp_path)
    errors = [f for f in findings if f.level == "error"]
    assert [f.file for f in errors] == ["broken.yaml"]
    assert "not valid UTF-8" in errors[0].message
    assert not [f for f in findings if f.file == "ok.yaml"]
    assert "ok.yaml" in {p.name for p in DashboardStore(tmp_path).discover().values()}


def test_invalid_utf8_metrics_yaml_is_a_finding_not_a_crash(tmp_path):
    """`layer.metrics_file()` read outside lint's SemanticError guard, so a
    non-UTF-8 metrics.yaml was the one file lint could not report. #290."""
    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "metrics.yaml").write_bytes(b"metrics:\n\xff\xfe bad\n")
    findings = lint(tmp_path / ".sqldash")
    bad = [f for f in findings if f.file == "metrics.yaml"]
    assert len(bad) == 1
    assert bad[0].level == "error"
    assert "not valid UTF-8" in bad[0].message
    assert any(f.file == "demo.yaml" and "unknown metric" in f.message for f in findings), (
        "demo.yaml was still linted after the metrics.yaml failure"
    )


OPTIONAL_TILE_YAML = (
    "title: Optional query\n"
    "source: {type: duckdb, attach_files: true}\n"
    "filters:\n"
    "  - {name: region, type: select, label: Region, options: [us, eu, apac]}\n"
    "tiles:\n"
    "  - title: Region total\n"
    "    size: 6x3\n"
    "    sql: |\n"
    "      {% if region %}SELECT region, SUM(amount) AS total\n"
    "      FROM orders WHERE region = {{ region }} GROUP BY 1{% endif %}\n"
)


def test_validate_dashboard_agrees_with_lint_on_an_inactive_if_block(tmp_path):
    """Lint passed the issue's optional.yaml; the dry-run probed `SELECT * FROM (\\n)`
    and returned a DuckDB parser error. A blank render is inactive, not broken. #284."""
    from sqldash.execution import ExecutionRegistry
    from sqldash.lint import validate_dashboard

    create_demo(tmp_path)
    root = tmp_path / ".sqldash"
    (root / "optional.yaml").write_text(OPTIONAL_TILE_YAML)
    assert [f for f in lint(root) if f.file == "optional.yaml"] == []
    store = DashboardStore(root)
    registry = ExecutionRegistry(max_workers=1)
    try:
        payload = validate_dashboard(
            OPTIONAL_TILE_YAML, store=store, layer=SemanticLayer(store), registry=registry
        )
        assert payload["valid"] is True, payload["errors"]
        assert payload["sql_checked"] is True
        assert payload["rendered_sql"]["region_total"].strip() == ""

        typoed = OPTIONAL_TILE_YAML.replace("SELECT region,", "SELECT regionn,")
        payload = validate_dashboard(
            typoed, store=store, layer=SemanticLayer(store), registry=registry
        )
    finally:
        registry.shutdown()
    assert payload["valid"] is False
    assert any("with filters active" in e and "regionn" in e for e in payload["errors"]), payload


BRANCHING_TILE_YAML = (
    "title: Branching\n"
    "source: {type: duckdb, attach_files: true}\n"
    "filters:\n"
    "  - {name: region, type: select, label: Region, options: [us, eu, apac]}\n"
    "tiles:\n"
    "  - title: Region total\n"
    "    sql: |\n"
    "      {% if region %}SELECT region, SUM(amount) AS total\n"
    "      FROM orders WHERE region = {{ region }} GROUP BY 1\n"
    "      {% else %}SELECT 'all' AS region, SUM(amount) AS total FROM orders{% endif %}\n"
)


def test_validate_dashboard_probes_both_sides_of_an_if_else(tmp_path):
    """#657: the two render variants lint already had map onto the two branches —
    no filters takes the `{% else %}` body, filters active takes the `{% if %}`."""
    from sqldash.execution import ExecutionRegistry
    from sqldash.lint import validate_dashboard

    create_demo(tmp_path)
    root = tmp_path / ".sqldash"
    (root / "branching.yaml").write_text(BRANCHING_TILE_YAML)
    assert [f for f in lint(root) if f.file == "branching.yaml"] == []
    store = DashboardStore(root)
    registry = ExecutionRegistry(max_workers=1)
    try:
        payload = validate_dashboard(
            BRANCHING_TILE_YAML, store=store, layer=SemanticLayer(store), registry=registry
        )
        assert payload["valid"] is True, payload["errors"]
        assert payload["rendered_sql"]["region_total"].strip().startswith("SELECT 'all'")

        in_else = validate_dashboard(
            BRANCHING_TILE_YAML.replace("'all' AS region", "'all' AS regionn, nope"),
            store=store,
            layer=SemanticLayer(store),
            registry=registry,
        )
        in_if = validate_dashboard(
            BRANCHING_TILE_YAML.replace("SELECT region,", "SELECT regionn,"),
            store=store,
            layer=SemanticLayer(store),
            registry=registry,
        )
    finally:
        registry.shutdown()
    assert in_else["valid"] is False
    assert any("nope" in e for e in in_else["errors"]), in_else["errors"]
    assert in_if["valid"] is False
    assert any("with filters active" in e and "regionn" in e for e in in_if["errors"]), in_if


def test_validate_dashboard_probes_tile_sql_ending_in_a_semicolon(tmp_path):
    """Lint passed and the tile rendered; the probe wrapped `SELECT 1 AS a;` in a
    subquery and DuckDB called the `;` a syntax error. #542."""
    from sqldash.execution import ExecutionRegistry
    from sqldash.lint import validate_dashboard

    create_demo(tmp_path)
    root = tmp_path / ".sqldash"
    text = (
        "title: Semi\nsource: {type: duckdb, attach_files: true}\n"
        "filters:\n  - {name: region, type: select, options: [East, West]}\n"
        "tiles:\n"
        "  - {title: T, sql: 'SELECT 1 AS a;'}\n"
        "  - title: R\n"
        "    sql: |\n"
        "      SELECT region FROM orders\n"
        "      {% if region %}WHERE region = {{ region }}{% endif %};\n"
    )
    (root / "semi.yaml").write_text(text)
    assert [f for f in lint(root) if f.file == "semi.yaml"] == []
    store = DashboardStore(root)
    registry = ExecutionRegistry(max_workers=1)
    try:
        payload = validate_dashboard(
            text, store=store, layer=SemanticLayer(store), registry=registry
        )
        assert payload["valid"] is True, payload["errors"]
        assert payload["rendered_sql"]["t"] == "SELECT 1 AS a;"

        typoed = text.replace("SELECT region FROM", "SELECT regionn FROM")
        payload = validate_dashboard(
            typoed, store=store, layer=SemanticLayer(store), registry=registry
        )
    finally:
        registry.shutdown()
    assert payload["valid"] is False
    assert any("regionn" in e for e in payload["errors"]), payload["errors"]


def test_reserved_word_names_are_lint_errors(tmp_path):
    """#291: a reserved metric or dimension name is a static property of the file,
    so `sqldash lint` refuses it before any warehouse sees `SUM(trailing) OVER`."""
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: duckdb, database: ':memory:'}\n"
        "relations:\n  t: {sql: 'SELECT 1 AS amount'}\n"
        "metrics:\n  trailing: {relation: t, expr: SUM(amount)}\n"
    )
    (tmp_path / "d.yaml").write_text(
        "title: T\nsource: {type: duckdb, database: ':memory:'}\n"
        "metrics:\n  m: {sql: 'SELECT 1 AS amount', expr: SUM(amount), "
        "dimensions: [{name: order}]}\n"
        "tiles: [{metric: m}]\n"
    )
    errors = [f"{f.file}: {f.message}" for f in lint(tmp_path) if f.level == "error"]
    assert any(
        e.startswith("metrics.yaml: ") and "metric name 'trailing' is a SQL reserved word" in e
        for e in errors
    ), errors
    assert any(
        e.startswith("d.yaml: ") and "dimension name 'order' is a SQL reserved word" in e
        for e in errors
    ), errors


def test_lint_and_validate_agree_on_an_inverted_daterange_default(tmp_path):
    """#361: an authored `{start, end}` with start after end would 422 every
    tile at runtime; lint and validate_dashboard must both name it first."""
    from sqldash.lint import validate_dashboard

    text = _daterange_dash("{start: '2026-09-01', end: '2026-01-01'}")
    (tmp_path / "d.yaml").write_text(text)
    errors = [f.message for f in lint(tmp_path) if f.level == "error"]
    assert any("start '2026-09-01' is after end '2026-01-01'" in m for m in errors), errors
    store = DashboardStore(tmp_path)
    payload = validate_dashboard(
        text, store=store, layer=SemanticLayer(store), registry=None, check_sql=False
    )
    assert payload["valid"] is False
    assert len([e for e in payload["errors"] if "inverted" in e]) == 1, payload["errors"]
    (tmp_path / "d.yaml").write_text(_daterange_dash("{start: '2026-01-01', end: '2026-01-01'}"))
    assert not [f for f in lint(tmp_path) if f.level == "error"]


def test_validate_dashboard_warns_when_a_chart_names_a_column_the_query_lacks(tmp_path):
    """The query page used to pin inferred `x`/`y` into the file; a later alias
    rename then rendered every value null with no error and a clean lint (#357).
    Lint cannot know a query's columns, but the dry-run probe already has them."""
    from sqldash.execution import ExecutionRegistry
    from sqldash.lint import validate_dashboard
    from sqldash.semantics import SemanticLayer

    text = (
        "title: D\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "queries: {q: 'SELECT 1 AS a, 2 AS c'}\n"
        "tiles:\n"
        "  - {title: Stale, query: q, chart: {type: bar, x: a, y: [b]}}\n"
        "  - {title: Cased, query: q, chart: {type: bar, x: A, y: [c]}}\n"
        "  - {title: Fine, query: q, chart: {type: bar, x: a, y: [c]}}\n"
        "  - {title: Bare, query: q, chart: bar}\n"
    )
    (tmp_path / "d.yaml").write_text(text)
    store = DashboardStore(tmp_path)
    registry = ExecutionRegistry(max_workers=1)
    try:
        payload = validate_dashboard(
            text, store=store, layer=SemanticLayer(store), registry=registry, check_sql=True
        )
    finally:
        registry.shutdown()
    assert payload["valid"] is True, payload
    stale = [w for w in payload["lint"] if "chart y 'b'" in w]
    assert len(stale) == 1, payload["lint"]
    assert stale[0].startswith("warning: tile 'stale':"), stale
    assert "columns: a, c" in stale[0], stale
    cased = [w for w in payload["lint"] if "chart x 'A'" in w]
    assert len(cased) == 1, payload["lint"]
    assert cased[0].startswith("warning: tile 'cased':"), cased
    assert not any("'fine'" in w or "'bare'" in w for w in payload["lint"]), payload["lint"]


def test_page_level_css_that_cannot_apply_is_named_by_lint(tmp_path):
    (tmp_path / "d.yaml").write_text(
        "title: D\nsource: {type: duckdb}\n"
        "css: |\n  :root { --page: #101010; --crawl: red; --accent: url(x); --holo: url(y); }\n"
        '  body { margin: 0; }\n  :root[data-theme="dark"] { padding: 0; }\n'
        "  @layer x { body { color: red; } }\n  .tile { color: red; }\n"
        "tiles: [{title: W, sql: 'SELECT 1'}]\n"
    )
    warnings = [f.message for f in lint(tmp_path) if f.level == "warning" and "css:" in f.message]
    assert len(warnings) == 2
    dropped, nested = warnings
    assert "--accent must be a plain colour or gradient" in dropped
    assert "--holo must be a plain value" in dropped
    assert "'margin: 0' is not a --token" in dropped
    assert "'padding: 0' is not a --token" in dropped
    assert "--page" not in dropped
    assert "--crawl" not in dropped
    assert "nested inside another block do nothing" in nested
    assert "body inside @layer x" in nested
    (tmp_path / "d.yaml").write_text(
        "title: D\nsource: {type: duckdb}\n"
        "css: |\n  --page: #101010;\n  body { background: #101010; }\n"
        "  .tile { color: red; font-family: html, serif; }\n"
        '  .tile::after { content: "body {"; }\n'
        "tiles: [{title: W, sql: 'SELECT 1'}]\n"
    )
    assert not [f for f in lint(tmp_path) if "css:" in f.message]


def test_a_rule_nested_inside_a_page_selector_is_named_by_lint(tmp_path):
    """#650: a rule or at-rule nested in `body`/`html`/`:root` used to be thrown away
    before it could be kept, dropped or named, so lint reported nothing at all."""
    (tmp_path / "d.yaml").write_text(
        "title: D\nsource: {type: duckdb}\n"
        "css: |\n  body {\n    --ink-1: green;\n    .foo { color: red }\n"
        "    @media (min-width: 40em) { .bar { color: purple } }\n  }\n"
        "tiles: [{title: W, sql: 'SELECT 1'}]\n"
    )
    warnings = [f.message for f in lint(tmp_path) if f.level == "warning" and "css:" in f.message]
    assert len(warnings) == 1, warnings
    assert "'.foo' is a rule nested in body" in warnings[0]
    assert "'@media (min-width: 40em)' is a rule nested in body" in warnings[0]
    assert "nest it inside :scope" in warnings[0]

    (tmp_path / "d.yaml").write_text(
        "title: D\nsource: {type: duckdb}\n"
        "css: |\n  :scope {\n    --ink-1: green;\n    .foo { color: red }\n"
        "    @media (min-width: 40em) { .bar { color: purple } }\n  }\n"
        "tiles: [{title: W, sql: 'SELECT 1'}]\n"
    )
    assert not [f for f in lint(tmp_path) if "css:" in f.message]


def test_page_prefixed_selectors_that_match_nothing_are_named_by_lint(tmp_path):
    (tmp_path / "d.yaml").write_text(
        "title: D\nsource: {type: duckdb}\n"
        "css: |\n  :root[data-theme] .tile { color: red; }\n"
        '  :root[data-theme="dark"] > .tile, body, .x { color: red; }\n'
        "tiles: [{title: W, sql: 'SELECT 1'}]\n"
    )
    warnings = [f.message for f in lint(tmp_path) if f.level == "warning" and "css:" in f.message]
    assert len(warnings) == 1, warnings
    assert """':root[data-theme="dark"] > .tile'; 'body'""" in warnings[0]
    assert "match nothing in the dashboard" in warnings[0]


def _validate_metric_tile(tmp_path, metrics_yaml: str, tile: str) -> dict:
    from sqldash.execution import ExecutionRegistry
    from sqldash.lint import validate_dashboard

    (tmp_path / "metrics.yaml").write_text(metrics_yaml)
    text = f"title: D\nsource: {{type: duckdb, database: ':memory:'}}\ntiles:\n  - {tile}\n"
    store = DashboardStore(tmp_path)
    registry = ExecutionRegistry(max_workers=1)
    try:
        return validate_dashboard(
            text, store=store, layer=SemanticLayer(store), registry=registry, check_sql=True
        )
    finally:
        registry.shutdown()


_WINDOWED_METRICS = (
    "source: {type: duckdb, database: ':memory:'}\n"
    "relations:\n"
    "  orders: {sql: \"SELECT 1 AS amount, 'us' AS region, DATE '2026-01-01' AS day\"}\n"
    "metrics:\n"
    "  quarterly:\n"
    "    relation: orders\n"
    "    expr: SUM(amount)\n"
    "    window: 3 months\n"
    "    time_dimension: {name: day, grain: day}\n"
    "    dimensions: [{name: region}]\n"
)


def test_validate_dashboard_reports_a_bad_metric_dimension_once(tmp_path):
    """Lint names the dimension; the tile dry run used to repeat it as 'does not compile'. #543."""
    payload = _validate_metric_tile(
        tmp_path, _WINDOWED_METRICS, "{title: T, metric: {name: quarterly, dimensions: [nope]}}"
    )
    assert payload["valid"] is False
    assert payload["errors"] == [
        "tile 't': metric 'quarterly' has no dimension 'nope' — valid: region"
    ], payload["errors"]


def test_validate_dashboard_reports_a_grain_without_time_dimension_once(tmp_path):
    metrics = (
        "source: {type: duckdb, database: ':memory:'}\n"
        'relations:\n  orders: {sql: "SELECT 1 AS amount"}\n'
        "metrics:\n  revenue: {relation: orders, expr: SUM(amount)}\n"
    )
    payload = _validate_metric_tile(
        tmp_path, metrics, "{title: T, metric: {name: revenue, grain: day}}"
    )
    assert payload["errors"] == [
        "tile 't': grain 'day' set but metric 'revenue' has no time_dimension"
    ], payload["errors"]


def test_validate_dashboard_still_reports_a_metric_tile_only_the_dry_run_sees(tmp_path):
    payload = _validate_metric_tile(
        tmp_path, _WINDOWED_METRICS, "{title: T, metric: {name: quarterly, grain: day}}"
    )
    assert payload["valid"] is False
    assert len(payload["errors"]) == 1, payload["errors"]
    assert "does not compile" in payload["errors"][0], payload["errors"]
    assert "query grain 'day' is finer" in payload["errors"][0], payload["errors"]


def test_an_unrelated_lint_error_does_not_hide_a_metric_tile_compile_failure(tmp_path):
    payload = _validate_metric_tile(
        tmp_path,
        _WINDOWED_METRICS,
        "{title: T, metric: {name: quarterly, grain: day}, compare: previous_period}",
    )
    errors = payload["errors"]
    assert any("has no daterange filter" in e for e in errors), errors
    assert any("does not compile" in e and "is finer" in e for e in errors), errors


def test_validate_dashboard_still_probes_a_metric_tile_lint_flagged_for_something_else(tmp_path):
    metrics = (
        "source: {type: duckdb, database: ':memory:'}\n"
        "relations:\n"
        "  orders: {sql: \"SELECT 1 AS amount, DATE '2026-01-01' AS day\"}\n"
        "metrics:\n"
        "  broken:\n"
        "    relation: orders\n"
        "    expr: SUM(no_such_column)\n"
        "    time_dimension: {name: day, grain: day}\n"
    )
    payload = _validate_metric_tile(
        tmp_path,
        metrics,
        "{title: T, metric: {name: broken, grain: day}, compare: previous_period}",
    )
    errors = payload["errors"]
    assert any("has no daterange filter" in e for e in errors), errors
    assert any("SQL fails against the source" in e for e in errors), errors


def _macro_project(tmp_path, expr: str, relation_sql: str | None = None) -> list:
    relation = f'  base:\n    sql: "{relation_sql}"\n' if relation_sql else ""
    base = "    relation: base\n" if relation_sql else "    table: orders\n"
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: duckdb, database: ':memory:'}\n"
        + (f"relations:\n{relation}" if relation else "")
        + "metrics:\n  active_weeks:\n"
        + base
        + f'    expr: "{expr}"\n'
    )
    store = DashboardStore(tmp_path)
    return lint_project(store, SemanticLayer(store))


def test_lint_reports_an_unknown_trunc_macro_grain(tmp_path):
    """The macro is resolved when a metric compiles, and lint does not compile
    metrics, so a mistyped grain used to lint clean and fail at query time."""
    findings = _macro_project(tmp_path, "COUNT(DISTINCT SQLDASH_TRUNC('fortnight', ordered_at))")
    errors = [f.message for f in findings if f.level == "error"]
    assert any("grain 'fortnight' is not one of" in m for m in errors), findings
    assert all(m.startswith("metric 'active_weeks': ") for m in errors), errors


def test_lint_reports_a_malformed_trunc_macro(tmp_path):
    findings = _macro_project(tmp_path, "COUNT(DISTINCT SQLDASH_TRUNC(week, ordered_at))")
    assert any(
        f.level == "error" and "could not read SQLDASH_TRUNC" in f.message for f in findings
    ), findings


def test_lint_reports_a_bad_trunc_macro_in_a_relation_sql(tmp_path):
    findings = _macro_project(
        tmp_path,
        "SUM(amount)",
        relation_sql="SELECT SQLDASH_TRUNC('fortnight', ordered_at) AS wk FROM orders",
    )
    assert any(
        f.level == "error" and f.message.startswith("relation 'base': ") for f in findings
    ), findings


def test_lint_stays_clean_for_a_well_formed_trunc_macro(tmp_path):
    findings = _macro_project(tmp_path, "COUNT(DISTINCT SQLDASH_TRUNC('week', ordered_at))")
    assert [f for f in findings if f.level == "error"] == [], findings


def test_lint_ignores_a_trunc_macro_the_author_commented_out(tmp_path):
    """#675: lint erred on a grain that appears nowhere in the file except inside
    a comment. Comment text is prose the warehouse throws away."""
    for index, expr in enumerate(
        (
            "SUM(amount) -- SQLDASH_TRUNC('fortnight', ordered_at)",
            "SUM(amount) /* SQLDASH_TRUNC('fortnight', ordered_at) */",
            "SUM(amount) -- SQLDASH_TRUNC(week, ordered_at)",
        )
    ):
        project = tmp_path / f"p{index}"
        project.mkdir()
        findings = _macro_project(project, expr)
        assert [f for f in findings if f.level == "error"] == [], (expr, findings)


def test_lint_still_reports_the_macro_name_inside_a_string_literal(tmp_path):
    """Deliberate and unchanged by #675: telling a literal holding
    `SQLDASH_TRUNC(` from a real call needs a SQL parser, so it stays a
    malformed call. Only comments are exempted."""
    findings = _macro_project(tmp_path, "SUM(CASE WHEN r = 'SQLDASH_TRUNC(' THEN 1 END)")
    assert any(
        f.level == "error" and "could not read SQLDASH_TRUNC" in f.message for f in findings
    ), findings


def _demo_with_metrics(tmp_path, metrics: str):
    create_demo(tmp_path)
    path = tmp_path / ".sqldash" / "metrics.yaml"
    text = path.read_text()
    text = text.replace(
        "relations:\n  orders: {table: orders}\n",
        "relations:\n  orders: {table: orders}\n"
        "  reserved:\n"
        '    sql: SELECT order_date, amount AS "order" FROM orders\n',
        1,
    )
    path.write_text(text.replace("metrics:\n", "metrics:\n" + metrics, 1))
    return DashboardStore(tmp_path)


def test_strict_lint_probes_a_metric_expr_that_cannot_compile(tmp_path):
    """`MAX(order)` passed lint and --strict, then every query was a syntax error."""
    store = _demo_with_metrics(
        tmp_path,
        "  bad_max:\n    relation: reserved\n    expr: MAX(order)\n"
        '  good_max:\n    relation: reserved\n    expr: MAX("order")\n',
    )
    assert not any("bad_max" in f.message for f in lint_project(store, SemanticLayer(store)))
    findings = lint_project(store, SemanticLayer(store), check_sql=True)
    errors = [f.message for f in findings if f.level == "error"]
    assert len(errors) == 1, errors
    assert errors[0].startswith("metric 'bad_max': SQL fails against the source: ")
    assert "syntax error" in errors[0]
    assert "LINE 1" not in errors[0]


def test_strict_lint_passes_the_demo_metrics(tmp_path):
    create_demo(tmp_path)
    store = DashboardStore(tmp_path)
    findings = lint_project(store, SemanticLayer(store), check_sql=True)
    assert findings == [], [f"{f.level}: {f.message}" for f in findings]


def test_strict_lint_probes_the_metric_time_dimension(tmp_path):
    """The probe compiles with the grain, so a time dimension the warehouse
    cannot resolve fails lint and not the first trend tile."""
    store = _demo_with_metrics(
        tmp_path,
        "  bad_trend:\n    relation: orders\n    expr: SUM(amount)\n"
        "    time_dimension: {name: shipped_at, grain: day}\n",
    )
    findings = lint_project(store, SemanticLayer(store), check_sql=True)
    errors = [f.message for f in findings if f.level == "error"]
    assert len(errors) == 1, errors
    assert errors[0].startswith("metric 'bad_trend': SQL fails against the source: ")
    assert "shipped_at" in errors[0]


def test_strict_lint_reports_an_unreachable_metrics_source_once(tmp_path, monkeypatch):
    create_demo(tmp_path)

    def boom(self, sql, bind, row_limit, cancel_token):
        raise ConnectorError("connection failed: could not connect to server")

    monkeypatch.setattr(EngineConnector, "execute", boom)
    store = DashboardStore(tmp_path)
    findings = lint_project(store, SemanticLayer(store), check_sql=True)
    metric_findings = [f.message for f in findings if f.file == "metrics.yaml"]
    assert metric_findings == [
        "cannot probe metrics: connection failed: could not connect to server"
    ]


TZ_METRICS = """
source: {type: snowflake, account: a, database: D, schema: S, username: u}
relations:
  events: {table: EVENTS}
  other_db: {table: OTHER.S.EVENTS}
  from_sql: {sql: SELECT created_at FROM EVENTS}
metrics:
  event_count:
    relation: events
    expr: COUNT(*)
    time_dimension: {name: created_at, grain: month}
  session_count:
    relation: events
    expr: COUNT(*)
    time_dimension: {name: created_at, grain: month, timezone: session}
  ltz_count:
    relation: events
    expr: COUNT(*)
    time_dimension: {name: updated_at, grain: month}
  other_db_count:
    relation: other_db
    expr: COUNT(*)
    time_dimension: {name: created_at, grain: month}
  sql_count:
    relation: from_sql
    expr: COUNT(*)
    time_dimension: {name: created_at, grain: month}
"""


def _fake_snowflake(monkeypatch) -> list[str]:
    pytest.importorskip("snowflake.sqlalchemy")
    introspected: list[str] = []

    def execute(self, sql, bind, row_limit, cancel_token):
        return QueryResult(columns=[], rows=[], row_count=0)

    def introspect(self):
        introspected.append("introspect")
        return [
            TableInfo(
                name="EVENTS",
                schema="S",
                columns=[("CREATED_AT", "TIMESTAMP_TZ"), ("UPDATED_AT", "TIMESTAMP_LTZ")],
            )
        ]

    monkeypatch.setattr(EngineConnector, "execute", execute)
    monkeypatch.setattr(EngineConnector, "introspect", introspect)
    return introspected


def test_strict_lint_warns_on_a_timestamp_tz_time_dimension_without_a_timezone(
    tmp_path, monkeypatch
):
    """A month over a TIMESTAMP_TZ column came back once per offset on Snowflake
    and nothing said `timezone: session` exists. Only --strict has column types,
    so plain lint stays connectionless and says nothing."""
    introspected = _fake_snowflake(monkeypatch)
    (tmp_path / "metrics.yaml").write_text(TZ_METRICS)
    store = DashboardStore(tmp_path)
    assert lint_project(store, SemanticLayer(store)) == []
    assert introspected == []
    findings = lint_project(store, SemanticLayer(store), check_sql=True)
    assert [(f.level, f.message.split(":")[0]) for f in findings] == [
        ("warning", "metric 'event_count'")
    ], findings
    assert "timezone: session" in findings[0].message
    assert introspected == ["introspect"]


def test_strict_lint_opens_one_connection_for_metrics_and_sql_tools(tmp_path, monkeypatch):
    """The metric probe and the sql tool probe each built a registry, so a
    strict run connected to the source twice (two SSO prompts on Snowflake)."""
    create_demo(tmp_path)
    assert "sql:" in (tmp_path / ".sqldash" / "agents.yaml").read_text()
    opened = []
    real = EngineConnector.connect

    def counting(self):
        opened.append(self.source.type)
        real(self)

    monkeypatch.setattr(EngineConnector, "connect", counting)
    store = DashboardStore(tmp_path)
    findings = lint_project(store, SemanticLayer(store), check_sql=True)
    assert findings == [], [f"{f.level}: {f.message}" for f in findings]
    assert opened == ["duckdb"]


def test_strict_lint_skips_the_case_fold_hint_when_the_column_resolved(tmp_path):
    """`sum(DATE)` names the type, not an unresolved column, yet the hint fired
    for any error that mentioned a no-expr dimension's name as a word."""
    store = _demo_with_metrics(
        tmp_path,
        "  sum_of_dates:\n    relation: dated\n    expr: SUM(date)\n"
        "    time_dimension: {name: date, grain: day}\n",
    )
    metrics = tmp_path / ".sqldash" / "metrics.yaml"
    metrics.write_text(
        metrics.read_text().replace(
            "  reserved:\n",
            "  dated:\n    sql: SELECT order_date AS date, amount FROM orders\n  reserved:\n",
            1,
        )
    )
    findings = lint_project(store, SemanticLayer(store), check_sql=True)
    errors = [f.message for f in findings if f.level == "error"]
    assert len(errors) == 1, errors
    assert "sum(DATE)" in errors[0]
    assert "case-folds" not in errors[0]


@pytest.mark.parametrize(
    ("error", "hinted"),
    [
        ("000904 (42000): SQL compilation error:\ninvalid identifier 'ORDER_DATE_LC'", True),
        ("000904 (42000): SQL compilation error:\ninvalid identifier 'O.ORDER_DATE_LC'", True),
        ('column "order_date_lc" does not exist', True),
        ("invalid identifier 'OTHER_COL'", False),
        ("No function matches sum(ORDER_DATE_LC)", False),
    ],
    ids=["snowflake", "snowflake-qualified", "postgres", "other-column", "mentions-name"],
)
def test_strict_lint_case_fold_hint_needs_the_dimension_unresolved(
    tmp_path, monkeypatch, error, hinted
):
    store = _demo_with_metrics(
        tmp_path,
        "  lc_trend:\n    relation: orders\n    expr: SUM(amount)\n"
        "    time_dimension: {name: order_date_lc, grain: day}\n",
    )
    real = EngineConnector.execute

    def warehouse(self, sql, bind, row_limit, cancel_token):
        if "order_date_lc" in sql:
            raise ConnectorError(error)
        return real(self, sql, bind, row_limit, cancel_token)

    monkeypatch.setattr(EngineConnector, "execute", warehouse)
    findings = lint_project(store, SemanticLayer(store), check_sql=True)
    errors = [f.message for f in findings if f.level == "error"]
    assert len(errors) == 1, errors
    assert errors[0].startswith("metric 'lc_trend': SQL fails against the source: ")
    assert ("time_dimension 'order_date_lc' has no expr" in errors[0]) is hinted
