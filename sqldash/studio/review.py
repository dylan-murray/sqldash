import contextlib
import difflib
import hashlib
import json
import os
import secrets
import stat
from datetime import date, datetime
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from ruamel.yaml.nodes import MappingNode, ScalarNode, SequenceNode

from sqldash.models.source import is_secret_bag_key
from sqldash.studio.entrypoints import StudioError

MAX_BYTES = 2 * 1024 * 1024
MAX_NODES = 10000
MAX_DEPTH = 40
UNAVAILABLE = "[Text unavailable: invalid YAML or unsupported YAML types. Inspect it locally.]\n"
COMPLEXITY_LIMIT = (
    "[Text unavailable: YAML exceeds the review complexity limit. Inspect it locally.]\n"
)


def _snapshot(directory: int) -> dict[str, bytes]:
    files = {}
    for name in sorted(os.listdir(directory)):
        path = Path(name)
        if path.suffix not in {".yaml", ".yml", ".css"} or path.stem in {"profiles", "config"}:
            continue
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except OSError as exc:
            raise StudioError("Studio cannot review symlinked or unreadable project files") from exc
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise StudioError("Studio can review only regular project files")
            if info.st_size > MAX_BYTES:
                raise StudioError("Project files exceed Studio's 2 MiB review limit")
            files[name] = stream.read(MAX_BYTES + 1)
        if len(files) > 100 or sum(map(len, files.values())) > MAX_BYTES:
            raise StudioError("Project files exceed Studio's 100 file / 2 MiB review limit")
    return files


def _open_root(root: Path) -> int:
    try:
        return os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise StudioError("Studio project directory is unavailable or symlinked") from exc


def snapshot(root: Path) -> dict[str, bytes]:
    directory = _open_root(root)
    try:
        return _snapshot(directory)
    finally:
        os.close(directory)


def digest(files: dict[str, bytes]) -> str:
    hashed = hashlib.sha256()
    for name, content in sorted(files.items()):
        hashed.update(name.encode() + b"\0" + content + b"\0")
    return hashed.hexdigest()


def _redacted(key: str) -> bool:
    return key.lower() in {"source", "sources", "url", "env"} or is_secret_bag_key(key)


def _check_yaml_node(root):
    costs = {}
    active = {}
    merges = []
    prefix = "tag:yaml.org,2002:"
    scalar_tags = {prefix + name for name in ("null", "bool", "int", "float", "str", "timestamp")}

    def measure(node, depth=0):
        if node is None:
            return 0, 0
        if depth > MAX_DEPTH:
            raise ValueError("YAML review complexity limit")
        identity = id(node)
        if identity in active:
            if any(merges[active[identity] :]):
                raise ValueError("Recursive YAML merge")
            return 1, 0
        if identity in costs:
            cost, height = costs[identity]
            if depth + height > MAX_DEPTH:
                raise ValueError("YAML review complexity limit")
            return cost, height
        merge = False
        if isinstance(node, MappingNode):
            if node.tag != prefix + "map":
                raise TypeError("Unsupported YAML mapping")
            children = []
            for key, value in node.value:
                if not isinstance(key, ScalarNode) or key.tag not in {
                    prefix + "str",
                    prefix + "merge",
                }:
                    raise TypeError("Unsupported YAML key")
                merge |= key.tag == prefix + "merge"
                children.append(value)
            cost = 1 + len(node.value)
        elif isinstance(node, SequenceNode):
            if node.tag not in {prefix + "seq", prefix + "omap"}:
                raise TypeError("Unsupported YAML sequence")
            children = node.value
            cost = 1
        elif isinstance(node, ScalarNode) and node.tag in scalar_tags:
            return 1, 0
        else:
            raise TypeError("Unsupported YAML value")
        active[identity] = len(merges)
        merges.append(merge)
        height = 0
        for child in children:
            child_cost, child_height = measure(child, depth + 1)
            cost += child_cost
            height = max(height, child_height + 1)
            if cost > MAX_NODES:
                raise ValueError("YAML review complexity limit")
        merges.pop()
        del active[identity]
        costs[identity] = cost, height
        return cost, height

    measure(root)


def check_validation_inputs(files: dict[str, bytes]) -> None:
    for name, content in files.items():
        if Path(name).suffix not in {".yaml", ".yml"}:
            continue
        try:
            node = YAML(typ="safe").compose(content.decode())
        except (YAMLError, UnicodeError):
            continue
        try:
            _check_yaml_node(node)
        except (ValueError, TypeError, RecursionError) as exc:
            raise StudioError("Project YAML exceeds Studio's safe validation limits") from exc


def _document(content: bytes, redact: bool) -> tuple[bool, object]:
    try:
        loader = YAML(typ="safe")
        node = loader.compose(content.decode())
    except Exception:
        return False, UNAVAILABLE
    try:
        _check_yaml_node(node)
    except (ValueError, RecursionError):
        return False, COMPLEXITY_LIMIT
    except TypeError:
        return False, UNAVAILABLE
    try:
        doc = loader.constructor.construct_document(node) if node is not None else None
    except Exception:
        return False, UNAVAILABLE

    seen = set()
    remaining = MAX_NODES

    def clean(value, depth=0):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > MAX_DEPTH:
            raise ValueError("YAML review complexity limit")
        if isinstance(value, (dict, list)):
            if id(value) in seen:
                return "[Repeated YAML alias omitted]"
            seen.add(id(value))
        if isinstance(value, dict):
            if any(not isinstance(key, str) for key in value):
                raise TypeError("Unsupported YAML key")
            return {
                key: "[omitted]" if redact and _redacted(key) else clean(item, depth + 1)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [clean(item, depth + 1) for item in value]
        if value is None or isinstance(value, (str, bool, int, float, date, datetime)):
            return value
        raise TypeError("Unsupported YAML value")

    try:
        return True, clean(doc)
    except (ValueError, RecursionError):
        return False, COMPLEXITY_LIMIT
    except TypeError:
        return False, UNAVAILABLE


def safe_document(content: bytes | None) -> str:
    if content is None:
        return ""
    available, document = _document(content, redact=True)
    if not available:
        return document
    try:
        return json.dumps(document, indent=2, default=str, ensure_ascii=False) + "\n"
    except (ValueError, RecursionError):
        return COMPLEXITY_LIMIT


def _formatting_only(old: bytes | None, new: bytes | None) -> bool:
    if old is None or new is None:
        return False
    before = _document(old, redact=False)
    after = _document(new, redact=False)
    return before[0] and after[0] and before[1] == after[1]


def changes(before: dict, after: dict) -> list[dict]:
    rows = []
    for name in sorted(before.keys() | after.keys()):
        if before.get(name) == after.get(name):
            continue
        if Path(name).suffix == ".css":
            old = new = "[Text unavailable: inspect CSS changes locally.]\n"
        else:
            old = safe_document(before.get(name))
            new = safe_document(after.get(name))
        unavailable = old.startswith("[Text unavailable:") or new.startswith("[Text unavailable:")
        diff = "".join(
            difflib.unified_diff(
                old.splitlines(keepends=True),
                new.splitlines(keepends=True),
                fromfile=f"before/{name}",
                tofile=f"after/{name}",
            )
        )
        if not diff and unavailable:
            diff = "Content changed; a safe text comparison is unavailable. Inspect it locally."
        elif not diff and _formatting_only(before.get(name), after.get(name)):
            diff = (
                "Formatting-only change (comments, quoting, or key order). "
                "Inspect it locally with git diff."
            )
        elif not diff:
            diff = (
                "Content changed inside redacted fields (source, url, env, or secrets). "
                "Inspect it locally with git diff."
            )
        rows.append(
            {
                "file": name,
                "diff": diff,
                "kind": "added"
                if name not in before
                else "deleted"
                if name not in after
                else "modified",
            }
        )
    return rows


def _stage(directory: int, content: bytes, temporaries: set[str]) -> str:
    name = f".studio-{secrets.token_hex(16)}"
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
    temporaries.add(name)
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
    return name


def _replace_or_remove(directory: int, name: str, staged: dict[str, str]):
    if name in staged:
        os.replace(staged[name], name, src_dir_fd=directory, dst_dir_fd=directory)
    else:
        os.unlink(name, dir_fd=directory)


def restore(root: Path, before: dict, after: dict):
    changed = sorted(
        name for name in before.keys() | after.keys() if before.get(name) != after.get(name)
    )
    if any(Path(name).name != name or name in {".", ".."} for name in changed):
        raise StudioError("Invalid review filename")
    directory = _open_root(root)
    temporaries = set()
    try:
        current = _snapshot(directory)
        if any(current.get(name) != after.get(name) for name in changed):
            raise StudioError("Files changed since review. Review again before undoing.")
        try:
            replacements = {
                name: _stage(directory, before[name], temporaries)
                for name in changed
                if name in before
            }
            rollback = {
                name: _stage(directory, after[name], temporaries)
                for name in changed
                if name in after
            }
        except OSError as exc:
            raise StudioError(
                "Could not prepare undo. Project files were not changed. Retry undo."
            ) from exc
        if _snapshot(directory) != current:
            raise StudioError("Files changed since review. Review again before undoing.")
        applied = []
        try:
            for name in changed:
                _replace_or_remove(directory, name, replacements)
                applied.append(name)
        except OSError as exc:
            incomplete = False
            for name in reversed(applied):
                try:
                    _replace_or_remove(directory, name, rollback)
                except OSError:
                    incomplete = True
            if incomplete:
                raise StudioError(
                    "Undo failed and recovery was incomplete. Some files remain restored. "
                    "Review the current changes before retrying undo."
                ) from exc
            raise StudioError(
                "Undo failed. The reviewed changes were restored. Retry undo."
            ) from exc
    finally:
        for temporary in temporaries:
            with contextlib.suppress(OSError):
                os.unlink(temporary, dir_fd=directory)
        os.close(directory)
