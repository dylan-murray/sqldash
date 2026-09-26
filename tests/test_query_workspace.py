import re

import duckdb
from fastapi.testclient import TestClient

from sqldash.scaffold import create_demo
from sqldash.server import create_app


def test_workspace_identity_separates_equal_dashboard_names(tmp_path):
    identities = []
    for name in ("first", "second"):
        target = tmp_path / name
        create_demo(target)
        with TestClient(create_app(target, allowed_hosts=["testserver"])) as client:
            page = client.get("/d/demo/workspace")
            assert page.status_code == 200
            identities.append(re.search(r'data-identity="([a-f0-9]+)"', page.text)[1])
            assert str(target) not in page.text
    assert identities[0] != identities[1]


def test_schema_insertion_quotes_only_the_names_that_need_it(tmp_path):
    database = tmp_path / "q.duckdb"
    with duckdb.connect(str(database)) as con:
        con.execute(
            'CREATE TABLE "my table" (order_date DATE, "my col" INT, "Mixed" INT, "select" INT)'
        )
        con.execute("INSERT INTO \"my table\" VALUES ('2024-01-02', 7, 8, 9)")
    (tmp_path / "d.yaml").write_text(
        "title: Q\nsource: {type: duckdb, database: q.duckdb}\ntiles: []\n"
    )
    with TestClient(create_app(tmp_path, allowed_hosts=["testserver"])) as client:
        response = client.get("/api/dashboards/d/schema")
    assert response.status_code == 200
    (table,) = response.json()["tables"]
    assert table["sql"] == 'main."my table"'
    assert table["name_sql"] == '"my table"'
    columns = [c["sql"] for c in table["columns"]]
    assert columns == ["order_date", '"my col"', '"Mixed"', '"select"']
    with duckdb.connect(str(database)) as con:
        for relation in (table["sql"], table["name_sql"]):
            row = con.execute(f"SELECT {', '.join(columns)} FROM {relation}").fetchone()
            assert row[1:] == (7, 8, 9)


def test_copying_inline_query_owner_preserves_other_consumers(tmp_path):
    path = tmp_path / "d.yaml"
    path.write_text(
        "title: Shared\nsource: {type: duckdb}\ntiles:\n"
        "  - id: original\n    sql: SELECT 1 AS value\n"
        "  - id: other\n    query: original\n"
    )
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as client:
        before = client.get("/api/dashboards/d").json()
        tile = before["dashboard"]["tiles"][0]
        tile["query"] = "independent_copy"
        response = client.put(
            "/api/dashboards/d/tiles/original",
            json={"tile": tile, "sql": "SELECT 2 AS value"},
            headers={"If-Match": before["etag"], "X-Sqldash-Token": app.state.api_token},
        )
        assert response.status_code == 200, response.text
        after = client.get("/api/dashboards/d").json()["dashboard"]
        assert after["queries"]["original"].strip() == "SELECT 1 AS value"
        assert after["queries"]["independent_copy"].strip() == "SELECT 2 AS value"
        assert after["tiles"][1]["query"] == "original"
