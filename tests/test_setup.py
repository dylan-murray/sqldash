import difflib

import pytest
from typer.testing import CliRunner

from sqldash.cli import app
from sqldash.setup import SetupError, SetupPlan, apply_setup, env_var_name, missing_fields

runner = CliRunner()


def _snowflake(**overrides):
    plan = SetupPlan(
        source_type="snowflake",
        profile="acme",
        account="xy12345",
        warehouse="COMPUTE_WH",
        database="ANALYTICS",
        username="you@acme.com",
        authentication="externalbrowser",
    )
    for key, value in overrides.items():
        setattr(plan, key, value)
    return plan


def test_env_var_name_slugifies_hyphens():
    assert env_var_name("acme-prod", "PASSWORD") == "SQLDASH_ACME_PROD_PASSWORD"


def test_missing_fields_for_each_type():
    assert missing_fields(SetupPlan(source_type="duckdb")) == []
    assert missing_fields(SetupPlan(source_type="snowflake")) == ["account", "username"]
    assert missing_fields(SetupPlan(source_type="snowflake", account="a")) == ["username"]
    assert missing_fields(SetupPlan(source_type="postgres")) == ["host", "database"]
    assert missing_fields(SetupPlan(source_type="url")) == ["url"]
    assert missing_fields(SetupPlan(source_type="databricks", host="h")) == ["http-path"]
    assert missing_fields(
        SetupPlan(source_type="snowflake", account="a", authentication="keypair")
    ) == ["username", "private-key-path"]


def test_duckdb_setup_scaffolds_the_demo(tmp_path):
    result = apply_setup(tmp_path, SetupPlan(source_type="duckdb"))
    assert result.created_demo
    assert result.test_ok is True
    assert (tmp_path / ".sqldash" / "metrics.yaml").is_file()
    assert (tmp_path / ".sqldash" / "demo.yaml").is_file()
    assert result.profiles_path is None


def test_duckdb_rerun_is_idempotent(tmp_path):
    apply_setup(tmp_path, SetupPlan(source_type="duckdb"))
    again = apply_setup(tmp_path, SetupPlan(source_type="duckdb"))
    assert again.created_demo is False
    assert again.test_ok is True


def test_snowflake_writes_profile_and_source_without_secrets(tmp_path):
    profiles = tmp_path / "home" / "profiles.yaml"
    project = tmp_path / "proj"
    result = apply_setup(project, _snowflake(), profiles_file=profiles, skip_test=True)
    assert result.profile == "acme"
    assert result.profiles_path == profiles
    body = profiles.read_text()
    assert "username: you@acme.com" in body
    assert "authentication: externalbrowser" in body
    assert "password:" not in body
    metrics = (project / ".sqldash" / "metrics.yaml").read_text()
    assert "type: snowflake" in metrics
    assert "account: xy12345" in metrics
    assert "profile: acme" in metrics
    assert "password" not in metrics
    assert "you@acme.com" not in metrics


def test_password_auth_is_an_env_ref_not_a_literal(tmp_path):
    profiles = tmp_path / "profiles.yaml"
    apply_setup(
        tmp_path / "proj",
        _snowflake(authentication="password"),
        profiles_file=profiles,
        skip_test=True,
    )
    body = profiles.read_text()
    assert "password: ${env:SQLDASH_ACME_PASSWORD}" in body
    assert "hunter2" not in body


def test_custom_password_env(tmp_path):
    profiles = tmp_path / "profiles.yaml"
    result = apply_setup(
        tmp_path / "proj",
        _snowflake(authentication="password", password_env="SF_PASSWORD"),
        profiles_file=profiles,
        skip_test=True,
    )
    assert "SF_PASSWORD" in (tmp_path / "profiles.yaml").read_text()
    assert result.needed_env == ["SF_PASSWORD"]


def test_rerun_updates_the_profile_and_keeps_metrics(tmp_path):
    profiles = tmp_path / "profiles.yaml"
    profiles.write_text("other:\n  username: stay\n")
    project = tmp_path / "proj"
    apply_setup(project, _snowflake(), profiles_file=profiles, skip_test=True)
    metrics = project / ".sqldash" / "metrics.yaml"
    metrics.write_text(
        metrics.read_text()
        .replace("relations: {}", "relations:\n  orders: {table: orders}")
        .replace("metrics: {}", "metrics:\n  revenue: {relation: orders, expr: SUM(amount)}")
    )
    apply_setup(
        project,
        _snowflake(warehouse="OTHER_WH", username="new@acme.com"),
        profiles_file=profiles,
        skip_test=True,
    )
    after = profiles.read_text()
    assert "username: new@acme.com" in after
    assert "username: stay" in after
    kept = metrics.read_text()
    assert "warehouse: OTHER_WH" in kept
    assert "revenue:" in kept
    assert "orders:" in kept


def test_url_with_literal_password_is_refused(tmp_path):
    with pytest.raises(SetupError, match="literal secret"):
        apply_setup(
            tmp_path,
            SetupPlan(source_type="url", url="postgres://u:hunter2@localhost/db"),
            skip_test=True,
        )


def test_url_with_slash_in_password_is_refused(tmp_path):
    """Hand-split authority at '/' used to miss passwords containing '/'."""
    with pytest.raises(SetupError, match="literal secret"):
        apply_setup(
            tmp_path,
            SetupPlan(source_type="url", url="postgres://user:p/ass@host/db"),
            skip_test=True,
        )
    assert not (tmp_path / ".sqldash" / "metrics.yaml").exists()


def test_url_with_query_string_secret_is_refused(tmp_path):
    with pytest.raises(SetupError, match="literal secret"):
        apply_setup(
            tmp_path,
            SetupPlan(
                source_type="url",
                url="postgres://u@localhost/db?password=hunter2",
            ),
            skip_test=True,
        )
    assert not (tmp_path / ".sqldash" / "metrics.yaml").exists()


def test_url_with_non_secret_query_options_is_ok(tmp_path):
    result = apply_setup(
        tmp_path,
        SetupPlan(
            source_type="url",
            url="postgres://u:${env:DB_PASS}@host/db?sslmode=require&connect_timeout=10",
        ),
        skip_test=True,
    )
    assert "sslmode=require" in result.source["url"]
    assert "connect_timeout=10" in result.source["url"]


def test_url_with_port_and_no_password_is_ok(tmp_path):
    result = apply_setup(
        tmp_path,
        SetupPlan(source_type="url", url="postgres://localhost:5432/db"),
        skip_test=True,
    )
    assert result.source["url"] == "postgres://localhost:5432/db"


def test_url_with_env_ref_is_ok(tmp_path):
    result = apply_setup(
        tmp_path,
        SetupPlan(source_type="url", url="postgres://u:${env:DB_PASS}@localhost/db"),
        profiles_file=tmp_path / "profiles.yaml",
        skip_test=True,
    )
    assert "${env:DB_PASS}" in result.source["url"]
    assert result.profiles_path is None
    assert "hunter2" not in (tmp_path / ".sqldash" / "metrics.yaml").read_text()


def test_url_env_ref_skips_test_and_lists_needed_env(tmp_path):
    result = apply_setup(
        tmp_path,
        SetupPlan(source_type="url", url="postgres://u:${env:DB_PASS}@localhost/db"),
    )
    assert result.test_ok is None
    assert "DB_PASS" in result.needed_env
    assert any("skipped source test" in n for n in result.notes)


def test_password_auth_skips_test_when_env_unset(tmp_path):
    profiles = tmp_path / "profiles.yaml"
    result = apply_setup(
        tmp_path / "proj",
        _snowflake(authentication="password"),
        profiles_file=profiles,
    )
    assert result.test_ok is None
    assert any("skipped source test" in n for n in result.notes)
    assert "SQLDASH_ACME_PASSWORD" in result.needed_env
    assert result.profiles_path == profiles


def test_duckdb_setup_writes_source_into_existing_metrics(tmp_path):
    metrics = tmp_path / ".sqldash" / "metrics.yaml"
    metrics.parent.mkdir(parents=True)
    metrics.write_text(
        "source:\n  type: postgres\n  host: oldhost\n  database: olddb\n"
        "# Relations section\n"
        "relations:\n  orders: {table: orders}\n"
        "metrics:\n  revenue: {relation: orders, expr: SUM(amount)}\n"
    )
    result = apply_setup(tmp_path, SetupPlan(source_type="duckdb"))
    assert result.created_demo is False
    assert result.test_ok is True, result.test_error
    assert result.source.get("attach_files") is not True
    body = metrics.read_text()
    assert "type: duckdb" in body
    assert "oldhost" not in body
    assert "# Relations section" in body
    assert "revenue:" in body


def test_duckdb_setup_adds_source_when_metrics_lacks_one(tmp_path):
    metrics = tmp_path / ".sqldash" / "metrics.yaml"
    metrics.parent.mkdir(parents=True)
    metrics.write_text("relations:\n  orders: {table: orders}\n")
    result = apply_setup(tmp_path, SetupPlan(source_type="duckdb"))
    assert result.test_ok is True, result.test_error
    assert result.source.get("attach_files") is not True
    body = metrics.read_text()
    assert "type: duckdb" in body
    assert "orders:" in body


def test_profiles_yaml_is_owner_only(tmp_path):
    profiles = tmp_path / "profiles.yaml"
    apply_setup(
        tmp_path / "proj",
        _snowflake(),
        profiles_file=profiles,
        skip_test=True,
    )
    assert profiles.stat().st_mode & 0o777 == 0o600


def test_unknown_type_message_names_the_kind(tmp_path):
    with pytest.raises(SetupError, match="unknown type 'banana'"):
        apply_setup(tmp_path, SetupPlan(source_type="banana"), skip_test=True)
    assert missing_fields(SetupPlan(source_type="banana")) == []


def test_cli_unknown_type_names_the_kind():
    result = runner.invoke(app, ["setup", "--type", "banana"])
    assert result.exit_code == 1
    assert "unknown type 'banana'" in result.output
    assert "needs --type" not in result.output


def test_cli_setup_duckdb(tmp_path):
    result = runner.invoke(app, ["setup", str(tmp_path), "--type", "duckdb"])
    assert result.exit_code == 0, result.output
    assert "connected" in result.output
    assert "sqldash serve" in result.output
    assert "ok     source test" not in result.output
    assert (tmp_path / ".sqldash" / "demo.yaml").is_file()


def test_cli_setup_missing_flags():
    result = runner.invoke(app, ["setup", "--type", "snowflake"])
    assert result.exit_code == 1
    assert "--account" in result.output


def test_cli_setup_noninteractive_without_type_fails():
    result = runner.invoke(app, ["setup", "/tmp/unused-setup-dir"])
    assert result.exit_code == 1
    assert "--type" in result.output


def test_cli_setup_snowflake_writes_outside_the_repo(tmp_path, monkeypatch):
    from sqldash import setup as setup_mod

    profiles = tmp_path / "cfg" / "profiles.yaml"

    def _save(name, fields, path=None):
        from sqldash.secrets import save_profile

        return save_profile(name, fields, path=profiles)

    monkeypatch.setattr(setup_mod, "save_profile", _save)
    project = tmp_path / "proj"
    result = runner.invoke(
        app,
        [
            "setup",
            str(project),
            "--type",
            "snowflake",
            "--account",
            "xy12345",
            "--username",
            "you@acme.com",
            "--warehouse",
            "WH",
            "--profile",
            "acme",
            "--skip-test",
        ],
    )
    assert result.exit_code == 0, result.output
    assert profiles.is_file()
    assert "profile: acme" in (project / ".sqldash" / "metrics.yaml").read_text()
    assert "password" not in (project / ".sqldash" / "metrics.yaml").read_text()


def test_source_test_failure_is_reported(tmp_path, monkeypatch):
    from sqldash import setup as setup_mod

    monkeypatch.setattr(
        setup_mod, "_test_source", lambda source, base_dir: (False, "warehouse down")
    )
    result = apply_setup(tmp_path, SetupPlan(source_type="duckdb"))
    assert result.test_ok is False
    assert "warehouse down" in result.test_error


def test_source_test_ignores_broken_metrics(tmp_path):
    """Setup's connection verdict must not re-parse metrics.yaml — a bad metric
    is unrelated to whether the source we just wrote can SELECT 1."""
    metrics = tmp_path / ".sqldash" / "metrics.yaml"
    metrics.parent.mkdir(parents=True)
    metrics.write_text(
        "source: {type: duckdb, attach_files: true}\n"
        "relations:\n  orders: {table: orders}\n"
        "metrics:\n  revenue: {relation: nonexistent, expr: SUM(amount)}\n"
    )
    result = apply_setup(tmp_path, SetupPlan(source_type="duckdb"))
    assert result.test_ok is True, result.test_error
    assert "type: duckdb" in metrics.read_text()


def test_profile_flag_errors_for_url_and_bigquery(tmp_path):
    with pytest.raises(SetupError, match="does not use --profile"):
        apply_setup(
            tmp_path,
            SetupPlan(
                source_type="url",
                url="postgres://u:${env:DB_PASS}@localhost/db",
                profile="acme",
            ),
            skip_test=True,
        )
    with pytest.raises(SetupError, match="does not use --profile"):
        apply_setup(
            tmp_path,
            SetupPlan(source_type="bigquery", project="p", profile="acme"),
            skip_test=True,
        )


def test_url_rejects_profile_credential_flags(tmp_path):
    with pytest.raises(SetupError, match="does not use"):
        apply_setup(
            tmp_path,
            SetupPlan(
                source_type="url",
                url="postgres://u:${env:DB_PASS}@localhost/db",
                username="u",
                authentication="password",
            ),
            skip_test=True,
        )


def test_externalbrowser_skips_connection_test(tmp_path):
    profiles = tmp_path / "profiles.yaml"
    result = apply_setup(
        tmp_path / "proj",
        _snowflake(authentication="externalbrowser", username="you@acme.com"),
        profiles_file=profiles,
    )
    assert result.test_ok is None
    assert any("externalbrowser" in n for n in result.notes)


def test_duckdb_rejects_warehouse_flags(tmp_path):
    with pytest.raises(SetupError, match="does not use --host"):
        apply_setup(
            tmp_path,
            SetupPlan(source_type="duckdb", host="localhost"),
            skip_test=True,
        )


def test_duckdb_writes_database_file_through(tmp_path):
    """--skip-test is the one way to point at a file that does not exist yet;
    the path is still re-anchored on .sqldash/ so serve opens the same file."""
    result = apply_setup(
        tmp_path,
        SetupPlan(source_type="duckdb", database="local.duckdb"),
        skip_test=True,
    )
    assert result.source.get("database") == "../local.duckdb"
    assert "attach_files" not in result.source
    assert result.created_demo is False
    assert result.created_metrics is True
    metrics = (tmp_path / ".sqldash" / "metrics.yaml").read_text()
    assert "database: ../local.duckdb" in metrics
    assert "attach_files" not in metrics
    assert not (tmp_path / ".sqldash" / "demo.yaml").exists()
    assert not list(tmp_path.rglob("*.duckdb"))


def test_duckdb_database_skips_the_sample_scaffold(tmp_path):
    """Setup over a user's database scaffolded the sample orders demo and pointed
    it at that database, so every metric and tile errored while lint said ok. #305."""
    from sqldash.semantics.layer import parse_metrics_file
    from sqldash.setup_prompt import render_setup_result

    _make_duckdb(tmp_path / "my.duckdb")
    result = apply_setup(tmp_path, SetupPlan(source_type="duckdb", database="my.duckdb"))
    assert result.test_ok is True, result.test_error
    assert result.created_demo is False
    assert result.created_metrics is True
    assert result.sample_schema_files == []
    store = tmp_path / ".sqldash"
    assert sorted(p.name for p in store.rglob("*") if p.is_file()) == ["metrics.yaml"]
    parsed = parse_metrics_file((store / "metrics.yaml").read_text())
    assert parsed.relations == {}
    assert parsed.metrics == {}
    assert parsed.source.database == "../my.duckdb"
    text = "\n".join(render_setup_result(result, tmp_path))
    assert "sqldash source describe" in text
    assert "sqldash serve" in text
    lint = runner.invoke(app, ["lint", str(tmp_path)])
    assert lint.exit_code == 0, lint.output
    assert "✓ metrics.yaml" in lint.output
    query = runner.invoke(app, ["metric", "query", "revenue", "--target", str(tmp_path)])
    assert query.exit_code == 1
    assert "no metric named 'revenue'" in query.output
    assert "Catalog Error" not in query.output


def test_setup_over_an_existing_demo_warns_about_the_sample_schema(tmp_path):
    from sqldash.scaffold import create_demo
    from sqldash.setup_prompt import render_setup_result

    _make_duckdb(tmp_path / "my.duckdb")
    create_demo(tmp_path)
    result = apply_setup(tmp_path, SetupPlan(source_type="duckdb", database="my.duckdb"))
    assert result.updated_dashboards == ["demo.yaml"]
    assert result.sample_schema_files == ["demo.yaml", "metrics.yaml"]
    text = "\n".join(render_setup_result(result, tmp_path))
    assert "warning  demo.yaml, metrics.yaml still use the sample orders schema" in text
    assert "sqldash source describe" in text


def test_sample_schema_warning_needs_the_orders_table_itself(tmp_path):
    """`FROM orders_backup` is the user's own table, not the sample schema. #305 review."""
    _make_duckdb(tmp_path / "my.duckdb")
    store = tmp_path / ".sqldash"
    store.mkdir()
    (store / "demo.yaml").write_text(
        "title: Mine\nsource: {type: duckdb, database: ../my.duckdb}\ntiles:\n"
        "  - title: A\n    sql: SELECT count(*) AS n FROM orders_backup\n"
    )
    result = apply_setup(tmp_path, SetupPlan(source_type="duckdb", database="my.duckdb"))
    assert result.sample_schema_files == []


def test_rerun_into_demo_mode_with_no_dashboards_points_at_init_demo(tmp_path):
    """`setup --database x` then plain `setup` used to print a happy `next: serve`
    for a project with no dashboards and no metrics. #305 review."""
    from sqldash.setup_prompt import render_setup_result

    _make_duckdb(tmp_path / "my.duckdb")
    apply_setup(tmp_path, SetupPlan(source_type="duckdb", database="my.duckdb"))
    result = apply_setup(tmp_path, SetupPlan(source_type="duckdb"))
    assert result.created_demo is False
    assert result.no_dashboards is True
    text = "\n".join(render_setup_result(result, tmp_path))
    assert "sqldash init --demo" in text
    fresh = apply_setup(tmp_path / "other", SetupPlan(source_type="duckdb"))
    assert fresh.no_dashboards is False


def test_demo_mode_setup_has_no_sample_schema_warning(tmp_path):
    result = apply_setup(tmp_path, SetupPlan(source_type="duckdb"))
    assert result.created_demo is True
    assert result.created_metrics is False
    assert result.sample_schema_files == []
    memory = apply_setup(
        tmp_path, SetupPlan(source_type="duckdb", database=":memory:"), skip_test=True
    )
    assert memory.sample_schema_files == []


def test_fresh_metrics_for_a_warehouse_has_empty_sections(tmp_path):
    from sqldash.semantics.layer import parse_metrics_file

    project = tmp_path / "proj"
    result = apply_setup(
        project, _snowflake(), profiles_file=tmp_path / "profiles.yaml", skip_test=True
    )
    assert result.created_metrics is True
    parsed = parse_metrics_file((project / ".sqldash" / "metrics.yaml").read_text())
    assert parsed.relations == {}
    assert parsed.metrics == {}
    assert parsed.source.account == "xy12345"


def _make_duckdb(path, table="cities"):
    import duckdb

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(path))
    conn.execute(f"CREATE TABLE {table}(id INTEGER, name VARCHAR)")
    conn.execute(f"INSERT INTO {table} VALUES (1, 'x')")
    conn.close()


def test_duckdb_relative_database_is_anchored_on_the_project_dir(tmp_path):
    """`--database data/cities.duckdb` used to resolve against .sqldash/, where DuckDB
    created an empty copy, SELECT 1 passed, and setup printed connected. #303."""
    from sqldash.execution import ExecutionRegistry
    from sqldash.models.source import Source

    _make_duckdb(tmp_path / "data" / "cities.duckdb")
    result = apply_setup(tmp_path, SetupPlan(source_type="duckdb", database="data/cities.duckdb"))
    assert result.test_ok is True, result.test_error
    assert result.source["database"] == "../data/cities.duckdb"
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*.duckdb")) == [
        "data/cities.duckdb"
    ]
    registry = ExecutionRegistry(max_workers=1)
    try:
        outcome = registry.run_sync(
            Source.model_validate(result.source),
            tmp_path / ".sqldash",
            "SELECT name FROM cities",
            [],
            10,
            timeout=10,
        )
    finally:
        registry.shutdown()
    assert [row[0] for row in outcome.rows] == ["x"]


def test_skip_test_path_is_the_one_serve_opens_once_the_file_exists(tmp_path):
    """--skip-test writes a path for a database that does not exist yet; the
    written value must still resolve from .sqldash/ when the file appears. #303
    review."""
    from sqldash.execution import ExecutionRegistry
    from sqldash.models.source import Source
    from sqldash.project.sources import database_file_missing

    result = apply_setup(
        tmp_path, SetupPlan(source_type="duckdb", database="local.duckdb"), skip_test=True
    )
    assert result.test_ok is None
    assert result.source["database"] == "../local.duckdb"
    source = Source.model_validate(result.source)
    assert "not found" in database_file_missing(source, tmp_path / ".sqldash")
    assert not list(tmp_path.rglob("*.duckdb"))
    _make_duckdb(tmp_path / "local.duckdb")
    assert database_file_missing(source, tmp_path / ".sqldash") is None
    registry = ExecutionRegistry(max_workers=1)
    try:
        outcome = registry.run_sync(
            source, tmp_path / ".sqldash", "SELECT name FROM cities", [], 10, timeout=10
        )
    finally:
        registry.shutdown()
    assert [row[0] for row in outcome.rows] == ["x"]


def test_sqlite_uri_database_is_not_treated_as_a_file_path(tmp_path):
    from sqldash.models.source import Source
    from sqldash.project.sources import database_file_missing

    source = Source.model_validate({"type": "sqlite", "database": "file:data.db?mode=ro"})
    assert database_file_missing(source, tmp_path) is None


def test_duckdb_absolute_database_is_written_verbatim(tmp_path):
    db = tmp_path / "elsewhere" / "cities.duckdb"
    _make_duckdb(db)
    result = apply_setup(tmp_path / "proj", SetupPlan(source_type="duckdb", database=str(db)))
    assert result.test_ok is True, result.test_error
    assert result.source["database"] == str(db)


def test_duckdb_missing_database_is_an_error_before_anything_is_written(tmp_path):
    with pytest.raises(SetupError) as excinfo:
        apply_setup(tmp_path, SetupPlan(source_type="duckdb", database="nope.duckdb"))
    message = str(excinfo.value)
    assert message.startswith("database file not found: nope.duckdb (resolved to ")
    assert str(tmp_path / "nope.duckdb") in message
    assert ":memory:" in message
    assert not (tmp_path / ".sqldash" / "metrics.yaml").exists()
    assert not list(tmp_path.rglob("*.duckdb"))


def test_duckdb_memory_database_needs_no_file(tmp_path):
    result = apply_setup(tmp_path, SetupPlan(source_type="duckdb", database=":memory:"))
    assert result.test_ok is True, result.test_error


def test_rerun_updates_dashboards_on_the_previous_project_source(tmp_path):
    """Re-running setup rewrote source: only in metrics.yaml; demo.yaml kept the
    old database and silently queried an empty schema. #304."""
    from sqldash.setup_prompt import render_setup_result

    first_db = tmp_path / "my.duckdb"
    second_db = tmp_path / "other.duckdb"
    _make_duckdb(first_db)
    _make_duckdb(second_db)
    apply_setup(tmp_path, SetupPlan(source_type="duckdb"))
    apply_setup(tmp_path, SetupPlan(source_type="duckdb", database="my.duckdb"))
    demo = tmp_path / ".sqldash" / "demo.yaml"
    before = demo.read_text().splitlines()
    assert "  database: ../my.duckdb" in before
    result = apply_setup(tmp_path, SetupPlan(source_type="duckdb", database=str(second_db)))
    assert result.updated_dashboards == ["demo.yaml"]
    assert result.stale_dashboards == []
    after = demo.read_text().splitlines()
    assert f"  database: {second_db}" in after
    delta = [line for line in difflib.ndiff(before, after) if line[:2] in ("- ", "+ ")]
    assert delta == ["-   database: ../my.duckdb", f"+   database: {second_db}"], delta
    assert before[0].startswith("# Demo dashboard")
    assert after[0] == before[0]
    metrics = (tmp_path / ".sqldash" / "metrics.yaml").read_text()
    assert f"database: {second_db}" in metrics
    assert "  updated  demo.yaml source" in render_setup_result(result, tmp_path)


def test_a_failed_connection_test_restores_metrics_and_dashboards(tmp_path, monkeypatch):
    """A fat-fingered source must not leave the project pointing at something
    that does not connect: the files setup rewrote go back to their exact
    bytes and the FAILED block says so. #304 review."""
    from sqldash import setup as setup_mod
    from sqldash.scaffold import create_demo
    from sqldash.setup_prompt import render_setup_result

    first_db = tmp_path / "my.duckdb"
    second_db = tmp_path / "other.duckdb"
    _make_duckdb(first_db)
    _make_duckdb(second_db)
    create_demo(tmp_path)
    apply_setup(tmp_path, SetupPlan(source_type="duckdb", database="my.duckdb"))
    demo = tmp_path / ".sqldash" / "demo.yaml"
    metrics = tmp_path / ".sqldash" / "metrics.yaml"
    demo_before, metrics_before = demo.read_text(), metrics.read_text()
    monkeypatch.setattr(setup_mod, "_test_source", lambda source, base_dir: (False, "boom"))
    result = apply_setup(tmp_path, SetupPlan(source_type="duckdb", database=str(second_db)))
    assert result.test_ok is False
    assert result.updated_dashboards == []
    assert result.restored_files == ["metrics.yaml", "demo.yaml"]
    assert demo.read_text() == demo_before
    assert metrics.read_text() == metrics_before
    text = "\n".join(render_setup_result(result, tmp_path))
    assert "restored  metrics.yaml, demo.yaml to what they were" in text
    assert "updated" not in text


def test_an_unparseable_dashboard_is_named_as_such_not_as_stale(tmp_path):
    from sqldash.setup_prompt import render_setup_result

    _make_duckdb(tmp_path / "my.duckdb")
    _make_duckdb(tmp_path / "other.duckdb")
    apply_setup(tmp_path, SetupPlan(source_type="duckdb", database="my.duckdb"))
    (tmp_path / ".sqldash" / "broken.yaml").write_text(
        "title: Broken\nsource: {type: duckdb\ntiles: [\n"
    )
    result = apply_setup(tmp_path, SetupPlan(source_type="duckdb", database="other.duckdb"))
    assert result.unreadable_dashboards == ["broken.yaml"]
    assert result.stale_dashboards == []
    text = "\n".join(render_setup_result(result, tmp_path))
    assert "broken.yaml could not be parsed; left alone" in text
    assert "different source" not in text


def test_rerun_leaves_a_dashboard_on_another_source_and_warns(tmp_path):
    from sqldash.setup_prompt import render_setup_result

    apply_setup(tmp_path, SetupPlan(source_type="duckdb"))
    mine = tmp_path / ".sqldash" / "mine.yaml"
    mine.write_text(
        "title: Mine\n"
        "# keep me\n"
        "source: {type: postgres, host: h, database: d}\n"
        "tiles:\n  - {title: T, sql: SELECT 1}\n"
    )
    original = mine.read_text()
    result = apply_setup(
        tmp_path, SetupPlan(source_type="duckdb", database=":memory:"), skip_test=True
    )
    assert result.updated_dashboards == ["demo.yaml"]
    assert result.stale_dashboards == ["mine.yaml"]
    assert mine.read_text() == original
    text = "\n".join(render_setup_result(result, tmp_path))
    assert "warning  mine.yaml still points at a different source" in text
    assert "demo.yaml still points" not in text


def test_rerun_with_the_same_source_touches_no_dashboard(tmp_path):
    apply_setup(tmp_path, SetupPlan(source_type="duckdb"))
    demo = tmp_path / ".sqldash" / "demo.yaml"
    original = demo.read_text()
    result = apply_setup(tmp_path, SetupPlan(source_type="duckdb"))
    assert result.updated_dashboards == []
    assert result.stale_dashboards == []
    assert demo.read_text() == original


def test_cli_rerun_prints_the_stale_warning_on_stdout(tmp_path):
    """The warning is the only notice that a dashboard was left behind; on a
    successful run it has to be where the rest of the output is. #304 review."""
    _make_duckdb(tmp_path / "a.duckdb")
    _make_duckdb(tmp_path / "b.duckdb")
    scaffold = runner.invoke(app, ["init", "--demo", str(tmp_path)])
    assert scaffold.exit_code == 0, scaffold.output
    first = runner.invoke(
        app, ["setup", str(tmp_path), "--type", "duckdb", "--database", "a.duckdb"]
    )
    assert first.exit_code == 0, first.output
    demo = tmp_path / ".sqldash" / "demo.yaml"
    assert "../a.duckdb" in demo.read_text()
    demo.write_text(demo.read_text().replace("../a.duckdb", "elsewhere.duckdb", 1))
    second = runner.invoke(
        app, ["setup", str(tmp_path), "--type", "duckdb", "--database", "b.duckdb"]
    )
    assert second.exit_code == 0, second.output
    assert "warning  demo.yaml still points at a different source" in second.stdout
    assert "elsewhere.duckdb" in demo.read_text()


def test_cli_rerun_reports_the_updated_dashboard(tmp_path):
    _make_duckdb(tmp_path / "a.duckdb")
    _make_duckdb(tmp_path / "b.duckdb")
    demo_run = runner.invoke(app, ["setup", str(tmp_path), "--type", "duckdb"])
    assert demo_run.exit_code == 0, demo_run.output
    first = runner.invoke(
        app, ["setup", str(tmp_path), "--type", "duckdb", "--database", "a.duckdb"]
    )
    assert first.exit_code == 0, first.output
    assert "still use the sample orders schema" in first.output
    second = runner.invoke(
        app, ["setup", str(tmp_path), "--type", "duckdb", "--database", "b.duckdb"]
    )
    assert second.exit_code == 0, second.output
    assert "updated  demo.yaml source" in second.output
    demo = (tmp_path / ".sqldash" / "demo.yaml").read_text()
    assert "database: ../b.duckdb" in demo
    assert "a.duckdb" not in demo


def test_test_source_refuses_a_missing_duckdb_file(tmp_path):
    """DuckDB creates the file on connect, so SELECT 1 alone proves nothing."""
    from sqldash.models.source import Source
    from sqldash.setup import _test_source

    ok, error = _test_source(Source(type="duckdb", database="ghost.duckdb"), tmp_path)
    assert ok is False
    assert error.startswith("database file not found: ghost.duckdb")
    assert not (tmp_path / "ghost.duckdb").exists()


def test_cli_setup_duckdb_missing_database_exits(tmp_path):
    result = runner.invoke(
        app, ["setup", str(tmp_path), "--type", "duckdb", "--database", "data/cities.duckdb"]
    )
    assert result.exit_code == 1, result.output
    assert "error: database file not found: data/cities.duckdb (resolved to " in result.output
    assert "connected" not in result.output
    assert not list(tmp_path.rglob("*.duckdb"))


def test_duckdb_without_database_still_attaches_files(tmp_path):
    result = apply_setup(tmp_path, SetupPlan(source_type="duckdb"), skip_test=True)
    assert result.source.get("attach_files") is True
    assert "database" not in result.source


def test_duckdb_memory_database_still_attaches_demo_files(tmp_path):
    """`:memory:` is not a file. The engine scans the project dir, where
    create_demo just wrote orders.csv — attach_files is that mode. #189."""
    from sqldash.execution import ExecutionRegistry
    from sqldash.models.source import Source

    result = apply_setup(
        tmp_path, SetupPlan(source_type="duckdb", database=":memory:"), skip_test=True
    )
    assert result.source.get("database") == ":memory:"
    assert result.source.get("attach_files") is True
    metrics = (tmp_path / ".sqldash" / "metrics.yaml").read_text()
    assert "attach_files: true" in metrics
    assert ":memory:" in metrics
    demo = (tmp_path / ".sqldash" / "demo.yaml").read_text()
    assert "attach_files: true" in demo
    source = Source.model_validate(result.source)
    registry = ExecutionRegistry(max_workers=1)
    try:
        outcome = registry.run_sync(
            source, tmp_path / ".sqldash", "SELECT region FROM orders LIMIT 1", [], 1, timeout=10
        )
    finally:
        registry.shutdown()
    assert outcome.rows, outcome


def test_duckdb_database_does_not_attach_sibling_csvs(tmp_path):
    """attach_files scans the database file's directory. A sibling orders.csv
    next to a real orders table is a Catalog Error on every query. #184."""
    import duckdb

    from sqldash.execution import ExecutionRegistry
    from sqldash.models.source import Source

    dbdir = tmp_path / "db"
    dbdir.mkdir()
    (dbdir / "orders.csv").write_text(
        "order_date,region,category,amount\n2026-01-01,csvwin,cat,1.0\n"
    )
    db = dbdir / "setupdb.duckdb"
    conn = duckdb.connect(str(db))
    conn.execute(
        "CREATE TABLE orders (order_date DATE, region VARCHAR, category VARCHAR, amount DOUBLE)"
    )
    conn.execute("INSERT INTO orders VALUES ('2026-01-01', 'dbwin', 'cat', 2.0)")
    conn.close()

    proj = tmp_path / "proj"
    result = apply_setup(proj, SetupPlan(source_type="duckdb", database=str(db)), skip_test=True)
    assert "attach_files" not in result.source
    source = Source.model_validate(result.source)
    registry = ExecutionRegistry(max_workers=1)
    try:
        outcome = registry.run_sync(
            source, proj / ".sqldash", "SELECT region FROM orders", [], 10, timeout=10
        )
    finally:
        registry.shutdown()
    regions = [row[0] for row in outcome.rows]
    assert regions == ["dbwin"]


def test_default_profile_name_is_a_valid_identifier(tmp_path):
    from sqldash.setup import default_profile_name

    numeric = tmp_path / "123"
    numeric.mkdir()
    name = default_profile_name(numeric)
    assert name[0].isalpha(), name
    result = apply_setup(
        numeric,
        SetupPlan(source_type="snowflake", account="xy12345", username="you@acme.com"),
        profiles_file=tmp_path / "profiles.yaml",
        skip_test=True,
    )
    assert result.profile == name


def test_skip_test_still_explains_unset_env(tmp_path):
    result = apply_setup(
        tmp_path / "proj",
        _snowflake(authentication="password"),
        profiles_file=tmp_path / "profiles.yaml",
        skip_test=True,
    )
    assert result.test_ok is None
    assert any("skipped source test" in n for n in result.notes)
    assert "SQLDASH_ACME_PASSWORD" in result.needed_env


def test_register_failure_is_an_error(tmp_path, monkeypatch):
    from sqldash import setup as setup_mod
    from sqldash.workspace import WorkspaceError

    def _boom(target, name=None, branch=None):
        raise WorkspaceError("a repo named 'edge3' is already registered — pass --name")

    monkeypatch.setattr(setup_mod, "add_repo", _boom)
    with pytest.raises(SetupError, match="already registered"):
        apply_setup(tmp_path, SetupPlan(source_type="duckdb"), register=True)


def test_snowflake_requires_username(tmp_path):
    with pytest.raises(SetupError, match="--username"):
        apply_setup(
            tmp_path,
            SetupPlan(source_type="snowflake", account="xy12345"),
            profiles_file=tmp_path / "profiles.yaml",
            skip_test=True,
        )


def test_rerun_keeps_username_when_flag_omitted(tmp_path):
    profiles = tmp_path / "profiles.yaml"
    project = tmp_path / "proj"
    apply_setup(project, _snowflake(), profiles_file=profiles, skip_test=True)
    apply_setup(
        project,
        SetupPlan(
            source_type="snowflake",
            profile="acme",
            account="xy12345",
            warehouse="OTHER_WH",
            username="you@acme.com",
        ),
        profiles_file=profiles,
        skip_test=True,
    )
    # omit username this time — must not wipe the one already written
    apply_setup(
        project,
        SetupPlan(
            source_type="snowflake",
            profile="acme",
            account="xy12345",
            username="you@acme.com",
        ),
        profiles_file=profiles,
        skip_test=True,
    )
    assert "username: you@acme.com" in profiles.read_text()


def test_rerun_merges_profile_and_does_not_drop_username(tmp_path):
    """save_profile used to replace the named entry wholesale, so a re-run
    that only changed warehouse (on the source) wiped username if the
    merge did not keep unmentioned keys."""
    profiles = tmp_path / "profiles.yaml"
    from sqldash.secrets import save_profile

    save_profile(
        "acme",
        {"username": "alice@acme.com", "authentication": "externalbrowser"},
        path=profiles,
    )
    save_profile("acme", {"authentication": "password"}, path=profiles)
    body = profiles.read_text()
    assert "username: alice@acme.com" in body
    assert "authentication: password" in body


def test_setup_on_a_file_is_a_clean_error(tmp_path):
    target = tmp_path / "dash.yaml"
    target.write_text("title: X\n")
    with pytest.raises(SetupError, match="not a directory"):
        apply_setup(target, SetupPlan(source_type="duckdb"), skip_test=True)


def test_corrupt_metrics_is_a_clean_error(tmp_path):
    metrics = tmp_path / ".sqldash" / "metrics.yaml"
    metrics.parent.mkdir()
    metrics.write_text("source: [\n")
    with pytest.raises(SetupError, match="invalid YAML"):
        apply_setup(tmp_path, SetupPlan(source_type="duckdb"), skip_test=True)


def test_bypass_rls_query_param_is_not_a_secret(tmp_path):
    result = apply_setup(
        tmp_path,
        SetupPlan(
            source_type="url",
            url="postgres://u:${env:DB_PASS}@host/db?bypass_rls=on",
        ),
        skip_test=True,
    )
    assert "bypass_rls=on" in result.source["url"]


def test_sslpassword_query_param_is_a_secret(tmp_path):
    with pytest.raises(SetupError, match="literal secret"):
        apply_setup(
            tmp_path,
            SetupPlan(
                source_type="url",
                url="postgres://u@host/db?sslpassword=hunter2",
            ),
            skip_test=True,
        )
    assert not (tmp_path / ".sqldash" / "metrics.yaml").exists()


def test_postgres_without_password_env_has_no_password(tmp_path):
    profiles = tmp_path / "profiles.yaml"
    result = apply_setup(
        tmp_path / "proj",
        SetupPlan(
            source_type="postgres",
            host="localhost",
            database="mydb",
            username="ada",
            profile="localpg",
        ),
        profiles_file=profiles,
        skip_test=True,
    )
    body = profiles.read_text()
    assert "username: ada" in body
    assert "password:" not in body
    assert result.needed_env == []


def test_postgres_password_env_is_opt_in(tmp_path):
    profiles = tmp_path / "profiles.yaml"
    apply_setup(
        tmp_path / "proj",
        SetupPlan(
            source_type="postgres",
            host="localhost",
            database="mydb",
            profile="localpg",
            password_env="PGPASSWORD",
        ),
        profiles_file=profiles,
        skip_test=True,
    )
    assert "password: ${env:PGPASSWORD}" in profiles.read_text()


def test_metrics_path_prefers_sqldash_dir_over_flat_file(tmp_path):
    """Store root is .sqldash/ whenever that directory exists — setup must
    write the file serve/source test will actually read."""
    (tmp_path / ".sqldash").mkdir()
    (tmp_path / ".sqldash" / "dash.yaml").write_text(
        "title: D\nsource: {type: duckdb}\ntiles: []\n"
    )
    (tmp_path / "metrics.yaml").write_text("source: {type: postgres, host: old, database: old}\n")
    result = apply_setup(tmp_path, SetupPlan(source_type="duckdb"), skip_test=True)
    assert result.metrics_path == tmp_path / ".sqldash" / "metrics.yaml"
    assert "type: duckdb" in result.metrics_path.read_text()
    assert "postgres" in (tmp_path / "metrics.yaml").read_text()


def test_register_same_path_is_idempotent(tmp_path, monkeypatch):
    from sqldash import setup as setup_mod
    from sqldash.workspace import add_repo

    registry = tmp_path / "repos.yaml"
    monkeypatch.setattr(
        setup_mod,
        "load_registry",
        lambda path=None: __import__("sqldash.workspace", fromlist=["load_registry"]).load_registry(
            registry
        ),
    )
    monkeypatch.setattr(
        setup_mod,
        "add_repo",
        lambda target, name=None, branch=None, path=None: add_repo(
            target, name=name, branch=branch, path=registry
        ),
    )
    project = tmp_path / "proj"
    project.mkdir()
    first = apply_setup(project, SetupPlan(source_type="duckdb"), register=True, skip_test=True)
    assert any("registered as" in n for n in first.notes)
    again = apply_setup(project, SetupPlan(source_type="duckdb"), register=True, skip_test=True)
    assert any("already registered" in n for n in again.notes)


def test_register_error_does_not_mention_name_flag(tmp_path, monkeypatch):
    from sqldash import setup as setup_mod

    monkeypatch.setattr(
        setup_mod, "load_registry", lambda path=None: {"proj": {"path": "/somewhere/else"}}
    )
    monkeypatch.setattr(setup_mod, "default_repo_name", lambda target: "proj")
    with pytest.raises(SetupError, match="drop --register") as exc:
        apply_setup(tmp_path, SetupPlan(source_type="duckdb"), register=True, skip_test=True)
    assert "--name" not in str(exc.value)


def test_register_collision_masks_the_registered_url_credentials(tmp_path, monkeypatch):
    from sqldash import setup as setup_mod

    url = "https://alice:ghp_fakeToken123@github.com/acme/proj.git"
    monkeypatch.setattr(setup_mod, "load_registry", lambda path=None: {"proj": {"url": url}})
    monkeypatch.setattr(setup_mod, "default_repo_name", lambda target: "proj")
    with pytest.raises(SetupError, match="already registered") as exc:
        apply_setup(tmp_path, SetupPlan(source_type="duckdb"), register=True, skip_test=True)
    assert "ghp_fakeToken123" not in str(exc.value)
    assert "github.com/acme/proj.git" in str(exc.value)


def test_postgres_rejects_snowflake_flags(tmp_path):
    with pytest.raises(SetupError, match="does not use --account"):
        apply_setup(
            tmp_path,
            SetupPlan(source_type="postgres", host="h", database="d", account="xy"),
            skip_test=True,
        )


def test_malformed_registry_is_a_clean_error(tmp_path, monkeypatch):
    from sqldash import setup as setup_mod
    from sqldash.workspace import WorkspaceError

    def _bad(path=None):
        raise WorkspaceError("repos.yaml: must be a YAML mapping")

    monkeypatch.setattr(setup_mod, "load_registry", _bad)
    with pytest.raises(SetupError, match="must be a YAML mapping"):
        apply_setup(tmp_path, SetupPlan(source_type="duckdb"), register=True, skip_test=True)


def test_unknown_query_literal_is_refused(tmp_path):
    with pytest.raises(SetupError, match="literal secret"):
        apply_setup(
            tmp_path,
            SetupPlan(source_type="url", url="postgres://u@host/db?api_token=hunter2"),
            skip_test=True,
        )


class _Script:
    def __init__(self, chooses, asks):
        self.chooses = list(chooses)
        self.asks = list(asks)
        self.ask_labels = []

    def choose(self, title, options, default, **kwargs):
        if not self.chooses:
            raise AssertionError(f"unexpected choose: {title}")
        return self.chooses.pop(0)

    def ask(self, label, required=True, default=None, **kwargs):
        self.ask_labels.append(label)
        if not self.asks:
            raise AssertionError(f"unexpected ask: {label}")
        raw = self.asks.pop(0)
        if raw == "" and default:
            return default
        if raw == "" and not required:
            return None
        return raw or None


def test_wizard_default_warehouse_is_duckdb(tmp_path):
    from sqldash.setup_prompt import WAREHOUSES, prompt_setup

    seen = []

    def choose(title, options, default, **kwargs):
        seen.append((title, default, [v for v, _ in options]))
        return default

    plan = prompt_setup(
        tmp_path,
        choose=choose,
        ask=lambda *a, **k: None,
        echo=lambda *a, **k: None,
        existing_profile=lambda n: None,
    )
    assert plan.source_type == "duckdb"
    assert seen[0][0] == "warehouse"
    assert seen[0][1] == "duckdb"
    assert [v for v, _ in WAREHOUSES] == seen[0][2]


def test_wizard_optional_blank_is_none(tmp_path):
    from sqldash.setup_prompt import prompt_setup

    script = _Script(["postgres"], ["", "localhost", "mydb", "", ""])
    plan = prompt_setup(
        tmp_path,
        choose=script.choose,
        ask=script.ask,
        echo=lambda *a, **k: None,
        existing_profile=lambda n: None,
    )
    assert script.chooses == []
    assert script.asks == []
    assert plan.source_type == "postgres"
    assert plan.host == "localhost"
    assert plan.database == "mydb"
    assert plan.username is None
    assert plan.password_env is None


def test_wizard_env_var_prompt_names_the_variable(tmp_path):
    from sqldash.setup_prompt import prompt_setup

    script = _Script(["snowflake", "password"], ["acme", "xy12345", "", "", "you@acme.com", ""])
    plan = prompt_setup(
        tmp_path,
        choose=script.choose,
        ask=script.ask,
        echo=lambda *a, **k: None,
        existing_profile=lambda n: None,
    )
    assert script.asks == []
    assert plan.authentication == "password"
    assert plan.password_env.startswith("SQLDASH_")
    assert "hunter2" not in (plan.password_env or "")
    assert any("env var that will hold the password" in t for t in script.ask_labels)


def test_wizard_fills_missing_flags_only(tmp_path):
    from sqldash.setup import SetupPlan
    from sqldash.setup_prompt import prompt_setup

    script = _Script([], ["xy12345", "you@acme.com"])
    plan = prompt_setup(
        tmp_path,
        SetupPlan(source_type="snowflake"),
        choose=script.choose,
        ask=script.ask,
        echo=lambda *a, **k: None,
        existing_profile=lambda n: None,
    )
    assert script.asks == []
    assert plan.account == "xy12345"
    assert plan.username == "you@acme.com"
    assert plan.warehouse is None


def test_wizard_keep_existing_profile_skips_credentials(tmp_path):
    from sqldash.setup_prompt import prompt_setup

    script = _Script(["postgres", "keep"], ["myproj", "localhost", "db"])
    plan = prompt_setup(
        tmp_path,
        choose=script.choose,
        ask=script.ask,
        echo=lambda *a, **k: None,
        existing_profile=lambda n: {"username": "alice"} if n == "myproj" else None,
    )
    assert script.asks == []
    assert plan.keep_profile is True
    assert plan.username == "alice"
    assert plan.host == "localhost"
    assert plan.password_env is None


def test_wizard_overwrite_existing_profile_asks_credentials(tmp_path):
    from sqldash.setup_prompt import prompt_setup

    script = _Script(["postgres", "overwrite"], ["myproj", "localhost", "db", "bob", ""])
    plan = prompt_setup(
        tmp_path,
        choose=script.choose,
        ask=script.ask,
        echo=lambda *a, **k: None,
        existing_profile=lambda n: {"username": "alice"} if n == "myproj" else None,
    )
    assert script.asks == []
    assert plan.keep_profile is False
    assert plan.username == "bob"


def test_keep_profile_does_not_rewrite_profiles_yaml(tmp_path):
    from sqldash.secrets import save_profile

    profiles = tmp_path / "profiles.yaml"
    save_profile("acme", {"username": "alice", "password": "${env:OLD_PASS}"}, path=profiles)
    before = profiles.read_text()
    result = apply_setup(
        tmp_path / "proj",
        SetupPlan(
            source_type="postgres",
            host="localhost",
            database="db",
            profile="acme",
            username="bob",
            keep_profile=True,
        ),
        profiles_file=profiles,
        skip_test=True,
    )
    assert profiles.read_text() == before
    assert "bob" not in profiles.read_text()
    assert result.source.get("profile") == "acme"
    assert "OLD_PASS" in result.needed_env


def test_menu_choose_uses_questionary_select(monkeypatch):
    from sqldash import setup_prompt as sp

    seen: dict = {}

    class _Q:
        def ask(self):
            return "postgres"

    def select(message, choices=None, **kwargs):
        seen["message"] = message
        seen["values"] = [c.value for c in choices]
        seen["arrows"] = kwargs.get("use_arrow_keys")
        return _Q()

    monkeypatch.setattr(sp.questionary, "select", select)
    assert sp._menu_choose("warehouse", sp.WAREHOUSES, "duckdb") == "postgres"
    assert seen["message"] == "warehouse"
    assert seen["values"][0] == "duckdb"
    assert seen["arrows"] is True


def test_render_setup_result_is_not_a_write_log(tmp_path):
    from sqldash.setup_prompt import render_setup_result

    result = apply_setup(tmp_path, SetupPlan(source_type="duckdb"))
    text = "\n".join(render_setup_result(result, tmp_path))
    assert text.startswith("ready  ")
    assert "connected" in text
    assert "sqldash serve" in text
    assert "wrote  " not in text
    assert "ok     source test" not in text
    assert "add a metric" not in text


def test_render_setup_result_failed_is_loud(tmp_path, monkeypatch):
    from sqldash import setup as setup_mod
    from sqldash.setup_prompt import render_setup_result

    monkeypatch.setattr(
        setup_mod,
        "_test_source",
        lambda source, base_dir: (False, "failed to resolve host 'smokeweed'"),
    )
    result = apply_setup(tmp_path, SetupPlan(source_type="duckdb"))
    text = "\n".join(render_setup_result(result, tmp_path))
    assert "FAILED  could not connect" in text
    assert "smokeweed" in text
    assert "ready" not in text
    assert "sqldash serve" not in text
    assert "re-run:  sqldash setup" in text


def test_cli_setup_connection_failure_exits(tmp_path, monkeypatch):
    from sqldash import setup as setup_mod

    monkeypatch.setattr(
        setup_mod,
        "_test_source",
        lambda source, base_dir: (False, "failed to resolve host 'smokeweed'"),
    )
    result = runner.invoke(app, ["setup", str(tmp_path), "--type", "duckdb"])
    assert result.exit_code == 1, result.output
    assert "FAILED" in result.output
    assert "smokeweed" in result.output
    assert "sqldash serve" not in result.output


def test_render_setup_result_unset_env_and_sso(tmp_path):
    from sqldash.setup_prompt import render_setup_result

    profiles = tmp_path / "profiles.yaml"
    password = apply_setup(
        tmp_path / "pw",
        _snowflake(authentication="password"),
        profiles_file=profiles,
    )
    text = "\n".join(render_setup_result(password, tmp_path / "pw", env={}))
    assert "export SQLDASH_ACME_PASSWORD=…" in text
    sso = apply_setup(
        tmp_path / "sso",
        _snowflake(),
        profiles_file=profiles,
    )
    sso_text = "\n".join(render_setup_result(sso, tmp_path / "sso", env={}))
    assert "browser SSO" in sso_text


def test_cli_setup_tty_asks_missing_flags(tmp_path, monkeypatch):
    from sqldash import setup as setup_mod

    profiles = tmp_path / "cfg" / "profiles.yaml"

    def _save(name, fields, path=None):
        from sqldash.secrets import save_profile

        return save_profile(name, fields, path=profiles)

    monkeypatch.setattr(setup_mod, "save_profile", _save)
    monkeypatch.setattr("sqldash.cli._stdin_is_tty", lambda: True)
    answers = iter(["xy12345", "you@acme.com"])
    monkeypatch.setattr(
        "sqldash.setup_prompt._menu_ask",
        lambda label, required=True, default=None, **k: next(answers),
    )
    project = tmp_path / "proj"
    result = runner.invoke(
        app,
        ["setup", str(project), "--type", "snowflake", "--profile", "acme", "--skip-test"],
    )
    assert result.exit_code == 0, result.output
    assert "ready" in result.output
    assert profiles.is_file()


def test_malformed_env_ref_in_url_password_is_refused(tmp_path):
    with pytest.raises(SetupError, match="literal secret"):
        apply_setup(
            tmp_path,
            SetupPlan(source_type="url", url="postgres://u:${env:DB-PASS}@host/db"),
            skip_test=True,
        )


@pytest.mark.parametrize("has_files", [False, True])
def test_existing_project_duckdb_setup_attaches_only_available_files(tmp_path, has_files):
    metrics = tmp_path / ".sqldash" / "metrics.yaml"
    metrics.parent.mkdir()
    metrics.write_text("# Keep this comment\nsource: {type: postgres}\nrelations: {}\n")
    if has_files:
        data = metrics.parent / "data"
        data.mkdir()
        (data / "orders.csv").write_text("amount\n42\n")
    result = apply_setup(tmp_path, SetupPlan(source_type="duckdb"))
    assert result.test_ok is True, result.test_error
    assert bool(result.source.get("attach_files")) is has_files
    assert result.restored_files == []
    assert "# Keep this comment" in metrics.read_text()
    assert "type: postgres" not in metrics.read_text()
    if not has_files:
        assert result.source["database"] == ":memory:"
        assert "attach_files" not in metrics.read_text()
        assert not (metrics.parent / "data").exists()


def test_duckdb_setup_without_files_preserves_existing_dashboard(tmp_path):
    root = tmp_path / ".sqldash"
    root.mkdir()
    dashboard = root / "demo.yaml"
    before = "title: Existing\nsource: {type: duckdb, database: ':memory:'}\ntiles: []\n"
    dashboard.write_text(before)
    result = apply_setup(tmp_path, SetupPlan(source_type="duckdb"))
    assert result.test_ok is True, result.test_error
    assert result.created_demo is False
    assert result.source == {"type": "duckdb", "database": ":memory:"}
    assert dashboard.read_text() == before
    assert not (root / "data").exists()
