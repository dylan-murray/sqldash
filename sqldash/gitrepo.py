"""Shallow clones of workspace repos, cached per-URL under the user cache dir."""

import hashlib
import re
import shutil
import subprocess
from pathlib import Path

from platformdirs import user_cache_dir

from sqldash.redact import mask_url_userinfo

GIT_URL = re.compile(
    r"^(git@[\w.-]+:|ssh://|git://|file://|https?://.+\.git$|https?://(github\.com|gitlab\.com|bitbucket\.org)/)"
)


class GitError(RuntimeError):
    fix = "fix conflicts there or delete it to re-clone"

    def __init__(self, message: str) -> None:
        super().__init__(mask_url_userinfo(message))


class _MissingBranch(GitError):
    fix = "check the branch name; the clone was left as it was"


class _DirtyClone(GitError):
    fix = "commit or stash the changes there, or delete it to re-clone"


class _NoDefaultBranch(GitError):
    fix = "the clone was left as it was"


def is_git_url(target: str) -> bool:
    return bool(GIT_URL.match(target))


def repo_cache_dir(url: str, branch: str | None = None, cache_root: Path | None = None) -> Path:
    """Stable per-URL-and-branch checkout location: readable slug plus a digest to
    split forks. No branch keeps the original `<slug>-<digest>` name, so caches
    from before branches got their own folder stay valid."""
    root = cache_root or Path(user_cache_dir("sqldash")) / "repos"
    slug = re.sub(r"\W+", "-", url.rstrip("/").split("/")[-1].removesuffix(".git")).strip("-")
    if not branch:
        return root / f"{slug}-{hashlib.sha256(url.encode()).hexdigest()[:10]}"
    branch_slug = re.sub(r"\W+", "-", branch).strip("-")
    digest = hashlib.sha256(f"{url}\0{branch}".encode()).hexdigest()[:10]
    return root / f"{slug}-{branch_slug}-{digest}"


def _run_git(args: list[str], cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120
        )
    except FileNotFoundError as exc:
        raise GitError("git is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {args[0]} timed out") from exc
    if result.returncode != 0:
        raise GitError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout


def _git_succeeds(args: list[str], cwd: Path) -> bool:
    try:
        _run_git(args, cwd=cwd)
    except GitError:
        return False
    return True


def _branch_missing(url: str, branch: str) -> bool:
    try:
        return not _run_git(["ls-remote", url, f"refs/heads/{branch}"]).strip()
    except GitError:
        return False


def _remote_heads(remote: str, cwd: Path | None = None) -> list[str]:
    listing = _run_git(["ls-remote", "--heads", remote], cwd=cwd)
    return [line.partition("\trefs/heads/")[2] for line in listing.splitlines()]


def _no_default_branch(where: str, heads: list[str]) -> str:
    """A remote whose HEAD names a branch it does not have advertises no default;
    `git clone` then checks out nothing and still exits 0."""
    if not heads:
        return f"{where} has no branches yet — push one first"
    shown = ", ".join(heads[:5]) + (", ..." if len(heads) > 5 else "")
    return (
        f"{where} does not advertise a default branch — its HEAD names a branch that does not"
        f" exist; pass --branch, e.g. one of: {shown}"
    )


def _current_branch(clone: Path) -> str | None:
    try:
        return _run_git(["symbolic-ref", "--short", "-q", "HEAD"], cwd=clone).strip() or None
    except GitError:
        return None


def _remote_branch(clone: Path, branch: str | None) -> str | None:
    """The branch to serve: `branch` if the remote has it, else the remote's default.

    Returns None only when no branch was requested and the remote does not
    advertise its default, so the caller keeps whatever is checked out if the
    remote still has that branch."""
    pattern = f"refs/heads/{branch}" if branch else "HEAD"
    listing = _run_git(["ls-remote", "--symref", "origin", pattern], cwd=clone)
    for line in listing.splitlines():
        left, _, ref = line.partition("\t")
        if branch and ref == pattern:
            return branch
        if not branch and ref == "HEAD" and left.startswith("ref: refs/heads/"):
            return left.removeprefix("ref: refs/heads/")
    if branch:
        raise _MissingBranch(f"branch '{branch}' does not exist on the remote")
    return None


def _track_branch(clone: Path, branch: str) -> None:
    """Widen a --single-branch clone's fetch refspec so `branch` gets a tracking ref."""
    refspec = f"+refs/heads/{branch}:refs/remotes/origin/{branch}"
    try:
        configured = _run_git(["config", "--get-all", "remote.origin.fetch"], cwd=clone)
    except GitError:
        configured = ""
    if {refspec, "+refs/heads/*:refs/remotes/origin/*"} & set(configured.split()):
        return
    _run_git(["remote", "set-branches", "--add", "origin", branch], cwd=clone)


def _sync_cached(clone: Path, branch: str | None) -> None:
    wanted = _remote_branch(clone, branch)
    current = _current_branch(clone)
    if wanted is None:
        heads = _remote_heads("origin", cwd=clone)
        if current not in heads:
            raise _NoDefaultBranch(_no_default_branch("the remote", heads))
        wanted = current
    if wanted != current and _run_git(["status", "--porcelain"], cwd=clone).strip():
        raise _DirtyClone(
            f"cannot switch to branch '{wanted}': uncommitted changes on "
            f"'{current or 'detached HEAD'}'"
        )
    tracking = f"refs/remotes/origin/{wanted}"
    fetch = ["fetch", "origin", f"+refs/heads/{wanted}:{tracking}"]
    if not _git_succeeds(["rev-parse", "--verify", "--quiet", tracking], cwd=clone):
        fetch[1:1] = ["--depth", "1"]
    _track_branch(clone, wanted)
    _run_git(fetch, cwd=clone)
    if wanted != current:
        if _git_succeeds(["rev-parse", "--verify", "--quiet", f"refs/heads/{wanted}"], cwd=clone):
            _run_git(["checkout", wanted], cwd=clone)
        else:
            _run_git(["checkout", "-b", wanted, "--track", f"origin/{wanted}"], cwd=clone)
    _run_git(["merge", "--ff-only", f"origin/{wanted}"], cwd=clone)


def clone_or_pull(url: str, branch: str | None = None, cache_root: Path | None = None) -> Path:
    """Clone shallowly once per URL and branch; later serves fetch that branch (the
    remote's default when none is given), switch to it and fast-forward. Failures
    on a cached clone name its path so the user can fix or delete it."""
    target = repo_cache_dir(url, branch, cache_root)
    if not (target / ".git").is_dir():
        target.parent.mkdir(parents=True, exist_ok=True)
        args = ["clone", "--depth", "1"]
        if branch:
            args += ["--branch", branch]
        try:
            _run_git([*args, url, str(target)])
        except GitError as exc:
            if branch and _branch_missing(url, branch):
                raise GitError(f"branch '{branch}' does not exist on {url}") from exc
            raise
        if not _git_succeeds(["rev-parse", "--verify", "--quiet", "HEAD"], cwd=target):
            shutil.rmtree(target, ignore_errors=True)
            raise GitError(_no_default_branch(url, _remote_heads(url)))
        return target
    try:
        _sync_cached(target, branch)
    except GitError as exc:
        raise GitError(f"{exc}\n(cached clone: {target} — {exc.fix})") from exc
    return target
