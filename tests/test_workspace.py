import json

import pytest
from fastapi.testclient import TestClient
from helpers import run_to_completion, warm_up
from typer.testing import CliRunner

import sqldash.workspace as workspace
from sqldash.scaffold import create_demo
from sqldash.server import create_app
from sqldash.workspace import (
    WorkspaceError,
    add_repo,
    load_registry,
    remove_repo,
    resolve_workspace,
)

runner = CliRunner()


def test_registry_round_trip(tmp_path):
    registry = tmp_path / "repos.yaml"
    repo_dir = tmp_path / "analytics"
    repo_dir.mkdir()
    name, entry = add_repo(str(repo_dir), path=registry)
    assert name == "analytics"
    assert entry == {"path": str(repo_dir)}
    with pytest.raises(WorkspaceError, match="already registered"):
        add_repo(str(repo_dir), path=registry)
    name2, entry2 = add_repo("git@github.com:acme/dashboards.git", branch="main", path=registry)
    assert name2 == "dashboards"
    assert entry2 == {"url": "git@github.com:acme/dashboards.git", "branch": "main"}
    assert set(load_registry(registry)) == {"analytics", "dashboards"}
    removed = remove_repo("dashboards", path=registry)
    assert removed["url"].endswith("dashboards.git")
    assert set(load_registry(registry)) == {"analytics"}
    with pytest.raises(WorkspaceError, match="no repo named"):
        remove_repo("nope", path=registry)


def test_add_repo_rejects_missing_dir(tmp_path):
    with pytest.raises(WorkspaceError, match="not a directory"):
        add_repo(str(tmp_path / "nope"), path=tmp_path / "repos.yaml")


def test_resolve_workspace_local_paths(tmp_path):
    repo_dir = tmp_path / "a"
    repo_dir.mkdir()
    resolved = resolve_workspace({"a": {"path": str(repo_dir)}})
    assert resolved == [("a", repo_dir)]
    with pytest.raises(WorkspaceError, match="does not exist"):
        resolve_workspace({"b": {"path": str(tmp_path / "gone")}})


def test_repo_cli(tmp_path, monkeypatch):
    from sqldash.cli import app

    monkeypatch.setattr(workspace, "registry_path", lambda: tmp_path / "repos.yaml")
    repo_dir = tmp_path / "analytics"
    repo_dir.mkdir()
    result = runner.invoke(app, ["repo", "add", str(repo_dir)])
    assert result.exit_code == 0, result.output
    assert "registered 'analytics'" in result.output
    result = runner.invoke(app, ["add", str(repo_dir), "--name", "analytics two"])
    assert result.exit_code == 0
    result = runner.invoke(app, ["repo", "list"])
    assert "analytics" in result.output
    assert "analytics_two" in result.output
    result = runner.invoke(app, ["repo", "remove", "analytics_two"])
    assert result.exit_code == 0
    result = runner.invoke(app, ["repo", "remove", "nope"])
    assert result.exit_code == 1


def test_export_context_falls_back_to_the_workspace(tmp_path, monkeypatch):
    """export context built a DashboardStore from cwd and emitted boilerplate
    with zero metrics while metric list showed the workspace. #418."""
    from sqldash.cli import app

    monkeypatch.setattr(workspace, "registry_path", lambda: tmp_path / "repos.yaml")
    demo = tmp_path / "analytics"
    create_demo(demo)
    added = runner.invoke(app, ["repo", "add", str(demo)])
    assert added.exit_code == 0, added.output
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    listed = runner.invoke(app, ["metric", "list"])
    assert listed.exit_code == 0, listed.output
    assert "revenue" in listed.output
    result = runner.invoke(app, ["export", "context"])
    assert result.exit_code == 0, result.output
    assert "###" in result.output
    assert "revenue" in result.output


def test_lint_from_empty_cwd_lints_registered_repos(tmp_path, monkeypatch):
    """sqldash lint from a workspace cwd used to warn 'no dashboards' and
    exit 0 even when a registered repo was extra=forbid broken. #443."""
    from sqldash.cli import app

    monkeypatch.setattr(workspace, "registry_path", lambda: tmp_path / "repos.yaml")
    acme = tmp_path / "acme"
    beta = tmp_path / "beta"
    create_demo(acme)
    create_demo(beta)
    demo = beta / ".sqldash" / "demo.yaml"
    demo.write_text(
        demo.read_text().replace(
            "title: Order Analytics", "title: Order Analytics\nnot_a_field: true", 1
        )
    )
    added = runner.invoke(app, ["repo", "add", str(acme)])
    assert added.exit_code == 0, added.output
    added = runner.invoke(app, ["repo", "add", str(beta)])
    assert added.exit_code == 0, added.output
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)

    result = runner.invoke(app, ["lint"])
    assert result.exit_code == 1, result.output
    assert "beta/demo.yaml" in result.output
    assert "Extra inputs" in result.output
    assert "acme/demo.yaml" in result.output
    assert "no dashboards or metrics.yaml found" not in result.output

    good = runner.invoke(app, ["lint", str(acme)])
    assert good.exit_code == 0, good.output
    assert "acme/" not in good.output
    broken = runner.invoke(app, ["lint", str(beta)])
    assert broken.exit_code == 1, broken.output
    assert "beta/" not in broken.output
    assert "Extra inputs" in broken.output


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_mcp_from_empty_cwd_serves_registered_repos(tmp_path, monkeypatch):
    """sqldash mcp . from a workspace cwd used to serve zero metrics while
    mcp --all served them. README tells agents `mcp .`. #443."""
    from test_mcp import call

    from sqldash.cli import _mcp_open, app
    from sqldash.mcp_server import create_mcp_server

    monkeypatch.setattr(workspace, "registry_path", lambda: tmp_path / "repos.yaml")
    acme = tmp_path / "acme"
    beta = tmp_path / "beta"
    create_demo(acme)
    create_demo(beta)
    added = runner.invoke(app, ["repo", "add", str(acme)])
    assert added.exit_code == 0, added.output
    added = runner.invoke(app, ["repo", "add", str(beta)])
    assert added.exit_code == 0, added.output
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)

    opened = _mcp_open(".")
    assert {name for name, _ in opened["workspace"]} == {"acme", "beta"}
    for name, root in opened["workspace"]:
        assert root == (tmp_path / name / ".sqldash").resolve()
    explicit = _mcp_open(str(acme))
    assert explicit == {"path": acme.resolve()}

    listed = await call(create_mcp_server(**opened), "list_metrics")
    assert {"acme/revenue", "beta/revenue"} <= {m["name"] for m in listed["metrics"]}

    missing = runner.invoke(app, ["mcp", str(tmp_path / "nope")])
    assert missing.exit_code == 1
    assert "does not exist" in missing.output


@pytest.fixture(scope="module")
def multi_client(tmp_path_factory):
    acme = tmp_path_factory.mktemp("acme")
    beta = tmp_path_factory.mktemp("beta")
    create_demo(acme)
    create_demo(beta)
    app = create_app(
        allowed_hosts=["testserver"],
        workspace=[("acme", acme / ".sqldash"), ("beta", beta / ".sqldash")],
    )
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        for dashboard in ("acme/demo", "beta/demo"):
            warm_up(c, dashboard)
        yield c


def test_workspace_index_groups_by_repo(multi_client):
    res = multi_client.get("/")
    assert res.status_code == 200
    assert res.text.count("dfolder") >= 2
    assert 'href="/d/acme/demo"' in res.text
    assert 'href="/d/beta/demo"' in res.text
    assert "2 repos: acme, beta" in res.text


def test_workspace_dashboard_page(multi_client):
    res = multi_client.get("/d/beta/demo")
    assert res.status_code == 200
    res = multi_client.get("/d/beta/demo/query")
    assert res.status_code == 200
    assert multi_client.get("/d/nope/demo").status_code == 404


def test_saving_a_duckdb_source_from_another_repo_keeps_its_files(tmp_path):
    """Preview against beta's attach_files, then save onto acme: the saved tile
    must still read beta's csv, not acme's same-named file."""
    acme = tmp_path / "acme"
    beta = tmp_path / "beta"
    create_demo(acme)
    create_demo(beta)
    (acme / ".sqldash" / "data" / "orders.csv").write_text(
        "order_date,region,category,amount\n2026-01-01,us,x,111\n"
    )
    (beta / ".sqldash" / "data" / "orders.csv").write_text(
        "order_date,region,category,amount\n2026-01-01,us,x,222\n"
    )
    app = create_app(
        allowed_hosts=["testserver"],
        workspace=[("acme", acme / ".sqldash"), ("beta", beta / ".sqldash")],
    )
    sql = "SELECT amount FROM orders"
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        _, preview = run_to_completion(
            c, {"dashboard": "acme/demo", "sql": sql, "source": "beta/demo.source"}
        )
        assert preview["status"] == "done", preview.get("error")
        assert preview["result"]["rows"] == [[222]], preview["result"]["rows"]
        etag = c.get("/api/dashboards/acme/demo").json()["etag"]
        res = c.post(
            "/api/dashboards/acme/demo/tiles",
            json={
                "tile": {
                    "id": "from_beta",
                    "title": "From beta",
                    "query": "from_beta",
                    "source": "beta/demo.source",
                    "chart": {"type": "table"},
                },
                "sql": sql,
            },
            headers={"If-Match": etag},
        )
        assert res.status_code == 200, res.text
        text = (acme / ".sqldash" / "demo.yaml").read_text()
        assert "base_dir:" in text.split("\nsource:")[1].split("\ntiles:")[0]
        _, saved = run_to_completion(
            c, {"dashboard": "acme/demo", "query": "from_beta", "source": "beta_demo"}
        )
        assert saved["status"] == "done", saved.get("error")
        assert saved["result"]["rows"] == [[222]], saved["result"]["rows"]


def test_saving_a_nested_attach_source_from_another_repo_keeps_its_files(tmp_path):
    """A relative `base_dir: data/csv` used to copy as the repo root, so the
    saved tile attached beta/.sqldash/data (the decoy csv) instead of
    data/csv. Preview was fine — engine derives; copy did not."""
    acme = tmp_path / "acme"
    beta = tmp_path / "beta"
    create_demo(acme)
    create_demo(beta)
    (acme / ".sqldash" / "data" / "orders.csv").write_text(
        "order_date,region,category,amount\n2026-01-01,us,x,111\n"
    )
    (beta / ".sqldash" / "data" / "orders.csv").write_text(
        "order_date,region,category,amount\n2026-01-01,us,x,999\n"
    )
    csv_dir = beta / ".sqldash" / "data" / "csv"
    csv_dir.mkdir(parents=True)
    (csv_dir / "orders.csv").write_text("order_date,region,category,amount\n2026-01-01,us,x,222\n")
    demo = beta / ".sqldash" / "demo.yaml"
    demo.write_text(
        demo.read_text().replace(
            "source: {type: duckdb, attach_files: true}",
            "source: {type: duckdb, attach_files: true, base_dir: data/csv}",
            1,
        )
    )
    app = create_app(
        allowed_hosts=["testserver"],
        workspace=[("acme", acme / ".sqldash"), ("beta", beta / ".sqldash")],
    )
    sql = "SELECT amount FROM orders"
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        _, preview = run_to_completion(
            c, {"dashboard": "acme/demo", "sql": sql, "source": "beta/demo.source"}
        )
        assert preview["status"] == "done", preview.get("error")
        assert preview["result"]["rows"] == [[222]], preview["result"]["rows"]
        etag = c.get("/api/dashboards/acme/demo").json()["etag"]
        res = c.post(
            "/api/dashboards/acme/demo/tiles",
            json={
                "tile": {
                    "id": "from_beta",
                    "title": "From beta",
                    "query": "from_beta",
                    "source": "beta/demo.source",
                    "chart": {"type": "table"},
                },
                "sql": sql,
            },
            headers={"If-Match": etag},
        )
        assert res.status_code == 200, res.text
        copied = (acme / ".sqldash" / "demo.yaml").read_text().split("\nsource:")[1]
        assert "data/csv" in copied.split("tiles:")[0]
        _, saved = run_to_completion(
            c, {"dashboard": "acme/demo", "query": "from_beta", "source": "beta_demo"}
        )
        assert saved["status"] == "done", saved.get("error")
        assert saved["result"]["rows"] == [[222]], saved["result"]["rows"]


def test_saving_a_file_database_from_another_repo_opens_the_nested_file(tmp_path):
    """`base_dir: data/csv` + `database: app.duckdb` used to preview the decoy
    at the repo root (999) and then fail or keep reading it after save."""
    import duckdb

    acme = tmp_path / "acme"
    beta = tmp_path / "beta"
    create_demo(acme)
    create_demo(beta)
    beta_root = beta / ".sqldash"
    nested = beta_root / "data" / "csv"
    nested.mkdir(parents=True)
    for path, value in (
        (nested / "app.duckdb", 42),
        (beta_root / "app.duckdb", 999),
        (acme / ".sqldash" / "app.duckdb", 111),
    ):
        conn = duckdb.connect(str(path))
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute(f"INSERT INTO t VALUES ({value})")
        conn.close()
    demo = beta_root / "demo.yaml"
    demo.write_text(
        demo.read_text().replace(
            "source: {type: duckdb, attach_files: true}",
            "source: {type: duckdb, attach_files: true, base_dir: data/csv, database: app.duckdb}",
            1,
        )
    )
    app = create_app(
        allowed_hosts=["testserver"],
        workspace=[("acme", acme / ".sqldash"), ("beta", beta / ".sqldash")],
    )
    sql = "SELECT x FROM t"
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        _, preview = run_to_completion(
            c, {"dashboard": "acme/demo", "sql": sql, "source": "beta/demo.source"}
        )
        assert preview["status"] == "done", preview.get("error")
        assert preview["result"]["rows"] == [[42]], preview["result"]["rows"]
        etag = c.get("/api/dashboards/acme/demo").json()["etag"]
        res = c.post(
            "/api/dashboards/acme/demo/tiles",
            json={
                "tile": {
                    "id": "from_beta",
                    "title": "From beta",
                    "query": "from_beta",
                    "source": "beta/demo.source",
                    "chart": {"type": "table"},
                },
                "sql": sql,
            },
            headers={"If-Match": etag},
        )
        assert res.status_code == 200, res.text
        copied = (acme / ".sqldash" / "demo.yaml").read_text().split("\nsource:")[1]
        assert "data/csv" in copied.split("tiles:")[0]
        assert "app.duckdb" in copied.split("tiles:")[0]
        _, saved = run_to_completion(
            c, {"dashboard": "acme/demo", "query": "from_beta", "source": "beta_demo"}
        )
        assert saved["status"] == "done", saved.get("error")
        assert saved["result"]["rows"] == [[42]], saved["result"]["rows"]


def test_workspace_run_query(multi_client):
    _, ex = run_to_completion(
        multi_client, {"dashboard": "beta/demo", "query": "revenue_by_category"}
    )
    assert ex["status"] == "done"
    assert ex["result"]["rows"]


def test_workspace_metrics_prefixed(multi_client):
    metrics = multi_client.get("/api/metrics").json()["metrics"]
    names = {m["name"] for m in metrics}
    assert "acme/revenue" in names
    assert "beta/revenue" in names
    assert multi_client.get("/m/acme/revenue").status_code == 200


def test_workspace_metrics_for_a_dashboard_are_bare_names(multi_client):
    """The query-page picker saves MetricRef.name, which rejects `/`."""
    scoped = multi_client.get("/api/metrics", params={"dashboard": "acme/demo"}).json()["metrics"]
    names = {m["name"] for m in scoped}
    assert "revenue" in names
    assert "acme/revenue" not in names
    assert "beta/revenue" not in names
    missing = multi_client.get("/api/metrics", params={"dashboard": "nope/demo"})
    assert missing.status_code == 404


def test_workspace_bare_metric_ambiguous(multi_client):
    res = multi_client.post("/api/run", json={"metric": "revenue"})
    assert res.status_code == 404
    assert "more than one repo" in res.json()["detail"]
    res = multi_client.post("/api/run", json={"metric": "acme/revenue"})
    assert res.status_code == 202


def test_workspace_metric_tile_put_uses_the_bare_name(multi_client):
    etag = multi_client.get("/api/dashboards/acme/demo").json()["etag"]
    prefixed = multi_client.post(
        "/api/dashboards/acme/demo/tiles",
        json={
            "tile": {
                "id": "from_ui",
                "type": "chart",
                "title": "From UI",
                "metric": {"name": "acme/revenue"},
                "chart": {"type": "big_number"},
            },
            "sql": None,
        },
        headers={"If-Match": etag},
    )
    assert prefixed.status_code == 422, prefixed.text
    res = multi_client.post(
        "/api/dashboards/acme/demo/tiles",
        json={
            "tile": {
                "id": "from_ui",
                "type": "chart",
                "title": "From UI",
                "metric": {"name": "revenue"},
                "chart": {"type": "big_number"},
            },
            "sql": None,
        },
        headers={"If-Match": etag},
    )
    assert res.status_code == 200, res.text


def test_workspace_edit_flow(multi_client):
    body = multi_client.get("/api/dashboards/acme/demo").json()
    res = multi_client.patch(
        "/api/dashboards/acme/demo/meta",
        json={"title": "Acme Orders"},
        headers={"If-Match": body["etag"]},
    )
    assert res.status_code == 200, res.text
    assert (
        multi_client.get("/api/dashboards/acme/demo").json()["dashboard"]["title"] == "Acme Orders"
    )


def test_workspace_create_requires_repo(multi_client):
    res = multi_client.post("/api/dashboards", json={"title": "New Thing"})
    assert res.status_code == 422
    assert "pick which repo" in res.json()["detail"]
    res = multi_client.post("/api/dashboards", json={"title": "New Thing", "repo": "beta"})
    assert res.status_code == 201, res.text
    assert res.json()["name"] == "beta/new_thing"
    assert multi_client.get("/d/beta/new_thing").status_code == 200


def test_index_rows_have_delete_buttons(multi_client):
    res = multi_client.get("/")
    assert "card-delete" in res.text
    assert 'data-name="acme/demo"' in res.text


@pytest.fixture
def ws_client(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "registry_path", lambda: tmp_path / "repos.yaml")
    acme = tmp_path / "acme"
    create_demo(acme)
    workspace.add_repo(str(acme), name="acme")
    app = create_app(allowed_hosts=["testserver"], workspace=[("acme", acme / ".sqldash")])
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        yield c, tmp_path


def test_repo_management_api(ws_client):
    client, base = ws_client
    beta = base / "beta"
    create_demo(beta)

    rows = client.get("/api/repos").json()["repos"]
    assert [r["name"] for r in rows] == ["acme"]
    assert rows[0]["registered"] is True

    res = client.post("/api/repos", json={"target": str(beta)})
    assert res.status_code == 201, res.text
    assert res.json()["name"] == "beta"
    assert res.json()["dashboards"] == 1

    assert 'href="/d/beta/demo"' in client.get("/").text
    assert client.get("/d/beta/demo").status_code == 200
    assert "beta" in load_registry(base / "repos.yaml")

    res = client.post("/api/repos", json={"target": str(beta)})
    assert res.status_code == 422
    assert "already registered" in res.json()["detail"]

    assert client.delete("/api/repos/beta").status_code == 204
    assert 'href="/d/beta/demo"' not in client.get("/").text
    assert "beta" not in load_registry(base / "repos.yaml")
    assert client.delete("/api/repos/beta").status_code == 404


def test_repo_add_bad_target(ws_client):
    client, base = ws_client
    res = client.post("/api/repos", json={"target": str(base / "nope")})
    assert res.status_code == 422
    assert not (base / "repos.yaml").read_text().count("nope")


CREDENTIALED = "https://alice:ghp_s3cretToken@github.com/acme/private.git"


def test_repo_urls_never_surface_credentials(ws_client):
    client, base = ws_client
    registry = load_registry(base / "repos.yaml")
    registry["private"] = {"url": CREDENTIALED}
    registry["tokenuser"] = {"url": "https://ghp_userToken@github.com/acme/other.git"}
    workspace.save_registry(registry, base / "repos.yaml")

    rows = {r["name"]: r for r in client.get("/api/repos").json()["repos"]}
    assert rows["private"]["url"] == "https://•••@github.com/acme/private.git"
    assert rows["tokenuser"]["url"] == "https://•••@github.com/acme/other.git"
    page = client.get("/settings/panel").text
    assert "https://•••@github.com/acme/private.git" in page
    for text in (client.get("/api/repos").text, page):
        assert "ghp_s3cretToken" not in text
        assert "ghp_userToken" not in text
        assert "alice" not in text
    assert load_registry(base / "repos.yaml")["private"]["url"] == CREDENTIALED


def test_repo_add_errors_mask_credentials(ws_client):
    client, _ = ws_client
    res = client.post("/api/repos", json={"target": "https://bob:ghp_notGit@example.invalid/x"})
    assert res.status_code == 422
    assert "ghp_notGit" not in res.text
    assert "https://•••@example.invalid/x" in res.json()["detail"]


def test_repo_cli_masks_credentials(tmp_path, monkeypatch):
    from sqldash.cli import app

    monkeypatch.setattr(workspace, "registry_path", lambda: tmp_path / "repos.yaml")
    result = runner.invoke(app, ["repo", "add", CREDENTIALED])
    assert result.exit_code == 0, result.output
    listed = runner.invoke(app, ["repo", "list"]).output
    as_json = json.loads(runner.invoke(app, ["repo", "list", "--json"]).output)
    assert as_json["repos"]["private"]["url"] == "https://•••@github.com/acme/private.git"
    removed = runner.invoke(app, ["repo", "remove", "private"]).output
    for output in (result.output, listed, removed):
        assert "ghp_s3cretToken" not in output
        assert "https://•••@github.com/acme/private.git" in output


def test_settings_panel_fragment(ws_client):
    client, _ = ws_client
    page = client.get("/settings/panel").text
    assert "Workspace repos" in page
    assert "repo-add-form" in page


def test_workspace_prefixed_metric_compiles(multi_client):
    _, ex = run_to_completion(multi_client, {"metric": "acme/revenue", "dimensions": ["region"]})
    assert ex["status"] == "done", ex.get("error")
    assert [c["name"] for c in ex["result"]["columns"]] == ["region", "revenue"]


def test_metric_page_used_by_in_workspace_mode(multi_client):
    """The URL carries `repo/revenue` while a tile stores the bare `revenue`, so
    the exact match never fired and Used by was always empty."""
    page = multi_client.get("/m/acme/revenue")
    assert page.status_code == 200
    assert "No tiles reference this metric yet" not in page.text
    assert "Order Analytics" in page.text


def _used_by_block(html: str) -> str:
    """Just the Used-by list — the page also has a dashboards dropdown listing
    every repo, so asserting over the whole document proves nothing."""
    start = html.index('<ul class="used-by">')
    return html[start : html.index("</ul>", start)]


def test_dashboard_page_survives_the_other_repos_broken_metrics(tmp_path_factory):
    """A broken metrics.yaml in beta must not 422 acme's dashboard. #195."""
    acme = tmp_path_factory.mktemp("acme")
    beta = tmp_path_factory.mktemp("beta")
    create_demo(acme)
    create_demo(beta)
    (beta / ".sqldash" / "metrics.yaml").write_text("metrics: {}\n")
    app = create_app(
        allowed_hosts=["testserver"],
        workspace=[("acme", acme / ".sqldash"), ("beta", beta / ".sqldash")],
    )
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        res = c.get("/d/acme/demo")
    assert res.status_code == 200, res.text
    assert "text/html" in res.headers.get("content-type", "")


def test_dashboard_info_kin_stays_in_the_repo(multi_client):
    """Two demo repos both have an `orders` relation. That is not kinship."""
    html = multi_client.get("/d/acme/demo").text
    start = html.index("dd-menu dash-info")
    start = html.rfind("<", 0, start)
    block = html[start : html.index('id="dash-desc"', start)]
    assert "/m/beta/" not in block, block
    assert "shares a relation with" in block


def test_topbar_names_the_current_repo(multi_client):
    """#132: the switcher said 'dashboards' even in a workspace."""
    page = multi_client.get("/d/acme/demo").text
    assert 'class="dd-label">acme</span>' in page, page
    assert 'class="dd-menu-title current">acme</div>' in page
    assert 'class="dd-menu-title current">beta</div>' not in page
    other = multi_client.get("/d/beta/demo").text
    assert 'class="dd-label">beta</span>' in other


def test_metric_page_used_by_does_not_cross_repos(multi_client):
    """Both repos define `revenue`; acme's metric page must not claim beta's
    tiles just because they share a bare name."""
    block = _used_by_block(multi_client.get("/m/acme/revenue").text)
    assert "/d/acme/demo" in block, block
    assert "/d/beta/demo" not in block, block


def _registry(tmp_path, monkeypatch, repos):
    """Point the registry at a temp home so the real one is never touched."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    from sqldash import workspace as registry

    for name, path in repos.items():
        registry.add_repo(str(path), name=name)


def test_headless_commands_fall_back_to_the_registry(tmp_path, monkeypatch):
    """`serve` falls back to the registered repos when the cwd has none; the
    read-only commands printed "(no dashboards)" instead, which is the path the
    README points agents at."""
    from typer.testing import CliRunner

    from sqldash.cli import app

    for repo in ("acme", "beta"):
        create_demo(tmp_path / repo)
    _registry(tmp_path, monkeypatch, {r: tmp_path / r for r in ("acme", "beta")})

    empty = tmp_path / "elsewhere"
    empty.mkdir()
    monkeypatch.chdir(empty)

    result = CliRunner().invoke(app, ["dashboard", "list", "--json"])
    assert result.exit_code == 0, result.output
    names = {d["name"] for d in json.loads(result.output)["dashboards"]}
    assert names == {"acme/demo", "beta/demo"}, names


def test_a_local_project_still_wins_over_the_registry(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from sqldash.cli import app

    for repo in ("acme", "beta"):
        create_demo(tmp_path / repo)
    _registry(tmp_path, monkeypatch, {r: tmp_path / r for r in ("acme", "beta")})

    local = tmp_path / "local"
    create_demo(local)
    monkeypatch.chdir(local)

    result = CliRunner().invoke(app, ["dashboard", "list", "--json"])
    assert result.exit_code == 0, result.output
    names = {d["name"] for d in json.loads(result.output)["dashboards"]}
    assert names == {"demo"}, names


def test_query_addresses_a_workspace_dashboard(tmp_path, monkeypatch):
    """`dashboard list` advertises `acme/demo`; `query` rejected that same name,
    so an agent that self-oriented with one command failed on the next."""
    for repo in ("acme", "beta"):
        create_demo(tmp_path / repo)
    _registry(tmp_path, monkeypatch, {r: tmp_path / r for r in ("acme", "beta")})
    empty = tmp_path / "elsewhere"
    empty.mkdir()
    monkeypatch.chdir(empty)

    from sqldash.cli import app

    result = CliRunner().invoke(app, ["query", ".", "acme/demo.revenue_by_category", "-f", "json"])
    assert result.exit_code == 0, result.output
    # stdout, not output: naming the dashboard now puts a scope note on stderr (#676).
    assert json.loads(result.stdout), result.output
    assert "by dashboard 'acme/demo'" in result.stderr, result.stderr


def test_registry_fallback_does_no_git_work(tmp_path, monkeypatch):
    """A read-only listing must not clone or pull: it would stall with no
    explanation, and a transient failure would degrade to "(no dashboards)" —
    the symptom this fallback exists to fix."""
    import sqldash.gitrepo as gitrepo
    from sqldash.cli import _registered_repos

    create_demo(tmp_path / "local")
    _registry(tmp_path, monkeypatch, {"local": tmp_path / "local"})
    from sqldash import workspace as registry

    registry.add_repo("https://example.invalid/never-cloned.git", name="remote")

    def explode(*args, **kwargs):
        raise AssertionError("a read-only listing tried to reach the network")

    monkeypatch.setattr(gitrepo, "clone_or_pull", explode)
    resolved = _registered_repos()
    assert [name for name, _ in resolved] == ["local"], resolved


def test_query_accepts_the_identifier_dashboard_list_prints(tmp_path, monkeypatch):
    """`dashboard list` prints `acme/demo`, so that is what gets reached for
    first; it used to answer "acme/demo does not exist"."""
    for repo in ("acme", "beta"):
        create_demo(tmp_path / repo)
    _registry(tmp_path, monkeypatch, {r: tmp_path / r for r in ("acme", "beta")})
    empty = tmp_path / "elsewhere"
    empty.mkdir()
    monkeypatch.chdir(empty)

    from sqldash.cli import app

    result = CliRunner().invoke(app, ["query", "acme/demo", "revenue_by_category", "-f", "json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout), result.output


def test_ambiguous_query_names_the_reachable_queries(tmp_path, monkeypatch):
    """It said "available: (none)" while two dashboards defined the query — the
    list has to carry the prefix that makes each one runnable."""
    for repo in ("acme", "beta"):
        create_demo(tmp_path / repo)
    _registry(tmp_path, monkeypatch, {r: tmp_path / r for r in ("acme", "beta")})
    empty = tmp_path / "elsewhere"
    empty.mkdir()
    monkeypatch.chdir(empty)

    from sqldash.cli import app

    result = CliRunner().invoke(app, ["query", ".", "revenue_by_category"])
    assert result.exit_code == 1
    assert "available: (none)" not in result.output, result.output
    assert "acme/demo.revenue_by_category" in result.output, result.output


def _workspace_with_unique_and_shared_metrics(tmp_path, monkeypatch):
    """Two repos, each with a unique metric and a colliding `shared`.

    Mirrors the #463 explorer fixture: from the workspace cwd, `metric list`
    prints repo-prefixed names and a bare colliding name does not resolve.
    """
    for repo, unique in (("repo1", "revenue"), ("repo2", "profit")):
        root = tmp_path / repo
        root.mkdir()
        (root / "metrics.yaml").write_text(
            "source: {type: duckdb, database: ':memory:'}\n"
            "metrics:\n"
            f"  {unique}:\n"
            "    sql: 'SELECT 1 AS amount'\n"
            "    expr: SUM(amount)\n"
            "    dimensions: [{name: region}]\n"
            "  shared:\n"
            "    sql: 'SELECT 1 AS amount'\n"
            "    expr: SUM(amount)\n"
            "    dimensions: [{name: region}]\n"
        )
        dash = "dash1" if repo == "repo1" else "dash2"
        (root / f"{dash}.yaml").write_text(
            f"title: {dash}\n"
            "source: {type: duckdb, database: ':memory:'}\n"
            "tiles: [{markdown: 'x'}]\n"
        )
    _registry(tmp_path, monkeypatch, {"repo1": tmp_path / "repo1", "repo2": tmp_path / "repo2"})
    empty = tmp_path / "elsewhere"
    empty.mkdir()
    monkeypatch.chdir(empty)


def test_workspace_query_unknown_lists_prefixed_metrics(tmp_path, monkeypatch):
    """#463: the not-found hint spliced the inner repo's bare names into a
    workspace-mode error, so the suggestions did not resolve from that cwd.
    The trailing hint was single-project advice ('this project has multiple
    dashboards') when no dashboard was selected.
    """
    from sqldash.cli import app

    _workspace_with_unique_and_shared_metrics(tmp_path, monkeypatch)

    listed = CliRunner().invoke(app, ["metric", "list"])
    assert listed.exit_code == 0, listed.output
    assert "repo1/revenue" in listed.output
    assert "repo2/shared" in listed.output

    dotted = CliRunner().invoke(app, ["query", ".", "repo1/dash1.orders_by_region"])
    assert dotted.exit_code == 1
    assert "repo1/revenue" in dotted.output, dotted.output
    assert "repo1/shared" in dotted.output, dotted.output
    assert "repo2/profit" in dotted.output, dotted.output
    assert "available metrics: revenue, shared" not in dotted.output, dotted.output
    assert "this project has multiple dashboards" not in dotted.output, dotted.output

    slashed = CliRunner().invoke(app, ["query", ".", "repo1/dash1/orders_by_region"])
    assert slashed.exit_code == 1
    assert "repo1/revenue" in slashed.output, slashed.output
    assert "repo2/shared" in slashed.output, slashed.output
    assert "available metrics: revenue, shared" not in slashed.output, slashed.output
    assert "this project has multiple dashboards" not in slashed.output, slashed.output
    assert "metrics live in repos" in slashed.output, slashed.output
    assert "sqldash metric list" in slashed.output, slashed.output


def test_skipped_repos_are_announced(tmp_path, monkeypatch, capsys):
    """Silently incomplete is a close cousin of silently empty."""
    create_demo(tmp_path / "acme")
    _registry(tmp_path, monkeypatch, {"acme": tmp_path / "acme"})
    from sqldash import workspace as registry

    registry.add_repo("https://example.invalid/never-fetched.git", name="remote")

    from sqldash.cli import _registered_repos

    resolved = _registered_repos()
    assert [name for name, _ in resolved] == ["acme"]
    assert "skipping remote" in capsys.readouterr().err


def test_one_broken_metrics_yaml_stays_in_its_repo(tmp_path):
    """/m/beta/... answered raw JSON and the index dropped every repo's metrics. #593."""
    for repo in ("acme", "beta"):
        create_demo(tmp_path / repo)
    (tmp_path / "beta" / ".sqldash" / "metrics.yaml").write_text("metrics:\n  revenue: [oops\n")
    app = create_app(
        allowed_hosts=["testserver"],
        workspace=[(r, tmp_path / r / ".sqldash") for r in ("acme", "beta")],
    )
    with TestClient(app) as c:
        assert c.get("/m/acme/revenue").status_code == 200
        res = c.get("/m/beta/revenue")
        assert res.status_code == 422
        assert "text/html" in res.headers["content-type"]
        assert '<code class="error-file">metrics.yaml</code>' in res.text
        index = c.get("/").text
        assert 'href="/m/acme/revenue"' in index
        assert 'href="/m/beta/revenue"' not in index
        assert "metrics.yaml: invalid YAML" in index


def _workspace_with_a_broken_repo(tmp_path, monkeypatch):
    for repo in ("acme", "beta"):
        create_demo(tmp_path / repo)
    metrics = tmp_path / "beta" / ".sqldash" / "metrics.yaml"
    metrics.write_text(
        metrics.read_text().replace("    expr: SUM(amount)\n", "    expr: [oops\n", 1)
    )
    _registry(tmp_path, monkeypatch, {r: tmp_path / r for r in ("acme", "beta")})
    empty = tmp_path / "elsewhere"
    empty.mkdir()
    monkeypatch.chdir(empty)


@pytest.mark.parametrize(
    ("args", "key"),
    [(["metric", "list", "--json"], "metrics"), (["agent", "list", "--json"], "agents")],
)
def test_one_broken_repo_does_not_empty_the_workspace_listing(tmp_path, monkeypatch, args, key):
    """`source list` kept listing and named the broken repo; these exited 1 with
    empty stdout and an error that did not say which repo's metrics.yaml. #603."""
    from sqldash.cli import app

    _workspace_with_a_broken_repo(tmp_path, monkeypatch)
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    names = [entry["name"] for entry in payload[key]]
    assert names, payload
    assert all(name.startswith("acme/") for name in names), names
    assert len(payload["errors"]) == 1, payload["errors"]
    assert payload["errors"][0].startswith("beta/metrics.yaml: invalid YAML")
    assert "error: beta/metrics.yaml: invalid YAML" in result.stderr


def test_export_context_keeps_the_healthy_repos(tmp_path, monkeypatch):
    from sqldash.cli import app

    _workspace_with_a_broken_repo(tmp_path, monkeypatch)
    result = runner.invoke(app, ["export", "context"])
    assert result.exit_code == 0, result.output
    assert "### `acme/revenue`" in result.stdout
    assert "`acme/finance_analyst`" in result.stdout
    assert "beta/revenue" not in result.stdout
    assert result.stderr.count("error: beta/metrics.yaml: invalid YAML") == 1, result.stderr


def test_bare_metric_names_refuse_while_a_repo_is_broken(tmp_path):
    """Skipping beta must not turn an ambiguous bare name into acme's metric."""
    from sqldash.project.store import DashboardStore
    from sqldash.semantics import SemanticError, SemanticLayer
    from sqldash.semantics.layer import WorkspaceLayer

    for repo in ("acme", "beta"):
        create_demo(tmp_path / repo)
    (tmp_path / "beta" / ".sqldash" / "metrics.yaml").write_text("metrics: [\n")
    layer = WorkspaceLayer(
        {r: SemanticLayer(DashboardStore(tmp_path / r / ".sqldash")) for r in ("acme", "beta")}
    )
    with pytest.raises(SemanticError, match=r"name it as repo/revenue"):
        layer.resolve("revenue")
    assert layer.resolve("acme/revenue").name == "acme/revenue"


@pytest.mark.anyio
async def test_mcp_list_metrics_names_the_broken_repo(tmp_path):
    from test_mcp import call

    from sqldash.mcp_server import create_mcp_server

    for repo in ("acme", "beta"):
        create_demo(tmp_path / repo)
    (tmp_path / "beta" / ".sqldash" / "metrics.yaml").write_text("metrics: [\n")
    server = create_mcp_server(workspace=[(r, tmp_path / r / ".sqldash") for r in ("acme", "beta")])
    listed = await call(server, "list_metrics")
    assert "acme/revenue" in {m["name"] for m in listed["metrics"]}
    assert [e.split(":", 1)[0] for e in listed["errors"]] == ["beta/metrics.yaml"]


def _validators_workspace(tmp_path, monkeypatch, repos):
    from sqldash.mcp_server import create_mcp_server

    for repo in repos:
        create_demo(tmp_path / repo)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    server = create_mcp_server(workspace=[(r, tmp_path / r / ".sqldash") for r in repos])
    demo = tmp_path / repos[0] / ".sqldash"
    return server, (demo / "metrics.yaml").read_text(), (demo / "demo.yaml").read_text()


@pytest.mark.anyio
async def test_workspace_validators_resolve_sources_like_lint(tmp_path, monkeypatch):
    """The validators resolved the demo's file source against the process cwd in
    a workspace, so the demo `sqldash lint` accepts came back valid: false. #600."""
    from test_mcp import call

    server, metrics, demo = _validators_workspace(tmp_path, monkeypatch, ["acme"])
    result = await call(server, "validate_metrics", {"yaml_text": metrics})
    assert result["valid"], result
    assert result["schema_checked"], result
    result = await call(server, "validate_dashboard", {"yaml_text": demo, "name": "demo"})
    assert result["valid"], result
    assert result["rendered_sql"], result


@pytest.mark.anyio
async def test_workspace_validators_need_the_repo_to_check_a_file_source(tmp_path, monkeypatch):
    from test_mcp import call

    server, metrics, demo = _validators_workspace(tmp_path, monkeypatch, ["acme", "beta"])
    unnamed = await call(server, "validate_metrics", {"yaml_text": metrics})
    assert unnamed["valid"], unnamed
    assert not unnamed["errors"], unnamed
    assert "pass repo" in unnamed["lint"][0], unnamed

    named = await call(server, "validate_metrics", {"yaml_text": metrics, "repo": "beta"})
    assert named["valid"], named
    assert named["schema_checked"], named

    broken = metrics.replace("attach_files: true", "attach_files: true\n  base_dir: missing", 1)
    result = await call(server, "validate_metrics", {"yaml_text": broken, "repo": "beta"})
    assert not result["valid"], result
    assert "base_dir 'missing' does not exist" in result["errors"][0], result

    unknown = await call(server, "validate_metrics", {"yaml_text": metrics, "repo": "nope"})
    assert unknown == {"error": "no repo named 'nope' (repos: acme, beta)"}

    dashboard = await call(server, "validate_dashboard", {"yaml_text": demo, "name": "demo"})
    assert not dashboard["sql_checked"], dashboard
    assert any("name must be 'repo/dashboard'" in n for n in dashboard["lint"]), dashboard
    assert not any("no csv/parquet files" in e for e in dashboard["errors"]), dashboard


@pytest.mark.anyio
async def test_an_absolute_database_with_a_relative_base_dir_still_needs_the_repo(
    tmp_path, monkeypatch
):
    """attach_files checks base_dir against the repo even when database: is
    absolute, so without the repo that check was skipped with no note, and a
    missing base_dir came back valid where lint fails it."""
    from test_mcp import call

    server, metrics, _ = _validators_workspace(tmp_path, monkeypatch, ["acme", "beta"])
    database = tmp_path / "acme" / ".sqldash" / "demo.duckdb"
    broken = metrics.replace(
        "attach_files: true",
        f"attach_files: true\n  base_dir: missing\n  database: '{database}'",
        1,
    )
    unnamed = await call(server, "validate_metrics", {"yaml_text": broken})
    assert any("pass repo" in note for note in unnamed["lint"]), unnamed
    named = await call(server, "validate_metrics", {"yaml_text": broken, "repo": "acme"})
    assert not named["valid"], named
    assert "base_dir 'missing' does not exist" in named["errors"][0], named


@pytest.mark.anyio
async def test_a_dashboard_name_for_an_unknown_repo_is_an_error(tmp_path, monkeypatch):
    """validate_metrics errors on an unknown repo; validate_dashboard answered
    valid with a note asking for the 'repo/dashboard' it had just been given."""
    from test_mcp import call

    server, _, demo = _validators_workspace(tmp_path, monkeypatch, ["acme", "beta"])
    result = await call(server, "validate_dashboard", {"yaml_text": demo, "name": "gamma/demo"})
    assert result == {"error": "no repo named 'gamma' (repos: acme, beta)"}


def _workspace_with_a_colliding_metric(tmp_path, monkeypatch):
    """Two repos whose `revenue` runs different SQL, each with a tile on it.

    The amounts differ so a run says which repo's definition executed.
    """
    for repo, amount in (("acme", 100), ("beta", 999)):
        root = tmp_path / repo
        root.mkdir()
        (root / "metrics.yaml").write_text(
            "source: {type: duckdb, database: ':memory:'}\n"
            "metrics:\n"
            "  revenue:\n"
            f"    sql: \"SELECT {amount} AS amount, '{repo}' AS region\"\n"
            "    expr: SUM(amount)\n"
            "    dimensions: [{name: region}]\n"
        )
        (root / "demo.yaml").write_text(
            f"title: {repo} demo\n"
            "source: {type: duckdb, database: ':memory:'}\n"
            "tiles:\n"
            "  - id: rev\n"
            "    type: chart\n"
            "    title: Revenue\n"
            "    metric: revenue\n"
            "    chart: big_number\n"
        )
    _registry(tmp_path, monkeypatch, {"acme": tmp_path / "acme", "beta": tmp_path / "beta"})
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    return (tmp_path / "acme" / "demo.yaml").read_text()


def test_a_dashboard_tile_runs_its_own_repos_metric(tmp_path, monkeypatch):
    """Serve time scopes a bare metric name to the dashboard's repo, which is
    what makes the validator's ambiguity error wrong (#629)."""
    _workspace_with_a_colliding_metric(tmp_path, monkeypatch)
    app = create_app(
        allowed_hosts=["testserver"],
        workspace=[(r, tmp_path / r) for r in ("acme", "beta")],
    )
    with TestClient(app) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        for repo, amount in (("acme", 100), ("beta", 999)):
            _, ex = run_to_completion(
                client,
                {"dashboard": f"{repo}/demo", "metric": "revenue", "dimensions": ["region"]},
            )
            assert ex["status"] == "done", ex
            assert ex["result"]["rows"] == [[repo, amount]], ex


@pytest.mark.anyio
async def test_validate_dashboard_scopes_a_bare_metric_to_its_repo(tmp_path, monkeypatch):
    """#629: the catalog merged every repo's metrics, so a name two repos define
    errored as ambiguous on a dashboard that renders its own repo's metric."""
    from test_mcp import call

    from sqldash.mcp_server import create_mcp_server

    demo = _workspace_with_a_colliding_metric(tmp_path, monkeypatch)
    server = create_mcp_server(workspace=[(r, tmp_path / r) for r in ("acme", "beta")])

    named = await call(server, "validate_dashboard", {"yaml_text": demo, "name": "acme/demo"})
    assert named["valid"], named
    assert not named["errors"], named

    unnamed = await call(server, "validate_dashboard", {"yaml_text": demo})
    assert not unnamed["valid"], unnamed
    assert any("exists in more than one repo" in e for e in unnamed["errors"]), unnamed


@pytest.mark.anyio
async def test_validate_dashboard_rejects_another_repos_metric(tmp_path, monkeypatch):
    """A bare name only another repo defines does not resolve at serve time
    either, so the scoped catalog has to call it unknown rather than borrow it."""
    from test_mcp import call

    from sqldash.mcp_server import create_mcp_server

    _workspace_with_a_colliding_metric(tmp_path, monkeypatch)
    (tmp_path / "beta" / "metrics.yaml").write_text(
        (tmp_path / "beta" / "metrics.yaml").read_text().replace("revenue:", "profit:", 1)
    )
    server = create_mcp_server(workspace=[(r, tmp_path / r) for r in ("acme", "beta")])
    borrowing = (
        (tmp_path / "acme" / "demo.yaml").read_text().replace("metric: revenue", "metric: profit")
    )

    owned = await call(server, "validate_dashboard", {"yaml_text": borrowing, "name": "beta/demo"})
    assert owned["valid"], owned

    result = await call(server, "validate_dashboard", {"yaml_text": borrowing, "name": "acme/demo"})
    assert not result["valid"], result
    assert any("unknown metric 'profit'" in e for e in result["errors"]), result
