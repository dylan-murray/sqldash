import hashlib
import subprocess

import pytest
from helpers import git_http_server

from sqldash.gitrepo import GitError, clone_or_pull, is_git_url, repo_cache_dir
from sqldash.workspace import resolve_workspace

DASH = """\
title: Team Dashboard
source: {type: duckdb, database: ':memory:'}
queries: {q: 'SELECT 1 AS n'}
tiles:
  - {id: w, query: q}
"""


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def remote(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    git("init", "-b", "main", cwd=work)
    git("config", "user.email", "t@t", cwd=work)
    git("config", "user.name", "t", cwd=work)
    (work / "team.yaml").write_text(DASH)
    git("add", "-A", cwd=work)
    git("commit", "-m", "init", cwd=work)
    bare = tmp_path / "central.git"
    git("clone", "--bare", str(work), str(bare), cwd=tmp_path)
    git("remote", "add", "origin", str(bare), cwd=work)
    return work, f"file://{bare}"


def test_is_git_url():
    assert is_git_url("git@github.com:acme/dashboards.git")
    assert is_git_url("https://github.com/acme/dashboards")
    assert is_git_url("https://example.com/x/dashboards.git")
    assert not is_git_url("./local/dir")
    assert not is_git_url("/abs/path")
    assert not is_git_url("demo.yaml")


def test_cache_dir_is_stable_and_distinct():
    a = repo_cache_dir("https://github.com/acme/dash.git")
    b = repo_cache_dir("https://github.com/other/dash.git")
    assert a == repo_cache_dir("https://github.com/acme/dash.git")
    assert a != b
    assert a.name.startswith("dash-")


def test_cache_dir_splits_branches_and_keeps_the_no_branch_name():
    url = "https://github.com/acme/dash.git"
    legacy = f"dash-{hashlib.sha256(url.encode()).hexdigest()[:10]}"
    assert repo_cache_dir(url).name == legacy
    feat = repo_cache_dir(url, "feat/x")
    assert feat.name.startswith("dash-feat-x-")
    assert feat != repo_cache_dir(url)
    assert feat != repo_cache_dir(url, "feat-x")


def test_clone_then_pull_updates(remote, tmp_path):
    work, url = remote
    cache = tmp_path / "cache"

    clone = clone_or_pull(url, cache_root=cache)
    assert (clone / "team.yaml").read_text() == DASH

    (work / "team.yaml").write_text(DASH.replace("Team Dashboard", "Team Dashboard v2"))
    git("commit", "-am", "update", cwd=work)
    git("push", "origin", "main", cwd=work)

    clone2 = clone_or_pull(url, cache_root=cache)
    assert clone2 == clone
    assert "Team Dashboard v2" in (clone / "team.yaml").read_text()


def test_diverged_pull_fails_and_names_cache_dir(remote, tmp_path):
    work, url = remote
    cache = tmp_path / "cache"
    clone = clone_or_pull(url, cache_root=cache)

    git("config", "user.email", "t@t", cwd=clone)
    git("config", "user.name", "t", cwd=clone)
    (clone / "team.yaml").write_text(DASH.replace("Team Dashboard", "Local edit"))
    git("commit", "-am", "local divergence", cwd=clone)

    (work / "team.yaml").write_text(DASH.replace("Team Dashboard", "Remote edit"))
    git("commit", "-am", "remote divergence", cwd=work)
    git("push", "origin", "main", cwd=work)

    with pytest.raises(GitError) as exc:
        clone_or_pull(url, cache_root=cache)
    assert str(clone) in str(exc.value)


def _push_branch(work, branch, title):
    git("checkout", "-b", branch, cwd=work)
    (work / "team.yaml").write_text(DASH.replace("Team Dashboard", title))
    git("commit", "-am", title, cwd=work)
    git("push", "origin", branch, cwd=work)
    git("checkout", "main", cwd=work)


def _stdout(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True).stdout.strip()


def test_branch_after_default_gets_its_own_clone(remote, tmp_path):
    """#510: `--branch feat` on a URL already served without it reused the
    single-branch shallow clone of main and died with a raw pathspec error."""
    work, url = remote
    cache = tmp_path / "cache"
    _push_branch(work, "feat", "Feat Dashboard")

    main = clone_or_pull(url, cache_root=cache)
    feat = clone_or_pull(url, branch="feat", cache_root=cache)
    assert feat != main
    assert "Feat Dashboard" in (feat / "team.yaml").read_text()
    assert "Team Dashboard" in (main / "team.yaml").read_text()
    assert _stdout("symbolic-ref", "--short", "HEAD", cwd=main) == "main"
    assert _stdout("rev-parse", "--is-shallow-repository", cwd=feat) == "true"


def test_workspace_entries_on_one_url_serve_their_own_branches(remote, tmp_path, monkeypatch):
    work, url = remote
    _push_branch(work, "feat", "Feat Dashboard")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))

    resolved = dict(resolve_workspace({"a": {"url": url}, "b": {"url": url, "branch": "feat"}}))
    assert resolved["a"] != resolved["b"]
    assert resolved["b"] == repo_cache_dir(url, "feat")
    assert "Team Dashboard" in (resolved["a"] / "team.yaml").read_text()
    assert "Feat Dashboard" in (resolved["b"] / "team.yaml").read_text()

    resolved = dict(resolve_workspace({"b": {"url": url, "branch": "feat"}, "a": {"url": url}}))
    assert "Team Dashboard" in (resolved["a"] / "team.yaml").read_text()
    assert "Feat Dashboard" in (resolved["b"] / "team.yaml").read_text()


def test_legacy_branch_clone_in_default_folder_returns_to_remote_default(remote, tmp_path):
    """Before #510 a cold `--branch feat` clone lived in the no-branch folder;
    a plain serve must move it back to the remote default, not keep serving feat."""
    work, url = remote
    cache = tmp_path / "cache"
    _push_branch(work, "feat", "Feat Dashboard")
    legacy = repo_cache_dir(url, cache_root=cache)
    git("clone", "--depth", "1", "--branch", "feat", url, str(legacy), cwd=tmp_path)

    assert clone_or_pull(url, cache_root=cache) == legacy
    assert _stdout("symbolic-ref", "--short", "HEAD", cwd=legacy) == "main"
    assert "Team Dashboard" in (legacy / "team.yaml").read_text()


def test_legacy_switch_refuses_to_carry_uncommitted_edits(remote, tmp_path):
    work, url = remote
    cache = tmp_path / "cache"
    _push_branch(work, "feat", "Feat Dashboard")
    legacy = repo_cache_dir(url, cache_root=cache)
    git("clone", "--depth", "1", "--branch", "feat", url, str(legacy), cwd=tmp_path)
    (legacy / "new.yaml").write_text(DASH)

    with pytest.raises(GitError) as exc:
        clone_or_pull(url, cache_root=cache)
    assert "uncommitted changes on 'feat'" in str(exc.value)
    assert str(legacy) in str(exc.value)
    assert _stdout("symbolic-ref", "--short", "HEAD", cwd=legacy) == "feat"
    assert (legacy / "new.yaml").exists()


def test_pull_on_branch_clone_fast_forwards(remote, tmp_path):
    work, url = remote
    cache = tmp_path / "cache"
    _push_branch(work, "feat", "Feat Dashboard")
    feat = clone_or_pull(url, branch="feat", cache_root=cache)

    git("checkout", "feat", cwd=work)
    (work / "team.yaml").write_text(DASH.replace("Team Dashboard", "Feat v2"))
    git("commit", "-am", "feat v2", cwd=work)
    git("push", "origin", "feat", cwd=work)

    assert clone_or_pull(url, branch="feat", cache_root=cache) == feat
    assert "Feat v2" in (feat / "team.yaml").read_text()


def test_missing_branch_fails_cleanly_and_leaves_no_clone(remote, tmp_path):
    _, url = remote
    cache = tmp_path / "cache"

    with pytest.raises(GitError) as exc:
        clone_or_pull(url, branch="nope", cache_root=cache)
    assert str(exc.value) == f"branch 'nope' does not exist on {url}"
    assert not repo_cache_dir(url, "nope", cache).exists()


def test_branch_deleted_on_remote_names_the_cached_clone(remote, tmp_path):
    work, url = remote
    cache = tmp_path / "cache"
    _push_branch(work, "feat", "Feat Dashboard")
    feat = clone_or_pull(url, branch="feat", cache_root=cache)
    git("push", "origin", "--delete", "feat", cwd=work)

    with pytest.raises(GitError) as exc:
        clone_or_pull(url, branch="feat", cache_root=cache)
    assert "branch 'feat' does not exist on the remote" in str(exc.value)
    assert str(feat) in str(exc.value)
    assert "Feat Dashboard" in (feat / "team.yaml").read_text()


def _dangle_head(url):
    git("symbolic-ref", "HEAD", "refs/heads/nope", cwd=url.removeprefix("file://"))


def test_dangling_remote_head_fails_cleanly_and_leaves_no_clone(remote, tmp_path):
    """#519: a remote HEAD naming a missing branch made `git clone` exit 0 with an
    empty checkout, which was served silently and then failed every later start."""
    _, url = remote
    cache = tmp_path / "cache"
    _dangle_head(url)

    with pytest.raises(GitError) as exc:
        clone_or_pull(url, cache_root=cache)
    assert str(exc.value) == (
        f"{url} does not advertise a default branch — its HEAD names a branch that does not"
        " exist; pass --branch, e.g. one of: main"
    )
    assert not repo_cache_dir(url, cache_root=cache).exists()

    main = clone_or_pull(url, branch="main", cache_root=cache)
    assert "Team Dashboard" in (main / "team.yaml").read_text()


def test_empty_remote_fails_cleanly_and_leaves_no_clone(tmp_path):
    bare = tmp_path / "empty.git"
    git("init", "--bare", str(bare), cwd=tmp_path)
    url = f"file://{bare}"
    cache = tmp_path / "cache"

    with pytest.raises(GitError) as exc:
        clone_or_pull(url, cache_root=cache)
    assert str(exc.value) == f"{url} has no branches yet — push one first"
    assert not repo_cache_dir(url, cache_root=cache).exists()


def test_empty_clone_from_dangling_head_explains_and_recovers(remote, tmp_path):
    """A cache left empty by the old behavior said 'fix conflicts' on every start."""
    _, url = remote
    cache = tmp_path / "cache"
    _dangle_head(url)
    poisoned = repo_cache_dir(url, cache_root=cache)
    git("clone", "--depth", "1", url, str(poisoned), cwd=tmp_path)

    with pytest.raises(GitError) as exc:
        clone_or_pull(url, cache_root=cache)
    message = str(exc.value)
    assert "its HEAD names a branch that does not exist; pass --branch" in message
    assert f"(cached clone: {poisoned} — the clone was left as it was)" in message
    assert "conflicts" not in message
    assert (poisoned / ".git").is_dir()

    git("symbolic-ref", "HEAD", "refs/heads/main", cwd=url.removeprefix("file://"))
    assert clone_or_pull(url, cache_root=cache) == poisoned
    assert _stdout("symbolic-ref", "--short", "HEAD", cwd=poisoned) == "main"
    assert "Team Dashboard" in (poisoned / "team.yaml").read_text()


def test_head_dangling_after_clone_keeps_pulling_the_checked_out_branch(remote, tmp_path):
    work, url = remote
    cache = tmp_path / "cache"
    clone = clone_or_pull(url, cache_root=cache)
    _dangle_head(url)
    (work / "team.yaml").write_text(DASH.replace("Team Dashboard", "Main v2"))
    git("commit", "-am", "v2", cwd=work)
    git("push", "origin", "main", cwd=work)

    assert clone_or_pull(url, cache_root=cache) == clone
    assert "Main v2" in (clone / "team.yaml").read_text()


def test_workspace_entries_sharing_a_dangling_url_fail_cleanly(remote, tmp_path, monkeypatch):
    """Two entries on one URL share a cache folder: the first used to leave an
    empty clone that the second then reported as conflicts."""
    _, url = remote
    _dangle_head(url)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))

    with pytest.raises(GitError) as exc:
        resolve_workspace({"a": {"url": url}, "b": {"url": url}})
    assert "pass --branch, e.g. one of: main" in str(exc.value)
    assert not repo_cache_dir(url).exists()

    entry = {"url": url, "branch": "main"}
    resolved = dict(resolve_workspace({"a": entry, "b": dict(entry)}))
    assert resolved["a"] == resolved["b"] == repo_cache_dir(url, "main")
    assert "Team Dashboard" in (resolved["a"] / "team.yaml").read_text()


SECRET = "ghp_s3cretToken"


@pytest.fixture
def credentialed_remote(remote, tmp_path):
    bare = tmp_path / "central.git"
    git("update-server-info", cwd=bare)
    with git_http_server(tmp_path, "alice", SECRET) as base:
        yield base.replace("://", f"://alice:{SECRET}@") + "/central.git"


def test_credentialed_url_still_clones_and_pulls(credentialed_remote, tmp_path):
    cache = tmp_path / "cache"
    clone = clone_or_pull(credentialed_remote, cache_root=cache)
    assert "Team Dashboard" in (clone / "team.yaml").read_text()
    assert clone_or_pull(credentialed_remote, cache_root=cache) == clone


def test_git_errors_mask_url_credentials(credentialed_remote, tmp_path):
    cache = tmp_path / "cache"
    with pytest.raises(GitError) as exc:
        clone_or_pull(credentialed_remote, branch="nope", cache_root=cache)
    assert SECRET not in str(exc.value)
    assert "alice" not in str(exc.value)
    assert "does not exist on http://•••@127.0.0.1:" in str(exc.value)

    wrong = credentialed_remote.replace(SECRET, "ghp_wrongToken")
    with pytest.raises(GitError) as exc:
        clone_or_pull(wrong, cache_root=cache)
    assert "ghp_wrongToken" not in str(exc.value)


def test_git_stderr_is_masked():
    url = f"https://alice:{SECRET}@127.0.0.1:9/acme/x.git"
    err = GitError(f"git clone failed: fatal: repository '{url}' not found")
    assert SECRET not in str(err)
    assert "'https://•••@127.0.0.1:9/acme/x.git'" in str(err)
