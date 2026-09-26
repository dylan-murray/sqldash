"""First-run setup: write a local profile and a project source that references it.

The project file never gets a secret. Credentials live in
``~/.config/sqldash/profiles.yaml`` (or a test-injected path), and passwords /
tokens are ``${env:VAR}`` references — the existing contract, made the default
path instead of something you have to know about.
"""

from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ruamel.yaml.comments import CommentedMap
from sqlalchemy.engine.url import make_url

from sqldash.execution import ExecutionRegistry
from sqldash.models.source import (
    AUTHENTICATION_METHODS,
    DEFAULT_MARK,
    Source,
    default_source_name,
)
from sqldash.project.sources import (
    attach_data_files,
    attach_dir_missing,
    database_file_missing,
    source_files_dir,
)
from sqldash.project.store import DashboardStore, yaml
from sqldash.redact import mask_url_userinfo
from sqldash.scaffold import ScaffoldExists, create_demo
from sqldash.secrets import (
    CREDENTIAL_FIELDS,
    ENV_REF,
    SecretError,
    load_profiles,
    save_profile,
)
from sqldash.secrets import (
    profiles_path as default_profiles_file,
)
from sqldash.workspace import WorkspaceError, add_repo, default_repo_name, load_registry

SOURCE_TYPES = (
    "duckdb",
    "postgres",
    "snowflake",
    "bigquery",
    "databricks",
    "mysql",
    "url",
)

_PROFILE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_FLAG = {
    "account": "--account",
    "host": "--host",
    "port": "--port",
    "database": "--database",
    "db_schema": "--schema",
    "warehouse": "--warehouse",
    "role": "--role",
    "username": "--username",
    "authentication": "--auth",
    "password_env": "--password-env",
    "token_env": "--token-env",
    "private_key_path": "--private-key-path",
    "url": "--url",
    "project": "--project",
    "http_path": "--http-path",
    "catalog": "--catalog",
    "profile": "--profile",
}
# Query keys that may carry a literal. Everything else with a non-env value is
# treated as a secret — an allowlist cannot miss a new credential the way a
# denylist did (sslpassword, then passwd, then …).
_SAFE_QUERY_KEYS = frozenset(
    {
        "sslmode",
        "sslcert",
        "sslkey",
        "sslrootcert",
        "sslcrl",
        "connect_timeout",
        "application_name",
        "options",
        "target_session_attrs",
        "client_encoding",
        "keepalives",
        "gssencmode",
        "channel_binding",
        "bypass_rls",
    }
)
_SOURCE_COPY = (
    ("account", "account"),
    ("host", "host"),
    ("port", "port"),
    ("database", "database"),
    ("schema", "db_schema"),
    ("warehouse", "warehouse"),
    ("role", "role"),
    ("project", "project"),
    ("http_path", "http_path"),
    ("catalog", "catalog"),
)


class SetupError(ValueError):
    def __init__(self, message: str) -> None:
        super().__init__(mask_url_userinfo(message))


@dataclass(frozen=True)
class TypeSpec:
    required: tuple[str, ...] = ()
    allowed: frozenset[str] = frozenset()
    uses_profile: bool = True


# One table drives required flags, unused-flag errors, and whether a profile
# is written. Add a type here — do not add another if/else in apply_setup.
TYPE_SPECS: dict[str, TypeSpec] = {
    "duckdb": TypeSpec(allowed=frozenset({"database"}), uses_profile=False),
    "url": TypeSpec(required=("url",), allowed=frozenset({"url"}), uses_profile=False),
    "bigquery": TypeSpec(
        allowed=frozenset({"project", "host", "database", "db_schema"}),
        uses_profile=False,
    ),
    "snowflake": TypeSpec(
        required=("account", "username"),
        allowed=frozenset(
            {
                "account",
                "username",
                "authentication",
                "password_env",
                "token_env",
                "private_key_path",
                "warehouse",
                "database",
                "db_schema",
                "role",
                "profile",
            }
        ),
    ),
    "postgres": TypeSpec(
        required=("host", "database"),
        allowed=frozenset(
            {"host", "port", "database", "db_schema", "username", "password_env", "profile"}
        ),
    ),
    "mysql": TypeSpec(
        required=("host", "database"),
        allowed=frozenset(
            {"host", "port", "database", "db_schema", "username", "password_env", "profile"}
        ),
    ),
    "databricks": TypeSpec(
        required=("host", "http_path"),
        allowed=frozenset({"host", "http_path", "catalog", "username", "token_env", "profile"}),
    ),
}


@dataclass
class SetupPlan:
    source_type: str
    profile: str | None = None
    account: str | None = None
    host: str | None = None
    port: int | None = None
    database: str | None = None
    db_schema: str | None = None
    warehouse: str | None = None
    role: str | None = None
    username: str | None = None
    authentication: str | None = None
    password_env: str | None = None
    token_env: str | None = None
    private_key_path: str | None = None
    url: str | None = None
    project: str | None = None
    http_path: str | None = None
    catalog: str | None = None
    keep_profile: bool = False


@dataclass
class SetupResult:
    directory: Path
    metrics_path: Path
    profiles_path: Path | None
    profile: str | None
    source: dict[str, Any]
    created_demo: bool
    test_ok: bool | None
    test_error: str | None
    needed_env: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    updated_dashboards: list[str] = field(default_factory=list)
    stale_dashboards: list[str] = field(default_factory=list)
    created_metrics: bool = False
    sample_schema_files: list[str] = field(default_factory=list)
    no_dashboards: bool = False
    unreadable_dashboards: list[str] = field(default_factory=list)
    restored_files: list[str] = field(default_factory=list)


def env_var_name(profile: str, kind: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", profile).strip("_").upper()
    return f"SQLDASH_{slug}_{kind}"


def default_profile_name(directory: Path) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", directory.resolve().name).strip("-").lower()
    if not slug:
        return "default"
    if _PROFILE_NAME.match(slug):
        return slug
    prefixed = f"p{slug}"
    return prefixed if _PROFILE_NAME.match(prefixed) else "default"


def _set(value: Any) -> bool:
    return value not in (None, "")


def missing_fields(plan: SetupPlan) -> list[str]:
    """Flag names a non-interactive run still needs. Profile is defaulted, not required.

    Unknown types return [] so the caller falls through to apply_setup's
    \"unknown type '...'\".
    """
    spec = TYPE_SPECS.get(plan.source_type)
    if spec is None:
        return []
    missing = [
        _FLAG[name].removeprefix("--") for name in spec.required if not _set(getattr(plan, name))
    ]
    if plan.source_type == "bigquery" and not _set(plan.project) and not _set(plan.host):
        missing.append("project")
    auth = plan.authentication or ("externalbrowser" if plan.source_type == "snowflake" else None)
    if auth == "keypair" and not _set(plan.private_key_path):
        missing.append("private-key-path")
    return missing


def unused_flags(plan: SetupPlan) -> list[str]:
    spec = TYPE_SPECS.get(plan.source_type)
    if spec is None:
        return []
    unused = []
    for name, flag in _FLAG.items():
        if name in spec.allowed:
            continue
        if _set(getattr(plan, name, None)):
            unused.append(flag)
    return unused


def _env_ref(name: str) -> str:
    return f"${{env:{name}}}"


def _is_env_ref(value: str) -> bool:
    return bool(value) and ENV_REF.fullmatch(value.strip()) is not None


def profile_fields(plan: SetupPlan, profile: str) -> dict[str, Any]:
    """Credential keys only. Secrets are opt-in except databricks token / password auth."""
    fields: dict[str, Any] = {}
    if plan.username:
        fields["username"] = plan.username
    auth = plan.authentication
    if plan.source_type == "snowflake":
        auth = auth or "externalbrowser"
    if auth:
        if auth not in AUTHENTICATION_METHODS:
            raise SetupError(
                f"authentication '{auth}' is not valid — use one of "
                f"{', '.join(AUTHENTICATION_METHODS)}"
            )
        fields["authentication"] = auth
    if auth == "password" or _set(plan.password_env):
        fields["password"] = _env_ref(plan.password_env or env_var_name(profile, "PASSWORD"))
    if plan.source_type == "databricks" or auth == "pat" or _set(plan.token_env):
        fields["token"] = _env_ref(plan.token_env or env_var_name(profile, "TOKEN"))
    if plan.private_key_path or auth == "keypair":
        if not plan.private_key_path:
            raise SetupError("keypair authentication needs --private-key-path")
        fields["private_key_path"] = plan.private_key_path
    return fields


def _refuse_literal_secret(kind: str) -> None:
    raise SetupError(
        f"put the {kind} in ${{env:VAR}} inside the url "
        "(or use --type plus --password-env) — setup will not write a literal secret"
    )


def source_fields(plan: SetupPlan, profile: str | None) -> dict[str, Any]:
    """Project-side source: connection shape + profile name, never a secret."""
    if plan.source_type == "url":
        if not plan.url:
            raise SetupError("url sources need --url")
        try:
            parsed = make_url(plan.url)
        except Exception as exc:
            raise SetupError(f"could not parse --url: {exc}") from exc
        if parsed.password and not _is_env_ref(parsed.password):
            _refuse_literal_secret("password")
        for key, value in (parsed.query or {}).items():
            values = value if isinstance(value, (list, tuple)) else (value,)
            for item in values:
                if not item or _is_env_ref(str(item)):
                    continue
                if str(key).lower() in _SAFE_QUERY_KEYS:
                    continue
                _refuse_literal_secret(f"query parameter '{key}'")
        source: dict[str, Any] = {"url": plan.url}
        if profile:
            source["profile"] = profile
        return source
    if plan.source_type == "duckdb":
        source = {"type": "duckdb"}
        if plan.database:
            source["database"] = plan.database
        if not plan.database or plan.database == ":memory:":
            source["attach_files"] = True
        return source
    source = {"type": plan.source_type}
    for dest, attr in _SOURCE_COPY:
        value = getattr(plan, attr)
        if _set(value):
            source[dest] = value
    if profile:
        source["profile"] = profile
    return source


def _metrics_path(directory: Path) -> Path:
    """Same rule as DashboardStore: if `.sqldash/` exists, that is the root."""
    nested_dir = directory / ".sqldash"
    if nested_dir.is_dir():
        return nested_dir / "metrics.yaml"
    flat = directory / "metrics.yaml"
    return flat if flat.exists() else nested_dir / "metrics.yaml"


def _env_refs_in(values: Any) -> list[str]:
    found: list[str] = []
    if isinstance(values, str):
        found.extend(match.group(1) for match in ENV_REF.finditer(values))
    elif isinstance(values, dict):
        for value in values.values():
            found.extend(_env_refs_in(value))
    elif isinstance(values, (list, tuple)):
        for value in values:
            found.extend(_env_refs_in(value))
    return found


def _write_source_mapping(path: Path, source: dict[str, Any]) -> None:
    """Replace only the ``source:`` mapping in an existing YAML file, or create one."""
    if path.exists():
        try:
            doc = yaml.load(path.read_text())
        except Exception as exc:
            raise SetupError(f"{path}: invalid YAML: {exc}") from exc
        if not isinstance(doc, dict):
            raise SetupError(f"{path} must be a YAML mapping")
        block = doc.get("source")
        # A file that names its connections keeps them; only its default is repointed.
        entry = default_source_name(block)
        old = block[entry] if entry is not None else block
        carried = None
        if isinstance(old, CommentedMap) and old:
            last_key = next(reversed(old))
            carried = old.ca.items.get(last_key) if old.ca.items else None
        new_source = CommentedMap(source)
        if entry is not None:
            if len(block) > 1:
                new_source[DEFAULT_MARK] = True
            block[entry] = new_source
        else:
            doc["source"] = new_source
        if carried is not None and new_source:
            new_source.ca.items[next(reversed(new_source))] = carried
    else:
        doc = CommentedMap({"source": CommentedMap(source)})
    buffer = io.StringIO()
    yaml.dump(doc, buffer)
    path.write_text(buffer.getvalue())


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    return str(value)


_UNREADABLE = object()


def _read_source_mapping(path: Path) -> Any:
    """The file's ``source:`` as plain values; None when it has none, and
    ``_UNREADABLE`` when the file cannot be parsed, so a broken dashboard is
    named as broken rather than as one on a different source."""
    if not path.is_file():
        return None
    try:
        doc = yaml.load(path.read_text())
    except Exception:
        return _UNREADABLE
    block = doc.get("source") if isinstance(doc, dict) else None
    entry = default_source_name(block)
    if entry is not None:
        block = block[entry]
    if not isinstance(block, dict):
        return None
    plain = _plain(block)
    plain.pop(DEFAULT_MARK, None)
    return plain


def sync_dashboard_sources(
    store_dir: Path, previous: dict[str, Any] | None, source: dict[str, Any]
) -> tuple[list[str], list[str], list[str]]:
    """Point every dashboard whose ``source:`` was the project source setup wrote
    last time at the new one; a dashboard on any other source is the author's
    and is only named, never rewritten, and one that cannot be parsed is named
    as unreadable rather than as stale (#304)."""
    updated: list[str] = []
    stale: list[str] = []
    unreadable: list[str] = []
    wanted = _plain(source)
    for path in DashboardStore(store_dir).discover().values():
        current = _read_source_mapping(path)
        if current is _UNREADABLE:
            unreadable.append(path.name)
            continue
        if current is None or current == wanted:
            continue
        if previous is not None and current == previous:
            _write_source_mapping(path, source)
            updated.append(path.name)
        else:
            stale.append(path.name)
    return updated, stale, unreadable


_FRESH_METRICS_HEADER = (
    "# The semantic layer: governed metrics that dashboards reference by name\n"
    "# (tile `metric: revenue`) and agents query via `sqldash mcp`.\n"
    "# `sqldash source describe` lists the tables to define relations and metrics over.\n"
)


def _write_fresh_metrics(path: Path, source: dict[str, Any]) -> None:
    """A metrics.yaml for a real source: the source plus empty sections to fill in.

    The sample orders scaffold only makes sense over the sample CSV; written over
    a user's database every metric and tile errored while lint stayed green (#305).
    """
    buffer = io.StringIO()
    yaml.dump(CommentedMap({"source": CommentedMap(source)}), buffer)
    path.write_text(_FRESH_METRICS_HEADER + buffer.getvalue() + "\nrelations: {}\n\nmetrics: {}\n")


def _sample_schema_files(store_dir: Path) -> list[str]:
    """Scaffold files still shaped for the sample orders CSV."""
    found: list[str] = []
    demo = store_dir / "demo.yaml"
    if demo.is_file() and re.search(r"\bFROM\s+orders\b", demo.read_text()):
        found.append("demo.yaml")
    metrics = store_dir / "metrics.yaml"
    if metrics.is_file():
        try:
            doc = yaml.load(metrics.read_text())
        except Exception:
            doc = None
        relations = doc.get("relations") if isinstance(doc, dict) else None
        orders = relations.get("orders") if isinstance(relations, dict) else None
        if isinstance(orders, dict) and orders.get("table") == "orders":
            found.append("metrics.yaml")
    return found


def write_project_source(directory: Path, source: dict[str, Any]) -> Path:
    """Create .sqldash/metrics.yaml or replace only its ``source:`` mapping."""
    path = _metrics_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        _write_source_mapping(path, source)
    else:
        _write_fresh_metrics(path, source)
    return path


def apply_setup(
    directory: Path,
    plan: SetupPlan,
    *,
    profiles_file: Path | None = None,
    skip_test: bool = False,
    register: bool = False,
) -> SetupResult:
    try:
        return _apply_setup(
            directory, plan, profiles_file=profiles_file, skip_test=skip_test, register=register
        )
    except SetupError:
        raise
    except Exception as exc:
        raise SetupError(str(exc)) from exc


def _apply_setup(
    directory: Path,
    plan: SetupPlan,
    *,
    profiles_file: Path | None,
    skip_test: bool,
    register: bool,
) -> SetupResult:
    spec = TYPE_SPECS.get(plan.source_type)
    if spec is None:
        raise SetupError(
            f"unknown type '{plan.source_type}' — use one of {', '.join(SOURCE_TYPES)}"
        )
    missing = missing_fields(plan)
    if missing:
        raise SetupError(f"--type {plan.source_type} needs {', '.join('--' + m for m in missing)}")
    unused = unused_flags(plan)
    if unused:
        raise SetupError(f"--type {plan.source_type} does not use {', '.join(unused)} — drop them")
    if plan.profile and not _PROFILE_NAME.match(plan.profile):
        raise SetupError(f"profile '{plan.profile}' must match [A-Za-z][A-Za-z0-9_-]*")

    directory = directory.expanduser().resolve()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except FileExistsError as exc:
        raise SetupError(f"{directory} exists and is not a directory") from exc
    except OSError as exc:
        raise SetupError(f"could not create {directory}: {exc}") from exc

    notes: list[str] = []
    needed_env: list[str] = []
    created_demo = False
    profiles_path: Path | None = None
    profile: str | None = None
    source_obj: Source | None = None
    metrics_existing = _metrics_path(directory)
    metrics_existed = metrics_existing.exists()
    metrics_before = metrics_existing.read_text() if metrics_existed else None
    previous_source = _read_source_mapping(metrics_existing)
    if previous_source is _UNREADABLE:
        previous_source = None

    if plan.source_type == "duckdb":
        plan = _anchor_duckdb_database(plan, directory, metrics_existing.parent, skip_test)
        source = source_fields(plan, None)
        will_scaffold = not metrics_existed and bool(source.get("attach_files"))
        if will_scaffold:
            try:
                create_demo(directory)
                created_demo = True
            except ScaffoldExists:
                pass
        if source.get("attach_files") and not attach_data_files(_metrics_path(directory).parent):
            source.pop("attach_files")
            source.setdefault("database", ":memory:")
        write_project_source(directory, source)
        if created_demo and plan.database:
            demo = metrics_existing.parent / "demo.yaml"
            if demo.is_file():
                _write_source_mapping(demo, source)
    else:
        fields: dict[str, Any] = {}
        if spec.uses_profile:
            profile = plan.profile or default_profile_name(directory)
            if not _PROFILE_NAME.match(profile):
                raise SetupError(f"profile '{profile}' must match [A-Za-z][A-Za-z0-9_-]*")
            fields = profile_fields(plan, profile)
            if plan.keep_profile:
                profiles_path = profiles_file or default_profiles_file()
                try:
                    existing = load_profiles(profiles_path).get(profile) or {}
                except SecretError:
                    existing = {}
                needed_env = list(_env_refs_in(existing))
            elif fields:
                try:
                    profiles_path = save_profile(profile, fields, path=profiles_file)
                except SecretError as exc:
                    raise SetupError(str(exc)) from exc
            else:
                profile = None
        source = source_fields(plan, profile)
        try:
            source_obj = Source.model_validate(source)
        except Exception as exc:
            raise SetupError(f"source is not valid: {exc}") from exc
        leftover = [k for k in source if k in CREDENTIAL_FIELDS]
        if leftover:
            raise SetupError(
                f"internal: credential field(s) {', '.join(leftover)} must not land in the project"
            )
        write_project_source(directory, source)
        if not plan.keep_profile:
            needed_env = list(dict.fromkeys(_env_refs_in(fields) + _env_refs_in(source)))
        else:
            needed_env = list(dict.fromkeys(needed_env + _env_refs_in(source)))

    metrics_path = _metrics_path(directory)
    dashboards_before = {
        path.name: path.read_text()
        for path in DashboardStore(metrics_path.parent).discover().values()
        if path.is_file()
    }
    updated_dashboards, stale_dashboards, unreadable_dashboards = sync_dashboard_sources(
        metrics_path.parent, previous_source, source
    )
    sample_schema_files = (
        [] if source.get("attach_files") else _sample_schema_files(metrics_path.parent)
    )
    no_dashboards = (
        plan.source_type == "duckdb"
        and (not plan.database or plan.database == ":memory:")
        and not created_demo
        and not DashboardStore(metrics_path.parent).discover()
    )
    test_ok: bool | None = None
    test_error: str | None = None
    unset_env = [name for name in needed_env if name not in os.environ]
    if unset_env:
        skip_test = True
        notes.append(
            "skipped source test — set "
            + ", ".join(unset_env)
            + " then re-run without --skip-test (or: sqldash source test)"
        )
    auth = plan.authentication
    if plan.source_type == "snowflake":
        auth = auth or "externalbrowser"
    if auth == "externalbrowser" and not skip_test:
        skip_test = True
        notes.append(
            "skipped source test — externalbrowser needs interactive SSO; run: sqldash source test"
        )
    if not skip_test:
        if source_obj is None:
            try:
                source_obj = Source.model_validate(source)
            except Exception as exc:
                raise SetupError(f"source is not valid: {exc}") from exc
        test_ok, test_error = _test_source(source_obj, metrics_path.parent)

    restored_files: list[str] = []
    if test_ok is False:
        if metrics_before is not None:
            metrics_path.write_text(metrics_before)
            restored_files.append(metrics_path.name)
        for name in updated_dashboards:
            if name in dashboards_before:
                (metrics_path.parent / name).write_text(dashboards_before[name])
                restored_files.append(name)
        updated_dashboards = []

    if register:
        notes.append(_register(directory))

    return SetupResult(
        directory=directory,
        metrics_path=metrics_path,
        profiles_path=profiles_path,
        profile=profile,
        source=source,
        created_demo=created_demo,
        test_ok=test_ok,
        test_error=test_error,
        needed_env=needed_env,
        notes=notes,
        updated_dashboards=updated_dashboards,
        stale_dashboards=stale_dashboards,
        created_metrics=not metrics_existed and not created_demo,
        sample_schema_files=sample_schema_files,
        no_dashboards=no_dashboards,
        unreadable_dashboards=unreadable_dashboards,
        restored_files=restored_files,
    )


def _register(directory: Path) -> str:
    """Idempotent workspace registration. Errors are setup-shaped (no --name)."""
    resolved = directory.resolve()
    try:
        name = default_repo_name(str(resolved))
        existing = load_registry().get(name)
    except WorkspaceError as exc:
        raise SetupError(str(exc).replace(" — pass --name", " — drop --register")) from exc
    if existing:
        if existing.get("path") == str(resolved):
            return f"already registered as '{name}'"
        raise SetupError(
            f"a repo named '{name}' is already registered "
            f"(at {existing.get('path') or existing.get('url')}) — "
            f"drop --register, or: sqldash repo remove {name}"
        )
    try:
        added, _entry = add_repo(str(resolved))
    except WorkspaceError as exc:
        raise SetupError(str(exc).replace(" — pass --name", " — drop --register")) from exc
    return f"registered as '{added}'"


def _anchor_duckdb_database(
    plan: SetupPlan, directory: Path, store_dir: Path, skip_test: bool
) -> SetupPlan:
    """A relative ``--database`` is the user's path from the project dir, not from
    ``.sqldash/`` where serve resolves it, so the written value is re-anchored on
    the store root. DuckDB creates a missing file on connect, so the file must
    already exist unless ``--skip-test`` says it will (#303)."""
    if not plan.database or plan.database == ":memory:" or "${" in plan.database:
        return plan
    given = Path(plan.database).expanduser()
    resolved = (given if given.is_absolute() else directory / given).resolve()
    if not skip_test and not resolved.is_file():
        raise SetupError(
            f"database file not found: {plan.database} (resolved to {resolved}) — "
            "create it first, use :memory:, or pass --skip-test to write the path anyway"
        )
    if given.is_absolute():
        written = str(given)
    else:
        written = Path(os.path.relpath(resolved, store_dir)).as_posix()
    return replace(plan, database=written)


def _test_source(source: Source, base_dir: Path) -> tuple[bool, str | None]:
    """SELECT 1 against the source setup just wrote — not a full metrics parse."""
    scan_dir = source_files_dir(source, base_dir)
    missing = attach_dir_missing(source, scan_dir) or database_file_missing(source, scan_dir)
    if missing:
        return False, missing
    registry = ExecutionRegistry(max_workers=1)
    try:
        registry.run_sync(source, base_dir, "SELECT 1", [], 1, timeout=30)
    except Exception as exc:
        return False, str(exc)
    finally:
        registry.shutdown()
    return True, None
