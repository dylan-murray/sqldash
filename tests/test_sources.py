"""Connection pickers list each connection once (pin 0c62195f, 2026-09-18).

A dashboard and metrics.yaml defining the same Snowflake connection used to show
up as two sources that looked like different connections.
"""

import pytest
from fastapi.testclient import TestClient

from sqldash.models.source import Source
from sqldash.project.sources import PickerSource, distinct_picker_sources, resolve_picker_source
from sqldash.server import create_app


def _write_twins(tmp_path, metrics_source):
    (tmp_path / "d.yaml").write_text(
        "title: D\nsource: {type: snowflake, account: acme, database: DEV}\ntiles: []\n"
    )
    (tmp_path / "metrics.yaml").write_text(f"source: {metrics_source}\nrelations: {{}}\n")


@pytest.mark.parametrize(
    ("metrics_source", "labels"),
    [
        pytest.param(
            "{type: snowflake, account: acme, database: DEV}",
            ["default · snowflake"],
            id="same-connection-listed-once",
        ),
        pytest.param(
            "{type: snowflake, account: acme, database: SALES}",
            ["default · snowflake", "metrics.yaml · snowflake"],
            id="different-database-kept",
        ),
    ],
)
def test_pickers_list_a_connection_once_but_every_key_still_resolves(
    tmp_path, metrics_source, labels
):
    _write_twins(tmp_path, metrics_source)
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as client:
        listed = client.get("/api/dashboards/d/available-sources").json()["sources"]
        assert [entry["label"] for entry in listed] == labels
        page = client.get("/d/d/query").text
        assert ("metrics.yaml · snowflake" in page) == (len(labels) == 2)
    source, _ = resolve_picker_source(app.state.store, app.state.layer, "d", "metrics.yaml")
    assert source.type == "snowflake"


def test_file_databases_in_different_directories_are_not_merged(tmp_path):
    here, there = tmp_path / "a", tmp_path / "b"
    entries = [
        PickerSource(
            "", "default · duckdb", "default", Source(type="duckdb", database="x.db"), here
        ),
        PickerSource("b", "b · duckdb", "project", Source(type="duckdb", database="x.db"), there),
        PickerSource("c", "c · duckdb", "project", Source(type="duckdb", database="x.db"), here),
    ]
    assert [entry.key for entry in distinct_picker_sources(entries)] == ["", "b"]


def test_a_dashboards_own_named_twin_is_kept(tmp_path):
    twin = Source(type="snowflake", account="acme", database="DEV")
    entries = [
        PickerSource("", "default · snowflake", "default", twin, tmp_path),
        PickerSource("alt", "alt · snowflake", "named", twin, tmp_path),
        PickerSource("metrics.yaml", "metrics.yaml · snowflake", "project", twin, tmp_path),
    ]
    assert [entry.key for entry in distinct_picker_sources(entries)] == ["", "alt"]


@pytest.mark.parametrize("url", ["not a url", "${env:SQLDASH_TEST_EMPTY_URL}"])
def test_an_unparseable_source_url_does_not_break_the_pickers(tmp_path, monkeypatch, url):
    """De-duplication must not be the first thing to parse a source url: the
    pickers rendered fine with a broken one before, and a bad url is a tile
    error, not a 500 on every page."""
    monkeypatch.setenv("SQLDASH_TEST_EMPTY_URL", "")
    (tmp_path / "d.yaml").write_text(f"title: D\nsource: {{url: '{url}'}}\ntiles: []\n")
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as client:
        assert client.get("/api/dashboards/d/available-sources").status_code == 200
        assert client.get("/d/d/query").status_code == 200
