import pytest

from sqldash.project.store import ConflictError, DashboardStore, compute_etag

DOC = """\
# my dashboard — hand-written comment that must survive
title: Test
source: {type: duckdb, database: ':memory:'}

queries:
  q1: |
    SELECT 1 AS n
  q2: |
    SELECT 2 AS m

tiles:
  # trend comes first
  - id: w1
    query: q1
    position: {x: 0, y: 0, w: 6, h: 3}
    chart: {type: line, x: n, y: [n]}
  - id: w2
    query: q2
    position: {x: 6, y: 0, w: 6, h: 3}
"""


@pytest.fixture
def store(tmp_path):
    (tmp_path / "test.yaml").write_text(DOC)
    return DashboardStore(tmp_path), tmp_path / "test.yaml"


def test_position_update_is_surgical(store):
    s, path = store
    etag = compute_etag(DOC)
    s.update_positions("test", {"w1": {"x": 0, "y": 3, "w": 12, "h": 4}}, etag)
    text = path.read_text()
    assert "position: {x: 0, y: 3, w: 12, h: 4}" in text
    assert "# my dashboard — hand-written comment that must survive" in text
    assert "# trend comes first" in text
    changed = [(a, b) for a, b in zip(DOC.splitlines(), text.splitlines(), strict=False) if a != b]
    assert len(changed) == 1
    assert "position" in changed[0][1]


def test_position_update_conflict(store):
    s, _ = store
    with pytest.raises(ConflictError):
        s.update_positions("test", {"w1": {"x": 0, "y": 0, "w": 1, "h": 1}}, "wrong-etag")


def test_upsert_new_tile_and_query(store):
    s, path = store
    s.upsert_tile(
        "test",
        {
            "id": "w3",
            "query": "q3",
            "title": "New tile",
            "position": {"x": 0, "y": 6, "w": 6, "h": 3},
            "chart": {"type": "bar", "x": "a", "y": ["b"], "stacked": False},
        },
        sql="SELECT 'a' AS a, 3 AS b",
        if_match=None,
    )
    text = path.read_text()
    dashboard, _, _ = s.load("test")
    assert "q3" in dashboard.queries
    assert dashboard.tiles[2].id == "w3"
    assert "# my dashboard — hand-written comment that must survive" in text
    assert "SELECT 'a' AS a, 3 AS b" in text


def test_upsert_existing_tile_updates_in_place(store):
    s, _path = store
    s.upsert_tile(
        "test",
        {
            "id": "w1",
            "query": "q1",
            "position": {"x": 0, "y": 0, "w": 6, "h": 3},
            "chart": {"type": "bar", "x": "n", "y": ["n"]},
        },
        sql=None,
        if_match=None,
    )
    dashboard, _, _ = s.load("test")
    assert dashboard.tiles[0].chart.type == "bar"
    assert [w.id for w in dashboard.tiles] == ["w1", "w2"]


def test_deleting_an_untitled_tile_does_not_rename_the_survivor(tmp_path):
    """tile_N is positional. Without pinning, deleting tile_1 makes the
    survivor derive as tile_1, and the next delete hits the wrong tile."""
    from sqldash.project.store import DashboardStore

    (tmp_path / "d.yaml").write_text(
        "title: T\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - {sql: 'SELECT 1 AS a'}\n"
        "  - {sql: 'SELECT 2 AS a'}\n"
    )
    store = DashboardStore(tmp_path)
    dashboard, _, etag = store.load("d")
    assert [w.id for w in dashboard.tiles] == ["tile_1", "tile_2"]
    store.delete_tile("d", "tile_1", etag)
    dashboard, text, _ = store.load("d")
    assert [w.id for w in dashboard.tiles] == ["tile_2"]
    assert "id: tile_2" in text


def test_an_explicit_tile_n_does_not_make_the_dashboard_uneditable(tmp_path):
    """#183: untitled tile_2 + authored id: tile_2 used to 422 every mutation."""
    from sqldash.project.store import DashboardStore

    (tmp_path / "collide.yaml").write_text(
        "title: Collision probe\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - {title: Revenue, sql: 'SELECT 1 AS x'}\n"
        "  - {markdown: '## notes'}\n"
        "  - {id: tile_2, title: Explicit, sql: 'SELECT 2 AS y'}\n"
    )
    store = DashboardStore(tmp_path)
    dashboard, _, etag = store.load("collide")
    assert [w.id for w in dashboard.tiles] == ["revenue", "tile_2_2", "tile_2"]
    store.update_positions(
        "collide",
        {"revenue": {"x": 0, "y": 0, "w": 6, "h": 4}},
        etag,
    )
    dashboard, text, _ = store.load("collide")
    assert [w.id for w in dashboard.tiles] == ["revenue", "tile_2_2", "tile_2"]
    assert "id: tile_2_2" in text


def test_delete_tile_removes_orphan_query(store):
    s, _path = store
    s.delete_tile("test", "w2", None)
    dashboard, _, _ = s.load("test")
    assert [w.id for w in dashboard.tiles] == ["w1"]
    assert "q2" not in dashboard.queries
    assert "q1" in dashboard.queries


def test_delete_tile_keeps_shared_query(store):
    s, _path = store
    s.upsert_tile(
        "test",
        {"id": "w3", "query": "q1", "position": {"x": 0, "y": 6, "w": 3, "h": 2}},
        sql=None,
        if_match=None,
    )
    s.delete_tile("test", "w1", None)
    dashboard, _, _ = s.load("test")
    assert "q1" in dashboard.queries


def test_delete_last_query_bearing_tile_drops_queries_key(tmp_path):
    """Popping the last named query used to leave `queries: {}` — dead config
    the author never wrote, which then survived every later UI edit. #462."""
    (tmp_path / "t.yaml").write_text(
        "title: T\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - title: Note\n"
        "    markdown: hi\n"
        "  - id: w1\n"
        "    query: q1\n"
        "    position: {x: 0, y: 0, w: 6, h: 3}\n"
        "  - id: w2\n"
        "    query: q2\n"
        "    position: {x: 6, y: 0, w: 6, h: 3}\n"
        "queries:\n"
        "  q1: |\n"
        "    SELECT 1 AS n\n"
        "  q2: |\n"
        "    SELECT 2 AS m\n"
    )
    store = DashboardStore(tmp_path)
    store.delete_tile("t", "w2", store.load("t")[2])
    text = (tmp_path / "t.yaml").read_text()
    assert "q2:" not in text
    assert "q1:" in text
    store.delete_tile("t", "w1", store.load("t")[2])
    after = (tmp_path / "t.yaml").read_text()
    assert "queries:" not in after, after
    assert "q1:" not in after
    assert "title: Note" in after
    dashboard, _, _ = store.load("t")
    assert dashboard.queries == {}
    assert _round_trips(after)


def test_adding_then_deleting_the_only_named_query_does_not_leave_an_empty_mapping(tmp_path):
    """The UI add-then-delete path: a dashboard with no `queries:` key must not
    gain `queries: {}` after the last query-bearing tile is removed. #462."""
    (tmp_path / "t.yaml").write_text(
        "title: T\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - title: Note\n"
        "    markdown: hi\n"
    )
    store = DashboardStore(tmp_path)
    store.upsert_tile(
        "t",
        {
            "title": "Temp tile",
            "query": "temp_q",
            "chart": "table",
            "position": {"x": 0, "y": 12, "w": 6, "h": 4},
        },
        sql="SELECT 1",
        if_match=store.load("t")[2],
    )
    assert "temp_q:" in (tmp_path / "t.yaml").read_text()
    store.delete_tile("t", "temp_tile", store.load("t")[2])
    after = (tmp_path / "t.yaml").read_text()
    assert "queries:" not in after, after
    assert "temp_q" not in after
    assert "title: Note" in after
    assert _round_trips(after)


def test_update_meta_title_and_description(store):
    s, _path = store
    s.update_meta("test", None, title="Renamed", description="A new description")
    dashboard, text, _ = s.load("test")
    assert dashboard.title == "Renamed"
    assert dashboard.description == "A new description"
    lines = text.splitlines()
    assert lines.index("title: Renamed") + 1 == lines.index("description: A new description")
    assert "# my dashboard — hand-written comment that must survive" in text


def test_update_meta_clears_description(store):
    s, _ = store
    s.update_meta("test", None, description="temp")
    s.update_meta("test", None, description="")
    dashboard, _, _ = s.load("test")
    assert dashboard.description is None


def test_update_filters_roundtrip(store):
    s, _path = store
    s.update_filters(
        "test",
        [
            {"name": "region", "type": "select", "default": "all", "options": ["all", "us"]},
            {
                "name": "dates",
                "type": "daterange",
                "default": "last_30_days",
                "bind": {"start": "start_date", "end": "end_date"},
            },
        ],
        None,
    )
    dashboard, text, _ = s.load("test")
    assert [f.name for f in dashboard.filters] == ["region", "dates"]
    assert "{name: region, type: select, default: all, options: [all, us]}" in text
    s.update_filters("test", [], None)
    dashboard, _, _ = s.load("test")
    assert dashboard.filters == []


def test_upsert_tile_preserves_metric(store):
    s, path = store
    s.upsert_tile(
        "test",
        {
            "id": "m1",
            "title": "Rev",
            "metric": {"name": "revenue", "grain": "day"},
            "chart": {"type": "line"},
        },
        sql=None,
        if_match=None,
    )
    text = path.read_text()
    assert "metric: {name: revenue, grain: day}" in text
    s.upsert_tile("test", {"id": "m2", "metric": {"name": "revenue"}}, sql=None, if_match=None)
    assert "metric: revenue" in path.read_text()


def test_upsert_tile_auto_positions_at_bottom(store):
    s, _ = store
    s.upsert_tile("test", {"id": "auto", "query": "q1"}, sql=None, if_match=None)
    dashboard, _, _ = s.load("test")
    auto = next(w for w in dashboard.tiles if w.id == "auto")
    assert auto.position.y == 3
    assert auto.position.x == 0


AUTHORED_DOC = """title: Authored
source: {type: duckdb}
sources:
  warehouse: {type: duckdb}
queries:
  q: SELECT 1 AS x
tiles:
  - title: Rich tile
    source: warehouse
    query: q
    chart: line
    format: currency
"""


def test_editing_a_tile_keeps_authored_fields_the_editor_never_sees(tmp_path):
    """Regression for #75, and for the class it belongs to.

    The editor PUTs a fixed key set. Anything else on the tile used to be pruned,
    which silently re-pointed the tile at the dashboard's default source and
    dropped its formatting — no error, the chart just started reading a different
    database. `format` cannot even be round-tripped by the browser: validation
    folds it into `chart.format` and blanks it before the payload is built.
    `source` is now editor-managed (the query-page picker) and is sent here;
    format still is not. A bare-string `chart` cannot say anything about the
    format, so it must not clear one either (#508).
    """
    (tmp_path / "authored.yaml").write_text(AUTHORED_DOC)
    store = DashboardStore(tmp_path)

    # The editor's payload shape: the keys it sends and no others. Position is a
    # materialized dict because the browser always has one by the time it saves.
    store.upsert_tile(
        "authored",
        {
            "id": "rich_tile",
            "type": "chart",
            "title": "Renamed",
            "query": "q",
            "metric": None,
            "source": "warehouse",
            "position": {"x": 0, "y": 0, "w": 6, "h": 4},
            "chart": "line",
            "markdown": None,
        },
        "SELECT 1 AS x",
        None,
    )

    text = (tmp_path / "authored.yaml").read_text()
    assert "source: warehouse" in text, (
        "per-tile source was stripped — tile now hits the wrong database"
    )
    assert "format: currency" in text, "tile formatting was stripped"
    assert "Renamed" in text, "the edit itself must still apply"


def test_upsert_tile_copies_a_named_source_in_the_same_write(tmp_path):
    from sqldash.models.source import Source

    (tmp_path / "d.yaml").write_text(
        "title: D\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "queries: {q: 'SELECT 1 AS n'}\n"
        "tiles: [{title: T, query: q}]\n"
    )
    store = DashboardStore(tmp_path)
    store.upsert_tile(
        "d",
        {"id": "t", "title": "T", "query": "q", "source": "metrics"},
        None,
        None,
        named_source=("metrics", Source(type="duckdb", database="other.db")),
    )
    text = (tmp_path / "d.yaml").read_text()
    assert "metrics: {" in text.split("\nsource:")[1].split("\ntiles:")[0]
    assert "other.db" in text
    assert "source: metrics" in text.split("tiles:")[1]


def test_source_for_copy_records_a_foreign_duckdb_dir(tmp_path):
    from sqldash.models.source import Source
    from sqldash.project.sources import source_files_dir, source_for_copy

    src_dir = tmp_path / "beta"
    dest_dir = tmp_path / "acme"
    src_dir.mkdir()
    dest_dir.mkdir()
    source = Source(type="duckdb", attach_files=True)
    copied = source_for_copy(source, src_dir, dest_dir)
    assert copied.base_dir is not None
    assert source_files_dir(copied, dest_dir) == src_dir.resolve()
    nested = Source(type="duckdb", attach_files=True, base_dir="data/csv")
    copied_nested = source_for_copy(nested, src_dir, dest_dir)
    assert source_files_dir(copied_nested, dest_dir) == (src_dir / "data" / "csv").resolve()
    assert copied_nested.base_dir.endswith("data/csv")
    same = source_for_copy(source, dest_dir, dest_dir)
    assert same.base_dir is None
    assert source_for_copy(nested, dest_dir, dest_dir).base_dir == "data/csv"
    url = Source(type="postgres", url="postgresql://u@db/app")
    assert source_for_copy(url, src_dir, dest_dir).base_dir is None


def test_source_as_project_yaml_drops_plaintext_secrets():
    from sqldash.models.source import Source, source_as_project_yaml

    dumped = source_as_project_yaml(
        Source(type="postgres", host="h", database="d", username="u", password="hunter2")
    )
    assert "password" not in dumped
    kept = source_as_project_yaml(
        Source(type="postgres", host="h", database="d", password="${env:PGPASSWORD}")
    )
    assert kept["password"] == "${env:PGPASSWORD}"


def test_source_as_project_yaml_strips_url_and_bag_secrets():
    from sqldash.models.source import Source, source_as_project_yaml

    dumped = source_as_project_yaml(
        Source.model_validate(
            {
                "url": (
                    "postgresql://u:hunter2@db.example.com:5432/app"
                    "?token=supersecret&api_key=hunter2&passwd=hunter2&sslmode=require"
                ),
                "connect_args": {
                    "token": "supersecret",
                    "passwd": "hunter2",
                    "sslmode": "require",
                },
                "options": {"secret_key": "supersecret", "s3_staging_dir": "s3://b/p"},
            }
        )
    )
    blob = repr(dumped)
    assert "hunter2" not in blob
    assert "supersecret" not in blob
    assert dumped["url"] == "postgresql://u@db.example.com:5432/app?sslmode=require"
    assert dumped["connect_args"] == {"sslmode": "require"}
    assert dumped["options"] == {"s3_staging_dir": "s3://b/p"}


def test_source_as_project_yaml_strips_percent_encoded_url_password():
    """`URL.set(password=None)` is a no-op, so a percent-encoded userinfo
    password used to be re-rendered decoded into the copy."""
    from sqldash.models.source import Source, source_as_project_yaml

    dumped = source_as_project_yaml(
        Source.model_validate(
            {
                "type": "postgres",
                "url": "postgresql://u:hunt%65r2@db.example.com:5432/app?sslmode=require",
            }
        )
    )
    assert dumped["url"] == "postgresql://u@db.example.com:5432/app?sslmode=require"
    assert "hunter2" not in dumped["url"]
    assert "hunt%65r2" not in dumped["url"]

    special = source_as_project_yaml(
        Source.model_validate({"url": "postgresql://u:p%40ss%3Aword@db.example.com:5432/app"})
    )
    assert special["url"] == "postgresql://u@db.example.com:5432/app"
    assert "p%40ss" not in special["url"]
    assert "ss:" not in special["url"]

    mixed = source_as_project_yaml(
        Source.model_validate({"url": "postgresql://u:h%75ter2@db.example.com/app?x=:huter2@z"})
    )
    assert "h%75ter2" not in mixed["url"], mixed["url"]
    assert "huter2" not in mixed["url"], mixed["url"]
    assert mixed["url"] == "postgresql://u@db.example.com/app"


def test_source_as_project_yaml_scrubs_the_password_from_query_values():
    """The query filter drops pairs by *key* name, so the same plaintext password
    reused under an innocent key survived the userinfo strip and landed in git."""
    from sqldash.models.source import Source, source_as_project_yaml

    dumped = source_as_project_yaml(
        Source.model_validate({"url": "postgresql://u:hunter2@h:5432/d?opt=:hunter2@z"})
    )
    assert "hunter2" not in dumped["url"], dumped["url"]

    kept = source_as_project_yaml(
        Source.model_validate({"url": "postgresql://u:hunter2@h:5432/d?sslmode=require"})
    )
    assert kept["url"] == "postgresql://u@h:5432/d?sslmode=require"

    encoded = source_as_project_yaml(
        Source.model_validate({"url": "postgresql://u:hunter2@db/app?b=hunt%65r2@z"})
    )
    assert "hunter2" not in encoded["url"], encoded["url"]
    assert "hunt%65r2" not in encoded["url"], encoded["url"]

    kept_param = source_as_project_yaml(
        Source.model_validate(
            {"url": "postgresql://u:admin@db/app?application_name=administration"}
        )
    )
    assert "application_name=administration" in kept_param["url"]
    assert "admin@" not in kept_param["url"]

    mid = source_as_project_yaml(
        Source.model_validate({"url": "postgresql://u:hunter2@h:5432/d?x=foo:hunter2"})
    )
    assert "hunter2" not in mid["url"], mid["url"]
    suffix = source_as_project_yaml(
        Source.model_validate({"url": "postgresql://u:hunter2@h:5432/d?x=hunter2:"})
    )
    assert "hunter2" not in suffix["url"], suffix["url"]
    wrapped = source_as_project_yaml(
        Source.model_validate({"url": "postgresql://u:hunter2@h:5432/d?x=@hunter2@"})
    )
    assert "hunter2" not in wrapped["url"], wrapped["url"]


def test_source_as_project_yaml_strips_password_when_port_is_an_env_ref():
    """Alphanumeric env tokens are not ints, so make_url failed on the port
    and the fallback returned the original URL, password included."""
    from sqldash.models.source import Source, source_as_project_yaml

    dumped = source_as_project_yaml(
        Source.model_validate({"url": "postgresql://u:hunter2@db:${env:PORT}/app"})
    )
    assert dumped["url"] == "postgresql://u@db:${env:PORT}/app"
    assert "hunter2" not in dumped["url"]

    kept = source_as_project_yaml(
        Source.model_validate({"url": "postgresql://u:${env:PGPASSWORD}@db:${env:PORT}/app"})
    )
    assert kept["url"] == "postgresql://u:${env:PGPASSWORD}@db:${env:PORT}/app"

    query_num = source_as_project_yaml(
        Source.model_validate({"url": "postgresql://u:hunter2@db:${env:PORT}/app?x=61000"})
    )
    assert query_num["url"] == "postgresql://u@db:${env:PORT}/app?x=61000"

    query_token = source_as_project_yaml(
        Source.model_validate({"url": "postgresql://u:${env:PGPASSWORD}@db/app?x=SQLDASHENV0X"})
    )
    assert query_token["url"] == "postgresql://u:${env:PGPASSWORD}@db/app?x=SQLDASHENV0X"

    path_num = source_as_project_yaml(
        Source.model_validate({"url": "postgresql://u:hunter2@db:${env:PORT}/app/61000"})
    )
    assert path_num["url"] == "postgresql://u@db:${env:PORT}/app/61000"


def test_unparseable_env_url_drops_empty_username_password():
    """`://:password@` used to miss the fallback regex and copy hunter2 into git."""
    from sqldash.models.source import Source, source_as_project_yaml

    dumped = source_as_project_yaml(
        Source.model_validate(
            {"type": "postgres", "url": "postgresql://:hunter2@${env:PGHOST}:notaport/app"}
        )
    )
    assert "hunter2" not in dumped["url"], dumped["url"]
    assert "${env:PGHOST}" in dumped["url"]
    reused = source_as_project_yaml(
        Source.model_validate(
            {
                "url": (
                    "postgresql://:hunter2@${env:PGHOST}:notaport/app?opt=hunter2&sslmode=require"
                )
            }
        )
    )
    assert "hunter2" not in reused["url"], reused["url"]
    assert "sslmode=require" in reused["url"]


def test_unparseable_env_url_still_drops_secret_query_keys():
    """The fallback that runs when make_url cannot parse used to keep ?token=."""
    from sqldash.models.source import Source, source_as_project_yaml

    dumped = source_as_project_yaml(
        Source.model_validate(
            {
                "url": (
                    "postgresql://u:hunter2@${env:PGHOST}:notaport/db"
                    "?token=supersecret&sslmode=require"
                )
            }
        )
    )
    assert "hunter2" not in dumped["url"]
    assert "supersecret" not in dumped["url"]
    assert "token=" not in dumped["url"]
    assert "sslmode=require" in dumped["url"]
    assert "${env:PGHOST}" in dumped["url"]

    reused = source_as_project_yaml(
        Source.model_validate(
            {
                "url": (
                    "postgresql://u:hunter2@${env:PGHOST}:notaport/db?opt=hunter2&sslmode=require"
                )
            }
        )
    )
    assert "hunter2" not in reused["url"], reused["url"]
    assert "sslmode=require" in reused["url"]


def test_port_restore_does_not_rewrite_user_or_host():
    from sqldash.models.source import Source, source_as_project_yaml

    user = source_as_project_yaml(
        Source.model_validate({"url": "postgresql://us61000er:hunter2@db:${env:PORT}/app"})
    )
    assert user["url"] == "postgresql://us61000er@db:${env:PORT}/app"
    host = source_as_project_yaml(
        Source.model_validate({"url": "postgresql://u:hunter2@db61000.example.com:${env:PORT}/app"})
    )
    assert host["url"] == "postgresql://u@db61000.example.com:${env:PORT}/app"


def test_source_as_project_yaml_drops_credential_bag_keys():
    """`credentials_info` is a BigQuery service-account blob and `key` is a bare
    secret, but neither contains password/token/secret, so the substring rules
    alone let them through into a tracked file."""
    from sqldash.models.source import Source, source_as_project_yaml

    dumped = source_as_project_yaml(
        Source.model_validate(
            {
                "type": "bigquery",
                "connect_args": {
                    "credentials_info": '{"private_key": "-----BEGIN..."}',
                    "credentials": "hunter2",
                    "key": "hunter2",
                    "passphrase": "hunter2",
                },
                "options": {"credentials_path": "/tmp/sa.json"},
            }
        )
    )
    blob = repr(dumped)
    assert "hunter2" not in blob, blob
    assert "BEGIN" not in blob, blob
    assert dumped.get("connect_args", {}) == {}
    assert dumped.get("options") == {"credentials_path": "/tmp/sa.json"}

    nested = source_as_project_yaml(
        Source.model_validate(
            {
                "type": "postgres",
                "connect_args": {"ssl": {"password": "hunter2", "sslmode": "require"}},
            }
        )
    )
    assert "hunter2" not in repr(nested)
    assert nested["connect_args"] == {"ssl": {"sslmode": "require"}}


def test_source_as_project_yaml_keeps_benign_connect_args():
    """The denylist must not swallow ordinary connection config — dropping
    `authenticator` would silently break externalbrowser SSO on the copy."""
    from sqldash.models.source import Source, source_as_project_yaml

    dumped = source_as_project_yaml(
        Source.model_validate(
            {
                "type": "snowflake",
                "account": "acme-xy12345",
                "connect_args": {
                    "authenticator": "externalbrowser",
                    "application_name": "sqldash",
                    "sslmode": "require",
                },
            }
        )
    )
    assert dumped["connect_args"] == {
        "authenticator": "externalbrowser",
        "application_name": "sqldash",
        "sslmode": "require",
    }


def test_source_as_project_yaml_keeps_env_refs_in_urls():
    from sqldash.models.source import Source, source_as_project_yaml

    stripped = source_as_project_yaml(
        Source.model_validate(
            {
                "type": "postgres",
                "url": "postgresql://u:hunter2@${env:PGHOST}:5432/app",
            }
        )
    )
    assert stripped["url"] == "postgresql://u@${env:PGHOST}:5432/app"
    assert "hunter2" not in stripped["url"]

    kept = source_as_project_yaml(
        Source.model_validate(
            {
                "url": "postgresql://${env:PGUSER}:${env:PGPASSWORD}@db.example.com:5432/app",
            }
        )
    )
    assert kept["url"] == ("postgresql://${env:PGUSER}:${env:PGPASSWORD}@db.example.com:5432/app")

    collided = source_as_project_yaml(
        Source.model_validate(
            {
                "url": "postgresql://u:${env:PGPASSWORD}@db:5432/app?x=:1@y&sslmode=require",
            }
        )
    )
    assert collided["url"] == (
        "postgresql://u:${env:PGPASSWORD}@db:5432/app?x=:1@y&sslmode=require"
    )

    plaintext_and_sentinel = source_as_project_yaml(
        Source.model_validate(
            {
                "url": "postgresql://u:hunter2@${env:PGHOST}:5432/app?x=:1@y",
            }
        )
    )
    assert plaintext_and_sentinel["url"] == "postgresql://u@${env:PGHOST}:5432/app?x=:1@y"
    assert "hunter2" not in plaintext_and_sentinel["url"]


def test_copying_a_secret_source_twice_reuses_the_alias(tmp_path):
    from sqldash.models.source import Source
    from sqldash.project.sources import alias_for_picker_key

    (tmp_path / "d.yaml").write_text(
        "title: D\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "queries: {q: 'SELECT 1 AS n'}\n"
        "tiles: [{title: T, query: q}]\n"
    )
    store = DashboardStore(tmp_path)
    incoming = Source(type="postgres", host="h", database="d", username="u", password="hunter2")
    sname = alias_for_picker_key("other.sources.prod", {}, source=incoming)
    store.upsert_tile(
        "d",
        {"id": "t", "title": "T", "query": "q", "source": sname},
        None,
        None,
        named_source=(sname, incoming),
    )
    dash, _, _ = store.load("d")
    again = alias_for_picker_key("other.sources.prod", dash.sources, source=incoming)
    assert again == sname
    assert f"{sname}_2" not in dash.sources
    assert "hunter2" not in (tmp_path / "d.yaml").read_text()


def test_copying_a_different_plaintext_secret_does_not_reuse_the_alias(tmp_path):
    from sqldash.models.source import Source
    from sqldash.project.sources import alias_for_picker_key

    (tmp_path / "d.yaml").write_text(
        "title: D\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "sources:\n"
        "  other_prod: {type: postgres, host: h, database: d, username: u, password: hunter2}\n"
        "queries: {q: 'SELECT 1 AS n'}\n"
        "tiles: [{title: T, query: q}]\n"
    )
    store = DashboardStore(tmp_path)
    dash, _, _ = store.load("d")
    incoming = Source(type="postgres", host="h", database="d", username="u", password="hunter3")
    sname = alias_for_picker_key("other.sources.prod", dash.sources, source=incoming)
    assert sname == "other_prod_2"


def test_editor_managed_keys_stay_in_step_with_the_tile_model():
    """A new Tile field must be classified, not silently added to the pruned set.

    Fields the editor manages get removed when absent from a payload; everything
    else is authored config that has to survive. Adding a field to the model
    without deciding which it is is how #75 happened, so decide it here.
    """
    from sqldash.models.dashboard import Tile
    from sqldash.project.store import EDITOR_MANAGED_TILE_KEYS

    # Authored config the editor does not send, and which must never be pruned.
    preserved = {"id", "format", "compare", "grain"}

    assert set(Tile.model_fields) >= EDITOR_MANAGED_TILE_KEYS, (
        "EDITOR_MANAGED_TILE_KEYS names a field the Tile model does not have"
    )
    unclassified = set(Tile.model_fields) - EDITOR_MANAGED_TILE_KEYS - preserved
    assert not unclassified, (
        f"new Tile field(s) {sorted(unclassified)}: add to EDITOR_MANAGED_TILE_KEYS if the "
        f"editor owns them, or to `preserved` here if they are authored-only"
    )


FMT_DOC = (
    "title: Fmt\n"
    "source: {type: duckdb, attach_files: true}\n"
    "sources:\n  other: {type: duckdb, database: other.db}\n"
    "queries: {q: 'SELECT 1 AS n'}\n"
    "tiles:\n"
    "  - title: Rich\n"
    "    query: q\n"
    "    chart: line\n"
    "    format: currency\n"
    "    source: other\n"
)


def _rich_payload(chart):
    return {
        "id": "rich",
        "title": "Rich",
        "query": "q",
        "source": "other",
        "chart": {**chart, "stacked": False, "legend": True},
    }


def test_editing_a_tile_drops_the_format_its_chart_now_carries(tmp_path):
    """The browser sends `chart` as a full object with the tile-level `format`
    already folded in, so #83's "keep authored keys" left `format:` behind on
    every edit (#92). Worse than noise: Tile folds it only when chart.format is
    empty and then clears it, so the leftover line looks live and is not —
    hand-editing it would change nothing, silently. That only happens once the
    chart actually carries a *different* format; an unchanged one keeps
    living on the tile-level line (#360).
    """
    (tmp_path / "d.yaml").write_text(FMT_DOC)
    store = DashboardStore(tmp_path)
    _, _, etag = store.load("d")
    store.upsert_tile(
        "d", _rich_payload({"type": "line", "format": "percent"}), sql=None, if_match=etag
    )
    text = (tmp_path / "d.yaml").read_text()
    tile_lines = text.split("tiles:")[1]
    assert "format: currency" not in tile_lines, text
    assert "chart: {type: line, format: percent}" in text, text
    # The authored key #83 exists to protect is untouched.
    assert "source: other" in text, text
    dashboard, _, _ = store.load("d")
    assert dashboard.tiles[0].chart.format == "percent"
    assert dashboard.tiles[0].source == "other"


def test_saving_a_tile_unchanged_leaves_the_file_byte_for_byte(tmp_path):
    """The editor round-trips the parsed tile; nothing in it changed, so nothing
    in the file may change — not the `chart: line` shorthand, not the
    tile-level `format:` the browser handed back inside the chart (#360)."""
    (tmp_path / "d.yaml").write_text(FMT_DOC)
    store = DashboardStore(tmp_path)
    _, _, etag = store.load("d")
    store.upsert_tile(
        "d", _rich_payload({"type": "line", "format": "currency"}), sql=None, if_match=etag
    )
    assert (tmp_path / "d.yaml").read_text() == FMT_DOC


def test_changing_the_chart_type_rewrites_only_that_key(tmp_path):
    """`chart: line` → `chart: bar` is one line; the format the chart still
    shares with the tile-level `format:` keeps living there (#360)."""
    (tmp_path / "d.yaml").write_text(FMT_DOC)
    store = DashboardStore(tmp_path)
    _, _, etag = store.load("d")
    store.upsert_tile(
        "d", _rich_payload({"type": "bar", "format": "currency"}), sql=None, if_match=etag
    )
    text = (tmp_path / "d.yaml").read_text()
    assert text == FMT_DOC.replace("chart: line", "chart: bar"), text
    dashboard, _, _ = store.load("d")
    assert dashboard.tiles[0].chart.format == "currency"


DEMO_TILE_PAYLOAD = {
    "id": "revenue_by_category",
    "type": "chart",
    "title": "Revenue by category",
    "query": "revenue_by_category",
    "metric": None,
    "position": {"x": 6, "y": 2, "w": 6, "h": 4},
    "source": None,
    "chart": {
        "format": "currency",
        "type": "bar",
        "x": None,
        "y": None,
        "group_by": None,
        "stacked": False,
        "orientation": None,
        "color_by": None,
        "value": None,
        "label": None,
        "legend": True,
    },
}


def _demo(tmp_path):
    from sqldash.scaffold import create_demo

    create_demo(tmp_path)
    store = DashboardStore(tmp_path / ".sqldash")
    dashboard, _, etag = store.load("demo")
    sql = dashboard.queries["revenue_by_category"].strip()
    return store, tmp_path / ".sqldash" / "demo.yaml", etag, sql


def test_renaming_a_demo_tile_diffs_as_one_line(tmp_path):
    """The query page's payload for the scaffold's inline-sql tile, with only
    the title changed. It used to hoist the sql into a new top-level
    `queries:` block, expand `chart: bar` + `format: currency` into a dict,
    add `id:` and `query:`, and pin every flowing tile's position (#360)."""
    store, path, etag, sql = _demo(tmp_path)
    before = path.read_text()
    store.upsert_tile(
        "demo", {**DEMO_TILE_PAYLOAD, "title": "Revenue by category v2"}, sql=sql, if_match=etag
    )
    after = path.read_text()
    assert after == before.replace(
        "  - title: Revenue by category\n", "  - title: Revenue by category v2\n"
    ), after
    dashboard, _, _ = store.load("demo")
    tile = next(t for t in dashboard.tiles if t.title == "Revenue by category v2")
    assert tile.id == "revenue_by_category_v2"
    assert dashboard.queries["revenue_by_category_v2"].strip() == sql


def test_renaming_a_demo_tile_without_sql_keeps_the_inline_block(tmp_path):
    """GET returns the hoisted tile as `query: <name>, sql: null`, so a caller
    doing the documented GET + PUT round trip sends `query` and no `sql`. The
    managed-key prune dropped the inline block anyway and left the tile
    referencing a query nothing defined (#392). The diff is still one line."""
    store, path, etag, sql = _demo(tmp_path)
    before = path.read_text()
    store.upsert_tile(
        "demo", {**DEMO_TILE_PAYLOAD, "title": "Revenue by category v2"}, sql=None, if_match=etag
    )
    after = path.read_text()
    assert after == before.replace(
        "  - title: Revenue by category\n", "  - title: Revenue by category v2\n"
    ), after
    assert "queries:" not in after
    dashboard, _, _ = store.load("demo")
    tile = next(t for t in dashboard.tiles if t.title == "Revenue by category v2")
    assert tile.id == "revenue_by_category_v2"
    assert dashboard.queries["revenue_by_category_v2"].strip() == sql


def test_repointing_a_hoisted_tile_at_a_named_query_still_drops_the_block(tmp_path):
    """#392 keeps the tile's *own* hoisted query only. A payload naming some
    other query is the author moving the tile off its inline sql."""
    (tmp_path / "d.yaml").write_text(
        "title: D\nsource: {type: duckdb, database: ':memory:'}\n"
        "queries: {other: 'SELECT 2 AS b'}\n"
        "tiles:\n"
        "  - title: A\n"
        "    sql: SELECT 1 AS a\n"
    )
    store = DashboardStore(tmp_path)
    _, _, etag = store.load("d")
    store.upsert_tile("d", {"id": "a", "title": "A", "query": "other"}, sql=None, if_match=etag)
    text = (tmp_path / "d.yaml").read_text()
    assert "sql:" not in text
    assert "query: other" in text


def test_editing_inline_sql_keeps_it_inline(tmp_path):
    """The author chose `sql:` on the tile; a SQL edit changes that block in
    place rather than moving it into `queries:` (#360)."""
    store, path, etag, sql = _demo(tmp_path)
    before = path.read_text()
    new_sql = sql.replace("ORDER BY 2 DESC", "ORDER BY 1")
    store.upsert_tile("demo", DEMO_TILE_PAYLOAD, sql=new_sql, if_match=etag)
    after = path.read_text()
    assert after == before.replace("      ORDER BY 2 DESC\n", "      ORDER BY 1\n", 1), after
    assert "queries:" not in after
    dashboard, _, _ = store.load("demo")
    assert dashboard.queries["revenue_by_category"].strip() == new_sql


def test_changing_a_demo_tiles_chart_type_rewrites_only_that_key(tmp_path):
    store, path, etag, sql = _demo(tmp_path)
    before = path.read_text()
    payload = {**DEMO_TILE_PAYLOAD, "chart": {**DEMO_TILE_PAYLOAD["chart"], "type": "line"}}
    store.upsert_tile("demo", payload, sql=sql, if_match=etag)
    after = path.read_text()
    assert after == before.replace("    chart: bar\n", "    chart: line\n"), after


def test_a_block_chart_mapping_is_edited_key_by_key(tmp_path):
    (tmp_path / "d.yaml").write_text(
        "title: D\nsource: {type: duckdb, database: ':memory:'}\n"
        'queries: {q: "SELECT 1 AS a, 2 AS b"}\n'
        "tiles:\n"
        "  - title: A\n"
        "    query: q\n"
        "    chart:\n"
        "      type: bar\n"
        "      x: a\n"
        "      y: [b]\n"
    )
    store = DashboardStore(tmp_path)
    _, _, etag = store.load("d")
    store.upsert_tile(
        "d",
        {"id": "a", "title": "A", "query": "q", "chart": {"type": "line", "x": "a", "y": ["b"]}},
        sql=None,
        if_match=etag,
    )
    text = (tmp_path / "d.yaml").read_text()
    assert "    chart:\n      type: line\n      x: a\n      y: [b]\n" in text, text


def test_a_derived_id_another_tile_queries_by_is_pinned_on_rename(tmp_path):
    """Renaming an inline-sql tile moves its hoisted query name with it; a
    sibling that reads `query: <old id>` would go dark, so the old id is
    pinned in that one case."""
    (tmp_path / "d.yaml").write_text(
        "title: D\nsource: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - title: A\n"
        "    sql: SELECT 1 AS n\n"
        "  - title: B\n"
        "    query: a\n"
    )
    store = DashboardStore(tmp_path)
    _, _, etag = store.load("d")
    store.upsert_tile(
        "d", {"id": "a", "title": "A2", "query": "a", "chart": "table"}, "SELECT 1 AS n", etag
    )
    dashboard, _, _ = store.load("d")
    assert [t.id for t in dashboard.tiles] == ["a", "b"]
    assert dashboard.tiles[0].title == "A2"
    assert dashboard.tiles[1].query == "a"


def test_a_tile_level_format_survives_when_the_chart_still_carries_it(tmp_path):
    """Only a format the chart no longer agrees with is dead. The editor hands the
    tile-level `format:` back inside `chart`, so a chart change that keeps it
    leaves the shorthand where it was authored."""
    (tmp_path / "d.yaml").write_text(
        "title: Fmt\n"
        "source: {type: duckdb, attach_files: true}\n"
        "queries: {q: 'SELECT 1 AS n'}\n"
        "tiles:\n"
        "  - {title: Plain, query: q, format: percent}\n"
    )
    store = DashboardStore(tmp_path)
    _, _, etag = store.load("d")
    store.upsert_tile(
        "d",
        {
            "id": "plain",
            "title": "Plain",
            "query": "q",
            "chart": {"type": "bar", "format": "percent"},
        },
        sql=None,
        if_match=etag,
    )
    text = (tmp_path / "d.yaml").read_text()
    assert "format: percent" in text, text
    assert "chart: bar" in text, text
    dashboard, _, _ = store.load("d")
    assert dashboard.tiles[0].chart.format == "percent"


def test_editing_a_metric_tile_drops_the_grain_and_compare_it_folded_in(tmp_path):
    """Same defect as the format one, on the sibling shorthands. `grain` and
    `compare` fold into `metric` and — unlike format — are never cleared, so
    once the browser round-trips them back inside the metric object the
    tile-level lines are inert. `compare` is worse than inert: lint reads
    tile.compare while rendering reads metric.compare, so hand-editing the
    leftover makes the two surfaces answer differently on the same file.
    A shorthand whose value the metric still agrees with keeps carrying it;
    an unchanged round-trip touches nothing (#360).
    """
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: duckdb, attach_files: true}\n"
        "relations:\n  t: {sql: 'SELECT 1 AS amount, DATE ''2026-01-01'' AS d'}\n"
        "metrics:\n"
        "  revenue: {relation: t, expr: SUM(amount), time_dimension: {name: d, grain: day}}\n"
    )
    (tmp_path / "d.yaml").write_text(
        "title: GC\n"
        "source: {type: duckdb, attach_files: true}\n"
        "sources:\n  alt: {type: duckdb, database: alt.db}\n"
        "tiles:\n"
        "  - title: Rev\n"
        "    metric: revenue\n"
        "    grain: day\n"
        "    compare: yoy\n"
        "    source: alt\n"
    )
    store = DashboardStore(tmp_path)
    before = (tmp_path / "d.yaml").read_text()
    dashboard, _, etag = store.load("d")
    tile = dashboard.tiles[0]
    payload = {"id": tile.id, "title": "Rev", "source": "alt"}
    etag = store.upsert_tile(
        "d",
        {**payload, "metric": {"name": "revenue", "grain": "day", "compare": "yoy"}},
        sql=None,
        if_match=etag,
    )
    assert (tmp_path / "d.yaml").read_text() == before
    store.upsert_tile(
        "d",
        {**payload, "metric": {"name": "revenue", "grain": "day", "compare": "previous_period"}},
        sql=None,
        if_match=etag,
    )
    body = (tmp_path / "d.yaml").read_text().split("tiles:")[1]
    assert "\n    grain: day" in body, body
    assert "\n    compare:" not in body, body
    assert "metric: {name: revenue, compare: previous_period}" in body, body
    assert "source: alt" in body, body
    reloaded, _, _ = store.load("d")
    assert reloaded.tiles[0].metric.grain == "day"
    assert reloaded.tiles[0].metric.compare == "previous_period"


GC_METRICS = (
    "source: {type: duckdb, attach_files: true}\n"
    "relations:\n  t: {sql: 'SELECT 1 AS amount, DATE ''2026-01-01'' AS d'}\n"
    "metrics:\n"
    "  revenue: {relation: t, expr: SUM(amount), time_dimension: {name: d, grain: day}}\n"
)

GC_DOC = (
    "title: GC\n"
    "source: {type: duckdb, attach_files: true}\n"
    "tiles:\n"
    "  - title: Rev\n"
    "    metric: revenue\n"
    "    # rolled up per day\n"
    "    grain: day\n"
    "    compare: yoy\n"
    "    size: 6x4\n"
)


def _gc_store(tmp_path):
    (tmp_path / "metrics.yaml").write_text(GC_METRICS)
    (tmp_path / "d.yaml").write_text(GC_DOC)
    store = DashboardStore(tmp_path)
    _, _, etag = store.load("d")
    return store, tmp_path / "d.yaml", etag


def test_grain_and_compare_survive_when_the_metric_still_carries_them(tmp_path):
    """A shorthand the incoming metric still agrees with is still authoritative.

    The fixture must actually author `grain:`/`compare:` — an earlier version
    had neither, so there was nothing to preserve and the assertion passed
    with or without the fix, and would have passed under a supersede that
    pruned every tile-level grain whenever a metric was present.
    """
    store, path, etag = _gc_store(tmp_path)
    store.upsert_tile(
        "d",
        {
            "id": "rev",
            "title": "Revenue",
            "metric": {"name": "revenue", "dimensions": [], "grain": "day", "compare": "yoy"},
        },
        sql=None,
        if_match=etag,
    )
    assert path.read_text() == GC_DOC.replace("title: Rev\n", "title: Revenue\n")


@pytest.mark.parametrize(
    "metric",
    [
        {"name": "revenue", "compare": "yoy"},
        {"name": "revenue", "dimensions": [], "grain": None, "compare": "yoy"},
    ],
)
def test_clearing_a_tile_level_grain_removes_the_key(tmp_path, metric):
    """The editor's grain picker set to "none" sends the metric without a grain
    (a GET round trip sends `grain: null`). The tile-level `grain:` used to be
    removed only when a new value superseded it, so the cleared value never
    reached the file: 200, byte-identical YAML, the tile still at `day` (#508).
    The comment above it and every other key stay put."""
    store, path, etag = _gc_store(tmp_path)
    store.upsert_tile("d", {"id": "rev", "title": "Rev", "metric": metric}, sql=None, if_match=etag)
    assert path.read_text() == GC_DOC.replace("    grain: day\n", ""), path.read_text()
    reloaded, _, _ = store.load("d")
    assert reloaded.tiles[0].metric.grain is None
    assert reloaded.tiles[0].metric.compare == "yoy"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"metric": "revenue", "grain": "day", "compare": "yoy"}, GC_DOC),
        (
            {"metric": "revenue", "grain": "month", "compare": "yoy"},
            GC_DOC.replace(
                "    metric: revenue\n    # rolled up per day\n    grain: day\n",
                "    metric: {name: revenue, grain: month}\n    # rolled up per day\n",
            ),
        ),
        (
            {"metric": {"name": "revenue"}, "grain": "day", "compare": "yoy"},
            GC_DOC,
        ),
    ],
)
def test_a_payloads_tile_level_shorthand_is_read_like_the_file(tmp_path, payload, expected):
    """`grain:`/`compare:` beside a bare `metric:` is the documented file form, so a
    payload in that shape means what the file would: it sets the value. Reading
    only the nested metric treated it as a clear and deleted the authored key."""
    store, path, etag = _gc_store(tmp_path)
    store.upsert_tile("d", {"id": "rev", "title": "Rev", **payload}, sql=None, if_match=etag)
    assert path.read_text() == expected, path.read_text()
    reloaded, _, _ = store.load("d")
    assert reloaded.tiles[0].metric.grain == payload["grain"]
    assert reloaded.tiles[0].metric.compare == "yoy"


def test_a_bare_string_metric_leaves_the_tile_level_grain_and_compare(tmp_path):
    """`metric: revenue` is the file's own shorthand and cannot carry a grain, so a
    payload in that shape says nothing about `grain:`/`compare:` and keeps them;
    only a metric object that omits them clears them (#508)."""
    store, path, etag = _gc_store(tmp_path)
    store.upsert_tile(
        "d", {"id": "rev", "title": "Rev", "metric": "revenue"}, sql=None, if_match=etag
    )
    assert path.read_text() == GC_DOC, path.read_text()


def test_clearing_a_tile_level_compare_removes_the_key(tmp_path):
    store, path, etag = _gc_store(tmp_path)
    store.upsert_tile(
        "d",
        {"id": "rev", "title": "Rev", "metric": {"name": "revenue", "grain": "day"}},
        sql=None,
        if_match=etag,
    )
    assert path.read_text() == GC_DOC.replace("    compare: yoy\n", ""), path.read_text()
    reloaded, _, _ = store.load("d")
    assert reloaded.tiles[0].metric.grain == "day"
    assert reloaded.tiles[0].metric.compare is None


def test_clearing_an_inline_grain_still_rewrites_the_metric(tmp_path):
    """The form the clear already worked on keeps working the same way (#508)."""
    (tmp_path / "metrics.yaml").write_text(GC_METRICS)
    doc = GC_DOC.replace(
        "    metric: revenue\n    # rolled up per day\n    grain: day\n",
        "    metric: {name: revenue, grain: day}\n",
    )
    (tmp_path / "d.yaml").write_text(doc)
    store = DashboardStore(tmp_path)
    _, _, etag = store.load("d")
    store.upsert_tile(
        "d",
        {"id": "rev", "title": "Rev", "metric": {"name": "revenue", "compare": "yoy"}},
        sql=None,
        if_match=etag,
    )
    text = (tmp_path / "d.yaml").read_text()
    assert text == doc.replace("metric: {name: revenue, grain: day}", "metric: revenue"), text


def test_moving_a_tile_off_its_metric_drops_the_shorthands_that_needed_it(tmp_path):
    """`grain:`/`compare:` require a `metric:`. Switching such a tile to SQL left
    them behind and the write was refused with `'grain' requires a 'metric'`."""
    store, path, etag = _gc_store(tmp_path)
    store.upsert_tile(
        "d",
        {"id": "rev", "title": "Rev", "query": "rev", "metric": None, "chart": {"type": "table"}},
        sql="SELECT 1 AS n",
        if_match=etag,
    )
    body = path.read_text().split("tiles:")[1]
    assert "grain:" not in body, body
    assert "compare:" not in body, body
    assert "metric:" not in body, body
    reloaded, _, _ = store.load("d")
    assert reloaded.tiles[0].query == "rev"


def test_clearing_the_demo_daily_revenue_grain_removes_one_line(tmp_path):
    """The exact payload the query page sent in #508 for the scaffold's tile."""
    from sqldash.scaffold import create_demo

    create_demo(tmp_path)
    store = DashboardStore(tmp_path / ".sqldash")
    path = tmp_path / ".sqldash" / "demo.yaml"
    before = path.read_text()
    _, _, etag = store.load("demo")
    store.upsert_tile(
        "demo",
        {
            "id": "daily_revenue",
            "type": "chart",
            "title": "Daily revenue",
            "query": None,
            "metric": {"name": "revenue"},
            "position": {"x": 0, "y": 2, "w": 6, "h": 4},
            "source": None,
            "chart": {"format": "currency", "type": "area"},
        },
        sql=None,
        if_match=etag,
    )
    after = path.read_text()
    assert after == before.replace(
        "    metric: revenue\n    grain: day\n", "    metric: revenue\n"
    ), after
    dashboard, _, _ = store.load("demo")
    tile = next(t for t in dashboard.tiles if t.id == "daily_revenue")
    assert tile.metric.grain is None


@pytest.mark.parametrize("fmt", ["absent", None, {}])
def test_clearing_a_demo_tiles_format_removes_the_tile_level_key(tmp_path, fmt):
    """`format: currency` beside `chart: bar` was only removed when a different
    format replaced it; a payload with the format cleared kept it (#508)."""
    store, path, etag, sql = _demo(tmp_path)
    before = path.read_text()
    chart = {k: v for k, v in DEMO_TILE_PAYLOAD["chart"].items() if k != "format"}
    if fmt != "absent":
        chart["format"] = fmt
    store.upsert_tile("demo", {**DEMO_TILE_PAYLOAD, "chart": chart}, sql=sql, if_match=etag)
    after = path.read_text()
    assert after == before.replace(
        "    chart: bar\n    format: currency\n", "    chart: bar\n", 1
    ), after
    dashboard, _, _ = store.load("demo")
    tile = next(t for t in dashboard.tiles if t.id == "revenue_by_category")
    assert tile.chart.format == {}


def test_a_payloads_tile_level_format_is_read_like_the_file(tmp_path):
    store, path, etag, sql = _demo(tmp_path)
    before = path.read_text()
    chart = {k: v for k, v in DEMO_TILE_PAYLOAD["chart"].items() if k != "format"}
    payload = {**DEMO_TILE_PAYLOAD, "chart": chart, "format": "currency"}
    store.upsert_tile("demo", payload, sql=sql, if_match=etag)
    assert path.read_text() == before


def test_editing_filters_does_not_materialize_a_derived_default(tmp_path):
    """The sibling of the derived bind: an options_sql select gets `default:
    "all"` filled in by the model, and the editor writes it back. The demo
    dashboard that ships with the product grew that line on its first edit.

    An authored default survives — including an explicit `all` on an options
    list, which is not derived and so is not ours to remove.
    """
    (tmp_path / "d.yaml").write_text(
        "title: D\n"
        "source: {type: duckdb, attach_files: true}\n"
        "filters:\n"
        "  - {name: a, type: select, label: A, options_sql: 'SELECT 1', default: us}\n"
        "  - {name: b, type: select, label: B, options: [all, us], default: all}\n"
        "  - {name: c, type: select, label: C, options_sql: 'SELECT 1'}\n"
        "queries: {q: 'SELECT 1 AS n'}\n"
        "tiles: [{id: t, query: q}]\n"
    )
    store = DashboardStore(tmp_path)
    dashboard, _before, etag = store.load("d")
    store.update_filters("d", [f.model_dump(exclude_none=True) for f in dashboard.filters], etag)
    block = (tmp_path / "d.yaml").read_text().split("filters:")[1].split("queries:")[0]

    # The author's quoting survives now, so this is the authored form verbatim.
    assert "{name: c, type: select, label: C, options_sql: 'SELECT 1'}" in block, block
    assert "default: us" in block, block
    # Authored key order survives too, so this matches the file as written.
    assert "{name: b, type: select, label: B, options: [all, us], default: all}" in block, block

    reloaded, _, _ = store.load("d")
    assert [f.default for f in reloaded.filters] == ["us", "all", "all"]


def test_editing_filters_does_not_materialize_a_derived_bind(tmp_path):
    """A daterange's bind is derived from its name when absent, and the editor
    round-trips the parsed model — so the first UI edit rewrote every terse
    authored filter into a verbose one. Same shape as the dead `format:` in #92:
    the editor writing derived state back into the file as if it were authored.
    """
    (tmp_path / "d.yaml").write_text(
        "title: D\n"
        "source: {type: duckdb, attach_files: true}\n"
        "filters:\n"
        "  - {name: dates, type: daterange, label: When, default: last_30_days}\n"
        "  - {name: window, type: daterange, label: Custom, "
        "bind: {start: from_d, end: to_d}}\n"
        "queries: {q: 'SELECT 1 AS n'}\n"
        "tiles: [{id: t, query: q}]\n"
    )
    store = DashboardStore(tmp_path)
    dashboard, _before, etag = store.load("d")
    store.update_filters("d", [f.model_dump(exclude_none=True) for f in dashboard.filters], etag)
    after = (tmp_path / "d.yaml").read_text()

    filters_block = after.split("filters:")[1].split("queries:")[0]
    # The derived one goes back exactly as authored...
    assert "{name: dates, type: daterange, label: When, default: last_30_days}" in filters_block
    # ...and the hand-written one is untouched, because it is not derivable.
    assert "bind: {start: from_d, end: to_d}" in filters_block

    reloaded, _, _ = store.load("d")
    assert reloaded.filters[0].bind == {"start": "dates_start", "end": "dates_end"}
    assert reloaded.filters[1].bind == {"start": "from_d", "end": "to_d"}


AUTHORED_FILTERS = (
    "title: D\n"
    "source: {type: duckdb, attach_files: true}\n"
    "filters:\n"
    "  # the date window everything hangs off\n"
    "  - name: dates\n"
    "    type: daterange\n"
    "    label: When\n"
    "    default: last_30_days\n"
    "  - {name: tier, type: select, label: Tier, options: [a, b], default: a}\n"
    "queries: {q: 'SELECT 1 AS n'}\n"
    "tiles: [{id: t, query: q}]\n"
)


def _save_filters(store, transform=lambda fs: fs):
    dashboard, _, etag = store.load("d")
    filters = [f.model_dump(exclude_none=True) for f in dashboard.filters]
    store.update_filters("d", transform(filters), etag)


def test_a_no_op_filter_save_leaves_the_file_untouched(tmp_path):
    """update_filters rebuilt the list as flow items, so editing one filter
    reflowed every filter and buried the real change in noise. The whole point
    of the surgical writes is that a diff shows what changed."""
    (tmp_path / "d.yaml").write_text(AUTHORED_FILTERS)
    store = DashboardStore(tmp_path)
    _save_filters(store)
    assert (tmp_path / "d.yaml").read_text() == AUTHORED_FILTERS


def test_editing_one_filter_touches_only_that_filter(tmp_path):
    """Block form, quoting and the neighbouring flow filter all survive."""
    (tmp_path / "d.yaml").write_text(AUTHORED_FILTERS)
    store = DashboardStore(tmp_path)

    def rename(filters):
        filters[0]["label"] = "Reporting window"
        return filters

    _save_filters(store, rename)
    after = (tmp_path / "d.yaml").read_text()
    changed = [
        (a, b)
        for a, b in zip(AUTHORED_FILTERS.splitlines(), after.splitlines(), strict=True)
        if a != b
    ]
    assert changed == [("    label: When", "    label: Reporting window")], changed
    assert "# the date window everything hangs off" in after


def test_adding_and_removing_filters_leaves_the_others_alone(tmp_path):
    (tmp_path / "d.yaml").write_text(AUTHORED_FILTERS)
    store = DashboardStore(tmp_path)

    _save_filters(
        store,
        lambda fs: [*fs, {"name": "seg", "type": "select", "label": "Seg", "options": ["x"]}],
    )
    after = (tmp_path / "d.yaml").read_text()
    assert "    label: When" in after, after
    assert "{name: seg, type: select, label: Seg, options: [x]}" in after, after

    _save_filters(store, lambda fs: [f for f in fs if f["name"] != "tier"])
    after = (tmp_path / "d.yaml").read_text()
    assert "    label: When" in after, after
    assert "name: tier" not in after, after


def test_a_hand_written_block_bind_keeps_its_shape(tmp_path):
    """Writing every key back flow-ifies a nested map even when the value is
    unchanged, so only the keys that actually differ are written. Without that,
    a bind the author spread over three lines collapses on the first save."""
    authored = (
        "title: D\n"
        "source: {type: duckdb, attach_files: true}\n"
        "filters:\n"
        "  - name: window\n"
        "    type: daterange\n"
        "    label: Custom\n"
        "    bind:\n"
        "      start: from_d\n"
        "      end: to_d\n"
        "queries: {q: 'SELECT 1 AS n'}\n"
        "tiles: [{id: t, query: q}]\n"
    )
    (tmp_path / "d.yaml").write_text(authored)
    store = DashboardStore(tmp_path)
    _save_filters(store)
    assert (tmp_path / "d.yaml").read_text() == authored


COMMENTED_FILTERS = (
    "title: D\n"
    "source: {type: duckdb, attach_files: true}\n"
    "filters:\n"
    "  - name: a\n"
    "    type: select\n"
    "    label: A\n"
    "    options: [1, 2]\n"
    "\n"
    "  # second filter\n"
    "  - name: b\n"
    "    type: select\n"
    "    label: B\n"
    "    options: [3, 4]\n"
    "queries: {q: 'SELECT 1 AS n'}\n"
    "tiles: [{id: t, query: q}]\n"
)


def test_a_comment_between_filters_survives_a_save(tmp_path):
    """A comment or blank line *between* filters lives in the sequence's
    positional comment table, not on either node — and slice-assigning the list
    wipes that table, so every save silently deleted them. The earlier test only
    covered a comment before the first filter, which is stored elsewhere and
    survived either way.
    """
    (tmp_path / "d.yaml").write_text(COMMENTED_FILTERS)
    store = DashboardStore(tmp_path)
    _save_filters(store)
    assert (tmp_path / "d.yaml").read_text() == COMMENTED_FILTERS


def test_a_comment_between_filters_survives_an_edit(tmp_path):
    (tmp_path / "d.yaml").write_text(COMMENTED_FILTERS)
    store = DashboardStore(tmp_path)

    def rename(filters):
        filters[0]["label"] = "Alpha"
        return filters

    _save_filters(store, rename)
    after = (tmp_path / "d.yaml").read_text()
    assert "  # second filter\n" in after, after
    assert "    label: Alpha" in after, after


def test_two_filters_sharing_a_name_stay_two_filters(tmp_path):
    """Nothing forbids duplicate names. Matching by name pointed both incoming
    filters at one node, so ruamel wrote an anchor and an alias — and the two
    entries became one object, where editing either changed both.
    """
    authored = (
        "title: D\n"
        "source: {type: duckdb, attach_files: true}\n"
        "filters:\n"
        "  - {name: a, type: select, label: A1, options: [1, 2]}\n"
        "  - {name: a, type: select, label: A2, options: [3, 4]}\n"
        "queries: {q: 'SELECT 1 AS n'}\n"
        "tiles: [{id: t, query: q}]\n"
    )
    (tmp_path / "d.yaml").write_text(authored)
    store = DashboardStore(tmp_path)
    _save_filters(store)
    after = (tmp_path / "d.yaml").read_text()
    assert "&id" not in after, after
    assert "*id" not in after, after
    assert "label: A1" in after, after
    assert "label: A2" in after, after

    # ...and they are still independent: editing the first leaves the second.
    def rename(filters):
        filters[0]["label"] = "A1 edited"
        return filters

    _save_filters(store, rename)
    after = (tmp_path / "d.yaml").read_text()
    assert "label: A1 edited" in after, after
    assert "label: A2" in after, after


def test_adding_a_second_filter_with_an_existing_name_stays_two_filters(tmp_path):
    """The other direction: the file has one `a`, the incoming list has two. The
    name is unambiguous in the file, so matching by it alone would point both
    incoming filters at that one node."""
    (tmp_path / "d.yaml").write_text(
        "title: D\n"
        "source: {type: duckdb, attach_files: true}\n"
        "filters:\n"
        "  - {name: a, type: select, label: A1, options: [1, 2]}\n"
        "queries: {q: 'SELECT 1 AS n'}\n"
        "tiles: [{id: t, query: q}]\n"
    )
    store = DashboardStore(tmp_path)
    _save_filters(
        store,
        lambda fs: [*fs, {"name": "a", "type": "select", "label": "A2", "options": ["x"]}],
    )
    after = (tmp_path / "d.yaml").read_text()
    assert "&id" not in after, after
    assert "label: A1" in after, after
    assert "label: A2" in after, after


def test_a_filter_that_omitted_its_type_does_not_gain_one(tmp_path):
    """`text` is FilterDef's default, so the model dump always carries it and
    the browser always sends it. Writing it back put `type: text` on every
    filter that had omitted it — the same verbosity as the derived bind and
    default, on the commonest authored form, and every existing test used an
    explicit `type:` so the suite stayed green.
    """
    authored = (
        "title: D\n"
        "source: {type: duckdb, attach_files: true}\n"
        "filters:\n"
        "  - name: region\n"
        "    label: Region\n"
        "    options: [us, eu]\n"
        "queries: {q: 'SELECT 1 AS n'}\n"
        "tiles: [{id: t, query: q}]\n"
    )
    (tmp_path / "d.yaml").write_text(authored)
    store = DashboardStore(tmp_path)
    _save_filters(store)
    assert (tmp_path / "d.yaml").read_text() == authored
    reloaded, _, _ = store.load("d")
    assert reloaded.filters[0].type == "text"


def test_reordering_carries_a_between_filters_comment_with_its_filter(tmp_path):
    """The comment is keyed by sequence *index*, so popping a node to move it
    deletes the entry outright. An earlier version of this PR claimed reorder
    was safe on the strength of a reorder that moved a different filter and
    never popped the commented one.
    """
    (tmp_path / "d.yaml").write_text(COMMENTED_FILTERS)
    store = DashboardStore(tmp_path)
    _save_filters(store, lambda filters: list(reversed(filters)))
    after = (tmp_path / "d.yaml").read_text()
    assert "# second filter" in after, after
    body = after.split("filters:")[1].split("queries:")[0]
    # It describes `b`, and follows it to the front.
    assert body.index("# second filter") < body.index("name: b"), body
    assert body.index("name: b") < body.index("name: a"), body


def test_editing_one_filter_does_not_rewrite_the_others(tmp_path):
    """Dropping a derived field on equality reached into filters nobody edited:
    saving a change to one deleted an authored `type: text` and an authored
    `bind:` from two others, because both happened to equal what the model
    would have derived. A field the author wrote stays, whatever it says —
    only a field they omitted is kept out.
    """
    authored = (
        "title: D\n"
        "source: {type: duckdb, attach_files: true}\n"
        "filters:\n"
        "  - name: note\n"
        "    type: text\n"
        "    label: Note\n"
        "  - name: window\n"
        "    type: daterange\n"
        "    label: Window\n"
        "    bind: {start: window_start, end: window_end}\n"
        "  - {name: seg, type: select, label: Seg, options_sql: 'SELECT 1', default: all}\n"
        "queries: {q: 'SELECT 1 AS n'}\n"
        "tiles: [{id: t, query: q}]\n"
    )
    (tmp_path / "d.yaml").write_text(authored)
    store = DashboardStore(tmp_path)

    def rename_seg(filters):
        filters[2]["label"] = "Segment"
        return filters

    _save_filters(store, rename_seg)
    after = (tmp_path / "d.yaml").read_text()
    changed = [
        (a, b) for a, b in zip(authored.splitlines(), after.splitlines(), strict=True) if a != b
    ]
    assert len(changed) == 1, changed
    assert "Segment" in changed[0][1], changed
    # ...and the two untouched filters keep every line they were written with.
    assert "    type: text\n" in after, after
    assert "    bind: {start: window_start, end: window_end}\n" in after, after


def test_a_comment_that_reaches_the_top_of_the_block_stays_there(tmp_path):
    """The limit of the carrying, pinned so it is a known shape rather than a
    surprise. A comment at index 0 is indistinguishable in YAML from a comment
    about the whole block, so it reloads as the sequence's head comment and
    stops moving. It is never lost — which is the invariant that matters, and
    what the old code broke on every save.
    """
    (tmp_path / "d.yaml").write_text(
        "title: D\n"
        "source: {type: duckdb, attach_files: true}\n"
        "filters:\n"
        "  - {name: a, type: select, label: A, options: [1, 2]}\n"
        "\n"
        "  # comment on b\n"
        "  - {name: b, type: select, label: B, options: [1, 2]}\n"
        "queries: {q: 'SELECT 1 AS n'}\n"
        "tiles: [{id: t, query: q}]\n"
    )
    store = DashboardStore(tmp_path)
    # One reorder carries it: b goes to the front and the comment goes with it.
    _save_filters(store, lambda filters: list(reversed(filters)))
    assert "# comment on b" in (tmp_path / "d.yaml").read_text()

    # From the top it no longer travels — but it is still there.
    _save_filters(
        store,
        lambda fs: [*fs, {"name": "c", "type": "select", "label": "C", "options": ["1"]}],
    )
    _save_filters(store, lambda filters: list(reversed(filters)))
    after = (tmp_path / "d.yaml").read_text()
    assert "# comment on b" in after, after
    reloaded, _, _ = store.load("d")
    assert {f.name for f in reloaded.filters} == {"a", "b", "c"}


LAYOUT_MIXED = """title: R
source: {type: duckdb, database: ':memory:'}
queries: {q: "SELECT 1 AS v"}
tiles:
  - {markdown: "## Section one", size: 12x1}
  - {title: A, query: q, size: 3x2}
  - {title: B, query: q, size: 3x2}
  - {markdown: "## Section two", size: 12x1}
  - {title: C, query: q, size: 12x4}
"""


def _layout(store, name="d"):
    d, _, _ = store.load(name)
    return {w.id: (w.position.x, w.position.y) for w in d.tiles}


def test_editing_one_tile_does_not_move_the_others(tmp_path):
    """A flowed tile's position is computed at parse time and never recorded,
    and the flow cursor starts *below* every pinned tile — so pinning one tile
    moved every tile still flowing. Editing a section header shoved the whole
    dashboard down and reordered it, and each later edit pinned a tile at its
    already-drifted spot, so a few edits walked the content thousands of pixels
    off the top of the page.
    """
    (tmp_path / "d.yaml").write_text(LAYOUT_MIXED)
    store = DashboardStore(tmp_path)
    before = _layout(store)

    target = "tile_4"
    x, y = before[target]
    store.upsert_tile(
        "d",
        {
            "id": target,
            "type": "text",
            "markdown": "## Section two",
            "position": {"x": x, "y": y, "w": 12, "h": 1},
        },
        None,
        None,
    )
    after = _layout(store)
    moved = {k: (before[k], after[k]) for k in before if before[k] != after[k]}
    assert not moved, moved


def test_repeated_edits_do_not_walk_the_layout(tmp_path):
    """The drift compounded: each edit pinned its tile at the position the
    previous edit had already pushed it to."""
    (tmp_path / "d.yaml").write_text(LAYOUT_MIXED)
    store = DashboardStore(tmp_path)
    before = _layout(store)
    for target in ("tile_4", "tile_1", "a"):
        x, y = _layout(store)[target]
        d, _, _ = store.load("d")
        w = next(t for t in d.tiles if t.id == target)
        payload = {"id": target, "position": {"x": x, "y": y, "w": w.position.w, "h": w.position.h}}
        payload |= (
            {"type": "text", "markdown": w.markdown}
            if w.type == "text"
            else {"title": w.title, "query": w.query}
        )
        store.upsert_tile("d", payload, None, None)
    assert _layout(store) == before, (before, _layout(store))


def _overlaps(store, name="d"):
    """Every (id_a, id_b) pair sharing a grid cell."""
    d, _, _ = store.load(name)
    cells: dict[tuple[int, int], list[str]] = {}
    for w in d.tiles:
        for x in range(w.position.x, w.position.x + w.position.w):
            for y in range(w.position.y, w.position.y + w.position.h):
                cells.setdefault((x, y), []).append(w.id)
    return {tuple(sorted(ids)) for ids in cells.values() if len(ids) > 1}


def test_adding_tiles_in_a_row_does_not_stack_them_on_each_other(tmp_path):
    """A new tile was placed below only the tiles already carrying a
    `position:`. Once pinning wrote the flowing tiles down where they were, the
    next added tile landed on top of one of them — sharing every cell of its
    footprint, so one tile was simply hidden behind another.
    """
    (tmp_path / "d.yaml").write_text(
        "title: D\nsource: {type: duckdb, database: ':memory:'}\n"
        'queries: {q: "SELECT 1 AS v"}\n'
        "tiles:\n"
        "  - {title: A, query: q, size: 3x2}\n"
        "  - {title: B, query: q, size: 3x2}\n"
        "  - {title: C, query: q, size: 3x2}\n"
    )
    store = DashboardStore(tmp_path)
    for new_id in ("d", "e", "f"):
        store.upsert_tile("d", {"id": new_id, "title": new_id.upper(), "query": "q"}, None, None)
        assert not _overlaps(store), (new_id, _overlaps(store))


def _boxes(store, name="d"):
    d, _, _ = store.load(name)
    return {w.id: (w.position.x, w.position.y, w.position.w, w.position.h) for w in d.tiles}


def test_an_edit_that_says_nothing_about_position_moves_nothing(tmp_path):
    """An edit is not a request to move. Placing a positionless payload at the
    bottom relocated the very tile being edited — and resized it to the default
    6x4 — which is the failure this whole change exists to stop, one path over.
    """
    (tmp_path / "d.yaml").write_text(
        "title: D\nsource: {type: duckdb, database: ':memory:'}\n"
        'queries: {q: "SELECT 1 AS v"}\n'
        "tiles:\n"
        "  - {title: A, query: q, size: 3x2}\n"
        "  - {title: B, query: q, size: 3x2}\n"
        "  - {title: C, query: q, size: 3x2}\n"
    )
    store = DashboardStore(tmp_path)
    before = _boxes(store)
    store.upsert_tile("d", {"id": "b", "title": "B renamed", "query": "q"}, None, None)
    after = _boxes(store)
    assert list(after) == ["a", "b_renamed", "c"], after
    assert list(after.values()) == list(before.values()), (before, after)


def test_an_edit_that_does_carry_a_position_still_moves_that_tile(tmp_path):
    """Keeping the pre-edit position must not swallow a real move."""
    (tmp_path / "d.yaml").write_text(
        "title: D\nsource: {type: duckdb, database: ':memory:'}\n"
        'queries: {q: "SELECT 1 AS v"}\n'
        "tiles:\n"
        "  - {title: A, query: q, size: 3x2}\n"
        "  - {title: B, query: q, size: 3x2}\n"
    )
    store = DashboardStore(tmp_path)
    store.upsert_tile(
        "d",
        {"id": "b", "title": "B", "query": "q", "position": {"x": 0, "y": 8, "w": 4, "h": 3}},
        None,
        None,
    )
    assert _boxes(store)["b"] == (0, 8, 4, 3), _boxes(store)
    assert _boxes(store)["a"] == (0, 0, 3, 2), _boxes(store)


def test_position_stays_inside_the_last_tile_when_a_blank_line_follows(tmp_path):
    """A blank line before queries: used to park position: after it. #296."""
    text = (
        "title: T\n"
        "source: {type: duckdb, attach_files: true}\n"
        "tiles:\n  - title: A\n    query: qa\n  - title: B\n    query: qb\n"
        "\nqueries:\n  qa: SELECT 1 AS n\n  qb: SELECT 1 AS n\n"
    )
    (tmp_path / "t.yaml").write_text(text)
    store = DashboardStore(tmp_path)
    store.update_positions(
        "t",
        {"a": {"x": 0, "y": 0, "w": 6, "h": 4}, "b": {"x": 6, "y": 0, "w": 6, "h": 4}},
        store.load("t")[2],
    )
    after = (tmp_path / "t.yaml").read_text()
    assert "query: qb\n    position: {x: 6, y: 0, w: 6, h: 4}\n" in after, after
    assert after.index("position: {x: 6, y: 0, w: 6, h: 4}") < after.index("queries:"), after


def test_position_stays_inside_the_last_tile_when_a_comment_follows(tmp_path):
    text = (
        "title: T\n"
        "source: {type: duckdb, attach_files: true}\n"
        "tiles:\n  - title: First\n    query: q1\n  - title: Second\n    query: q2\n"
        "\n# comment between tiles and queries\n"
        "queries:\n  q1: SELECT 1 AS n\n  q2: SELECT 1 AS n\n"
    )
    (tmp_path / "t.yaml").write_text(text)
    store = DashboardStore(tmp_path)
    store.update_positions(
        "t",
        {
            "first": {"x": 0, "y": 0, "w": 6, "h": 4},
            "second": {"x": 6, "y": 0, "w": 6, "h": 4},
        },
        store.load("t")[2],
    )
    after = (tmp_path / "t.yaml").read_text()
    assert "# comment between tiles and queries" in after
    pos = after.index("position: {x: 6, y: 0, w: 6, h: 4}")
    comment = after.index("# comment between tiles and queries")
    queries = after.index("queries:")
    assert pos < comment < queries, after


def test_upserted_tile_stays_inside_tiles_when_a_comment_follows(tmp_path):
    text = (
        "title: T\n"
        "source: {type: duckdb, attach_files: true}\n"
        "tiles:\n  - title: A\n    query: qa\n  - size: 12x2\n    markdown: hello\n"
        "\n# between\nqueries:\n  qa: SELECT 1 AS n\n"
    )
    (tmp_path / "t.yaml").write_text(text)
    store = DashboardStore(tmp_path)
    store.upsert_tile(
        "t",
        {
            "id": "b",
            "title": "B",
            "query": "qb",
            "chart": {"type": "table"},
            "position": {"x": 0, "y": 6, "w": 6, "h": 4},
        },
        "SELECT 2 AS n",
        store.load("t")[2],
    )
    after = (tmp_path / "t.yaml").read_text()
    assert "# between" in after
    assert after.index("title: B") < after.index("# between"), after
    assert after.index("# between") < after.index("queries:"), after


def test_upsert_after_a_positioned_tile_keeps_the_trailing_comment(tmp_path):
    """Last tile already has position: plus a trailer; appending used to
    park the new tile after the comment, and a second after-comment on
    dest would have been clobbered. #296 review."""
    text = (
        "title: T\n"
        "source: {type: duckdb, attach_files: true}\n"
        "tiles:\n"
        "  - title: A\n    query: qa\n    position: {x: 0, y: 0, w: 6, h: 4}\n"
        "\n# between\n"
        "queries:\n  qa: SELECT 1 AS n\n"
    )
    (tmp_path / "t.yaml").write_text(text)
    store = DashboardStore(tmp_path)
    store.upsert_tile(
        "t",
        {
            "id": "b",
            "title": "B",
            "query": "qb",
            "chart": {"type": "table"},
            "position": {"x": 0, "y": 4, "w": 6, "h": 4},
        },
        "SELECT 2 AS n",
        store.load("t")[2],
    )
    after = (tmp_path / "t.yaml").read_text()
    assert "position: {x: 0, y: 0, w: 6, h: 4}" in after
    assert after.index("title: B") < after.index("# between") < after.index("queries:"), after


def test_updating_existing_positions_does_not_move_a_trailing_comment(tmp_path):
    """_put_key returns early when position already exists; the trailer stays. #296."""
    text = (
        "title: T\n"
        "source: {type: duckdb, attach_files: true}\n"
        "tiles:\n"
        "  - title: A\n    query: qa\n    position: {x: 0, y: 0, w: 6, h: 4}\n"
        "  - title: B\n    query: qb\n    position: {x: 6, y: 0, w: 6, h: 4}\n"
        "\n# between\n"
        "queries:\n  qa: SELECT 1 AS n\n  qb: SELECT 1 AS n\n"
    )
    (tmp_path / "t.yaml").write_text(text)
    store = DashboardStore(tmp_path)
    store.update_positions(
        "t",
        {"a": {"x": 0, "y": 1, "w": 6, "h": 4}, "b": {"x": 6, "y": 1, "w": 6, "h": 4}},
        store.load("t")[2],
    )
    after = (tmp_path / "t.yaml").read_text()
    assert after.index("position: {x: 6, y: 1, w: 6, h: 4}") < after.index("# between")
    assert after.index("# between") < after.index("queries:")


THREE_TILES = """\
title: Comments
source: {type: duckdb, attach_files: true}
tiles:
  - title: First
    size: 6x3
    sql: |
      SELECT 1 AS a
  # c1 before second
  - title: Second
    size: 6x3
    sql: |
      SELECT 2 AS b
  # c2 before third
  - title: Third
    size: 6x3
    sql: |
      SELECT 3 AS c
"""


def _three(tmp_path):
    (tmp_path / "t.yaml").write_text(THREE_TILES)
    store = DashboardStore(tmp_path)
    return store, store.load("t")[2]


def test_deleting_the_first_tile_keeps_the_next_tiles_comment(tmp_path):
    """The `#` line above Second is stored on First's last key; popping First
    used to take it along. #285."""
    store, etag = _three(tmp_path)
    store.delete_tile("t", "first", etag)
    after = (tmp_path / "t.yaml").read_text()
    assert after == THREE_TILES.replace(
        "  - title: First\n    size: 6x3\n    sql: |\n      SELECT 1 AS a\n", ""
    ), after


def test_deleting_a_middle_tile_removes_only_its_lines(tmp_path):
    store, etag = _three(tmp_path)
    store.delete_tile("t", "second", etag)
    after = (tmp_path / "t.yaml").read_text()
    assert after == THREE_TILES.replace(
        "  - title: Second\n    size: 6x3\n    sql: |\n      SELECT 2 AS b\n", ""
    ), after


def test_deleting_the_last_tile_removes_only_its_lines(tmp_path):
    store, etag = _three(tmp_path)
    store.delete_tile("t", "third", etag)
    after = (tmp_path / "t.yaml").read_text()
    assert after == THREE_TILES.replace(
        "  - title: Third\n    size: 6x3\n    sql: |\n      SELECT 3 AS c\n", ""
    ), after


def test_editing_a_tile_keeps_the_next_tiles_comment(tmp_path):
    """PUT swaps sql: for query: — deleting the key that carried the comment. #285.
    The new key takes the old one's slot, and nothing else on the tile moves."""
    store, etag = _three(tmp_path)
    store.upsert_tile(
        "t", {"id": "first", "title": "First", "query": "first_q"}, "SELECT 111", etag
    )
    after = (tmp_path / "t.yaml").read_text()
    assert (
        "  - title: First\n"
        "    size: 6x3\n"
        "    query: first_q\n"
        "  # c1 before second\n"
        "  - title: Second\n"
    ) in after, after
    assert "  # c2 before third\n  - title: Third\n" in after
    assert after.count("#") == 2


def test_deleting_after_a_flow_ended_tile_keeps_both_comments(tmp_path):
    """After a flow value ruamel keys the comment on the *next* index instead,
    and drops it with the index. Both the line above the deleted tile and the
    line below it stay, and an end-of-line comment goes with its tile."""
    text = (
        "title: T\n"
        "source: {type: duckdb, attach_files: true}\n"
        "tiles:\n"
        "  - title: A\n    query: q\n    position: {x: 0, y: 0, w: 6, h: 3}\n"
        "  # about b\n"
        "  - title: B\n    query: q  # eol on b\n    position: {x: 6, y: 0, w: 6, h: 3}\n"
        "  # about c\n"
        "  - title: C\n    query: q\n    position: {x: 0, y: 3, w: 6, h: 3}\n"
        "queries:\n  q: SELECT 1 AS n\n"
    )
    (tmp_path / "t.yaml").write_text(text)
    store = DashboardStore(tmp_path)
    store.delete_tile("t", "b", store.load("t")[2])
    after = (tmp_path / "t.yaml").read_text()
    assert after == text.replace(
        "  - title: B\n    query: q  # eol on b\n    position: {x: 6, y: 0, w: 6, h: 3}\n", ""
    ), after


def test_pinning_a_tile_that_ends_in_a_block_chart_keeps_position_inside_it(tmp_path):
    """The comment after a nested block mapping lives on its deepest last key;
    _put_key only looked at the tile's own last key. #296 gap found by #285."""
    text = (
        "title: T\n"
        "source: {type: duckdb, attach_files: true}\n"
        "tiles:\n"
        "  - title: A\n    query: q\n    chart:\n      type: bar\n      x: n\n"
        "  # about b\n"
        "  - title: B\n    query: q\n"
        "queries:\n  q: SELECT 1 AS n\n"
    )
    (tmp_path / "t.yaml").write_text(text)
    store = DashboardStore(tmp_path)
    store.upsert_tile(
        "t",
        {"id": "b", "title": "B", "query": "q", "position": {"x": 6, "y": 0, "w": 4, "h": 4}},
        None,
        store.load("t")[2],
    )
    after = (tmp_path / "t.yaml").read_text()
    assert (
        "      x: n\n    position: {x: 0, y: 0, w: 6, h: 4}\n  # about b\n  - title: B\n"
    ) in after, after


def _round_trips(text: str) -> bool:
    """The store's own loader/dumper reproduces the file byte for byte, so the
    carried comments landed in slots ruamel agrees with, not just ones that
    happened to print right once."""
    from io import StringIO

    from sqldash.project.store import yaml

    out = StringIO()
    yaml.dump(yaml.load(text), out)
    return out.getvalue() == text


@pytest.mark.parametrize("tile", ["first", "second", "third"])
def test_a_delete_from_the_three_tile_file_round_trips(tmp_path, tile):
    store, etag = _three(tmp_path)
    store.delete_tile("t", tile, etag)
    assert _round_trips((tmp_path / "t.yaml").read_text())


BLANK_SEPARATED = """\
title: Blanks
source: {type: duckdb, attach_files: true}
tiles:
  - title: First
    sql: SELECT 1 AS a

  - title: Second
    sql: SELECT 2 AS b

  # c2 before third
  - title: Third
    sql: |
      SELECT 3 AS c

queries: {}
"""


@pytest.mark.parametrize(
    ("tile", "expected"),
    [
        (
            "first",
            BLANK_SEPARATED.replace("  - title: First\n    sql: SELECT 1 AS a\n\n", ""),
        ),
        (
            "second",
            BLANK_SEPARATED.replace("  - title: Second\n    sql: SELECT 2 AS b\n\n", ""),
        ),
        (
            "third",
            BLANK_SEPARATED.replace("  - title: Third\n    sql: |\n      SELECT 3 AS c\n", ""),
        ),
    ],
)
def test_deleting_a_blank_separated_tile_takes_one_separator_with_it(tmp_path, tile, expected):
    """A blank line between tiles is a trailing comment too. Carrying it verbatim
    stacked two blanks at the seam (or left one under `tiles:`); dropping it
    lost the separator before `queries:`. #285 review."""
    (tmp_path / "t.yaml").write_text(BLANK_SEPARATED)
    store = DashboardStore(tmp_path)
    store.delete_tile("t", tile, store.load("t")[2])
    after = (tmp_path / "t.yaml").read_text()
    assert after == expected, after
    assert _round_trips(after)


def test_deleting_the_last_tile_keeps_the_comments_above_and_after_it(tmp_path):
    """Both lines end up after the new last tile, in order: that is the only
    place text between the tiles and the next key can live. #285 review."""
    text = (
        "title: T\n"
        "source: {type: duckdb, attach_files: true}\n"
        "tiles:\n"
        "  - title: First\n    sql: SELECT 1 AS a\n    position: {x: 0, y: 0, w: 6, h: 3}\n"
        "  # above last\n"
        "  - title: Last\n    sql: |\n      SELECT 2 AS b\n"
        "  # trailer after last\n"
        "queries: {}\n"
    )
    (tmp_path / "t.yaml").write_text(text)
    store = DashboardStore(tmp_path)
    store.delete_tile("t", "last", store.load("t")[2])
    after = (tmp_path / "t.yaml").read_text()
    assert after == text.replace("  - title: Last\n    sql: |\n      SELECT 2 AS b\n", ""), after
    assert _round_trips(after)


def test_pruning_a_tiles_first_key_keeps_its_comment_ahead_of_the_next_key(tmp_path):
    """The comment under a deleted first key has no key above to live on; it
    used to be parked after the *next* key (or after that key's whole block),
    so it read as a note on the wrong thing. #285 review."""
    text = (
        "title: T\n"
        "source: {type: duckdb, attach_files: true}\n"
        "sources:\n  alt: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - source: alt\n"
        "    # why this source\n"
        "    title: A\n"
        "    query: q\n"
        "  # about b\n"
        "  - title: B\n"
        "    query: q\n"
        "queries:\n  q: SELECT 1 AS n\n"
    )
    (tmp_path / "t.yaml").write_text(text)
    store = DashboardStore(tmp_path)
    store.upsert_tile("t", {"id": "a", "title": "A", "query": "q"}, None, store.load("t")[2])
    after = (tmp_path / "t.yaml").read_text()
    assert after.startswith(
        "title: T\n"
        "source: {type: duckdb, attach_files: true}\n"
        "sources:\n  alt: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  # why this source\n"
        "  - title: A\n"
        "    query: q\n"
        "  # about b\n"
        "  - title: B\n"
    ), after
    assert _round_trips(after)


def test_a_position_patch_drops_a_stale_size_beside_it(tmp_path):
    """A tile carrying both keeps position (dimensions_hint prefers it) and
    loses size, which was dead config a hand edit could change to no effect.
    The comment under size and the end-of-line comment on position stay. #338."""
    text = (
        "title: T\n"
        "source: {type: duckdb, attach_files: true}\n"
        "tiles:\n"
        "  - title: A\n    chart: bar\n    format: currency\n    size: 12x8\n"
        "    position: {x: 6, y: 2, w: 6, h: 4}\n    query: q\n"
        "  # about b\n"
        "  - title: B\n    size: 6x3\n    # why b is small\n"
        "    position: {x: 0, y: 6, w: 6, h: 3}  # keep\n    query: q\n"
        "queries:\n  q: SELECT 1 AS n\n"
    )
    (tmp_path / "t.yaml").write_text(text)
    store = DashboardStore(tmp_path)
    store.update_positions(
        "t",
        {"a": {"x": 0, "y": 9, "w": 6, "h": 4}, "b": {"x": 0, "y": 7, "w": 6, "h": 3}},
        store.load("t")[2],
    )
    after = (tmp_path / "t.yaml").read_text()
    expected = (
        text.replace("    size: 12x8\n", "")
        .replace("{x: 6, y: 2, w: 6, h: 4}", "{x: 0, y: 9, w: 6, h: 4}")
        .replace("    size: 6x3\n", "")
        .replace("{x: 0, y: 6, w: 6, h: 3}", "{x: 0, y: 7, w: 6, h: 3}")
    )
    assert after == expected, after
    assert "size" not in after


@pytest.mark.parametrize(
    ("below", "expected_head"),
    [
        (
            "    title: A\n",
            "  # why this size\n  - title: A\n",
        ),
        (
            "    chart:\n      type: bar\n    title: A\n",
            "  # why this size\n  - chart:\n      type: bar\n    title: A\n",
        ),
    ],
)
def test_dropping_a_leading_size_keeps_its_comment_ahead_of_the_next_key(
    tmp_path, below, expected_head
):
    """A position PATCH deletes `size:`; when it was the tile's first key the
    comment under it used to slide below the next key, or below that key's
    whole block. #338 via the #285 review."""
    text = (
        "title: T\n"
        "source: {type: duckdb, attach_files: true}\n"
        "tiles:\n"
        "  - size: 6x3\n"
        "    # why this size\n" + below + "    sql: SELECT 1 AS a\n"
        "    position: {x: 6, y: 0, w: 6, h: 3}\n"
        "queries: {}\n"
    )
    (tmp_path / "t.yaml").write_text(text)
    store = DashboardStore(tmp_path)
    store.update_positions("t", {"a": {"x": 0, "y": 0, "w": 6, "h": 3}}, store.load("t")[2])
    after = (tmp_path / "t.yaml").read_text()
    assert after == (
        "title: T\n"
        "source: {type: duckdb, attach_files: true}\n"
        "tiles:\n" + expected_head + "    sql: SELECT 1 AS a\n"
        "    position: {x: 0, y: 0, w: 6, h: 3}\n"
        "queries: {}\n"
    ), after
    assert _round_trips(after)


COMMENTED_FOUR = """\
title: Layout
source: {type: duckdb, attach_files: true}

tiles:
  # first tile comment
  - title: Revenue by category
    sql: |
      SELECT 1 AS a

  # second tile comment
  - title: Regions
    sql: |
      SELECT 2 AS a

  # third tile comment
  - title: Recent orders
    sql: |
      SELECT 3 AS a

  # last tile comment
  - markdown: |
      A note.
"""


def test_pinning_one_tiles_position_leaves_every_other_comment_alone(tmp_path):
    """A drag PATCHes one tile, and `pin_derived_ids` names the untitled one.
    Both append a key after a block scalar, and ruamel parks the *next* tile's
    comment in that key's slot with a start_mark at column 0 — so every append
    re-indented a comment three tiles away to column 0 (#393)."""
    (tmp_path / "layout.yaml").write_text(COMMENTED_FOUR)
    store = DashboardStore(tmp_path)
    store.update_positions(
        "layout", {"revenue_by_category": {"x": 0, "y": 0, "w": 6, "h": 4}}, store.load("layout")[2]
    )
    after = (tmp_path / "layout.yaml").read_text()
    assert after == COMMENTED_FOUR.replace(
        "      SELECT 1 AS a\n",
        "      SELECT 1 AS a\n    position: {x: 0, y: 0, w: 6, h: 4}\n",
    ).replace("      A note.\n", "      A note.\n    id: tile_4\n"), after
    assert _round_trips(after)


def test_a_delete_after_pinning_keeps_the_comments_at_their_indent(tmp_path):
    """The knock-on the issue reports: once a comment has been moved out of its
    tile's slot to column 0, deleting the tile it now precedes strands it. With
    the slot intact the delete reads like a hand edit — the `#` above the
    removed tile stays with the tile above it, at the indent it was written
    at (#393, #285)."""
    (tmp_path / "layout.yaml").write_text(COMMENTED_FOUR)
    store = DashboardStore(tmp_path)
    store.update_positions(
        "layout", {"revenue_by_category": {"x": 0, "y": 0, "w": 6, "h": 4}}, store.load("layout")[2]
    )
    store.delete_tile("layout", "regions", store.load("layout")[2])
    after = (tmp_path / "layout.yaml").read_text()
    assert after == (
        "title: Layout\n"
        "source: {type: duckdb, attach_files: true}\n"
        "\n"
        "tiles:\n"
        "  # first tile comment\n"
        "  - title: Revenue by category\n"
        "    sql: |\n"
        "      SELECT 1 AS a\n"
        "    position: {x: 0, y: 0, w: 6, h: 4}\n"
        "\n"
        "  # second tile comment\n"
        "\n"
        "  # third tile comment\n"
        "  - title: Recent orders\n"
        "    sql: |\n"
        "      SELECT 3 AS a\n"
        "\n"
        "  # last tile comment\n"
        "  - markdown: |\n"
        "      A note.\n"
        "    id: tile_4\n"
    ), after
    assert _round_trips(after)


def test_appending_a_key_after_a_block_scalar_keeps_the_next_comments_indent(tmp_path):
    """The narrow shape, on the two slots the issue names: a comment directly
    under a block scalar (no blank line) already worked because the token's
    mark is the comment's own column; put a blank line in front of it and the
    token starts on the scalar's last line at column 0 instead (#393)."""
    text = (
        "title: T\n"
        "source: {type: duckdb, attach_files: true}\n"
        "tiles:\n"
        "  - title: A\n"
        "    sql: SELECT 1 AS a\n"
        "    position: {x: 0, y: 0, w: 6, h: 4}\n"
        "\n"
        "  # before the markdown tile\n"
        "  - markdown: |\n"
        "      A note.\n"
        "\n"
        "  # a trailing note about the whole tiles block\n"
        "layout: {columns: 12}\n"
    )
    (tmp_path / "t.yaml").write_text(text)
    store = DashboardStore(tmp_path)
    store.update_positions("t", {"a": {"x": 0, "y": 4, "w": 6, "h": 4}}, store.load("t")[2])
    after = (tmp_path / "t.yaml").read_text()
    assert after == text.replace(
        "    position: {x: 0, y: 0, w: 6, h: 4}\n", "    position: {x: 0, y: 4, w: 6, h: 4}\n"
    ).replace("      A note.\n", "      A note.\n    id: tile_2\n"), after
    assert _round_trips(after)


def test_a_new_tile_with_only_a_chart_type_writes_the_shorthand(store):
    """The query page no longer sends inferred encodings (#357), so a fresh
    bar tile is `chart: bar`, the form an author would write."""
    s, path = store
    s.upsert_tile(
        "test",
        {"id": "w3", "query": "q3", "title": "Bare", "chart": {"type": "bar", "x": None, "y": []}},
        sql="SELECT 'a' AS a, 3 AS b",
        if_match=None,
    )
    text = path.read_text()
    assert "    chart: bar\n" in text, text


REFERENCES_DOC = (
    "title: D\nsource: {type: duckdb, database: ':memory:'}\n"
    "queries: {q: \"SELECT DATE '2026-09-01' AS d, 2 AS b\"}\n"
    "tiles:\n"
    "  - title: A\n"
    "    query: q\n"
    "    chart:\n"
    "      type: bar\n"
    "      references:\n"
    "        # the quarterly goal\n"
    "        - {y: 150000, label: Goal}\n"
    "        - {x: 2026-09-01, label: Launch}\n"
    "        - {y: [1, 2]}\n"
)


def _save_chart(tmp_path, chart):
    store = DashboardStore(tmp_path)
    _, _, etag = store.load("d")
    store.upsert_tile(
        "d", {"id": "a", "title": "A", "query": "q", "chart": chart}, sql=None, if_match=etag
    )
    return (tmp_path / "d.yaml").read_text()


def test_changing_the_chart_type_keeps_the_authored_references_untouched(tmp_path):
    (tmp_path / "d.yaml").write_text(REFERENCES_DOC)
    text = _save_chart(
        tmp_path,
        {
            "type": "line",
            "references": [
                {"y": 150000, "label": "Goal"},
                {"x": "2026-09-01", "label": "Launch"},
                {"y": [1, 2]},
            ],
        },
    )
    assert text == REFERENCES_DOC.replace("      type: bar\n", "      type: line\n"), text


def test_removing_one_reference_deletes_only_its_line(tmp_path):
    (tmp_path / "d.yaml").write_text(REFERENCES_DOC)
    text = _save_chart(
        tmp_path,
        {"type": "bar", "references": [{"y": 150000, "label": "Goal"}, {"y": [1, 2]}]},
    )
    assert text == REFERENCES_DOC.replace("        - {x: 2026-09-01, label: Launch}\n", ""), text


def test_adding_a_reference_appends_one_flow_mapping(tmp_path):
    (tmp_path / "d.yaml").write_text(REFERENCES_DOC)
    text = _save_chart(
        tmp_path,
        {
            "type": "bar",
            "references": [
                {"y": 150000, "label": "Goal"},
                {"x": "2026-09-01", "label": "Launch"},
                {"y": [1, 2]},
                {"metric": "revenue_target", "color": "good"},
            ],
        },
    )
    assert text == REFERENCES_DOC + "        - {metric: revenue_target, color: good}\n", text
    dashboard, _, _ = DashboardStore(tmp_path).load("d")
    assert [r.metric for r in dashboard.tiles[0].chart.references] == [
        None,
        None,
        None,
        "revenue_target",
    ]


def test_references_on_a_new_chart_mapping_write_as_flow(tmp_path):
    (tmp_path / "d.yaml").write_text(
        "title: D\nsource: {type: duckdb, database: ':memory:'}\n"
        'queries: {q: "SELECT 1 AS a"}\n'
        "tiles:\n"
        "  - {title: A, query: q, chart: bar}\n"
    )
    text = _save_chart(tmp_path, {"type": "bar", "references": [{"y": 5, "label": "Target"}]})
    dashboard, _, _ = DashboardStore(tmp_path).load("d")
    assert dashboard.tiles[0].chart.references[0].y == 5
    assert "references: [{y: 5, label: Target}]" in text, text


COMMENTED_REFERENCES = (
    "title: D\nsource: {type: duckdb, database: ':memory:'}\n"
    'queries: {q: "SELECT 1 AS a, 2 AS b"}\n'
    "tiles:\n"
    "  - title: A\n"
    "    query: q\n"
    "    chart:\n"
    "      type: bar\n"
    "      references:\n"
    "        - {y: 10}  # quarterly goal\n"
    "        # the stretch target\n"
    "        - {y: 15}  # stretch\n"
)


@pytest.mark.parametrize(
    ("references", "expected"),
    [
        (
            [{"y": 10}, {"y": 15}, {"y": 20}],
            COMMENTED_REFERENCES + "        - {y: 20}\n",
        ),
        (
            [{"y": 10}],
            COMMENTED_REFERENCES.replace("        - {y: 15}  # stretch\n", ""),
        ),
        (
            [{"y": 15}],
            COMMENTED_REFERENCES.replace("        - {y: 10}  # quarterly goal\n", ""),
        ),
        (
            [{"y": 12}, {"y": 15}],
            COMMENTED_REFERENCES.replace("{y: 10}", "{y: 12}"),
        ),
    ],
    ids=["append", "remove-last", "remove-first", "edit-in-place"],
)
def test_editing_references_keeps_the_comments_around_them(tmp_path, references, expected):
    (tmp_path / "d.yaml").write_text(COMMENTED_REFERENCES)
    text = _save_chart(tmp_path, {"type": "bar", "references": references})
    assert text == expected, text


def _save_refs(tmp_path, tile_id, references):
    store = DashboardStore(tmp_path)
    _, _, etag = store.load("d")
    store.upsert_tile(
        "d",
        {
            "id": tile_id,
            "title": tile_id.upper(),
            "query": "q",
            "chart": {"type": "bar", "references": references},
        },
        sql=None,
        if_match=etag,
    )
    dashboard, _, _ = store.load("d")
    refs = {
        t.id: [r.model_dump(exclude_none=True) for r in t.chart.references] for t in dashboard.tiles
    }
    return (tmp_path / "d.yaml").read_text(), refs


def _ref_tiles(a_refs: str, b_refs: str) -> str:
    return (
        "title: D\nsource: {type: duckdb, database: ':memory:'}\n"
        'queries: {q: "SELECT 1 AS a, 2 AS b"}\n'
        "tiles:\n"
        "  - title: A\n    query: q\n    chart:\n      type: bar\n      references:\n"
        + a_refs
        + "  - title: B\n    query: q\n    chart:\n      type: bar\n      references:\n"
        + b_refs
    )


SHARED_REFERENCE = _ref_tiles("        - &goal {y: 10, label: Goal}\n", "        - *goal\n")


@pytest.mark.parametrize("edited", ["a", "b"])
def test_editing_a_shared_reference_leaves_the_other_tile_alone(tmp_path, edited):
    (tmp_path / "d.yaml").write_text(SHARED_REFERENCE)
    text, refs = _save_refs(tmp_path, edited, [{"y": 20, "label": "Goal"}])
    other = "b" if edited == "a" else "a"
    assert refs[edited] == [{"y": 20, "label": "Goal"}], text
    assert refs[other] == [{"y": 10, "label": "Goal"}], text


def test_nested_aliases_in_references_are_expanded_before_an_edit(tmp_path):
    (tmp_path / "d.yaml").write_text(
        _ref_tiles(
            "        - {y: 10, label: &name Goal}\n        - {y: 20, label: *name}\n",
            "        - {y: 5}\n",
        )
    )
    text, refs = _save_refs(
        tmp_path, "a", [{"y": 10, "label": "Goal"}, {"y": 20, "label": "Stretch"}]
    )
    assert refs["a"] == [{"y": 10, "label": "Goal"}, {"y": 20, "label": "Stretch"}], text
    assert "&" not in text, text
    assert "*" not in text, text


MERGED_REFERENCE = _ref_tiles(
    "        - &target {y: 10, color: good, style: solid}\n        - {<<: *target, y: 20}\n",
    "        - *target\n",
)


def test_overriding_a_merged_reference_key_writes_the_override(tmp_path):
    (tmp_path / "d.yaml").write_text(MERGED_REFERENCE)
    text, refs = _save_refs(
        tmp_path,
        "a",
        [
            {"y": 10, "color": "good", "style": "solid"},
            {"y": 20, "color": "bad", "style": "solid"},
        ],
    )
    assert refs["a"][1] == {"y": 20, "color": "bad", "style": "solid"}, text
    assert refs["b"] == [{"y": 10, "color": "good", "style": "solid"}], text
    assert "<<" not in text.split("  - title: B")[0], text


def test_deleting_a_merged_reference_key_takes_it_off(tmp_path):
    (tmp_path / "d.yaml").write_text(MERGED_REFERENCE)
    text, refs = _save_refs(
        tmp_path, "a", [{"y": 10, "color": "good", "style": "solid"}, {"y": 20, "color": "good"}]
    )
    assert refs["a"][1] == {"y": 20, "color": "good"}, text
    assert refs["b"] == [{"y": 10, "color": "good", "style": "solid"}], text


@pytest.mark.parametrize("doc", [SHARED_REFERENCE, MERGED_REFERENCE], ids=["alias", "merge"])
def test_an_unchanged_save_of_shared_references_is_byte_identical(tmp_path, doc):
    (tmp_path / "d.yaml").write_text(doc)
    dashboard, _, _ = DashboardStore(tmp_path).load("d")
    references = [r.model_dump(exclude_none=True) for r in dashboard.tiles[0].chart.references]
    text, _ = _save_refs(tmp_path, "a", references)
    assert text == doc, text


BLOCK_REFERENCES = _ref_tiles(
    "        - y: 10\n"
    "        # keep this for 20\n"
    "        - y: 20\n"
    "          label: Twenty\n"
    "        # keep this for 30\n"
    "        - y: 30\n",
    "        - {y: 5}\n",
)


@pytest.mark.parametrize(
    ("references", "expected"),
    [
        (
            [{"y": 20, "label": "Twenty"}, {"y": 30}],
            BLOCK_REFERENCES.replace("        - y: 10\n", "", 1),
        ),
        (
            [{"y": 10}, {"y": 30}],
            BLOCK_REFERENCES.replace("        - y: 20\n          label: Twenty\n", "", 1),
        ),
    ],
    ids=["remove-first", "remove-middle"],
)
def test_removing_a_block_reference_keeps_the_comment_over_the_next(tmp_path, references, expected):
    (tmp_path / "d.yaml").write_text(BLOCK_REFERENCES)
    text, _ = _save_refs(tmp_path, "a", references)
    assert text == expected, text


LABELLED_BLOCK_REFERENCES = _ref_tiles(
    "        - y: 10\n          label: Ten\n        # keep this for 20\n        - y: 20\n",
    "        - {y: 5}\n",
)


@pytest.mark.parametrize(
    ("references", "expected"),
    [
        (
            [{"y": 10}, {"y": 20}],
            LABELLED_BLOCK_REFERENCES.replace("          label: Ten\n", "", 1),
        ),
        (
            [{"x": "a", "label": "Ten"}, {"y": 20}],
            LABELLED_BLOCK_REFERENCES.replace(
                "        - y: 10\n          label: Ten\n",
                "        - label: Ten\n          x: a\n",
                1,
            ),
        ),
        (
            [{"y": 12, "label": "Ten"}, {"y": 20}],
            LABELLED_BLOCK_REFERENCES.replace("        - y: 10\n", "        - y: 12\n", 1),
        ),
    ],
    ids=["drop-label", "y-to-x", "change-value"],
)
def test_editing_a_block_reference_keeps_the_comment_over_the_next(tmp_path, references, expected):
    (tmp_path / "d.yaml").write_text(LABELLED_BLOCK_REFERENCES)
    text, _ = _save_refs(tmp_path, "a", references)
    assert text == expected, text


def test_a_comment_under_a_deleted_key_moves_to_the_key_above(tmp_path):
    doc = _ref_tiles(
        "        - y: 10\n"
        "          label: Ten\n"
        "          # the goal colour\n"
        "          color: good\n"
        "        - y: 20\n",
        "        - {y: 5}\n",
    )
    (tmp_path / "d.yaml").write_text(doc)
    text, _ = _save_refs(tmp_path, "a", [{"y": 10, "label": "Ten"}, {"y": 20}])
    assert "# the goal colour" in text, text
    assert "color: good" not in text, text


_GRID_HEAD = (
    "title: D\nsource: {type: duckdb, database: ':memory:'}\n"
    'queries: {q: "SELECT 1 AS a, 2 AS b"}\n'
    "tiles:\n  - title: A\n    query: q\n    chart:\n"
)
_GRID_TAIL = "  # the next tile\n  - {title: B, query: q}\n"
_BAND = "            - 10\n            - 20\n"


@pytest.mark.parametrize(
    ("before", "chart", "after"),
    [
        (
            "      type: bar\n      references:\n        - y:\n"
            + _BAND
            + "          # keep this label\n          label: Band\n        - y: 30\n",
            {"type": "bar", "references": [{"y": [10, 25], "label": "Band"}, {"y": 30}]},
            "      type: bar\n      references:\n        - y: [10, 25]\n"
            "          # keep this label\n          label: Band\n        - y: 30\n",
        ),
        (
            "      type: bar\n      references:\n        - label: Band\n          y:\n"
            + _BAND
            + "        # keep this for 30\n        - y: 30\n",
            {"type": "bar", "references": [{"y": [10, 25], "label": "Band"}, {"y": 30}]},
            "      type: bar\n      references:\n        - label: Band\n          y: [10, 25]\n"
            "        # keep this for 30\n        - y: 30\n",
        ),
        (
            "      type: bar\n      references:\n        - y: 30\n        - label: Band\n"
            "          y:\n" + _BAND,
            {"type": "bar", "references": [{"y": 30}, {"y": [10, 25], "label": "Band"}]},
            "      type: bar\n      references:\n        - y: 30\n        - label: Band\n"
            "          y: [10, 25]\n",
        ),
        (
            "      type: bar\n      references:\n        - y: 10\n"
            "      # keep this x\n      x: a\n",
            {"type": "bar", "x": "a"},
            "      type: bar\n      # keep this x\n      x: a\n",
        ),
        (
            "      type: bar\n      references: [{y: 10, label: Ten}]\n"
            "      # keep this x\n      x: a\n",
            {"type": "bar", "x": "a"},
            "      type: bar\n      # keep this x\n      x: a\n",
        ),
        (
            "      type: bar\n      x: a\n      references:\n        - y: 10\n",
            {"type": "bar", "x": "a"},
            "      type: bar\n      x: a\n",
        ),
        (
            "      type: bar\n      x: a\n      references: [{y: 10, label: Ten}]\n",
            {"type": "bar", "x": "a", "references": [{"y": 12, "label": "Ten"}]},
            "      type: bar\n      x: a\n      references: [{y: 12, label: Ten}]\n",
        ),
        (
            "      type: bar\n      references:\n        - {y: 10, label: Ten}\n"
            "      # keep this x\n      x: a\n",
            {"type": "bar", "x": "a", "references": [{"y": 12, "label": "Ten"}]},
            "      type: bar\n      references:\n        - {y: 12, label: Ten}\n"
            "      # keep this x\n      x: a\n",
        ),
    ],
    ids=[
        "nested-band-block-middle-key",
        "nested-band-block-last-key-middle-item",
        "nested-band-block-last-item",
        "delete-block-references-middle",
        "delete-flow-references-middle",
        "delete-block-references-last",
        "replace-flow-reference-last",
        "replace-flow-reference-middle",
    ],
)
def test_replacing_or_deleting_chart_values_keeps_every_other_line(tmp_path, before, chart, after):
    (tmp_path / "d.yaml").write_text(_GRID_HEAD + before + _GRID_TAIL)
    store = DashboardStore(tmp_path)
    _, _, etag = store.load("d")
    store.upsert_tile(
        "d", {"id": "a", "title": "A", "query": "q", "chart": chart}, sql=None, if_match=etag
    )
    text = (tmp_path / "d.yaml").read_text()
    assert text == _GRID_HEAD + after + _GRID_TAIL, text


TILE_MERGE_REFERENCES = (
    "title: D\nsource: {type: duckdb, database: ':memory:'}\n"
    'queries: {q: "SELECT 1 AS a, 2 AS b"}\n'
    "tiles:\n"
    "  - &base\n"
    "    title: A\n"
    "    query: q\n"
    "    chart:\n"
    "      type: bar\n"
    "      references: [{y: 10, label: Goal}]\n"
    "  - <<: *base\n"
    "    title: B\n"
)


def test_editing_a_tile_that_merges_another_leaves_its_references_alone(tmp_path):
    (tmp_path / "d.yaml").write_text(TILE_MERGE_REFERENCES)
    text, refs = _save_refs(tmp_path, "b", [{"y": 20, "label": "Goal"}])
    assert refs == {"a": [{"y": 10, "label": "Goal"}], "b": [{"y": 20, "label": "Goal"}]}, text
    assert text.startswith(TILE_MERGE_REFERENCES.split("  - <<: *base")[0]), text


COMBO_DOC = (
    "title: D\nsource: {type: duckdb, database: ':memory:'}\n"
    'queries: {q: "SELECT 1 AS w, 2 AS revenue, 0.5 AS rate"}\n'
    "tiles:\n"
    "  - title: A\n"
    "    query: q\n"
    "    chart:\n"
    "      type: bar\n"
    "      y: [revenue, rate]\n"
    "      series:\n"
    "        # the rate reads against its own axis\n"
    "        rate:\n"
    "          type: line\n"
    "          axis: right\n"
    "      axes:\n"
    "        right: {title: Rate, min: 0}\n"
)


def _save_combo(tmp_path, chart):
    store = DashboardStore(tmp_path)
    _, _, etag = store.load("d")
    store.upsert_tile(
        "d", {"id": "a", "title": "A", "query": "q", "chart": chart}, sql=None, if_match=etag
    )
    return (tmp_path / "d.yaml").read_text()


def test_changing_one_series_mark_rewrites_only_that_value(tmp_path):
    (tmp_path / "d.yaml").write_text(COMBO_DOC)
    text = _save_combo(
        tmp_path,
        {
            "type": "bar",
            "y": ["revenue", "rate"],
            "series": {"rate": {"type": "area", "axis": "right"}},
            "axes": {"right": {"title": "Rate", "min": 0}},
        },
    )
    assert text == COMBO_DOC.replace("          type: line\n", "          type: area\n"), text


def test_moving_a_series_back_to_the_left_drops_its_axis_key_only(tmp_path):
    (tmp_path / "d.yaml").write_text(COMBO_DOC)
    text = _save_combo(
        tmp_path,
        {
            "type": "bar",
            "y": ["revenue", "rate"],
            "series": {"rate": {"type": "line"}},
            "axes": {"left": {"title": "Revenue"}},
        },
    )
    expected = COMBO_DOC.replace("          axis: right\n", "").replace(
        "        right: {title: Rate, min: 0}\n", "        left: {title: Revenue}\n"
    )
    assert text == expected, text
    dashboard, _, _ = DashboardStore(tmp_path).load("d")
    assert dashboard.tiles[0].chart.series["rate"].axis is None


def test_a_combo_on_a_short_chart_writes_as_flow(tmp_path):
    (tmp_path / "d.yaml").write_text(
        "title: D\nsource: {type: duckdb, database: ':memory:'}\n"
        'queries: {q: "SELECT 1 AS a, 2 AS b"}\n'
        "tiles:\n"
        "  - {title: A, query: q, chart: {type: line, y: [a, b]}}\n"
    )
    text = _save_combo(
        tmp_path,
        {"type": "line", "y": ["a", "b"], "series": {"b": {"type": "bar", "axis": "right"}}},
    )
    assert "chart: {type: line, y: [a, b], series: {b: {type: bar, axis: right}}}" in text, text


def _save_tile(tmp_path, tile_id, chart):
    store = DashboardStore(tmp_path)
    _, _, etag = store.load("d")
    store.upsert_tile(
        "d",
        {"id": tile_id, "title": tile_id.upper(), "query": "q", "chart": chart},
        sql=None,
        if_match=etag,
    )
    dashboard, _, _ = store.load("d")
    return (tmp_path / "d.yaml").read_text(), {t.id: t.chart for t in dashboard.tiles}


def _two_tiles(a_chart: str, b_chart: str) -> str:
    return (
        "title: D\nsource: {type: duckdb, database: ':memory:'}\n"
        'queries: {q: "SELECT 1 AS w, 2 AS revenue, 0.5 AS rate, 3 AS profit"}\n'
        "tiles:\n"
        "  - title: A\n    query: q\n    chart:\n"
        + a_chart
        + "  - title: B\n    query: q\n    chart:\n"
        + b_chart
    )


SHARED_SERIES = _two_tiles(
    "      type: bar\n      y: [revenue, rate]\n"
    "      series: &shared\n        rate: {type: line, axis: right}\n",
    "      type: bar\n      y: [revenue, rate]\n      series: *shared\n",
)


@pytest.mark.parametrize("edited", ["a", "b"])
def test_editing_a_shared_series_mapping_leaves_the_other_tile_alone(tmp_path, edited):
    (tmp_path / "d.yaml").write_text(SHARED_SERIES)
    chart = {
        "type": "bar",
        "y": ["revenue", "rate"],
        "series": {"rate": {"type": "area", "axis": "right"}},
    }
    text, charts = _save_tile(tmp_path, edited, chart)
    marks = {tile: c.series["rate"].type for tile, c in charts.items()}
    assert marks == {"a": "line", "b": "line", edited: "area"}, text
    assert "&" not in text.split("  - title: " + edited.upper())[1].split("  - title:")[0], text


def test_nested_aliases_are_expanded_before_an_edit(tmp_path):
    (tmp_path / "d.yaml").write_text(
        _two_tiles(
            "      type: bar\n      y: [revenue, rate]\n"
            "      series: &shared {revenue: &line {type: line}, rate: *line}\n",
            "      type: bar\n      y: [revenue, rate]\n      series: *shared\n",
        )
    )
    chart = {
        "type": "bar",
        "y": ["revenue", "rate"],
        "series": {"revenue": {"type": "line"}, "rate": {"type": "area"}},
    }
    text, charts = _save_tile(tmp_path, "b", chart)
    assert charts["b"].series["revenue"].type == "line", text
    assert charts["b"].series["rate"].type == "area", text
    assert charts["a"].series["rate"].type == "line", text
    assert "*" not in text.split("  - title: B")[1], text


MERGED_SERIES = _two_tiles(
    "      type: bar\n      y: [revenue, rate, profit]\n      series:\n"
    "        rate: &right {type: line, axis: right}\n"
    "        profit: {<<: *right, label: Profit}\n",
    "      type: bar\n      y: [revenue, rate]\n      series: {rate: *right}\n",
)


def test_overriding_a_merged_key_writes_the_override(tmp_path):
    (tmp_path / "d.yaml").write_text(MERGED_SERIES)
    chart = {
        "type": "bar",
        "y": ["revenue", "rate", "profit"],
        "series": {
            "rate": {"type": "line", "axis": "right"},
            "profit": {"type": "area", "axis": "right", "label": "Profit"},
        },
    }
    text, charts = _save_tile(tmp_path, "a", chart)
    assert charts["a"].series["profit"].type == "area", text
    assert charts["a"].series["rate"].type == "line", text
    assert charts["b"].series["rate"].type == "line", text
    assert "<<" not in text, text


def test_deleting_a_merged_key_takes_it_off_the_series(tmp_path):
    (tmp_path / "d.yaml").write_text(MERGED_SERIES)
    chart = {
        "type": "bar",
        "y": ["revenue", "rate", "profit"],
        "series": {"rate": {"type": "line", "axis": "right"}, "profit": {"type": "line"}},
    }
    text, charts = _save_tile(tmp_path, "a", chart)
    assert charts["a"].series["profit"].axis is None, text
    assert charts["a"].series["rate"].axis == "right", text
    assert charts["b"].series["rate"].axis == "right", text


@pytest.mark.parametrize("doc", [SHARED_SERIES, MERGED_SERIES], ids=["alias", "merge"])
def test_an_unchanged_save_of_a_shared_chart_is_byte_identical(tmp_path, doc):
    (tmp_path / "d.yaml").write_text(doc)
    dashboard, _, _ = DashboardStore(tmp_path).load("d")
    chart = dashboard.tiles[0].chart.model_dump(exclude_defaults=True)
    text, _ = _save_tile(tmp_path, "a", chart)
    assert text == doc, text


TILE_MERGE = (
    "title: D\nsource: {type: duckdb, database: ':memory:'}\n"
    'queries: {q: "SELECT 1 AS w, 2 AS revenue, 0.5 AS rate"}\n'
    "tiles:\n"
    "  - &base\n"
    "    title: A\n"
    "    query: q\n"
    "    chart:\n"
    "      type: bar\n"
    "      y: [revenue, rate]\n"
    "      series: {rate: {type: line, axis: right}}\n"
    "  - <<: *base\n"
    "    title: B\n"
)


def test_editing_a_tile_that_merges_another_leaves_the_other_alone(tmp_path):
    (tmp_path / "d.yaml").write_text(TILE_MERGE)
    chart = {
        "type": "bar",
        "y": ["revenue", "rate"],
        "series": {"rate": {"type": "area", "axis": "right"}},
    }
    text, charts = _save_tile(tmp_path, "b", chart)
    marks = {tile: c.series["rate"].type for tile, c in charts.items()}
    assert marks == {"a": "line", "b": "area"}, text
    assert text.startswith(TILE_MERGE.split("  - <<: *base")[0]), text
