import base64
import contextlib
import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from typer.testing import CliRunner

from sqldash.cli import app as cli
from sqldash.project.store import DashboardStore, WorkspaceStore
from sqldash.server import create_app
from sqldash.studio.entrypoints import (
    AgentEntrypoint,
    StudioError,
    StudioNotFound,
    load_entrypoints,
    save_entrypoint,
)
from sqldash.studio.review import changes, restore, snapshot
from sqldash.studio.sessions import SessionRequest, Studio

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="Studio process management requires POSIX"
)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / ".sqldash"
    root.mkdir()
    (root / "d.yaml").write_text(
        "# preserve this comment\ntitle: Original\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "tiles: [{id: count, title: Count, sql: 'SELECT 1'}]\n"
    )
    return DashboardStore(tmp_path)


@pytest.fixture
def entrypoints_file(tmp_path, monkeypatch):
    path = tmp_path / "user" / "studio.json"
    monkeypatch.setattr("sqldash.studio.entrypoints.entrypoints_path", lambda: path)
    return path


def body(project, **kwargs):
    return SessionRequest(
        dashboard="d",
        etag=project.load("d")[2],
        annotations=[{"tile": "count", "note": "Make this blue"}],
        **kwargs,
    )


def wait_finished(process):
    deadline = time.monotonic() + 10
    while process.read(0)["running"]:
        assert time.monotonic() < deadline, process.read(0)
        time.sleep(0.02)
    return base64.b64decode(process.read(0)["data"]).decode(errors="replace")


def test_independent_entrypoints_and_private_config(entrypoints_file):
    save_entrypoint(
        AgentEntrypoint(
            name="Default agent", command=["claude", "{prompt}"], env={"AGENT_MODE": "default"}
        )
    )
    save_entrypoint(
        AgentEntrypoint(
            name="Custom agent",
            command=["claude-custom", "{prompt}"],
            shell="/bin/zsh",
            env={"AGENT_MODE": "custom"},
        )
    )
    entrypoints = load_entrypoints()
    assert entrypoints["Default agent"].env == {"AGENT_MODE": "default"}
    assert entrypoints["Custom agent"].env == {"AGENT_MODE": "custom"}
    assert entrypoints_file.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("command", [["claude"], ["claude", "--prompt={prompt}"], ["", "{prompt}"]])
def test_entrypoints_require_explicit_prompt_argument(command):
    with pytest.raises(ValidationError):
        AgentEntrypoint(name="Bad", command=command)


def test_prompt_is_never_shell_syntax():
    entrypoint = AgentEntrypoint(
        name="Custom agent", command=["claude-custom", "{prompt}"], shell="/bin/zsh"
    )
    prompt = '$(touch /tmp/nope); `whoami` " newline\n'
    argv = entrypoint.argv(prompt)
    assert argv[2] == 'claude-custom "$@"'
    assert argv[-1] == prompt
    with pytest.raises(ValidationError):
        AgentEntrypoint(name="Bad", command=["claude;bad", "{prompt}"], shell="/bin/zsh")


def test_alias_resolution_and_headless_arguments(tmp_path, entrypoints_file):
    if not Path("/bin/zsh").exists():
        pytest.skip("zsh not installed")
    (tmp_path / ".zshrc").write_text("alias test-agent='printf \"%s\\n\"'\n")
    entrypoint = AgentEntrypoint(
        name="Custom agent",
        command=["test-agent", "{prompt}"],
        shell="/bin/zsh",
        env={"ZDOTDIR": str(tmp_path)},
    )
    entrypoint.check()
    from sqldash.studio.process import AgentProcess

    text = 'literal $(touch not-created) `whoami` ; "quoted"'
    process = AgentProcess(entrypoint.argv(text), tmp_path, entrypoint.environment())
    try:
        assert text in wait_finished(process)
        assert not (tmp_path / "not-created").exists()
    finally:
        process.stop()


def test_studio_add_refusal_reads_without_pydantics_prefix(entrypoints_file):
    """`studio add` echoed pydantic's raw message, so the refusal read "Invalid
    entrypoint: Value error, command must contain ...". The HTTP tile routes lost
    the same prefix in #649; this is that fix on the CLI surface. #654."""
    result = CliRunner().invoke(cli, ["studio", "add", "Test", "--", "/nonexistent-cmd"])
    assert result.exit_code != 0
    assert "Invalid entrypoint: command must contain a separate {prompt} argument" in (
        result.output
    )
    assert "Value error" not in result.output


def test_entrypoint_cli_and_missing_entrypoint(entrypoints_file):
    runner = CliRunner()
    result = runner.invoke(
        cli, ["studio", "add", "Custom agent", "--", "claude-custom", "{prompt}"]
    )
    assert result.exit_code == 0, result.output
    assert load_entrypoints()["Custom agent"].command == ["claude-custom", "{prompt}"]
    save_entrypoint(AgentEntrypoint(name="Missing", command=["/no/such/agent", "{prompt}"]))
    result = runner.invoke(cli, ["studio", "check", "Missing"])
    assert result.exit_code == 1
    assert "Executable not found" in result.output


def test_cli_lists_and_checks_the_entrypoints_the_panel_offers(
    project, entrypoints_file, tmp_path, monkeypatch
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "ran"
    for agent in ("claude", "codex"):
        stub = bin_dir / agent
        stub.write_text(f"#!/bin/sh\ntouch {marker}\n")
        stub.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    save_entrypoint(AgentEntrypoint(name="Custom agent", command=["codex", "{prompt}"]))
    app = create_app(project.root, studio=True)
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 1234)) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        offered = {
            p["name"]: p["entrypoint"]
            for p in client.get("/api/studio/entrypoints").json()["entrypoints"]
        }
        for name in offered:
            response = client.post("/api/studio/entrypoints/check", json={"entrypoint": name})
            assert response.json() == {"ok": True}
    assert offered == {
        "Claude Code": str(bin_dir / "claude"),
        "Codex": str(bin_dir / "codex"),
        "Custom agent": "codex",
    }
    runner = CliRunner()
    result = runner.invoke(cli, ["studio", "list"])
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        str(entrypoints_file),
        *(f"{name}: {command}" for name, command in offered.items()),
    ]
    for name in offered:
        result = runner.invoke(cli, ["studio", "check", name])
        assert result.exit_code == 0, result.output
        assert result.stdout == f"{name}: entrypoint found\n"
    result = runner.invoke(cli, ["studio", "check", "Nope"])
    assert result.exit_code == 1
    assert result.stderr.strip() == (
        "Unknown entrypoint 'Nope'. Available: 'Claude Code', 'Codex', 'Custom agent'"
    )
    assert not marker.exists()


def test_unknown_entrypoint_names_the_one_passed_and_the_ones_that_exist(
    project, entrypoints_file, tmp_path, monkeypatch
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for agent in ("claude", "codex"):
        stub = bin_dir / agent
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    expected = "Unknown entrypoint 'claude'. Available: 'Claude Code', 'Codex'"

    result = CliRunner().invoke(cli, ["studio", "check", "claude"])
    assert result.exit_code == 1
    assert result.stderr.strip() == expected

    app = create_app(project.root, studio=True)
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 1234)) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        response = client.post("/api/studio/entrypoints/check", json={"entrypoint": "claude"})
    assert response.status_code == 404
    assert response.json() == {"detail": expected}

    monkeypatch.setattr("sqldash.studio.entrypoints.shutil.which", lambda name, path=None: None)
    result = CliRunner().invoke(cli, ["studio", "check", "claude"])
    assert result.exit_code == 1
    assert result.stderr.strip() == (
        "Unknown entrypoint 'claude'. No entrypoints: claude and codex were not found "
        "on PATH and none are saved"
    )


def test_cli_list_explains_when_nothing_is_discovered_or_saved(entrypoints_file, monkeypatch):
    monkeypatch.setattr("sqldash.studio.entrypoints.shutil.which", lambda name: None)
    result = CliRunner().invoke(cli, ["studio", "list"])
    assert result.exit_code == 0, result.output
    assert result.stdout == f"{entrypoints_file}\n"
    assert "not found on PATH and none are saved" in result.stderr
    assert not entrypoints_file.exists()


def test_module_cli_registers_studio_commands():
    result = subprocess.run(
        [sys.executable, "-m", "sqldash.cli", "studio", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert all(command in result.stdout for command in ("add", "list", "check"))


def test_entrypoint_parser_recursion_is_a_safe_configuration_error(entrypoints_file, monkeypatch):
    entrypoints_file.parent.mkdir(parents=True, exist_ok=True)
    entrypoints_file.write_text("[]")

    def fail_parse(data):
        raise RecursionError("secret-sentinel")

    monkeypatch.setattr("sqldash.studio.entrypoints.json.loads", fail_parse)
    with pytest.raises(StudioError, match="Invalid Studio entrypoints file") as error:
        load_entrypoints()
    assert "secret-sentinel" not in str(error.value)


@pytest.mark.parametrize("arguments", [["list"], ["check", "Invalid"]])
@pytest.mark.parametrize("invalid", ["missing-command", "nul-env", "nul-shell"])
def test_cli_invalid_saved_config_never_prints_secrets(entrypoints_file, arguments, invalid):
    config = {
        "name": "Invalid",
        "command": ["echo", "{prompt}"],
        "env": {"API_KEY": "secret-sentinel"},
    }
    if invalid == "missing-command":
        config.pop("command")
    elif invalid == "nul-env":
        config["env"]["API_KEY"] += "\0"
    else:
        config["shell"] = "/bad\0/zsh"
    entrypoints_file.parent.mkdir(parents=True, exist_ok=True)
    entrypoints_file.write_text(json.dumps([config]))
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; "
            "from sqldash.studio import entrypoints; "
            "from sqldash.cli import app; "
            "config = Path(sys.argv.pop(1)); "
            "entrypoints.entrypoints_path = lambda: config; app()",
            str(entrypoints_file),
            "studio",
            *arguments,
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 1
    assert "Invalid Studio entrypoints file" in result.stderr
    assert "secret-sentinel" not in result.stdout + result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("wait", [False, True])
def test_shell_check_cleans_startup_children(tmp_path, monkeypatch, wait):
    if not Path("/bin/zsh").exists():
        pytest.skip("zsh not installed")
    (tmp_path / ".zshrc").write_text(
        'sleep 30 &\nprintf "%s" "$!" > "$ZDOTDIR/child.pid"\n' + ("wait\n" if wait else "")
    )
    entrypoint = AgentEntrypoint(
        name="Probe",
        command=["echo", "{prompt}"],
        shell="/bin/zsh",
        env={"ZDOTDIR": str(tmp_path)},
    )
    monkeypatch.setattr("sqldash.studio.entrypoints.CHECK_TIMEOUT", 1)
    child = None
    try:
        if wait:
            with pytest.raises(StudioError, match="within 10 seconds"):
                entrypoint.check()
        else:
            entrypoint.check()
        child = int((tmp_path / "child.pid").read_text())
        state = subprocess.run(
            ["/bin/ps", "-o", "stat=", "-p", str(child)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout.strip()
        if not state or state.startswith("Z"):
            (tmp_path / "child.pid").unlink()
            child = None
        assert not state or state.startswith("Z"), state
    finally:
        if child is None and (tmp_path / "child.pid").exists():
            child = int((tmp_path / "child.pid").read_text())
        if child is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(child, signal.SIGKILL)


def test_context_targets_and_no_connection_details(project):
    path = project.path_for("d")
    path.write_text(
        path.read_text().replace("database: ':memory:'", "database: ':memory:', password: secret")
    )
    studio = Studio()
    session = studio.create(project, body(project))
    try:
        assert '"tile": "count"' in session.context
        assert "secret" not in session.context
        assert session.cwd == project.root.parent
        assert Path(session.directory.name).exists()
    finally:
        studio.close()
    assert not Path(session.directory.name).exists()


def test_stale_annotations_and_unknown_tiles(project):
    studio = Studio()
    request = body(project)
    project.path_for("d").write_text(
        project.path_for("d").read_text().replace("Original", "Changed")
    )
    with pytest.raises(StudioError, match="Dashboard changed"):
        studio.create(project, request)
    request = body(project)
    request.annotations[0].tile = "missing"
    with pytest.raises(StudioError, match="no longer exists"):
        studio.create(project, request)


def test_review_and_undo_preserve_initial_dirty_changes(project):
    initial = project.path_for("d").read_bytes()
    studio = Studio()
    session = studio.create(project, body(project))
    try:
        project.path_for("d").write_bytes(initial.replace(b"Original", b"Agent change"))
        result = session.review()
        assert len(result["changes"]) == 1
        assert "Agent change" in result["changes"][0]["diff"]
        assert result["validation"] == []
        assert result["validation_error"] is None
        session.finish(result["revision"], undo=True)
        assert project.path_for("d").read_bytes() == initial
    finally:
        studio.close()


@pytest.mark.parametrize("kind", ["types", "depth", "parser-depth", "merges"])
def test_review_checks_yaml_bounds_before_lint_and_preserves_undo(project, monkeypatch, kind):
    studio = Studio()
    session = studio.create(project, body(project))
    path = project.root / "extra.yaml"
    if kind == "types":
        document = "x: !!pairs [{password: secret-sentinel}]"
    elif kind in {"depth", "parser-depth"}:
        depth = 1000 if kind == "parser-depth" else 45
        document = "x: " + "[" * depth + "0" + "]" * depth
    else:
        lines = ["a0: &a0 {title: Small}"]
        for index in range(1, 14):
            lines.append(f"a{index}: &a{index} {{<<: [*a{index - 1}, *a{index - 1}]}}")
        document = "\n".join(lines)
    path.write_text(document)

    def unexpected_lint(*args, **kwargs):
        pytest.fail("Unbounded YAML reached lint")

    monkeypatch.setattr("sqldash.studio.sessions.lint_project", unexpected_lint)
    try:
        review = session.review()
        assert review["validation_error"]
        assert any(row["file"] == "extra.yaml" for row in review["changes"])
        assert "secret-sentinel" not in json.dumps(review)
        with pytest.raises(StudioError, match="Validation did not complete"):
            session.finish(review["revision"], undo=False)
        session.finish(review["revision"], undo=True)
        assert not path.exists()
    finally:
        studio.close()


def test_review_retains_lint_findings_for_ordinary_invalid_yaml(project):
    studio = Studio()
    session = studio.create(project, body(project))
    (project.root / "bad.yaml").write_text("x: [")
    try:
        review = session.review()
        assert review["validation_error"] is None
        assert {"file": "bad.yaml", "severity": "error"} in review["validation"]
    finally:
        studio.close()


def test_review_distinguishes_validation_failure_without_exposing_secrets(project, monkeypatch):
    def fail_validation(*args, **kwargs):
        raise RuntimeError("connection password=SECRET_SENTINEL")

    studio = Studio()
    session = studio.create(project, body(project))
    initial = project.path_for("d").read_bytes()
    try:
        project.path_for("d").write_bytes(initial.replace(b"Original", b"Agent change"))
        with monkeypatch.context() as patch:
            patch.setattr("sqldash.studio.sessions.lint_project", fail_validation)
            result = session.review()
        assert result["validation"] == []
        assert result["validation_error"] == (
            "Validation could not complete. Run sqldash lint locally for details."
        )
        assert "SECRET_SENTINEL" not in json.dumps(result)
        assert len(result["changes"]) == 1
        assert session.review()["validation_error"] is None
        session.finish(result["revision"], undo=True)
        assert project.path_for("d").read_bytes() == initial
    finally:
        studio.close()


def test_undo_refuses_post_review_edits(project):
    studio = Studio()
    session = studio.create(project, body(project))
    try:
        file = project.path_for("d")
        file.write_text(file.read_text().replace("Original", "Agent change"))
        review = session.review()
        file.write_text(file.read_text() + "description: User wrote this later\n")
        with pytest.raises(StudioError, match="changed since review"):
            session.finish(review["revision"], undo=True)
        assert "User wrote" in file.read_text()
    finally:
        studio.close()


def test_review_omits_sources_and_reports_invalid_yaml(project):
    before = snapshot(project.root)
    after = {**before, "metrics.yaml": b"source: {password: SECRET}\nmetrics: {}\n"}
    assert "SECRET" not in json.dumps(changes(before, after))
    assert "invalid YAML" in json.dumps(changes({}, {"bad.yaml": b"key: ["}))


@pytest.mark.parametrize("operation", ["added", "modified", "deleted"])
def test_css_review_omits_content_and_preserves_undo(project, operation):
    path = project.root / "theme.css"
    original = b"body{background:url(https://example.test/?token=old-secret)}"
    edited = b"body{background:url(https://example.test/?token=new-secret)}"
    if operation != "added":
        path.write_bytes(original)
    studio = Studio()
    session = studio.create(project, body(project))
    try:
        if operation == "deleted":
            path.unlink()
        else:
            path.write_bytes(edited)
        review = session.review()
        row = next(row for row in review["changes"] if row["file"] == "theme.css")
        assert row["kind"] == operation
        assert "safe text comparison is unavailable" in row["diff"]
        assert "secret" not in json.dumps(review)
        assert "background" not in json.dumps(review)
        session.finish(review["revision"], undo=True)
        if operation == "added":
            assert not path.exists()
        else:
            assert path.read_bytes() == original
    finally:
        studio.close()


def test_snapshot_rejects_symlinks_and_excludes_credentials(project, tmp_path):
    (project.root / "profiles.yaml").write_text("password: secret")
    assert "profiles.yaml" not in snapshot(project.root)
    target = tmp_path / "outside"
    target.write_text("secret")
    (project.root / "evil.yaml").symlink_to(target)
    with pytest.raises(StudioError, match="symlink"):
        snapshot(project.root)


def test_restore_only_changes_session_files(project):
    before = snapshot(project.root)
    project.path_for("d").write_text("title: Edited\n")
    after = snapshot(project.root)
    (project.root / "other.yaml").write_text("title: unrelated\n")
    restore(project.root, before, after)
    assert (project.root / "other.yaml").read_text() == "title: unrelated\n"


def test_workspace_context_uses_selected_repo(project):
    workspace = WorkspaceStore({"work": project})
    request = body(project)
    request.dashboard = "work/d"
    studio = Studio()
    session = studio.create(workspace, request)
    try:
        assert session.root == project.root
        assert session.cwd == project.root.parent
    finally:
        studio.close()


def test_create_replaces_an_unfinished_session_for_the_same_project(project):
    """Closing the owning tab used to 409 every later Open Studio. #501."""
    studio = Studio()
    try:
        first = studio.create(project, body(project))
        first_id = first.id
        second = studio.create(project, body(project))
        assert second.id != first_id
        assert first.closed
        assert first_id not in studio.sessions
        with pytest.raises(StudioNotFound):
            studio.get(first_id)
        assert studio.get(second.id) is second
    finally:
        studio.close()


def test_create_stops_the_replaced_session_process(project, entrypoints_file):
    save_entrypoint(
        AgentEntrypoint(
            name="Test",
            command=[sys.executable, "-c", "import time; time.sleep(30)", "{prompt}"],
        )
    )
    studio = Studio()
    try:
        first = studio.create(project, body(project))
        first.launch("Test")
        process = first.process
        assert process.read(0)["running"]
        second = studio.create(project, body(project))
        assert first.closed
        assert not process.read(0)["running"]
        assert second.id in studio.sessions
    finally:
        studio.close()


def test_http_create_replaces_the_previous_session(project):
    app = create_app(project.root, studio=True)
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 1234)) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        first = client.post("/api/studio/sessions", json=body(project).model_dump())
        assert first.status_code == 201, first.text
        first_id = first.json()["id"]
        second = client.post("/api/studio/sessions", json=body(project).model_dump())
        assert second.status_code == 201, second.text
        assert second.json()["id"] != first_id
        missing = client.get(f"/api/studio/sessions/{first_id}/output")
        assert missing.status_code == 404
        assert missing.json()["detail"] == "Studio session no longer exists"


class _PadSession:
    """Occupies a Studio slot without a real project. Finished, so not reclaimable."""

    def __init__(self, root):
        self.root = root
        self.finished = True
        self.closed = False

    def close(self):
        self.closed = True


def test_create_at_cap_still_replaces_the_same_project_session(project):
    """Nine other occupants + one same-root orphan: reclaiming that slot is the recovery."""
    studio = Studio()
    try:
        first = studio.create(project, body(project))
        for i in range(9):
            studio.sessions[f"pad-{i}"] = _PadSession(Path(f"/pad-{i}"))
        assert len(studio.sessions) == 10
        second = studio.create(project, body(project))
        assert first.closed
        assert first.id not in studio.sessions
        assert second.id in studio.sessions
        assert len(studio.sessions) == 10
    finally:
        studio.close()


def test_create_at_cap_does_not_close_the_project_session(project):
    """Ten other occupants: the cap 409 used to already have stopped the orphan. #501."""
    studio = Studio()
    try:
        orphan = studio.create(project, body(project))
        for i in range(10):
            studio.sessions[f"pad-{i}"] = _PadSession(Path(f"/pad-{i}"))
        with pytest.raises(StudioError, match="Close an old Studio session"):
            studio.create(project, body(project))
        assert orphan.id in studio.sessions
        assert not orphan.closed
    finally:
        studio.close()


def test_launch_checks_context_still_current(project, entrypoints_file):
    studio = Studio()
    session = studio.create(project, body(project))
    try:
        project.path_for("d").write_text("title: Concurrent\n")
        with pytest.raises(StudioError, match="Project changed"):
            session.launch("Default agent")
    finally:
        studio.close()


def test_headless_process_streams_output_has_no_stdin_and_cancels(tmp_path):
    from sqldash.studio.process import AgentProcess

    process = AgentProcess(
        [
            sys.executable,
            "-c",
            'import sys; print("READY", flush=True); print(repr(sys.stdin.read()))',
        ],
        tmp_path,
        os.environ.copy(),
    )
    output = wait_finished(process)
    assert "READY" in output
    assert "''" in output
    process.stop()
    process = AgentProcess(
        [sys.executable, "-c", "import time; time.sleep(60)"], tmp_path, os.environ.copy()
    )
    process.stop()
    assert not process.read(0)["running"]
    assert process.read(0)["cancelled"]


def test_api_requires_opt_in_token_origin_and_local_peer(project, entrypoints_file):
    app = create_app(project.root, studio=True)
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 1234)) as client:
        assert client.get("/api/studio/entrypoints").status_code == 403
        client.headers["X-Sqldash-Token"] = app.state.api_token
        assert client.get("/api/studio/entrypoints").status_code == 200
        assert (
            client.get(
                "/api/studio/entrypoints", headers={"Origin": "https://evil.test"}
            ).status_code
            == 403
        )
        response = client.post("/api/studio/sessions", json=body(project).model_dump())
        assert response.status_code == 201, response.text
        id = response.json()["id"]
        assert client.post(f"/api/studio/sessions/{id}/review").status_code == 200
        assert client.delete(f"/api/studio/sessions/{id}").status_code == 204
    with TestClient(app, base_url="http://localhost", client=("192.0.2.1", 1234)) as remote:
        remote.headers["X-Sqldash-Token"] = app.state.api_token
        assert remote.get("/api/studio/entrypoints").status_code == 403
    with pytest.raises(ValueError, match="loopback"):
        create_app(project.root, studio=True, allowed_hosts=["0.0.0.0"])
    app = create_app(project.root)
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 1234)) as client:
        assert client.get("/api/studio/entrypoints").status_code == 404


def test_discovery_needs_no_config_and_custom_names_override(entrypoints_file, monkeypatch):
    from sqldash.studio.entrypoints import available_entrypoints

    monkeypatch.setattr("sqldash.studio.entrypoints.shutil.which", lambda name: "/bin/" + name)
    found = available_entrypoints()
    assert found["Claude Code"].command == [
        "/bin/claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "{prompt}",
    ]
    assert found["Codex"].command == [
        "/bin/codex",
        "exec",
        "--skip-git-repo-check",
        "--json",
        "{prompt}",
    ]
    assert not entrypoints_file.exists()
    save_entrypoint(
        AgentEntrypoint(name="Claude Code", command=["claude-custom", "-p", "{prompt}"])
    )
    assert available_entrypoints()["Claude Code"].command[0] == "claude-custom"
    monkeypatch.setattr("sqldash.studio.entrypoints.shutil.which", lambda name: None)
    assert list(available_entrypoints()) == ["Claude Code"]


def test_browser_entrypoint_configuration_requires_local_authorization(project, entrypoints_file):
    app = create_app(project.root, studio=True)
    entrypoint = {
        "name": "Custom agent",
        "command": ["claude-custom", "-p", "{prompt}"],
        "shell": "/bin/zsh",
    }
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 1234)) as client:
        assert client.post("/api/studio/entrypoints", json=entrypoint).status_code == 403
        assert not entrypoints_file.exists()
        client.headers["X-Sqldash-Token"] = app.state.api_token
        assert (
            client.post(
                "/api/studio/entrypoints", json=entrypoint, headers={"Origin": "https://evil.test"}
            ).status_code
            == 403
        )
        assert not entrypoints_file.exists()
        assert (
            client.post(
                "/api/studio/entrypoints", json=entrypoint, headers={"Origin": "http://localhost"}
            ).status_code
            == 201
        )
        assert load_entrypoints()["Custom agent"].command[0] == "claude-custom"
        assert entrypoints_file.stat().st_mode & 0o777 == 0o600
        entrypoint["command"][0] = "claude; touch unexpected"
        assert client.post("/api/studio/entrypoints", json=entrypoint).status_code == 422
        assert load_entrypoints()["Custom agent"].command[0] == "claude-custom"


def test_ui_save_keeps_hand_edited_environment_unless_sent(project, entrypoints_file):
    app = create_app(project.root, studio=True)
    save_entrypoint(
        AgentEntrypoint(
            name="Custom agent",
            command=["claude-custom", "-p", "{prompt}"],
            pass_env=["ANTHROPIC_API_KEY"],
            env={"AGENT_MODE": "custom"},
        )
    )
    ui_save = {
        "name": "Custom agent",
        "command": ["claude-custom", "-p", "{prompt}"],
        "protocol": "claude",
        "shell": None,
    }
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 1234)) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        assert client.post("/api/studio/entrypoints", json=ui_save).status_code == 201
        saved = load_entrypoints()["Custom agent"]
        assert saved.protocol == "claude"
        assert saved.pass_env == ["ANTHROPIC_API_KEY"]
        assert saved.env == {"AGENT_MODE": "custom"}
        assert json.loads(entrypoints_file.read_text())[0]["pass_env"] == ["ANTHROPIC_API_KEY"]
        cleared = {**ui_save, "pass_env": [], "env": {}}
        assert client.post("/api/studio/entrypoints", json=cleared).status_code == 201
        saved = load_entrypoints()["Custom agent"]
        assert saved.pass_env == []
        assert saved.env == {}
        fresh = {**ui_save, "name": "Other agent"}
        assert client.post("/api/studio/entrypoints", json=fresh).status_code == 201
        assert load_entrypoints()["Other agent"].pass_env == []


def test_closed_session_cannot_launch_or_review(project, entrypoints_file, monkeypatch):
    studio = Studio()
    session = studio.create(project, body(project))
    session.close()
    with pytest.raises(StudioError, match="closed"):
        session.launch("Anything")
    with pytest.raises(StudioError, match="closed"):
        session.review()
    assert session.process is None
    assert not Path(session.directory.name).exists()


def test_agent_exit_stops_background_descendants(tmp_path):
    from sqldash.studio.process import AgentProcess

    marker = tmp_path / "background-write"
    process = AgentProcess(
        [
            sys.executable,
            "-c",
            "import os,time; "
            "pid=os.fork(); "
            "os._exit(0) if pid else None; "
            "time.sleep(0.8); "
            f"open({str(marker)!r}, 'w').write('escaped'); "
            "time.sleep(10)",
        ],
        tmp_path,
        os.environ.copy(),
    )
    try:
        wait_finished(process)
        time.sleep(1)
        assert not marker.exists()
    finally:
        process.stop()


def test_snapshot_does_not_follow_a_file_swapped_for_a_symlink(project, tmp_path, monkeypatch):
    target = tmp_path / "outside.yaml"
    target.write_text("secret outside the project")
    original_open = os.open
    dashboard_path = project.path_for("d")

    def swap(name, flags, *args, **kwargs):
        if name == "d.yaml":
            dashboard_path.unlink()
            dashboard_path.symlink_to(target)
        return original_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap)
    with pytest.raises(StudioError, match="symlink"):
        snapshot(project.root)


def test_snapshot_rejects_fifo_without_blocking(project):
    os.mkfifo(project.root / "pipe.yaml")
    with pytest.raises(StudioError, match="regular"):
        snapshot(project.root)


def test_undo_pins_directory_before_path_swap(project, tmp_path, monkeypatch):
    before = snapshot(project.root)
    project.path_for("d").write_text("title: Edited\n")
    after = snapshot(project.root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "d.yaml").write_text("untouched")
    moved = tmp_path / "moved-project"
    original_open = os.open

    def swap(name, flags, *args, **kwargs):
        if flags & os.O_CREAT and not moved.exists():
            project.root.rename(moved)
            project.root.symlink_to(outside, target_is_directory=True)
        return original_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap)
    restore(project.root, before, after)
    assert (outside / "d.yaml").read_text() == "untouched"
    assert (moved / "d.yaml").read_bytes() == before["d.yaml"]


def test_review_bounds_recursive_and_expanding_yaml_aliases():
    from sqldash.studio.review import safe_document

    recursive = safe_document(b"x: &x [*x]\n")
    assert "Repeated YAML alias omitted" in recursive
    lines = ["a0: &a0 [hello]"]
    for index in range(1, 25):
        lines.append(f"a{index}: &a{index} [*a{index - 1}, *a{index - 1}]")
    result = safe_document("\n".join(lines).encode())
    assert len(result) < 10000


def test_studio_prevents_cross_origin_framing(project):
    app = create_app(project.root, studio=True)
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 1234)) as client:
        response = client.get("/d/d")
        assert response.status_code == 200
        assert "frame-ancestors 'self'" in response.headers["Content-Security-Policy"]
        assert response.headers["X-Frame-Options"] == "SAMEORIGIN"


@pytest.mark.parametrize("operation", ["close", "stop"])
def test_cancellation_waits_for_inflight_launch(project, entrypoints_file, monkeypatch, operation):
    save_entrypoint(
        AgentEntrypoint(
            name="Test", command=[sys.executable, "-c", "import time; time.sleep(30)", "{prompt}"]
        )
    )
    studio = Studio()
    session = studio.create(project, body(project))
    checking = threading.Event()
    release = threading.Event()
    closing = threading.Event()
    closed = threading.Event()

    def check(self):
        checking.set()
        assert release.wait(5)

    def close():
        closing.set()
        getattr(session, operation)()
        closed.set()

    monkeypatch.setattr(AgentEntrypoint, "check", check)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            launch = pool.submit(session.launch, "Test")
            assert checking.wait(5)
            cleanup = pool.submit(close)
            assert closing.wait(5)
            closed.wait(0.1)
            release.set()
            launch.result(timeout=5)
            cleanup.result(timeout=5)
        assert session.process.read(0)["cancelled"]
        assert not session.process.read(0)["running"]
        assert Path(session.directory.name).exists() == (operation == "stop")
    finally:
        release.set()
        studio.close()


def test_every_studio_endpoint_rejects_cross_origin_remote_and_rebound_requests(project):
    app = create_app(project.root, studio=True)
    routes = [
        ("GET", "/entrypoints", None),
        ("POST", "/entrypoints/check", {"entrypoint": "Test"}),
        ("POST", "/sessions", body(project).model_dump()),
        ("POST", "/sessions/unknown/launch", {"entrypoint": "Test"}),
        ("GET", "/sessions/unknown/output", None),
        ("POST", "/sessions/unknown/stop", None),
        ("POST", "/sessions/unknown/review", None),
        ("POST", "/sessions/unknown/finish", {"revision": "abc", "undo": True}),
        ("DELETE", "/sessions/unknown", None),
    ]
    cases = [
        ("127.0.0.1", "http://localhost", {}),
        (
            "127.0.0.1",
            "http://localhost",
            {"Origin": "https://attacker.invalid", "X-Sqldash-Token": app.state.api_token},
        ),
        (
            "192.0.2.1",
            "http://localhost",
            {"X-Sqldash-Token": app.state.api_token, "X-Forwarded-For": "127.0.0.1"},
        ),
        ("127.0.0.1", "http://attacker.invalid", {"X-Sqldash-Token": app.state.api_token}),
    ]
    for peer, url, headers in cases:
        with TestClient(app, base_url=url, client=(peer, 1234)) as client:
            for method, route, payload in routes:
                response = client.request(
                    method, "/api/studio" + route, headers=headers, json=payload
                )
                assert response.status_code == 403, (peer, route, response.text)


def test_changed_unparseable_documents_are_not_reported_as_formatting():
    result = changes({"broken.yaml": b"x: [before"}, {"broken.yaml": b"x: [after"})
    assert "Content changed" in result[0]["diff"]
    assert "Formatting" not in result[0]["diff"]
    assert "before" not in result[0]["diff"]


@pytest.mark.parametrize(
    ("options", "command", "message"),
    [
        (["--env", "TOKEN_WITHOUT_EQUALS"], ["claude", "{prompt}"], "KEY=VALUE"),
        (["--shell", "/bin/sh"], ["claude", "{prompt}"], "absolute path to bash or zsh"),
        ([], ["claude"], "separate {prompt}"),
        ([], ["", "{prompt}"], "nonempty"),
    ],
)
def test_entrypoint_cli_reports_reason_without_credentials(
    entrypoints_file, options, command, message
):
    result = CliRunner().invoke(
        cli,
        ["studio", "add", "Example", "--env", "API_TOKEN=private-value", *options, "--", *command],
    )
    assert result.exit_code == 1
    assert message in result.output
    assert "private-value" not in result.output
    assert "TOKEN_WITHOUT_EQUALS" not in result.output
    assert not entrypoints_file.exists()


def test_missing_studio_resources_are_404_but_unstarted_session_is_409(project, entrypoints_file):
    app = create_app(project.root, studio=True)
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 1234)) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        response = client.post("/api/studio/sessions", json=body(project).model_dump())
        assert response.status_code == 201
        session = response.json()["id"]
        assert client.get("/api/studio/sessions/missing/output").status_code == 404
        assert (
            client.post("/api/studio/entrypoints/check", json={"entrypoint": "missing"}).status_code
            == 404
        )
        assert (
            client.post(
                f"/api/studio/sessions/{session}/launch", json={"entrypoint": "missing"}
            ).status_code
            == 404
        )
        assert client.get(f"/api/studio/sessions/{session}/output").status_code == 409
        assert client.delete(f"/api/studio/sessions/{session}").status_code == 204
        assert client.post(f"/api/studio/sessions/{session}/review").status_code == 404


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
def test_process_signals_only_before_reaping(tmp_path, monkeypatch, fallback, cancel):
    from sqldash.studio.process import AgentProcess

    if fallback and not hasattr(select, "kqueue"):
        pytest.skip("kqueue is the older macOS compatibility path")
    if fallback:
        monkeypatch.setattr(os, "waitid", None, raising=False)
    events = []
    original_kill = os.killpg
    original_wait = subprocess.Popen.wait
    original_poll = subprocess.Popen.poll

    def kill(pid, sig):
        assert "reaped" not in events
        events.append(sig)
        return original_kill(pid, sig)

    def wait(process, *args, **kwargs):
        if process.args[0] != sys.executable:
            return original_wait(process, *args, **kwargs)
        assert signal.SIGKILL in events
        result = original_wait(process, *args, **kwargs)
        events.append("reaped")
        return result

    def poll(process):
        if process.args[0] != sys.executable:
            return original_poll(process)
        raise AssertionError("poll() may reap the reserved leader")

    monkeypatch.setattr(os, "killpg", kill)
    monkeypatch.setattr(subprocess.Popen, "wait", wait)
    monkeypatch.setattr(subprocess.Popen, "poll", poll)
    script = "import time; time.sleep(30)" if cancel else "print('finished')"
    process = AgentProcess([sys.executable, "-c", script], tmp_path, os.environ.copy())
    try:
        if cancel:
            process.stop()
        output = wait_finished(process)
        assert process.read(0)["returncode"] is not None
        if not cancel:
            assert "finished" in output
        assert events[-1] == "reaped"
        process.stop()
        assert events[-1] == "reaped"
    finally:
        process.stop()


def test_agent_output_tail_survives_exit(tmp_path):
    from sqldash.studio.process import AgentProcess

    process = AgentProcess(
        [sys.executable, "-c", "print('x' * 300000); print('LAST LINE')"],
        tmp_path,
        os.environ.copy(),
    )
    try:
        assert wait_finished(process).endswith("LAST LINE\n")
    finally:
        process.stop()


@pytest.mark.parametrize(("states", "allowed"), [("Z\n", True), ("S\n", False), ("", False)])
def test_macos_permission_error_requires_a_zombie_only_group(monkeypatch, states, allowed):
    from types import SimpleNamespace

    from sqldash.studio.process import AgentProcess

    process = object.__new__(AgentProcess)
    process.process = SimpleNamespace(pid=12345)
    monkeypatch.setattr(sys, "platform", "darwin")

    def denied(*args):
        raise PermissionError("signal refused")

    monkeypatch.setattr(os, "killpg", denied)
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=states)
    )
    if allowed:
        process._signal(signal.SIGKILL)
    else:
        with pytest.raises(PermissionError):
            process._signal(signal.SIGKILL)


@pytest.mark.parametrize("failure", ["prepare", "replace", "remove", "recovery"])
def test_undo_recovers_from_filesystem_failures(project, monkeypatch, failure):
    root = project.root
    (root / "a.yaml").write_text("title: Before\n")
    (root / "b.yaml").write_text("title: Deleted\n")
    studio = Studio()
    session = studio.create(project, body(project))
    baseline = snapshot(root)
    (root / "a.yaml").write_text("title: Edited\n")
    (root / "b.yaml").unlink()
    (root / "c.yaml").write_text("title: Added\n")
    reviewed = snapshot(root)
    result = session.review()
    real_open, real_replace, real_unlink = os.open, os.replace, os.unlink
    creates = 0

    def fail_open(name, flags, *args, **kwargs):
        nonlocal creates
        if flags & os.O_CREAT:
            creates += 1
            if failure == "prepare" and creates == 2:
                raise OSError("SECRET_SENTINEL disk full")
        return real_open(name, flags, *args, **kwargs)

    def fail_replace(src, dst, **kwargs):
        if failure in {"replace", "recovery"} and dst == "b.yaml":
            raise OSError("SECRET_SENTINEL replace failure")
        if failure == "recovery" and dst == "a.yaml" and (root / dst).read_bytes() == baseline[dst]:
            raise OSError("SECRET_SENTINEL rollback failure")
        return real_replace(src, dst, **kwargs)

    def fail_unlink(name, **kwargs):
        if failure == "remove" and name == "c.yaml":
            raise OSError("SECRET_SENTINEL unlink failure")
        return real_unlink(name, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "open", fail_open)
            patch.setattr(os, "replace", fail_replace)
            patch.setattr(os, "unlink", fail_unlink)
            with pytest.raises(StudioError) as error:
                session.finish(result["revision"], undo=True)
        assert "SECRET_SENTINEL" not in str(error.value)
        assert not session.finished
        assert not list(root.glob(".studio-*"))
        if failure == "recovery":
            assert "recovery was incomplete" in str(error.value)
            assert snapshot(root) != reviewed
            result = session.review()
        else:
            assert snapshot(root) == reviewed
            assert "Retry undo" in str(error.value)
        session.finish(result["revision"], undo=True)
        assert snapshot(root) == baseline
        assert session.finished
    finally:
        studio.close()


@pytest.mark.parametrize("cancel", [False, True])
def test_signal_failure_surfaces_and_session_can_close(
    project, entrypoints_file, monkeypatch, cancel
):
    from sqldash.studio.process import AgentProcess

    ready = threading.Event()
    release = project.root / "release.txt"
    script = (
        "from pathlib import Path; import time\n"
        f"while not Path({str(release)!r}).exists(): time.sleep(0.01)\n"
    )
    save_entrypoint(
        AgentEntrypoint(name="Test", command=[sys.executable, "-c", script, "{prompt}"])
    )
    original_signal = AgentProcess._signal

    def denied(process, sig):
        ready.set()
        raise PermissionError("SECRET_SENTINEL")

    app = create_app(project.root, studio=True)
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 1234)) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        id = client.post("/api/studio/sessions", json=body(project).model_dump()).json()["id"]
        prefix = f"/api/studio/sessions/{id}"
        monkeypatch.setattr(AgentProcess, "_signal", denied)
        try:
            assert client.post(prefix + "/launch", json={"entrypoint": "Test"}).status_code == 200
            if cancel:
                response = client.post(prefix + "/stop")
                assert response.status_code == 409
                assert "Could not stop" in response.text
            release.touch()
            assert ready.wait(5)
            deadline = time.monotonic() + 5
            while True:
                result = client.get(prefix + "/output").json()
                if not result["running"]:
                    break
                assert time.monotonic() < deadline
                time.sleep(0.01)
            assert result["returncode"] == 0
            assert "Could not stop" in result["error"]
            assert "SECRET_SENTINEL" not in json.dumps(result)
            assert client.post(prefix + "/review").status_code == 409
            assert client.post(prefix + "/stop").status_code == 409
            monkeypatch.setattr(
                AgentProcess, "_signal", lambda *args: pytest.fail("signal after reap")
            )
            assert client.delete(prefix).status_code == 204
            assert (
                client.post("/api/studio/sessions", json=body(project).model_dump()).status_code
                == 201
            )
        finally:
            release.touch()
            monkeypatch.setattr(AgentProcess, "_signal", original_signal)


def test_launch_receives_context_directly_without_reading_a_file(project, entrypoints_file):
    save_entrypoint(
        AgentEntrypoint(
            name="Test",
            command=[
                sys.executable,
                "-c",
                "import sys; assert 'Make this blue' in sys.argv[1]; "
                "assert 'request.txt' not in sys.argv[1]; print('context received')",
                "{prompt}",
            ],
        )
    )
    studio = Studio()
    try:
        session = studio.create(project, body(project))
        session.launch("Test")
        assert "context received" in wait_finished(session.process)
        assert session.process.read(0)["returncode"] == 0
    finally:
        studio.close()


def test_oversized_prompt_is_rejected_before_spawn(project, entrypoints_file, monkeypatch):
    save_entrypoint(AgentEntrypoint(name="Test", command=[sys.executable, "{prompt}"]))
    studio = Studio()
    try:
        session = studio.create(project, body(project))
        session.prompt = "界" * 40000
        monkeypatch.setattr(
            "sqldash.studio.sessions.AgentProcess", lambda *a: pytest.fail("spawned")
        )
        with pytest.raises(StudioError, match="100 kB"):
            session.launch("Test")
    finally:
        studio.close()


def test_conversation_continues_and_undo_only_restores_latest_turn(
    project, entrypoints_file, monkeypatch
):
    script = project.root / "conversation_agent.py"
    script.write_text("""
import json,sys
from pathlib import Path
identity='00000000-0000-4000-8000-000000000001'
init=json.loads(sys.stdin.readline())
print(json.dumps({'type':'control_response','response':{
 'subtype':'success','request_id':init['request_id'],'response':{}}}),flush=True)
print(json.dumps({'type':'system','subtype':'init','session_id':identity}),flush=True)
prompt=json.loads(sys.stdin.readline())['message']['content']
resumed='--resume='+identity in sys.argv
assert resumed == ('Second' in prompt or 'Third' in prompt)
if 'Third' in prompt: assert 'previous turn' in prompt and 'undone' in prompt
value='Third' if 'Third' in prompt else 'Second' if resumed else 'First'
print(json.dumps({'type':'control_request','request_id':'write','request':{
 'subtype':'can_use_tool','tool_name':'Write','input':{'title':value}}}),flush=True)
answer=json.loads(sys.stdin.readline())['response']['response']
assert answer['behavior']=='allow'
path=Path('.sqldash/d.yaml')
text=path.read_text()
text=text.replace('title: Original','title: '+value).replace('title: First','title: '+value)
path.write_text(text)
print(json.dumps({'type':'result','subtype':'success'}),flush=True)
sys.stdin.read()
""")
    save_entrypoint(
        AgentEntrypoint(
            name="Conversation",
            protocol="claude",
            command=[sys.executable, str(script), "{prompt}"],
        )
    )
    studio = Studio()
    try:
        session = studio.create(project, body(project))
        session.set_auto_approve(True)
        session.launch("Conversation")
        wait_finished(session.process)
        path = project.root / "d.yaml"
        first = path.read_bytes()
        assert b"title: First" in first
        first_process = session.process
        first_review = session.review()
        with monkeypatch.context() as patch:

            def cannot_spawn(*args, **kwargs):
                raise OSError("spawn failed")

            patch.setattr("sqldash.studio.sessions.AgentProcess", cannot_spawn)
            with pytest.raises(StudioError, match="Could not start"):
                session.next_turn(project, body(project))
        assert session.process is first_process
        assert session.turn == 1
        assert session.review()["revision"] == first_review["revision"]
        session.next_turn(
            project,
            SessionRequest(
                dashboard="d", etag=project.load("d")[2], annotations=[{"note": "Second"}]
            ),
        )
        wait_finished(session.process)
        assert session.process is not first_process
        assert b"title: Second" in path.read_bytes()
        assert session.turn == 2
        result = session.review()
        session.undo_turn(result["revision"])
        assert path.read_bytes() == first
        assert not session.finished
        with pytest.raises(StudioError, match="already undone"):
            session.undo_turn(result["revision"])
        session.next_turn(
            project,
            SessionRequest(
                dashboard="d", etag=project.load("d")[2], annotations=[{"note": "Third"}]
            ),
        )
        wait_finished(session.process)
        assert b"title: Third" in path.read_bytes()
        session.set_auto_approve(False)
        assert not session.auto_approve
    finally:
        studio.close()


def test_auto_approval_api_requires_explicit_local_authenticated_choice(project):
    app = create_app(project.root, studio=True)
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 1234)) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        response = client.post("/api/studio/sessions", json=body(project).model_dump())
        session = app.state.studio.get(response.json()["id"])
        route = f"/api/studio/sessions/{session.id}/permission-mode"
        assert not session.auto_approve
        for value in ["true", 1, None]:
            assert client.post(route, json={"auto_approve": value}).status_code == 422
        assert client.post(route, json={"auto_approve": True, "always": True}).status_code == 422
        assert (
            client.post(
                route, json={"auto_approve": True}, headers={"Origin": "https://evil.test"}
            ).status_code
            == 403
        )
        assert (
            client.post(
                route, json={"auto_approve": True}, headers={"X-Sqldash-Token": ""}
            ).status_code
            == 403
        )
        assert not session.auto_approve
        assert client.post(route, json={"auto_approve": True}).status_code == 200
        assert session.auto_approve
        assert client.post(route, json={"auto_approve": False}).status_code == 200
        assert not session.auto_approve


def test_redacted_field_changes_are_content_not_formatting():
    shape = b"title: T\nsource: {type: duckdb, database: %s, password: %s}\n"
    secret = changes(
        {"d.yaml": shape % (b"a", b"OLD_SECRET")}, {"d.yaml": shape % (b"a", b"NEW_SECRET")}
    )
    assert "Content changed inside redacted fields" in secret[0]["diff"]
    assert "Formatting" not in secret[0]["diff"]
    assert "OLD_SECRET" not in json.dumps(secret)
    assert "NEW_SECRET" not in json.dumps(secret)
    database = changes({"d.yaml": shape % (b"a", b"x")}, {"d.yaml": shape % (b"b", b"x")})
    assert "Content changed inside redacted fields" in database[0]["diff"]
    formatting = changes(
        {"d.yaml": b"title: T\nsource: {type: duckdb, database: x}\n"},
        {"d.yaml": b"# note\ntitle: \"T\"\nsource: {database: x, type: 'duckdb'}\n"},
    )
    assert "Formatting-only change" in formatting[0]["diff"]
    assert "Content changed" not in formatting[0]["diff"]
    typed = changes({"d.yaml": b"source: {port: 1}\n"}, {"d.yaml": b"source: {port: '1'}\n"})
    assert "Content changed inside redacted fields" in typed[0]["diff"]


def test_accept_refuses_incomplete_validation_but_undo_still_works(project, monkeypatch):
    def fail_validation(*args, **kwargs):
        raise RuntimeError("connection password=SECRET_SENTINEL")

    initial = project.path_for("d").read_bytes()
    app = create_app(project.root, studio=True)
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 1234)) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        id = client.post("/api/studio/sessions", json=body(project).model_dump()).json()["id"]
        prefix = f"/api/studio/sessions/{id}"
        project.path_for("d").write_bytes(initial.replace(b"Original", b"Agent change"))
        with monkeypatch.context() as patch:
            patch.setattr("sqldash.studio.sessions.lint_project", fail_validation)
            review = client.post(prefix + "/review").json()
        assert review["validation_error"]
        accept = client.post(prefix + "/finish", json={"revision": review["revision"]})
        assert accept.status_code == 409
        assert "Validation did not complete" in accept.json()["detail"]
        assert "SECRET_SENTINEL" not in accept.text
        assert b"Agent change" in project.path_for("d").read_bytes()
        assert not app.state.studio.get(id).finished
        rechecked = client.post(prefix + "/review").json()
        assert rechecked["validation_error"] is None
        assert rechecked["revision"] == review["revision"]
        with monkeypatch.context() as patch:
            patch.setattr("sqldash.studio.sessions.lint_project", fail_validation)
            client.post(prefix + "/review")
        undo = client.post(prefix + "/finish", json={"revision": review["revision"], "undo": True})
        assert undo.status_code == 200
        assert undo.json() == {
            "ok": True,
            "undone": True,
            "validation_error": review["validation_error"],
        }
        assert project.path_for("d").read_bytes() == initial
        assert app.state.studio.get(id).finished


def test_accept_after_recheck_echoes_clean_validation(project):
    studio = Studio()
    session = studio.create(project, body(project))
    try:
        file = project.path_for("d")
        file.write_bytes(file.read_bytes().replace(b"Original", b"Agent change"))
        result = session.finish(session.review()["revision"], undo=False)
        assert result == {"undone": False, "validation_error": None}
        assert session.finished
        assert b"Agent change" in file.read_bytes()
    finally:
        studio.close()


def test_stop_reports_a_leader_that_ignored_signals(project, entrypoints_file, monkeypatch):
    from sqldash.studio.process import AgentProcess

    save_entrypoint(
        AgentEntrypoint(
            name="Test",
            command=[sys.executable, "-c", "import time; time.sleep(30)", "{prompt}"],
        )
    )
    original_signal = AgentProcess._signal
    app = create_app(project.root, studio=True)
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 1234)) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        id = client.post("/api/studio/sessions", json=body(project).model_dump()).json()["id"]
        prefix = f"/api/studio/sessions/{id}"
        assert client.post(prefix + "/launch", json={"entrypoint": "Test"}).status_code == 200
        monkeypatch.setattr("sqldash.studio.process.STOP_TIMEOUT", 0.05)
        try:
            monkeypatch.setattr(AgentProcess, "_signal", lambda self, sig: None)
            response = client.post(prefix + "/stop")
            assert response.status_code == 200
            assert response.json() == {"ok": False, "running": True}
            output = client.get(prefix + "/output").json()
            assert output["running"]
            assert output["cancelled"]
            assert output["error"] is None
            assert client.post(prefix + "/review").status_code == 409
        finally:
            monkeypatch.setattr(AgentProcess, "_signal", original_signal)
            monkeypatch.setattr("sqldash.studio.process.STOP_TIMEOUT", 2.0)
        assert client.post(prefix + "/stop").json() == {"ok": True, "running": False}
        assert not client.get(prefix + "/output").json()["running"]
        assert client.delete(prefix).status_code == 204


@pytest.mark.parametrize("special", [False, True])
def test_entrypoint_config_rejects_large_and_special_files(entrypoints_file, special):
    entrypoints_file.parent.mkdir()
    if special:
        os.mkfifo(entrypoints_file)
    else:
        with entrypoints_file.open("wb") as stream:
            stream.truncate(10 * 1024 * 1024 * 1024)
    with pytest.raises(StudioError, match="Invalid Studio entrypoints file"):
        load_entrypoints()


def test_oversized_entrypoint_save_preserves_previous_config(entrypoints_file):
    save_entrypoint(AgentEntrypoint(name="Existing", command=["claude", "{prompt}"]))
    before = entrypoints_file.read_bytes()
    with pytest.raises(StudioError, match="exceeds 1 MiB"):
        save_entrypoint(
            AgentEntrypoint(
                name="Large", command=["claude", "{prompt}"], env={"VALUE": "x" * (1024 * 1024)}
            )
        )
    assert entrypoints_file.read_bytes() == before
    assert set(load_entrypoints()) == {"Existing"}
