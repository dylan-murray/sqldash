import os
import stat
import threading
import unicodedata
from pathlib import Path

import pytest

from sqldash.models.dashboard import dashboard_stem, slugify
from sqldash.project.store import (
    ConflictError,
    DashboardStore,
    InvalidDashboardError,
    NotFoundError,
    atomic_write,
    bound_file_stem,
    parse_dashboard,
)

VALID = """
title: Test
source: {type: duckdb, database: ':memory:'}
queries: {q1: 'SELECT 1 AS n'}
tiles:
  - {id: w1, query: q1}
"""


def test_parse_valid():
    dashboard = parse_dashboard(VALID)
    assert dashboard.title == "Test"
    assert dashboard.tiles[0].chart is None


def test_parse_rejects_unknown_query_reference():
    bad = VALID.replace("query: q1", "query: nope")
    with pytest.raises(InvalidDashboardError, match="unknown query 'nope'"):
        parse_dashboard(bad)


def test_parse_rejects_non_mapping():
    with pytest.raises(InvalidDashboardError):
        parse_dashboard("- just\n- a list\n")


def test_store_discover_and_load(tmp_path):
    (tmp_path / "one.yaml").write_text(VALID)
    store = DashboardStore(tmp_path)
    assert list(store.discover()) == ["one"]
    dashboard, _text, etag = store.load("one")
    assert dashboard.title == "Test"
    assert len(etag) == 16


def test_store_single_file_mode(tmp_path):
    path = tmp_path / "solo.yaml"
    path.write_text(VALID)
    store = DashboardStore(path)
    assert list(store.discover()) == ["solo"]
    with pytest.raises(NotFoundError):
        store.load("other")


def test_single_file_metrics_yaml_is_not_a_dashboard(tmp_path):
    path = tmp_path / "metrics.yaml"
    path.write_text("source: {type: duckdb}\nmetrics: {}\n")
    store = DashboardStore(path)
    assert store.discover() == {}


def test_save_etag_conflict(tmp_path):
    path = tmp_path / "one.yaml"
    path.write_text(VALID)
    store = DashboardStore(tmp_path)
    _, _, etag = store.load("one")
    path.write_text(VALID + "\ndescription: edited externally\n")
    with pytest.raises(ConflictError):
        store.save_text("one", VALID, if_match=etag)


def test_save_text_refuses_to_write_outside_the_store_root(tmp_path):
    """PUT ../name used to write into the parent of the store root and skip If-Match. #198."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "ok.yaml").write_text(VALID)
    target = tmp_path / "escape_target.yaml"
    target.write_text("IMPORTANT-PARENT-CONTENT")
    store = DashboardStore(proj)
    with pytest.raises(InvalidDashboardError, match="invalid dashboard name"):
        store.save_text("../escape_target", VALID, if_match="doesnotmatter")
    assert target.read_text() == "IMPORTANT-PARENT-CONTENT"


def test_save_text_refuses_reserved_names(tmp_path):
    (tmp_path / "metrics.yaml").write_text("source: {type: duckdb}\nmetrics: {}\n")
    store = DashboardStore(tmp_path)
    with pytest.raises(InvalidDashboardError, match="invalid dashboard name"):
        store.save_text("metrics", VALID, if_match=None)


def test_save_text_refuses_a_slash_in_the_name(tmp_path):
    """PUT docs/notes used to write an unservable nested file and skip If-Match. #334."""
    (tmp_path / "ok.yaml").write_text(VALID)
    store = DashboardStore(tmp_path)
    with pytest.raises(InvalidDashboardError, match="cannot contain '/'"):
        store.save_text("docs/notes", VALID, if_match="doesnotmatter")
    assert not (tmp_path / "docs").exists()
    assert not (tmp_path / "notes.yaml").exists()


def test_save_rejects_invalid_yaml(tmp_path):
    (tmp_path / "one.yaml").write_text(VALID)
    store = DashboardStore(tmp_path)
    with pytest.raises(InvalidDashboardError):
        store.save_text("one", "title: broken\n")


def test_atomic_write_replaces_without_leaving_tmp(tmp_path):
    path = tmp_path / "one.yaml"
    path.write_text("old")
    atomic_write(path, "new")
    assert path.read_text() == "new"
    assert [p.name for p in tmp_path.iterdir()] == ["one.yaml"]


def test_save_ignores_a_planted_tmp_symlink(tmp_path):
    """A served repo can commit `one.yaml.tmp -> ~/anything`. The save used to
    write through it, overwriting the target and leaving one.yaml a symlink."""
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("untouched\n")
    (project / "one.yaml").write_text(VALID)
    (project / "one.yaml.tmp").symlink_to(outside)
    changed = VALID.replace("title: Test", "title: Changed")
    DashboardStore(project).save_text("one", changed)
    assert outside.read_text() == "untouched\n"
    path = project / "one.yaml"
    assert not path.is_symlink()
    assert path.read_text() == changed
    assert (project / "one.yaml.tmp").is_symlink()


def test_atomic_write_preserves_the_existing_mode(tmp_path):
    path = tmp_path / "one.yaml"
    path.write_text("old")
    path.chmod(0o640)
    atomic_write(path, "new")
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_atomic_write_new_file_follows_the_umask(tmp_path):
    previous = os.umask(0o022)
    try:
        atomic_write(tmp_path / "new.yaml", "x")
    finally:
        os.umask(previous)
    assert stat.S_IMODE((tmp_path / "new.yaml").stat().st_mode) == 0o644


def test_concurrent_atomic_writes_never_share_a_temp_file(tmp_path, monkeypatch):
    path = tmp_path / "one.yaml"
    path.write_text("old")
    real_replace = os.replace
    staged = []
    barrier = threading.Barrier(8)

    def replace(src, dst):
        staged.append(Path(src).read_text())
        barrier.wait(timeout=5)
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    texts = [f"writer {i}\n" * 50 for i in range(8)]
    threads = [threading.Thread(target=atomic_write, args=(path, t)) for t in texts]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(staged) == sorted(texts)
    assert path.read_text() in texts
    assert [p.name for p in tmp_path.iterdir()] == ["one.yaml"]


def test_atomic_write_cleans_up_when_the_replace_fails(tmp_path, monkeypatch):
    path = tmp_path / "one.yaml"
    path.write_text("old")

    def fail(src, dst):
        raise OSError("disk went away")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="disk went away"):
        atomic_write(path, "new")
    assert path.read_text() == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["one.yaml"]


def test_discover_skips_yaml_that_is_not_a_dashboard(tmp_path):
    (tmp_path / "one.yaml").write_text(VALID)
    (tmp_path / "docker-compose.yaml").write_text(
        "services:\n  db:\n    environment:\n      title: not-a-key\nvolumes:\n  data: {}\n"
    )
    (tmp_path / "Taskfile.yaml").write_text("version: '3'\ntasks:\n  test: {cmds: [echo]}\n")
    (tmp_path / ".pre-commit-config.yaml").write_text("repos: []\n")
    (tmp_path / "params.yaml").write_text("experiments: {}\n")
    (tmp_path / "prompt_config.yaml").write_text("prompts: {}\n")
    (tmp_path / "_quarto.yml").write_text("title: My Analysis\nformat: html\n")
    (tmp_path / "_book.yml").write_text("title: My Book\nchapters: []\n")
    (tmp_path / "compose-multi.yaml").write_text(
        "version: '3'\nservices:\n  db: {}\n---\nnetworks: {}\n"
    )
    store = DashboardStore(tmp_path)
    assert list(store.discover()) == ["one"]


def test_discover_still_lists_a_broken_dashboard(tmp_path):
    (tmp_path / "one.yaml").write_text(VALID)
    (tmp_path / "title_only.yaml").write_text("title: Broken\n")
    (tmp_path / "legacy.yaml").write_text("title: Legacy\nsource: {typ: duckdb}\ntiles: []\n")
    (tmp_path / "half.yaml").write_text("title: [unterminated\n")
    (tmp_path / "layout.yaml").write_text("tiles: []\n")
    (tmp_path / "quoted.yaml").write_text(
        '"title": Quoted\n"source": {type: duckdb, database: ":memory:"}\n"tiles": []\n'
    )
    (tmp_path / "binary.yaml").write_bytes(b"t\xfftle: x\nsource: {}\n")
    store = DashboardStore(tmp_path)
    assert set(store.discover()) == {
        "one",
        "title_only",
        "legacy",
        "half",
        "layout",
        "quoted",
        "binary",
    }


def test_sqldash_dir_convention(tmp_path):
    nested = tmp_path / ".sqldash"
    nested.mkdir()
    (nested / "one.yaml").write_text(VALID)
    store = DashboardStore(tmp_path)
    assert store.root == nested
    assert list(store.discover()) == ["one"]


def test_source_accepts_bare_url_string():
    d = parse_dashboard("title: T\nsource: sqlite:///x.db\ntiles: [{title: W, sql: 'SELECT 1'}]\n")
    assert d.source.type is None
    assert d.source.url == "sqlite:///x.db"
    from sqldash.models.source import source_label

    assert source_label(d.source) == "sqlite"


def test_source_label_does_not_return_a_url_password():
    from sqldash.models.source import Source, source_label

    url = "postgresql://u:hunter2@db.example.com:5432/app"
    assert source_label(Source.model_validate({"url": url})) == "postgresql"
    assert "hunter2" not in source_label(Source.model_validate({"url": "u:hunter2@db.example.com"}))


def test_source_accepts_url_mapping_without_type():
    d = parse_dashboard(
        "title: T\nsource: {url: 'sqlite:///x.db'}\n"
        "sources:\n  other: 'duckdb:///:memory:'\n"
        "tiles: [{title: W, sql: 'SELECT 1'}]\n"
    )
    assert d.source.type is None
    assert d.sources["other"].url == "duckdb:///:memory:"


def test_widgets_key_is_not_a_dashboard(tmp_path):
    """Pre-release leftover: `widgets:` is not an alias. A file whose only body
    key is that word is foreign YAML, not a broken dashboard."""
    (tmp_path / "legacy.yaml").write_text("widgets: []\n")
    store = DashboardStore(tmp_path)
    assert list(store.discover()) == []


def test_widgets_key_is_rejected(tmp_path):
    (tmp_path / "t.yaml").write_text(
        "title: T\nsource: {type: duckdb, database: ':memory:'}\n"
        "widgets: [{title: A, sql: SELECT 1}]\n"
    )
    store = DashboardStore(tmp_path)
    with pytest.raises(InvalidDashboardError, match="widgets"):
        store.load("t")


def test_surgical_write_uses_tiles_key(tmp_path):
    (tmp_path / "t.yaml").write_text(
        "title: T\nsource: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n  - {title: A, sql: SELECT 1}\n"
    )
    store = DashboardStore(tmp_path)
    store.upsert_tile(
        "t",
        {"id": "b", "query": "qb", "title": "B", "chart": "table"},
        sql="SELECT 2",
        if_match=None,
    )
    text = (tmp_path / "t.yaml").read_text()
    assert "tiles:" in text
    assert "widgets:" not in text
    dashboard, _, _ = store.load("t")
    assert [w.id for w in dashboard.tiles] == ["a", "b"]


def test_upsert_keeps_two_tiles_that_share_a_title(tmp_path):
    """Add used to slugify the title and replace the existing tile. #329."""
    (tmp_path / "t.yaml").write_text(
        "title: T\nsource: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n  - {title: Orders, sql: SELECT 1 AS n}\n"
    )
    store = DashboardStore(tmp_path)
    store.upsert_tile(
        "t",
        {"id": "orders_2", "title": "Orders", "query": "orders_2", "chart": "table"},
        sql="SELECT 2 AS n",
        if_match=None,
    )
    dashboard, _, _ = store.load("t")
    assert [w.title for w in dashboard.tiles] == ["Orders", "Orders"]
    assert [w.id for w in dashboard.tiles] == ["orders", "orders_2"]


def test_tiles_error_locations_use_authored_key(tmp_path):
    (tmp_path / "t.yaml").write_text(
        "title: T\nsource: {type: duckdb, database: ':memory:'}\n"
        "tiles: [{title: A, sql: SELECT 1, chart: nope}]\n"
    )
    with pytest.raises(InvalidDashboardError, match=r"tiles\.0"):
        DashboardStore(tmp_path).load("t")


def test_new_file_first_tile_gets_canonical_key(tmp_path):
    (tmp_path / "bare.yaml").write_text(
        "title: Bare\nsource: {type: duckdb, database: ':memory:'}\n"
    )
    store = DashboardStore(tmp_path)
    store.upsert_tile(
        "bare", {"id": "a", "query": "qa", "title": "A"}, sql="SELECT 1", if_match=None
    )
    text = (tmp_path / "bare.yaml").read_text()
    assert "tiles:" in text
    assert "widgets:" not in text


def test_validation_errors_read_cleanly(tmp_path):
    """Model-level validators produced a leading ": " with an empty location and
    carried pydantic's "Value error," prefix into user-facing messages."""
    (tmp_path / "bad.yaml").write_text(
        "title: Bad\n"
        'source: {type: duckdb, database: ":memory:"}\n'
        'tiles: [{title: T, chart: big_number, source: nope, sql: "SELECT 1 AS n"}]\n'
    )
    with pytest.raises(InvalidDashboardError) as excinfo:
        DashboardStore(tmp_path).load("bad")
    message = str(excinfo.value)
    assert "Value error" not in message, message
    assert not message.startswith(": "), message
    assert "unknown source 'nope'" in message


def test_shared_yaml_parser_is_safe_under_threads(tmp_path):
    from sqldash.project.store import build_dashboard_text

    (tmp_path / "one.yaml").write_text(VALID)
    store = DashboardStore(tmp_path)
    errors = []

    def hammer():
        try:
            for _ in range(80):
                parse_dashboard(VALID)
                build_dashboard_text("T", {"type": "duckdb", "database": ":memory:"})
                assert "one" in store.discover()
                store.load("one")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors, errors[:5]


def test_bound_file_stem_counts_bytes():
    """NAME_MAX is bytes. A stem trimmed to 246 *characters* of a multi-byte
    script is still three times too long for the directory entry (#391)."""
    assert bound_file_stem("ops_overview") == "ops_overview"
    assert len(bound_file_stem("x" * 300).encode()) == 246
    assert bound_file_stem("a" * 244 + "___" + "b" * 60) == "a" * 244
    trimmed = bound_file_stem("収" * 300)
    assert len(trimmed.encode()) <= 246
    assert trimmed == "収" * 82


def test_save_text_refuses_a_too_long_name_before_touching_the_disk(tmp_path, monkeypatch):
    """`Path.exists()` swallows only ENOENT/ENOTDIR/EBADF/ELOOP, so on Linux a
    too-long name raised out of the existence check before the guarded write was
    reached; macOS answered False and reached it, so CI failed where the laptop
    passed. The length check runs first, so no filesystem call happens at all."""
    store = DashboardStore(tmp_path)

    def boom(*_args, **_kwargs):
        raise AssertionError("touched the filesystem with an unwritable name")

    monkeypatch.setattr(DashboardStore, "discover", boom)
    with pytest.raises(InvalidDashboardError, match="too long for this filesystem"):
        store.save_text("y" * 300, VALID)


@pytest.mark.parametrize(
    "title",
    ["Quarterly sales", "Ops Overview", "Revenue (USD) - 2024!", "a__b", "  spaced  ", "Q3/Q4"],
)
def test_dashboard_stem_matches_slugify_for_ascii_titles(title):
    """Every name an ASCII title produced before #579 is produced unchanged."""
    assert dashboard_stem(title) == slugify(title)


def test_slugify_stays_ascii_so_implicit_tile_ids_do_not_move():
    """A tile whose id equals slugify(title) never has the id written, so the
    id is re-derived on every load. Widening slugify the way #579 widened
    dashboard names would rename every such tile with an accented title and
    break anything that pointed at it. Only new dashboard names changed."""
    assert slugify("Übersicht") == "bersicht"
    assert slugify("売上") == ""
    assert dashboard_stem("Übersicht") == "übersicht"


@pytest.mark.parametrize(
    ("title", "stem"),
    [
        ("\u0301", ""),
        ("\u0301\u0302", ""),
        ("——\u0301——", ""),
        ("—\u0301abc", "abc"),
        ("a\u0301", "\u00e1"),
        ("हिन्दी रिपोर्ट", "हिन्दी_रिपोर्ट"),
    ],
)
def test_dashboard_stem_keeps_a_mark_only_on_a_letter_or_digit(title, stem):
    """A combining mark with nothing to attach to would give a name that renders
    as nothing, or starts with a loose mark; it is dropped like punctuation."""
    assert dashboard_stem(title) == stem


@pytest.mark.parametrize("title", ["J\u030c", "H\u0331", "\u01f0", "I\u0307stanbul", "Ǆ"])
def test_dashboard_stem_is_nfc_even_when_lowering_decomposes(title):
    """`J̌` has no precomposed uppercase form, so NFC leaves it as J + caron, and
    lowering gives j + caron: a decomposed stem for a name whose precomposed
    twin `ǰ` looks identical. The stem is normalized after lowering."""
    stem = dashboard_stem(title)
    assert unicodedata.is_normalized("NFC", stem)
    assert stem == dashboard_stem(unicodedata.normalize("NFC", title.lower()))


def test_an_accented_tile_keeps_its_derived_id_on_load(tmp_path):
    (tmp_path / "d.yaml").write_text(
        "title: D\nsource: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n  - {title: Übersicht, sql: SELECT 1}\n",
        encoding="utf-8",
    )
    dashboard, _text, _etag = DashboardStore(tmp_path).load("d")
    assert [tile.id for tile in dashboard.tiles] == ["bersicht"]


def test_an_impossible_date_literal_is_reported_with_its_location():
    """A value the loader refuses to *construct* carries no ruamel mark, so an
    impossible date was reported as `invalid YAML: day 30 must be in range ...`
    with no file position and nothing to search for but the message. #672."""
    text = (
        "title: T\nsource: {type: duckdb}\n"
        "filters:\n  - {name: dates, type: daterange, default: 2026-02-30}\n"
        "tiles:\n  - {title: t, sql: SELECT 1}\n"
    )
    with pytest.raises(InvalidDashboardError) as exc:
        parse_dashboard(text)
    message = str(exc.value)
    assert "invalid YAML" in message
    assert "line 4" in message
    assert "2026-02-30" in message


def test_a_syntax_error_keeps_ruamels_own_location():
    """The marked case must not gain a second, hand-rolled position."""
    with pytest.raises(InvalidDashboardError) as exc:
        parse_dashboard("title: T\ntiles:\n  - {title: t\n")
    message = str(exc.value)
    assert "invalid YAML" in message
    assert 'in "<file>", line 3' in message
    assert "in line " not in message, message
