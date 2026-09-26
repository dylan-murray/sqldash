import pytest
from typer.testing import CliRunner

from sqldash.cli import app as cli_app
from sqldash.scaffold import create_demo
from sqldash.server import create_app
from sqldash.snapshot import SnapshotError, snapshot_dashboards


def _browser_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


BROKEN_YAML = "title: Broken\ntiles:\n  - title: x\n   metric: revenue\n"


def test_snapshot_writes_nothing_and_fails_when_no_dashboard_loads(tmp_path):
    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "demo.yaml").write_text(BROKEN_YAML)
    out = tmp_path / "out"
    with pytest.raises(SnapshotError, match=r"no snapshots written(.|\n)*demo: invalid YAML"):
        snapshot_dashboards(create_app(tmp_path), out)
    assert not out.exists()


def test_snapshot_cli_does_not_claim_an_index_it_never_wrote(tmp_path):
    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "demo.yaml").write_text(BROKEN_YAML)
    result = CliRunner().invoke(cli_app, ["snapshot", str(tmp_path), "-o", str(tmp_path / "o")])
    assert result.exit_code == 1
    assert "index.html" not in result.stdout
    assert "no snapshots written" in result.stderr
    assert "demo: invalid YAML" in result.stderr


def test_snapshot_unknown_dashboard(tmp_path):
    create_demo(tmp_path)
    app = create_app(tmp_path)
    with pytest.raises(SnapshotError, match="no dashboard named nope"):
        snapshot_dashboards(app, tmp_path / "out", names=["nope"])


@pytest.mark.skipif(not _browser_available(), reason="playwright browser not installed")
def test_snapshot_renders_demo(tmp_path):
    create_demo(tmp_path)
    app = create_app(tmp_path)
    out = tmp_path / "shots"
    entries = snapshot_dashboards(app, out)
    assert len(entries) == 1
    png = out / "demo.png"
    assert png.exists()
    assert png.stat().st_size > 50_000
    index = (out / "index.html").read_text()
    assert "Order Analytics" in index
    assert entries[0]["settled"] is True


@pytest.mark.skipif(not _browser_available(), reason="playwright browser not installed")
def test_snapshot_reports_a_dashboard_that_fails_to_load(tmp_path):
    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "second.yaml").write_text(BROKEN_YAML)
    out = tmp_path / "shots"
    result = CliRunner().invoke(cli_app, ["snapshot", str(tmp_path), "-o", str(out)])
    assert result.exit_code == 1
    assert "demo.png" in result.stdout
    assert "1 snapshot(s) + index.html" in result.stdout
    assert "second failed to load" in result.stderr
    assert (out / "demo.png").exists()
    assert not (out / "second.png").exists()
    index = (out / "index.html").read_text()
    assert "Order Analytics" in index
    assert "failed to load: invalid YAML" in index
    assert "<span>second</span>" in index


def test_index_html_escapes_the_dashboard_title():
    from sqldash.snapshot import _index_html

    entries = [
        {"file": "z.png", "title": 'z" onload="window.x=1" x="'},
        {"file": "ok.png", "title": "Revenue & Costs <hi>"},
    ]
    out = _index_html(entries, "2026-01-01")
    assert 'onload="window.x=1"' not in out
    assert "&quot;" in out
    assert "<hi>" not in out
    assert "Revenue &amp; Costs" in out
