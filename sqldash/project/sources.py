"""The one walk over every data source a project defines, shared by every surface
(CLI, MCP, settings) so the enumerations cannot drift apart."""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sqldash.connectors.engine_urls import FILE_DATABASES
from sqldash.connectors.roles import provider
from sqldash.models.source import source_as_project_yaml, source_label
from sqldash.project.source_context import split_source_context
from sqldash.semantics.layer import repo_problem

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqldash.models.source import Source
    from sqldash.project.store import Store
    from sqldash.semantics import SemanticLayer


@dataclass(frozen=True)
class LabeledSource:
    """One data source with its provenance; `dashboard is None` means a
    metrics.yaml source, `sname` set means a named `sources:` entry."""

    label: str
    source: "Source"
    base_dir: Path
    dashboard: str | None = None
    sname: str | None = None


_ATTACH_SUFFIXES = {".csv", ".parquet"}


def source_files_dir(source: "Source", fallback: Path) -> Path:
    """Directory duckdb attach_files and relative database paths resolve against."""
    raw = source.base_dir
    if not raw:
        return fallback
    path = Path(raw)
    if not path.is_absolute():
        path = fallback / path
    return path.resolve()


def attach_data_files(data_dir: Path) -> list[Path]:
    """csv/parquet in data_dir and its data/ subdirectory — the :memory: attach scan."""
    if not data_dir.is_dir():
        return []
    candidates = list(data_dir.iterdir())
    nested = data_dir / "data"
    if nested.is_dir():
        candidates.extend(nested.iterdir())
    return sorted(p for p in candidates if p.suffix.lower() in _ATTACH_SUFFIXES)


def attach_dir_missing(source: "Source", resolved: Path) -> str | None:
    """Why an attach_files source will not see any files, or None when it will.

    `source test` SELECT 1 still succeeds against :memory: when the scan dir is
    missing or exists with no csv/parquet, so the green line used to mean
    nothing (#339, #478). A file `database:` keeps its tables in the .duckdb
    file; attach_files is supplementary there.
    """
    if source.url or source.type != "duckdb" or not source.attach_files:
        return None
    if source.base_dir and not resolved.is_dir():
        return f"base_dir '{source.base_dir}' does not exist (resolved to {resolved})"
    database = source.database or ":memory:"
    if not _is_duckdb(source) or database not in (":memory:", ""):
        return None
    if not resolved.is_dir() or attach_data_files(resolved):
        return None
    return f"no csv/parquet files in {resolved} (0 files)"


def database_file_missing(source: "Source", base_dir: Path) -> str | None:
    """Why a file-database source has nothing to query, or None when its file exists.

    DuckDB and SQLite create a missing database on connect, so SELECT 1
    against a typo'd or wrongly anchored path succeeds against a brand-new
    empty file and leaves that file behind in the project (#303).
    """
    if (source.type or "") not in FILE_DATABASES or source.url:
        return None
    database = source.database or ":memory:"
    if database == ":memory:" or "${" in database or database.startswith("file:"):
        return None
    resolved = (base_dir / database).resolve()
    if resolved.is_file():
        return None
    return f"database file not found: {database} (resolved to {resolved})"


def _is_duckdb(source: "Source") -> bool:
    if (source.type or "").startswith("duckdb"):
        return True
    return (source.url or "").startswith("duckdb:")


def _duckdb_uses_files_dir(source: "Source") -> bool:
    if not _is_duckdb(source):
        return False
    database = source.database or ":memory:"
    if source.attach_files and (database == ":memory:" or not Path(database).is_absolute()):
        return True
    return database not in (":memory:", "") and not Path(database).is_absolute()


def reads_project_files(source: "Source") -> bool:
    """Whether the source reads files relative to the project that declares it,
    so any check of it needs to know which directory that is."""
    if _duckdb_uses_files_dir(source):
        return True
    if _is_duckdb(source) and source.attach_files and source.base_dir:
        # attach_files checks base_dir against the project even when database:
        # is absolute, so the check needs to know which project that is.
        return not Path(source.base_dir).is_absolute()
    if (source.type or "") not in FILE_DATABASES or source.url:
        return False
    database = source.database or ":memory:"
    if database in (":memory:", "") or database.startswith("file:"):
        return False
    return not Path(database).is_absolute()


def source_for_copy(source: "Source", src_base: Path, dest_base: Path) -> "Source":
    """A source safe to write into dest_base, with ``base_dir`` when duckdb needs it.

    ``src_base`` is the source dashboard directory — the same value
    ``resolve_picker_source`` returns — not the attach scan dir. Deriving
    here is what keeps a relative authored ``base_dir: data/csv`` pointed
    at the csv dir after a workspace copy, instead of rewriting it to the
    repo root.
    """
    if not _duckdb_uses_files_dir(source):
        return source
    src_base, dest_base = src_base.resolve(), dest_base.resolve()
    if src_base == dest_base:
        return source
    scan = source_files_dir(source, src_base)
    try:
        rel = Path(os.path.relpath(scan, dest_base)).as_posix()
    except ValueError:
        rel = str(scan)
    return source.model_copy(update={"base_dir": rel})


def source_problems(store: "Store", layer: "SemanticLayer") -> list[str]:
    """Why a source is missing from `labeled_sources` — a metrics.yaml that
    could not be parsed.

    Enumeration skips those by design, so one broken file cannot hide every
    other source. But a command whose entire job is diagnosing the connection
    must not inherit that silence: `source test` exiting 0 with no output on a
    project whose source config is invalid is the diagnostic lying.
    """
    problems: list[str] = []
    sublayers = getattr(layer, "layers", None)
    targets = sublayers.items() if sublayers is not None else [(None, layer)]
    for repo, sub in targets:
        try:
            sub.metrics_file()
        except Exception as exc:
            problems.append(repo_problem(repo, exc) if repo else str(exc))
    return problems


def labeled_sources(store: "Store", layer: "SemanticLayer") -> list[LabeledSource]:
    """Every source: metrics.yaml first (per repo in a workspace), then each
    dashboard's `name.source` and its named `name.sources.<sname>` entries.

    Mirrors the dashboards' skip-broken semantics: a metrics.yaml that fails to
    parse is skipped rather than taking down enumeration for everything else.
    """
    entries = []
    sublayers = getattr(layer, "layers", None)
    if sublayers is not None:
        for repo, sub in sublayers.items():
            try:
                mf = sub.metrics_file()
            except Exception:
                continue
            if mf is not None:
                entries.append(LabeledSource(f"{repo}/metrics.yaml", mf.source, sub.store.root))
    else:
        try:
            mf = layer.metrics_file()
        except Exception:
            mf = None
        if mf is not None:
            entries.append(LabeledSource("metrics.yaml", mf.source, store.root))
    for name, dashboard in store.iter_loaded():
        base = store.path_for(name).parent
        entries.append(
            LabeledSource(
                f"{name}.source",
                dashboard.source,
                base,
                dashboard=name,
            )
        )
        for sname, named in dashboard.sources.items():
            entries.append(
                LabeledSource(
                    f"{name}.sources.{sname}",
                    named,
                    base,
                    dashboard=name,
                    sname=sname,
                )
            )
    return entries


def declared_sources(store: "Store", layer: "SemanticLayer") -> "Callable[[Source, Path], bool]":
    """Whether a source config is still declared by something on disk, for the
    execution registry to tell an edited source from a second one (#642).

    Read fresh on every call: the question is asked about a source that may have
    just been edited away, so a cached answer is the wrong one.
    """

    def still_declared(source: "Source", base_dir: Path) -> bool:
        wanted = source.model_dump_json()
        target = Path(base_dir).resolve()
        return any(
            entry.source.model_dump_json() == wanted and Path(entry.base_dir).resolve() == target
            for entry in labeled_sources(store, layer)
        )

    return still_declared


def source_wire_keys(entry: LabeledSource) -> tuple[str, ...]:
    """Canonical `labeled_sources` label, then the old MCP `dashboard:` alias.

    CLI `source list` and MCP `list_sources` emit the canonical key. Lookup
    still accepts the alias so a prompt that cached `dashboard:demo` keeps
    working (#421).
    """
    keys = [entry.label]
    if entry.dashboard is not None:
        if entry.sname is None:
            keys.append(f"dashboard:{entry.dashboard}")
        else:
            keys.append(f"dashboard:{entry.dashboard}.sources.{entry.sname}")
    return tuple(dict.fromkeys(keys))


@dataclass(frozen=True)
class PickerSource:
    """One option on the query-page source picker. `key` is what run/schema/save
    accept: empty string for this dashboard's default, a `sources:` name, or a
    `labeled_sources` label for anything else in the project."""

    key: str
    label: str
    kind: str
    source: "Source"
    base_dir: Path


def picker_sources(
    store: "Store", layer: "SemanticLayer", name: str, dashboard=None
) -> list[PickerSource]:
    """Every source this user can point a query at, including ones their
    credentials cannot actually open — a miss is a tile error, not a 404.
    """
    if dashboard is None:
        dashboard, _, _ = store.load(name)
    base = store.path_for(name).parent
    out = [
        PickerSource(
            "",
            f"default · {source_label(dashboard.source)}",
            "default",
            dashboard.source,
            base,
        )
    ]
    for sname, src in dashboard.sources.items():
        out.append(
            PickerSource(
                sname,
                f"{sname} · {source_label(src)}",
                "named",
                src,
                base,
            )
        )
    for entry in labeled_sources(store, layer):
        if entry.dashboard == name:
            continue
        out.append(
            PickerSource(
                entry.label,
                f"{entry.label} · {source_label(entry.source)}",
                "project",
                entry.source,
                entry.base_dir,
            )
        )
    return out


def distinct_picker_sources(entries: list[PickerSource]) -> list[PickerSource]:
    """Drop project entries that repeat an earlier entry's connection.

    A project defines the same connection in several places (a dashboard and
    metrics.yaml, say), and listing each one reads as separate connections.
    The dashboard's own default and named sources always stay: a name the
    author chose is a choice even when two point at the same place. File
    engines keep their directory in the comparison, since a relative database
    path resolves against it. Every key still resolves; this is only what a
    picker shows.
    """
    seen: set[str] = set()
    out = []
    for entry in entries:
        url = entry.source.url or ""
        # Never parse the url here: a broken one is a tile error, not a picker
        # error. An env reference could be a file database, so it keeps its place.
        local = entry.source.type in FILE_DATABASES or url.startswith(FILE_DATABASES) or "${" in url
        place = str(entry.base_dir) if local else ""
        identity = json.dumps([entry.source.model_dump(mode="json"), place], sort_keys=True)
        if entry.kind == "project" and identity in seen:
            continue
        seen.add(identity)
        out.append(entry)
    return out


def resolve_picker_source(
    store: "Store", layer: "SemanticLayer", name: str, key: str | None, dashboard=None
) -> tuple["Source", Path]:
    """Resolve a picker key to a Source and its dashboard directory.

    Callers that attach files derive the scan dir from this (engine and
    ``source_for_copy``); passing the derived path back in double-joins.
    """
    if dashboard is None:
        dashboard, _, _ = store.load(name)
    base_key, role, database, warehouse = split_source_context(key)
    if role is not None or database is not None or warehouse is not None:
        if base_key not in {entry.label for entry in labeled_sources(store, layer)}:
            raise ValueError("Role context requires a canonical project source reference")
        selected, base = resolve_picker_source(store, layer, name, base_key, dashboard)
        snowflake = provider(selected) == "snowflake"
        if database is not None and not snowflake:
            raise ValueError("Choosing a query database is only available for Snowflake")
        if warehouse is not None and not snowflake:
            raise ValueError("Choosing a query warehouse is only available for Snowflake")
        update = {}
        for field, value in (("role", role), ("database", database), ("warehouse", warehouse)):
            if value is None:
                continue
            if snowflake and not re.fullmatch(r"[A-Z_][A-Z0-9_$]*", value):
                value = '"' + value.replace('"', '""') + '"'
            update[field] = value
        args = {k: v for k, v in selected.connect_args.items() if k not in update}
        if database is not None:
            update["db_schema"] = None
            args.pop("schema", None)
        return selected.model_copy(update={**update, "connect_args": args}), base
    if not key or key == dashboard.default_source_name:
        return dashboard.source, store.path_for(name).parent
    for entry in picker_sources(store, layer, name, dashboard=dashboard):
        if entry.key == key:
            return entry.source, entry.base_dir
    for entry in labeled_sources(store, layer):
        if key in source_wire_keys(entry):
            return entry.source, entry.base_dir
    known = []
    seen: set[str] = set()
    for entry in picker_sources(store, layer, name, dashboard=dashboard):
        if entry.key and entry.key not in seen:
            known.append(entry.key)
            seen.add(entry.key)
    for entry in labeled_sources(store, layer):
        if entry.label not in seen:
            known.append(entry.label)
            seen.add(entry.label)
    raise KeyError(key, known)


def alias_for_picker_key(key: str, existing: dict, source=None) -> str:
    """A `sources:` name to copy a project source into, unique on this dashboard."""
    base, role, database, warehouse = split_source_context(key)
    if role is not None or database is not None or warehouse is not None:
        alias = alias_for_picker_key(base, {})
        for override, fallback in (
            (database, "database"),
            (warehouse, "warehouse"),
            (role, "role"),
        ):
            if override is not None:
                alias += "_" + (re.sub(r"[^a-zA-Z0-9_]+", "_", override).strip("_") or fallback)
        key = alias
    if key == "metrics.yaml" or key.endswith("/metrics.yaml"):
        wanted = "metrics"
    elif key.endswith(".source"):
        wanted = key[: -len(".source")].replace("/", "_")
    elif ".sources." in key:
        dash, _, sname = key.partition(".sources.")
        wanted = f"{dash.replace('/', '_')}_{sname}"
    else:
        wanted = key.replace("/", "_").replace(".", "_")
    if wanted not in existing:
        return wanted
    if source is not None and _copied_source_matches(existing[wanted], source):
        return wanted
    n = 2
    while f"{wanted}_{n}" in existing:
        n += 1
    return f"{wanted}_{n}"


def _copied_source_matches(existing: "Source", incoming: "Source") -> bool:
    """True when copying ``incoming`` would write the same YAML as ``existing``.

    Stripped shapes can match while ``existing`` still holds a plaintext
    password this copy would drop — reuse then points the tile at the old
    credential. Only reuse when existing is already the stripped shape.
    """
    cleaned_existing = source_as_project_yaml(existing)
    if cleaned_existing != source_as_project_yaml(incoming):
        return False
    raw = existing.model_dump(by_alias=True, exclude_none=True, exclude_defaults=True)
    return raw == cleaned_existing


def pick_main_source(entries: list[LabeledSource]) -> LabeledSource | None:
    """The auto-select contract: exactly one metrics.yaml source wins, else
    exactly one dashboard main source. Named ``sources:`` entries never
    participate. None means the caller must ask."""
    mains = [e for e in entries if e.sname is None]
    metrics_entries = [e for e in mains if e.dashboard is None]
    dash_entries = [e for e in mains if e.dashboard is not None]
    if len(metrics_entries) == 1:
        return metrics_entries[0]
    if len(dash_entries) == 1:
        return dash_entries[0]
    return None
