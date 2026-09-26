"""The workspace registry: a local list of repos (git URLs or paths) that
`sqldash serve` can serve all at once. Lives next to profiles.yaml so it is
per-user machine config, never committed."""

import io
from pathlib import Path

from platformdirs import user_config_dir

from sqldash.gitrepo import clone_or_pull, is_git_url
from sqldash.models.dashboard import slugify
from sqldash.project.store import yaml
from sqldash.redact import mask_url_userinfo


class WorkspaceError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(mask_url_userinfo(message))


def registry_path() -> Path:
    return Path(user_config_dir("sqldash")) / "repos.yaml"


def load_registry(path: Path | None = None) -> dict[str, dict]:
    """Read repos.yaml, normalizing bare strings into {url: ...} or {path: ...} entries."""
    target = path or registry_path()
    if not target.is_file():
        return {}
    data = yaml.load(io.StringIO(target.read_text())) or {}
    if not isinstance(data, dict):
        raise WorkspaceError(f"{target}: must be a YAML mapping of repo name -> config")
    repos = data.get("repos", data)
    out: dict[str, dict] = {}
    for name, entry in repos.items():
        if isinstance(entry, str):
            entry = {"url": entry} if is_git_url(entry) else {"path": entry}
        if not isinstance(entry, dict) or not (entry.get("url") or entry.get("path")):
            raise WorkspaceError(f"{target}: repo '{name}' needs a 'url' or a 'path'")
        out[str(name)] = dict(entry)
    return out


def save_registry(repos: dict[str, dict], path: Path | None = None) -> None:
    target = path or registry_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.StringIO()
    yaml.dump({"repos": repos}, buffer)
    target.write_text(buffer.getvalue())


def default_repo_name(target: str) -> str:
    base = target.rstrip("/").split("/")[-1].removesuffix(".git")
    name = slugify(base)
    if not name:
        raise WorkspaceError(f"cannot derive a repo name from '{target}' — pass --name")
    return name


def add_repo(
    target: str,
    name: str | None = None,
    branch: str | None = None,
    path: Path | None = None,
) -> tuple[str, dict]:
    repos = load_registry(path)
    repo_name = slugify(name) if name else default_repo_name(target)
    if not repo_name:
        raise WorkspaceError("repo name must contain at least one letter or number")
    if repo_name in repos:
        raise WorkspaceError(f"a repo named '{repo_name}' is already registered — pass --name")
    if is_git_url(target):
        entry: dict = {"url": target}
    else:
        resolved = Path(target).expanduser().resolve()
        if not resolved.is_dir():
            raise WorkspaceError(f"'{target}' is not a directory or a recognized git URL")
        entry = {"path": str(resolved)}
    if branch:
        entry["branch"] = branch
    repos[repo_name] = entry
    save_registry(repos, path)
    return repo_name, entry


def remove_repo(name: str, path: Path | None = None) -> dict:
    repos = load_registry(path)
    if name not in repos:
        known = ", ".join(sorted(repos)) or "(none registered)"
        raise WorkspaceError(f"no repo named '{name}' — registered repos: {known}")
    entry = repos.pop(name)
    save_registry(repos, path)
    return entry


def resolve_workspace(repos: dict[str, dict]) -> list[tuple[str, Path]]:
    """Materialize each entry into a servable local path, cloning/pulling URL repos."""
    resolved = []
    for name, entry in repos.items():
        if entry.get("url"):
            resolved.append((name, clone_or_pull(entry["url"], branch=entry.get("branch"))))
        else:
            root = Path(entry["path"]).expanduser()
            if not root.is_dir():
                raise WorkspaceError(
                    f"repo '{name}': path '{root}' does not exist — "
                    "fix or remove it with 'sqldash repo remove'"
                )
            resolved.append((name, root))
    return resolved
