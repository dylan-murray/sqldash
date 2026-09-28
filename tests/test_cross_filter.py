"""Explicit cross-filters: the `cross_filter:` mapping and what lint says about it."""

import pytest

from sqldash.execution import ExecutionRegistry
from sqldash.lint import lint_dashboard, validate_dashboard
from sqldash.project.store import DashboardStore, InvalidDashboardError, parse_dashboard
from sqldash.semantics import SemanticLayer

HEAD = (
    "title: Overview\n"
    "source: {type: duckdb, database: ':memory:'}\n"
    "filters:\n"
    "  - {name: dates, type: daterange, default: last_30_days}\n"
    "  - {name: region, type: select, options: [all, us, eu]}\n"
    "  - {name: minimum, type: number}\n"
)


def _dashboard(cross_filter: str, sql: str = "SELECT 'us' AS region, 10 AS revenue") -> str:
    return (
        HEAD + "tiles:\n"
        "  - title: By region\n"
        "    chart: pie\n"
        f'    sql: "{sql}"\n'
        f"    cross_filter: {cross_filter}\n"
    )


def _findings(text: str, level: str) -> list[str]:
    dashboard = parse_dashboard(text)
    return [f.message for f in lint_dashboard(dashboard, "d.yaml") if f.level == level]


def test_a_mapping_and_false_both_parse():
    assert parse_dashboard(_dashboard("{region: region}")).tiles[0].cross_filter == {
        "region": "region"
    }
    assert parse_dashboard(_dashboard("false")).tiles[0].cross_filter is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("{}", "maps no filters"),
        ("true", "cross_filter"),
        ("{region: ''}", "empty column"),
        ("[region]", "cross_filter"),
    ],
)
def test_a_malformed_cross_filter_is_refused(value, expected):
    with pytest.raises(InvalidDashboardError, match=expected):
        parse_dashboard(_dashboard(value))


def test_a_click_cannot_both_drill_and_cross_filter():
    text = _dashboard("{region: region}") + "    drill: detail\n"
    with pytest.raises(InvalidDashboardError, match="either drills or cross-filters"):
        parse_dashboard(text)


def test_a_text_tile_cannot_cross_filter():
    text = HEAD + "tiles:\n  - {markdown: hi, cross_filter: {region: region}}\n"
    with pytest.raises(InvalidDashboardError, match="needs a chart or table tile"):
        parse_dashboard(text)


def test_lint_passes_a_mapping_onto_a_scalar_filter():
    text = _dashboard("{region: region, minimum: revenue}")
    assert _findings(text, "error") == []
    assert not [w for w in _findings(text, "warning") if "cross_filter" in w]


def test_lint_names_an_unknown_filter_and_a_date_range():
    errors = _findings(_dashboard("{regoin: region, dates: region}"), "error")
    assert any("cross_filter 'regoin' is not a filter" in e and "region" in e for e in errors)
    assert any("cross_filter 'dates' is a date range" in e for e in errors)


def test_lint_warns_when_the_tile_filters_itself():
    sql = "SELECT region, 10 AS revenue FROM t WHERE region = {{ region }}"
    warnings = _findings(_dashboard("{region: region}", sql), "warning")
    assert any("also read by this tile's own query" in w for w in warnings), warnings


def test_validate_dashboard_names_a_cross_filter_column_the_query_does_not_return(tmp_path):
    store = DashboardStore(tmp_path)
    registry = ExecutionRegistry(max_workers=1)
    try:
        payload = validate_dashboard(
            _dashboard("{region: area}"),
            store=store,
            layer=SemanticLayer(store),
            registry=registry,
            name="overview",
        )
    finally:
        registry.shutdown()
    assert payload["valid"] is False
    assert any("cross_filter reads column 'area'" in e for e in payload["errors"]), payload


def test_a_tile_write_keeps_replaces_and_removes_its_cross_filter(tmp_path):
    (tmp_path / "overview.yaml").write_text(_dashboard("{region: region}"))
    store = DashboardStore(tmp_path)
    base = {"id": "extra", "title": "Extra", "chart": "bar", "query": "q"}

    def write(tile):
        store.upsert_tile("overview", tile, "SELECT 'us' AS region", store.load("overview")[2])
        return next(t for t in store.load("overview")[0].tiles if t.id == "extra").cross_filter

    assert write({**base, "cross_filter": {"region": "region"}}) == {"region": "region"}
    assert write({**base, "cross_filter": False}) is False
    assert write(base) is False
    assert write({**base, "cross_filter": None}) is None
    text = (tmp_path / "overview.yaml").read_text()
    assert "    cross_filter: {region: region}\n" in text


def test_reordering_a_cross_filter_is_saved_since_the_first_entry_is_the_toggle(tmp_path):
    (tmp_path / "overview.yaml").write_text(
        HEAD + "  - {name: channel, type: text}\n" + "tiles:\n"
        "  - title: By region\n"
        "    chart: table\n"
        "    sql: \"SELECT 'us' AS region, 'web' AS channel\"\n"
        "    cross_filter: {region: region, channel: channel}\n"
        "\n  # The next tile\n  - {title: Next, sql: 'SELECT 1 AS n'}\n"
    )
    store = DashboardStore(tmp_path)
    tile = {"id": "by_region", "title": "By region", "chart": "table", "query": "by_region"}
    for mapping in ({"channel": "channel", "region": "region"}, None):
        store.upsert_tile(
            "overview", {**tile, "cross_filter": mapping}, None, store.load("overview")[2]
        )
        saved = store.load("overview")[0].tiles[0].cross_filter
        assert (list(saved) if saved else saved) == (list(mapping) if mapping else None)
        assert "\n\n  # The next tile\n" in (tmp_path / "overview.yaml").read_text()


OLD_MAPPINGS = {
    "block": "    cross_filter:\n      region: region\n",
    "flow": "    cross_filter: {region: region}\n",
    "off": "    cross_filter: false\n",
}
NEW_MAPPINGS = {
    "flow": (
        {"minimum": "revenue", "region": "region"},
        "    cross_filter: {minimum: revenue, region: region}\n",
    ),
    "off": (False, "    cross_filter: false\n"),
    "removed": (None, ""),
}


def _mapping_file(mapping: str, where: str) -> str:
    sql = "    sql: \"SELECT 'us' AS region, 10 AS revenue\"\n"
    note = "\n    # This query explains itself\n"
    head = "  - title: By region\n    chart: table\n"
    body = head + (mapping + note + sql if where == "middle" else sql + mapping)
    return (
        HEAD + "tiles:\n" + body + "\n  # The next tile\n  - {title: Next, sql: 'SELECT 1 AS n'}\n"
    )


@pytest.mark.parametrize("where", ["middle", "last"])
@pytest.mark.parametrize("new", list(NEW_MAPPINGS))
@pytest.mark.parametrize("old", list(OLD_MAPPINGS))
def test_rewriting_a_cross_filter_changes_only_its_own_lines(tmp_path, old, new, where):
    (tmp_path / "overview.yaml").write_text(_mapping_file(OLD_MAPPINGS[old], where))
    value, lines = NEW_MAPPINGS[new]
    store = DashboardStore(tmp_path)
    tile = {"id": "by_region", "title": "By region", "chart": "table", "query": "by_region"}
    store.upsert_tile("overview", {**tile, "cross_filter": value}, None, store.load("overview")[2])
    assert (tmp_path / "overview.yaml").read_text() == _mapping_file(lines, where)


@pytest.mark.parametrize("mapping", ["{region: region}", "false"])
def test_turning_a_cross_filter_tile_into_text_drops_its_mapping(tmp_path, mapping):
    (tmp_path / "overview.yaml").write_text(_dashboard(mapping))
    store = DashboardStore(tmp_path)
    tile = {"id": "by_region", "type": "text", "title": "By region", "markdown": "Notes"}
    store.upsert_tile("overview", tile, None, store.load("overview")[2])
    saved = store.load("overview")[0].tiles[0]
    assert saved.type == "text"
    assert saved.cross_filter is None
