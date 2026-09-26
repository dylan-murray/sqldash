import shutil

import pytest
from fastapi.testclient import TestClient

from sqldash.project.query_library import QueryLibrary
from sqldash.project.store import InvalidDashboardError
from sqldash.server import create_app


@pytest.fixture
def project(tmp_path):
    for name in ("a", "b"):
        (tmp_path / f"{name}.yaml").write_text(
            f"title: {name}\nsource: {{type: duckdb}}\ntiles: []\n"
        )
    return tmp_path


def client_for(path):
    app = create_app(path, allowed_hosts=["testserver"])
    client = TestClient(app)
    client.headers["X-Sqldash-Token"] = app.state.api_token
    return client


def test_library_restart_copies_and_delete_preserve_portable_dashboards(project, tmp_path):
    with client_for(project) as client:
        response = client.post(
            "/api/dashboards/a/library",
            json={
                "title": "Revenue",
                "sql": "SELECT 1 AS revenue",
            },
        )
        assert response.status_code == 201, response.text
        saved = response.json()
        assert (project / "queries" / f"{saved['id']}.yaml").exists()
        assert client.get("/api/dashboards/a").json()["dashboard"]["tiles"] == []
    with client_for(project) as client:
        for name in ("a", "b"):
            opened = client.get(f"/api/dashboards/{name}/library/{saved['id']}/open")
            assert opened.status_code == 200, opened.text
            state = opened.json()["state"]
            before = client.get(f"/api/dashboards/{name}").json()
            response = client.post(
                f"/api/dashboards/{name}/tiles",
                json={
                    "tile": {
                        "id": "revenue",
                        "type": "chart",
                        "query": "revenue",
                        "source": state["source"] or None,
                        "chart": {"type": "table"},
                    },
                    "sql": state["sql"],
                },
                headers={"If-Match": before["etag"]},
            )
            assert response.status_code == 200, response.text
        updated = client.put(
            f"/api/dashboards/a/library/{saved['id']}",
            json={
                "title": "Renamed",
                "sql": "SELECT 2 AS revenue",
            },
            headers={"If-Match": saved["etag"]},
        )
        assert updated.status_code == 200
        assert updated.json()["id"] == saved["id"]
        for name in ("a", "b"):
            dashboard = client.get(f"/api/dashboards/{name}").json()["dashboard"]
            assert dashboard["queries"]["revenue"].strip() == "SELECT 1 AS revenue"
        deleted = client.delete(
            f"/api/dashboards/a/library/{saved['id']}", headers={"If-Match": updated.json()["etag"]}
        )
        assert deleted.status_code == 200
        assert not client.get("/api/dashboards/b/library").json()["queries"]
    copied = tmp_path / "portable"
    copied.mkdir()
    shutil.copy(project / "b.yaml", copied / "b.yaml")
    with client_for(copied) as client:
        assert client.get("/api/dashboards/b").status_code == 200
        assert client.get("/api/dashboards/b").json()["dashboard"]["queries"][
            "revenue"
        ].strip() == ("SELECT 1 AS revenue")


def test_library_refuses_conflicts_and_missing_token(project):
    with client_for(project) as client:
        saved = client.post(
            "/api/dashboards/a/library",
            json={
                "title": "Query",
                "sql": "SELECT 1",
            },
        ).json()
        route = f"/api/dashboards/a/library/{saved['id']}"
        assert (
            client.put(
                route, json={"title": "Other", "sql": "SELECT 2"}, headers={"If-Match": "stale"}
            ).status_code
            == 409
        )
        assert client.delete(route, headers={"If-Match": "stale"}).status_code == 409
        client.headers.pop("X-Sqldash-Token")
        assert client.delete(route, headers={"If-Match": saved["etag"]}).status_code == 403


def test_library_if_match_accepts_the_quoted_etag(project):
    """The library checks If-Match itself, so it needs the same rule the
    dashboard routes got: quoted, weak and `*` all write, a stale one still
    409s, and the conflict names both sides. #651."""
    with client_for(project) as client:
        saved = client.post(
            "/api/dashboards/a/library", json={"title": "Query", "sql": "SELECT 1"}
        ).json()
        route = f"/api/dashboards/a/library/{saved['id']}"

        def etag():
            return client.get("/api/dashboards/a/library").json()["queries"][0]["etag"]

        res = client.put(
            route,
            json={"title": "Quoted", "sql": "SELECT 2"},
            headers={"If-Match": f'"{etag()}"'},
        )
        assert res.status_code == 200, res.text
        res = client.patch(route, json={"title": "Weak"}, headers={"If-Match": f'W/"{etag()}"'})
        assert res.status_code == 200, res.text
        current = etag()
        stale = client.put(
            route, json={"title": "Stale", "sql": "SELECT 3"}, headers={"If-Match": '"nope"'}
        )
        assert stale.status_code == 409, stale.text
        assert f"on disk {current!r}" in stale.json()["detail"]
        assert "If-Match '\"nope\"'" in stale.json()["detail"]
        assert client.delete(route, headers={"If-Match": "*"}).status_code == 200
        assert not (project / "queries" / f"{saved['id']}.yaml").exists()


def test_library_parameter_compatibility_and_missing_source(project):
    with (project / "a.yaml").open("a") as f:
        f.write("filters:\n  - {name: region, type: text, default: us}\n")
    with client_for(project) as client:
        saved = client.post(
            "/api/dashboards/a/library",
            json={
                "title": "Filtered",
                "sql": "SELECT {{ region }} AS region",
            },
        )
        assert saved.status_code == 201, saved.text
        query_id = saved.json()["id"]
        assert client.get(f"/api/dashboards/b/library/{query_id}/open").status_code == 422
        assert client.get(f"/api/dashboards/a/library/{query_id}/open").status_code == 200
        assert (
            client.post(
                "/api/dashboards/a/library",
                json={
                    "title": "Missing",
                    "sql": "SELECT 1",
                    "source": "not-present",
                },
            ).status_code
            == 404
        )


def test_library_rejects_path_escape_and_lists_malformed_files(project, tmp_path):
    library = QueryLibrary(project)
    with pytest.raises(InvalidDashboardError):
        library.path("../escape")
    queries = project / "queries"
    queries.mkdir()
    (queries / "bad.yaml").write_text("invalid: [")
    [error] = library.list()[1]
    assert error["id"] == "bad"
    assert error["message"] == "Could not read query file"
    assert error["reason"] == "invalid library query file"
    (queries / "escape.yaml").symlink_to(tmp_path.parent / "outside.yaml")
    with pytest.raises(InvalidDashboardError):
        library.load("escape")


HEALTHY = "version: 1\nid: healthy\ntitle: Healthy\nsql: SELECT 1\nsource: a.source\n"


def _with_undecodable_entry(project):
    queries = project / "queries"
    queries.mkdir()
    (queries / "healthy.yaml").write_text(HEALTHY)
    (queries / "broken.yaml").write_bytes(b"\xff\xfe\x00binary")
    return queries


def test_undecodable_entry_is_listed_without_hiding_healthy_ones(project):
    """One non-UTF-8 file 500'd the whole library listing. #594."""
    _with_undecodable_entry(project)
    with client_for(project) as client:
        listing = client.get("/api/dashboards/a/library")
        assert listing.status_code == 200, listing.text
        assert [q["id"] for q in listing.json()["queries"]] == ["healthy"]
        [error] = listing.json()["errors"]
        assert error["id"] == "broken"
        assert error["reason"] == "query file is not valid UTF-8"
        assert error["etag"]
        opened = client.get("/api/dashboards/a/library/broken/open")
        assert opened.status_code == 422
        assert opened.json()["detail"] == "query file is not valid UTF-8"


def test_undecodable_entry_can_be_deleted_with_its_listed_etag(project):
    queries = _with_undecodable_entry(project)
    with client_for(project) as client:
        [error] = client.get("/api/dashboards/a/library").json()["errors"]
        stale = client.delete("/api/dashboards/a/library/broken", headers={"If-Match": "nope"})
        assert stale.status_code == 409
        res = client.delete("/api/dashboards/a/library/broken", headers={"If-Match": error["etag"]})
        assert res.status_code == 200, res.text
        assert not (queries / "broken.yaml").exists()
        assert (queries / "healthy.yaml").read_text() == HEALTHY


def test_undecodable_entry_can_be_overwritten_with_its_listed_etag(project):
    queries = _with_undecodable_entry(project)
    with client_for(project) as client:
        [error] = client.get("/api/dashboards/a/library").json()["errors"]
        res = client.put(
            "/api/dashboards/a/library/broken",
            headers={"If-Match": error["etag"]},
            json={"title": "Fixed", "sql": "SELECT 2"},
        )
        assert res.status_code == 200, res.text
        listing = client.get("/api/dashboards/a/library").json()
        assert listing["errors"] == []
        assert {q["id"]: q["title"] for q in listing["queries"]} == {
            "broken": "Fixed",
            "healthy": "Healthy",
        }
        assert "SELECT 2" in (queries / "broken.yaml").read_text()


TERSE_FILTERS = """filters:
  - {name: dates, type: daterange, label: Date range, default: last_60_days}
  - name: region
    type: select
    label: Region
    options_sql: "SELECT 'us' AS region"
  - {name: search}
"""


def test_library_writes_parameters_as_authored_and_loads_them_back_equal(project):
    with (project / "a.yaml").open("a") as f:
        f.write(TERSE_FILTERS)
    with client_for(project) as client:
        plain = client.post("/api/dashboards/a/library", json={"title": "Plain", "sql": "SELECT 1"})
        filtered = client.post(
            "/api/dashboards/a/library",
            json={
                "title": "Filtered",
                "sql": "SELECT {{ dates_start }}, {{ dates_end }}, {{ region }}, {{ search }}",
            },
        )
        assert plain.status_code == 201, plain.text
        assert filtered.status_code == 201, filtered.text
        queries = project / "queries"
        plain_id = plain.json()["id"]
        assert (queries / f"{plain_id}.yaml").read_text() == (
            f"version: 1\nid: {plain_id}\ntitle: Plain\nsql: |\n  SELECT 1\nsource: a.source\n"
        )
        filtered_id = filtered.json()["id"]
        assert (queries / f"{filtered_id}.yaml").read_text() == (
            f"version: 1\nid: {filtered_id}\ntitle: Filtered\nsql: |\n"
            "  SELECT {{ dates_start }}, {{ dates_end }}, {{ region }}, {{ search }}\n"
            "source: a.source\n"
            "parameters:\n"
            "  - name: dates\n    type: daterange\n    label: Date range\n"
            "    default: last_60_days\n"
            "  - name: region\n    type: select\n    label: Region\n"
            "    options_sql: SELECT 'us' AS region\n"
            "  - name: search\n"
        )
        loaded, _ = QueryLibrary(project).load(filtered_id)
        response = filtered.json()
        response.pop("etag")
        assert loaded.model_dump(mode="json") == {**response, "sql": response["sql"] + "\n"}
        assert client.get(f"/api/dashboards/a/library/{filtered_id}/open").status_code == 200

        updated = client.put(
            f"/api/dashboards/a/library/{filtered_id}",
            json={"title": "Now plain", "sql": "SELECT 2"},
            headers={"If-Match": filtered.json()["etag"]},
        )
        assert updated.status_code == 200, updated.text
        assert "parameters" not in (queries / f"{filtered_id}.yaml").read_text()
        assert QueryLibrary(project).load(filtered_id)[0].parameters == []


def test_library_rewrites_a_verbose_file_tersely_on_rename(project):
    queries = project / "queries"
    queries.mkdir()
    (queries / "legacy.yaml").write_text(
        "version: 1\nid: legacy\ntitle: Legacy\nsql: |\n  SELECT {{ region }}\n"
        "source: a.source\nparameters:\n"
        "  - name: region\n    type: select\n    label: Region\n    default: all\n"
        "    options:\n    options_sql: SELECT 'us'\n    bind:\n"
    )
    library = QueryLibrary(project)
    before, etag = library.load("legacy")
    library.save(before.model_copy(update={"title": "Renamed"}), etag)
    assert (queries / "legacy.yaml").read_text() == (
        "version: 1\nid: legacy\ntitle: Renamed\nsql: |\n  SELECT {{ region }}\n"
        "source: a.source\nparameters:\n"
        "  - name: region\n    type: select\n    label: Region\n    options_sql: SELECT 'us'\n"
    )
    after, _ = library.load("legacy")
    assert after == before.model_copy(update={"title": "Renamed"})


def test_an_oversized_entry_is_listed_without_reading_it_whole(project, monkeypatch):
    """load() refuses a file over the size cap before reading it, but listing then
    hashed it with read_text for its etag, pulling the whole file into memory."""
    from pathlib import Path

    library = QueryLibrary(project)
    big = library.path("big")
    big.parent.mkdir(parents=True, exist_ok=True)
    big.write_bytes(b"x" * 1_000_059)
    whole_reads = []
    for method in ("read_text", "read_bytes"):
        original = getattr(Path, method)

        def guarded(self, *args, _original=original, _method=method, **kwargs):
            if self.name == "big.yaml":
                whole_reads.append(_method)
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(Path, method, guarded)
    _, errors = library.list()
    entry = next(e for e in errors if e["id"] == "big")
    assert entry["reason"] == "query file is too large"
    assert entry["etag"] == library.etag("big")
    assert whole_reads == [], whole_reads


def test_saving_over_an_oversized_entry_replaces_it_without_reading_it(project, monkeypatch):
    """Saving round-trips the old file to keep its comments. For an entry over the
    size cap that read it whole, and when the bulk was comments the save kept them:
    a 200 while the file stayed oversized and still would not open."""
    from pathlib import Path

    queries = project / "queries"
    queries.mkdir()
    big = queries / "big.yaml"
    big.write_text("# " + "x" * 1_200_000 + "\n" + HEALTHY.replace("healthy", "big"))
    whole_reads = []
    original = Path.read_text

    def guarded(self, *args, **kwargs):
        if self.name == "big.yaml":
            whole_reads.append(self.name)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded)
    with client_for(project) as client:
        [error] = client.get("/api/dashboards/a/library").json()["errors"]
        saved = client.put(
            "/api/dashboards/a/library/big",
            json={"title": "Big", "sql": "SELECT 2 AS n", "source": "a.source"},
            headers={"If-Match": error["etag"]},
        )
        assert saved.status_code == 200, saved.text
        assert whole_reads == [], whole_reads
        assert big.stat().st_size < 1_000
        listing = client.get("/api/dashboards/a/library").json()
        assert [q["id"] for q in listing["queries"]] == ["big"]
        assert listing["errors"] == []


CUSTOM_BIND_FILTERS = """filters:
  - {name: dates, type: daterange, default: last_60_days, bind: {start: from_d, end: to_d}}
  - {name: region, type: text, default: us, bind: {value: reg}}
"""


def test_library_saves_the_binds_the_dashboard_authored_not_the_derived_suffixes(project):
    with (project / "a.yaml").open("a") as f:
        f.write(CUSTOM_BIND_FILTERS)
    with client_for(project) as client:
        saved = client.post(
            "/api/dashboards/a/library",
            json={
                "title": "Windowed",
                "sql": "SELECT 1 WHERE d BETWEEN {{ from_d }} AND {{ to_d }} AND r = {{ region }}",
            },
        )
        assert saved.status_code == 201, saved.text
        query_id = saved.json()["id"]
        assert [p["name"] for p in saved.json()["parameters"]] == ["dates", "region"]
        assert saved.json()["parameters"][0]["bind"] == {"start": "from_d", "end": "to_d"}
        assert (
            "    bind:\n      start: from_d\n      end: to_d\n"
            in (project / "queries" / f"{query_id}.yaml").read_text()
        )
        assert client.get(f"/api/dashboards/a/library/{query_id}/open").status_code == 200

        for sql in (
            "SELECT {{ nope }}",
            "SELECT 1 WHERE d BETWEEN {{ dates_start }} AND {{ dates_end }}",
            "SELECT {{ reg }}",
        ):
            rejected = client.post("/api/dashboards/a/library", json={"title": "No", "sql": sql})
            assert rejected.status_code == 422, sql
            assert "Define the query's parameters" in rejected.json()["detail"]


def test_saving_a_new_library_query_returns_201(project):
    """Every other creating POST in the API answers 201; the library answered
    200, so a client keying off the status could not tell a create from an
    update. #663."""
    with client_for(project) as client:
        created = client.post(
            "/api/dashboards/a/library", json={"title": "Query", "sql": "SELECT 1"}
        )
        assert created.status_code == 201, created.text
        updated = client.put(
            f"/api/dashboards/a/library/{created.json()['id']}",
            json={"title": "Query", "sql": "SELECT 2"},
            headers={"If-Match": created.json()["etag"]},
        )
        assert updated.status_code == 200, updated.text


def test_library_ignores_placeholders_the_author_commented_out(project):
    """The save check read `{{ nope }}` inside a `--` comment as a parameter and
    refused the query, though `/api/run` runs the same SQL and binds nothing for
    it. It asks `extract_params`, the scanner every other surface uses."""
    with client_for(project) as client:
        for sql in (
            "SELECT 1 AS one -- {{ nope }} not a param\n",
            "SELECT 1 /* {% if nope %} {{ nope }} {% endif %} */",
            "SELECT '--' AS dash -- {{ nope }}",
        ):
            saved = client.post("/api/dashboards/a/library", json={"title": "C", "sql": sql})
            assert saved.status_code == 201, (sql, saved.text)
            assert saved.json()["parameters"] == []
        quoted_in_branch = "SELECT 1 AS x {% if region %}FROM t WHERE c = '{{ region }}'{% endif %}"
        with (project / "a.yaml").open("a") as f:
            f.write("filters:\n  - {name: region, type: text}\n")
        saved = client.post(
            "/api/dashboards/a/library", json={"title": "Q", "sql": quoted_in_branch}
        )
        assert saved.status_code == 201, saved.text
        still_refused = client.post(
            "/api/dashboards/a/library",
            json={"title": "C", "sql": "SELECT '--' AS dash, {{ nope }}"},
        )
        assert still_refused.status_code == 422
        assert "Define the query's parameters" in still_refused.json()["detail"]


def test_library_refuses_a_template_the_runtime_would_refuse_naming_it(project):
    with client_for(project) as client:
        for sql, named in (
            ("SELECT 1 {% if a == 'x' %}x{% endif %}", "{% if a == 'x' %}"),
            ("SELECT {{ a.b }}", "{{ a.b }}"),
            ("SELECT 1 {% if a %}", "never closed"),
        ):
            rejected = client.post("/api/dashboards/a/library", json={"title": "T", "sql": sql})
            assert rejected.status_code == 422, sql
            assert "SQL template syntax is invalid" in rejected.json()["detail"]
            assert named in rejected.json()["detail"], rejected.json()["detail"]
