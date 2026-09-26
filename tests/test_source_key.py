"""One key for connections (#397): `source:` is a connection or a named map.

The first half of this file is the compatibility floor — the two-key shape every
file in the wild was written in has to keep loading and serving exactly as it did.
"""

import pytest

from sqldash.models.source import Source, is_named_source_map
from sqldash.project.store import DashboardStore, InvalidDashboardError, parse_dashboard
from sqldash.setup import sync_dashboard_sources

LEGACY = """
title: Legacy
source: {type: duckdb, database: ':memory:'}
sources:
  alt: {type: duckdb, database: alt.db}
queries: {q: 'SELECT 1 AS n'}
tiles:
  - {title: Default tile, query: q}
  - {title: Named tile, query: q, source: alt}
"""

MERGED = """
title: Merged
source:
  warehouse: {type: duckdb, database: ':memory:', default: true}
  alt: {type: duckdb, database: alt.db}
queries: {q: 'SELECT 1 AS n'}
tiles:
  - {title: Default tile, query: q}
  - {title: Named tile, query: q, source: alt}
"""


def test_the_two_key_shape_still_loads_unchanged():
    d = parse_dashboard(LEGACY)
    assert d.source.database == ":memory:"
    assert d.sources["alt"].database == "alt.db"
    assert d.default_source_name is None
    assert d.named_source(None).database == ":memory:"
    assert d.named_source("alt").database == "alt.db"


def test_the_merged_shape_carries_the_same_information():
    legacy, merged = parse_dashboard(LEGACY), parse_dashboard(MERGED)
    assert merged.source == legacy.source
    assert merged.sources == legacy.sources
    assert merged.default_source_name == "warehouse"
    assert [t.source for t in merged.tiles] == [t.source for t in legacy.tiles]


def test_a_lone_connection_needs_no_name():
    d = parse_dashboard("title: T\nsource: {type: duckdb}\ntiles: []\n")
    assert d.source.type == "duckdb"
    assert d.sources == {}
    assert d.default_source_name is None


def test_a_bare_url_is_still_a_connection():
    d = parse_dashboard("title: T\nsource: 'duckdb:///:memory:'\ntiles: []\n")
    assert d.source.url == "duckdb:///:memory:"
    assert d.default_source_name is None


def test_a_named_entry_is_a_mapping_so_a_typo_stays_a_typo():
    """`source: {typ: duckdb}` must not read as a connection named "typ"."""
    with pytest.raises(InvalidDashboardError, match="did you mean 'type'"):
        parse_dashboard("title: T\nsource: {typ: duckdb}\ntiles: []\n")


def test_a_named_connection_beside_connection_fields_says_so():
    with pytest.raises(InvalidDashboardError, match="name every connection or none"):
        parse_dashboard("title: T\nsource: {type: duckdb, app_db: {type: duckdb}}\ntiles: []\n")


def test_one_named_connection_is_the_default_without_a_mark():
    d = parse_dashboard("title: T\nsource:\n  only: {type: duckdb}\ntiles: []\n")
    assert d.default_source_name == "only"
    assert d.sources == {}


def test_a_connection_may_be_named_after_a_connection_field():
    """`warehouse:`, `database:` and `project:` are Source fields *and* the most
    natural names for a connection — the shapes are told apart by their values."""
    d = parse_dashboard(
        "title: T\nsource:\n"
        "  warehouse: {type: duckdb, default: true}\n"
        "  database: {type: duckdb, database: app.db}\n"
        "tiles: []\n"
    )
    assert d.default_source_name == "warehouse"
    assert d.sources["database"].database == "app.db"


def test_several_connections_and_no_mark_is_an_error():
    with pytest.raises(InvalidDashboardError, match="none is the default"):
        parse_dashboard("title: T\nsource:\n  a: {type: duckdb}\n  b: {type: duckdb}\ntiles: []\n")


def test_two_marks_is_an_error():
    with pytest.raises(InvalidDashboardError, match="mark exactly one"):
        parse_dashboard(
            "title: T\nsource:\n"
            "  a: {type: duckdb, default: true}\n"
            "  b: {type: duckdb, default: true}\n"
            "tiles: []\n"
        )


def test_the_mark_must_be_a_boolean():
    with pytest.raises(InvalidDashboardError, match="must be true or false"):
        parse_dashboard("title: T\nsource:\n  a: {type: duckdb, default: yes please}\ntiles: []\n")


def test_marking_a_lone_connection_says_where_the_mark_belongs():
    with pytest.raises(InvalidDashboardError, match="already the default"):
        parse_dashboard("title: T\nsource: {type: duckdb, default: true}\ntiles: []\n")


def test_the_merged_map_and_the_legacy_key_cannot_both_be_used():
    with pytest.raises(InvalidDashboardError, match="remove the separate 'sources:' block"):
        parse_dashboard(
            "title: T\nsource:\n"
            "  a: {type: duckdb, default: true}\n"
            "  b: {type: duckdb}\n"
            "sources:\n  c: {type: duckdb}\n"
            "tiles: []\n"
        )


def test_a_typo_in_a_single_connection_still_reads_as_a_typo():
    with pytest.raises(InvalidDashboardError, match="did you mean 'host'"):
        parse_dashboard("title: T\nsource: {type: duckdb, hostt: x}\ntiles: []\n")


def test_a_connection_without_type_or_url_still_says_so():
    with pytest.raises(InvalidDashboardError, match="needs a 'type'"):
        parse_dashboard("title: T\nsource: {profile: prod}\ntiles: []\n")


def test_the_default_may_be_named_by_a_tile():
    d = parse_dashboard(
        MERGED.replace("- {title: Default tile, query: q}", "- {query: q, source: warehouse}")
    )
    tile = d.tiles[0]
    assert d.named_source(tile.source).database == ":memory:"


def test_an_unknown_tile_source_names_every_connection():
    with pytest.raises(InvalidDashboardError, match="named sources: alt, warehouse"):
        parse_dashboard(MERGED.replace("source: alt}", "source: nope}"))


def test_default_source_name_is_not_a_file_key():
    with pytest.raises(InvalidDashboardError, match="not a dashboard key"):
        parse_dashboard("title: T\nsource: {type: duckdb}\ndefault_source_name: x\ntiles: []\n")


def test_is_named_source_map_reads_values_not_names():
    assert is_named_source_map({"warehouse": {"type": "duckdb"}})
    assert not is_named_source_map({"type": "snowflake", "warehouse": "WH"})
    assert not is_named_source_map({"profile": "prod"})
    assert not is_named_source_map({"typ": "duckdb"})
    assert not is_named_source_map({})
    assert not is_named_source_map("duckdb:///:memory:")


def _save_named_source(tmp_path, text, sname="metrics"):
    (tmp_path / "d.yaml").write_text(text)
    store = DashboardStore(tmp_path)
    store.upsert_tile(
        "d",
        {"id": "t", "title": "T", "query": "q", "source": sname},
        None,
        None,
        named_source=(sname, Source(type="duckdb", database="other.db")),
    )
    return (tmp_path / "d.yaml").read_text(), store


BODY = "queries: {q: 'SELECT 1 AS n'}\ntiles: [{title: T, query: q}]\n"


def test_saving_promotes_a_lone_connection_into_the_named_map(tmp_path):
    text, store = _save_named_source(tmp_path, "title: D\nsource: {type: duckdb}\n" + BODY)
    assert "sources:" not in text
    assert "main: {type: duckdb, default: true}" in text
    dashboard, _, _ = store.load("d")
    assert dashboard.default_source_name == "main"
    assert dashboard.named_source("metrics").database == "other.db"


def test_promotion_keeps_the_comments_on_the_connection(tmp_path):
    text, _ = _save_named_source(
        tmp_path,
        "title: D\n# which database this reads\nsource:\n  type: duckdb  # local\n" + BODY,
    )
    assert "# which database this reads" in text
    assert "# local" in text


def test_saving_grows_a_map_that_is_already_there(tmp_path):
    text, store = _save_named_source(
        tmp_path,
        "title: D\nsource:\n  warehouse: {type: duckdb, default: true}\n  app: {type: duckdb}\n"
        + BODY,
    )
    assert "sources:" not in text
    dashboard, _, _ = store.load("d")
    assert dashboard.default_source_name == "warehouse"
    assert sorted(dashboard.sources) == ["app", "metrics"]


def test_saving_keeps_a_legacy_file_on_its_own_shape(tmp_path):
    """Rewriting an author's `sources:` block is not what "save this tile" asked for."""
    text, store = _save_named_source(
        tmp_path,
        "title: D\nsource: {type: duckdb}\nsources:\n  alt: {type: duckdb}\n" + BODY,
    )
    assert "\nsources:" in text
    assert "\nsource: {type: duckdb}" in text
    dashboard, _, _ = store.load("d")
    assert dashboard.default_source_name is None
    assert sorted(dashboard.sources) == ["alt", "metrics"]


def test_setup_repoints_the_default_and_keeps_the_other_connections(tmp_path):
    """`sqldash setup` moves dashboards off the project source it wrote last time.
    On a named map that means the default entry, not the whole block."""
    (tmp_path / "d.yaml").write_text(
        "title: D\nsource:\n"
        "  warehouse: {type: duckdb, database: old.duckdb, default: true}\n"
        "  alt: {type: duckdb, database: alt.db}\n"
        "tiles: []\n"
    )
    updated, stale, unreadable = sync_dashboard_sources(
        tmp_path,
        {"type": "duckdb", "database": "old.duckdb"},
        {"type": "postgres", "host": "db.internal", "database": "app"},
    )
    assert updated == ["d.yaml"]
    assert stale == []
    assert unreadable == []
    dashboard, _, _ = DashboardStore(tmp_path).load("d")
    assert dashboard.source.type == "postgres"
    assert dashboard.default_source_name == "warehouse"
    assert dashboard.sources["alt"].database == "alt.db"


def test_a_named_entry_with_only_the_default_mark_names_itself(tmp_path):
    """`warehouse: {default: true}` declares a name and no connection. The mark is
    stripped before the entry reaches Source, so the leftover empty mapping used
    to fail as a bare `source: needs a type` that named no entry. #397 review."""
    text = (
        "title: D\n"
        "source:\n"
        "  warehouse: {default: true}\n"
        "tiles:\n"
        "  - {title: A, sql: 'SELECT 1 AS n', chart: table}\n"
    )
    with pytest.raises(InvalidDashboardError, match="source 'warehouse'"):
        parse_dashboard(text)


def test_query_owner_source_treats_the_default_named_outright_as_the_default():
    dashboard = parse_dashboard(
        "title: T\nsource:\n  warehouse: {type: duckdb, default: true}\n  alt: {type: duckdb}\n"
        "queries: {q: 'SELECT 1 AS n', r: 'SELECT 2 AS n'}\n"
        "tiles:\n"
        "  - {title: Omitted, query: q}\n"
        "  - {title: By name, query: q, source: warehouse}\n"
        "  - {title: Elsewhere, query: r, source: alt}\n"
        "  - {title: Also by name, query: r, source: warehouse}\n"
    )
    assert dashboard.query_owner_source("q") is None
    with pytest.raises(ValueError, match="different sources"):
        dashboard.query_owner_source("r")
