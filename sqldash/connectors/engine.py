"""SQLAlchemy-backed execution for every source type.

Engines contribute the maintained machinery — QueuePool, pre-ping, per-dialect
disconnect detection — but statements run on the raw DBAPI cursor, not through
SQLAlchemy's execute. That is deliberate: drivers like psycopg block inside
``execute()`` and can only be interrupted out-of-band, so the per-dialect
canceller must be attached to the token *before* the blocking call, which
requires the naked cursor.

Snowflake authenticates through ``creator=`` (never URL auth). A lock per
(account, user, authenticator), shared by every engine in the process, serializes
connection attempts so externalbrowser SSO opens at most one browser tab per serve
session whatever role, warehouse or dashboard asked, and an auth failure pauses
further attempts for that identity for a cooldown rather than opening one tab per
pooled connection. A failure specific to one source (a role, warehouse or database
it cannot use) pauses only that source. An externalbrowser pool has no overflow, and the connector's
Keychain token is kept in memory after the first read (``snowflake_tokens``), so
later sessions do not go back to the Keychain. A browser sign-in
runs off the request thread with a bounded wait: the driver waits for the SSO
callback forever, and ``login_timeout`` does not cover that wait, so an
unanswered sign-in used to hang every request behind the lock. A sign-in that
finishes late is handed to the next connection attempt, and one still unanswered
after ``SIGNIN_ABANDON`` is dropped so the next request can open a fresh tab. Pool-checkout
timeouts are classified as :class:`ConnectionBusy` — after an idle spell,
pool_recycle replaces stale sessions in a burst and the registry's single
retry absorbs it. A Snowflake session the server ended (aborted, expired) counts
as a disconnect: pre-ping swaps it for a fresh one at checkout, and one lost
mid-query becomes :class:`ConnectionLost` for that same retry.
"""

import contextlib
import itertools
import re
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from secrets import token_hex
from typing import TYPE_CHECKING, Any

import sqlalchemy
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.exc import DBAPIError, DisconnectionError, SQLAlchemyError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.pool import QueuePool

from sqldash.connectors.base import (
    CancelToken,
    ConnectionBusy,
    ConnectionLost,
    Connector,
    ConnectorError,
    TableInfo,
    source_url,
)
from sqldash.connectors.engine_urls import (
    DRIVERS,
    FILE_DATABASES,
    build_url,
    resolve_source_env,
    snowflake_connect_kwargs,
)
from sqldash.connectors.roles import install_role, role_connect_args, role_context
from sqldash.connectors.wire import infer_columns
from sqldash.models.results import QueryResult
from sqldash.models.source import Source, scrub_error_text
from sqldash.project.sources import (
    attach_data_files,
    attach_dir_missing,
    database_file_missing,
    source_files_dir,
)
from sqldash.secrets import interpolate_env, resolve_credentials

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

FETCH_BATCH = 1000
POOL_SIZE = 4
POOL_TIMEOUT = 120
MAX_OVERFLOW = 4
AUTH_FAILURE_COOLDOWN = 30.0
SIGNIN_WAIT = 120.0
SIGNIN_RECHECK = 1.0
SIGNIN_ABANDON = 300.0
SIGNIN_PENDING = (
    "Snowflake is waiting for you to sign in: finish the sign-in in the browser tab "
    "that opened (check your other browser windows), then run this again."
)
CONNECT_TIMEOUT_S = 10
SNOWFLAKE_POLL_BACKOFF = (0.05, 0.1, 0.2, 0.4, 0.8, 1.0)
SNOWFLAKE_TIMEOUT = (
    "query exceeded Snowflake network_timeout — "
    "set connect_args.network_timeout on the source, "
    "or omit it (no client timebomb is the default)"
)
SNOWFLAKE_SESSION_GONE = frozenset({390111, 390112, 390114})
SNOWFLAKE_CONNECTION_CLOSED = 250002
SNOWFLAKE_SOURCE_ERRNOS = frozenset({390189, 390201, 390202, 390203})
SNOWFLAKE_SIGNIN_ERRNOS = frozenset(
    {250006, 250008, 250009, 251005, 251006, 251008, 251010, 251011, 251014, 251015, 251016}
)
SNOWFLAKE_SIGNIN_MARKERS = (
    "differs from the user currently logged in",
    "saml",
    "identity provider",
    "idp",
    "incorrect username or password",
    "authenticat",
    "browser",
    "sign-in",
    "sign in",
    "cancel",
)
SNOWFLAKE_RELOGIN = re.compile(r"\s*New login required to access the service\.")
SNOWFLAKE_SESSION_ENDED = (
    " Snowflake ended this session on the server (it was aborted or expired); "
    "sqldash opens a new one on the next run."
)
_DOUBLED_ERRNO = re.compile(r"^(\d{6}): \1: ")
# Handshake kwarg that is connect-only, keyed on the SQLAlchemy *driver*
# (the part after '+' in postgresql+psycopg). Type is the wrong axis:
# `driver: postgresql+pg8000` on a postgres source used to work and
# became TypeError when we injected connect_timeout. Unknown drivers
# get nothing — guessing a name is how a working source breaks.
_DRIVER_CONNECT_TIMEOUT = {
    "psycopg": "connect_timeout",
    "psycopg2": "connect_timeout",
    "pymysql": "connect_timeout",
    "mysqldb": "connect_timeout",
}
# Bare dialect with no '+' uses SQLAlchemy's default DBAPI.
_BARE_DIALECT_DRIVER = {
    "postgresql": "psycopg2",
    "postgres": "psycopg2",
    "mysql": "mysqldb",
    "mariadb": "mysqldb",
}

INFORMATION_SCHEMA_SQL = """
SELECT table_schema, table_name, column_name, data_type
FROM information_schema.columns
WHERE table_schema NOT IN ('pg_catalog', 'information_schema', 'INFORMATION_SCHEMA')
ORDER BY table_schema, table_name, ordinal_position
"""

PRECISION_DIALECTS = frozenset({"snowflake", "postgresql"})

INFORMATION_SCHEMA_PRECISION_SQL = """
SELECT table_schema, table_name, column_name, data_type, numeric_precision, numeric_scale
FROM information_schema.columns
WHERE table_schema NOT IN ('pg_catalog', 'information_schema', 'INFORMATION_SCHEMA')
ORDER BY table_schema, table_name, ordinal_position
"""

EXACT_NUMERIC_TYPES = frozenset({"NUMBER", "NUMERIC", "DECIMAL"})


def _column_type(dtype: object, precision: object = None, scale: object = None) -> str:
    """information_schema splits NUMBER(38,10) into a bare type name and two
    columns; put them back together the way DESCRIBE shows the type."""
    name = str(dtype)
    if precision is None or name.upper() not in EXACT_NUMERIC_TYPES:
        return name
    return f"{name}({precision},{scale or 0})"


def _tables_from_rows(rows) -> list[TableInfo]:
    tables: dict[tuple, TableInfo] = {}
    for schema, table, column, *dtype in rows:
        info = tables.setdefault((schema, table), TableInfo(name=table, schema=schema))
        info.columns.append((column, _column_type(*dtype)))
    return list(tables.values())


_ATTACH_READERS = {".csv": "read_csv_auto", ".parquet": "read_parquet"}

_DUCKDB_BLOCKED = re.compile(
    r'Cannot access (?:file|directory) "([^"]*)" - file system operations are disabled'
)
CONFINEMENT_HINT = (
    "sqldash confines this duckdb source to its project directory, so SQL cannot "
    "read files elsewhere on the machine. To let it out, set 'external_access: true' "
    "on the source — that gives everyone who can reach the server the server user's "
    "own file access."
)


def duckdb_allowed_dirs(source: "Source", base_dir: Path | None, scan_dir: Path) -> list[str]:
    """The directories a confined duckdb source may read: the folder holding its
    `database:` file, and the dir its own files resolve against — `base_dir:`
    when set, else the dashboard's folder, which is what `attach_files` walks and
    what a relative path in SQL means.

    Both, not only the database's folder, because a project keeps its data where
    it likes: `database: warehouse/w.duckdb` with csvs at the project root is an
    authored layout, and confining to the database's folder alone stopped such a
    project reading its own files (#622 follow-up).

    Two sources over one `.duckdb` file still cannot be given *different* reach
    (see `_compatible`), but the ordinary "one warehouse file with sibling project
    dirs beside it" layout is not a disagreement: the file's own folder is on both
    allowlists and each project dir sits under it, so neither source gains
    anything the other's reach did not already include.
    """
    try:
        database = interpolate_env(source.database) or ":memory:"
    except Exception:
        database = ":memory:"
    candidates = [scan_dir]
    if database not in (":memory:", "") and not database.startswith("file:"):
        candidates.append((scan_dir / database).parent)
    dirs: list[str] = []
    for candidate in candidates:
        try:
            resolved = str(Path(candidate).resolve())
        except OSError:
            resolved = str(candidate)
        if resolved not in dirs:
            dirs.append(resolved)
    return dirs


_confinements: dict[str, tuple[str, ...] | None] = {}
_confinement_holders: dict[str, int] = {}
_confinement_lock = threading.Lock()
SHARED_FILE_CONFLICT = (
    "two sources in this server want different file access on the same duckdb "
    "database, and DuckDB gives one database file one configuration per process. "
    "{database} is already open {held}, and this source asks for {wanted}. Point "
    "them at the same directory (or give them the same 'external_access'), or "
    "give each its own database file. Widening is never applied under a source "
    "that a dashboard still declares; if none does and this persists, restart the "
    "server."
)


class SharedFileConflict(ConnectorError):
    """A source refused because another one holds its duckdb file with different reach."""


STALE_CONFINEMENT = (
    "{database} is already open {held} by something else in this process, and "
    "DuckDB gives one database file one configuration per process, so this source "
    "cannot be given the reach it asks for ({wanted}). If no other source on this "
    "file disagrees, restart the server."
)


def _held_description(held: tuple[str, ...] | None) -> str:
    if held is None:
        return "with external access allowed"
    return "confined to " + ", ".join(held)


def dirs_within(dirs: tuple[str, ...], bounds: tuple[str, ...]) -> bool:
    return all(any(Path(one).is_relative_to(Path(bound)) for bound in bounds) for one in dirs)


def _compatible(wanted: tuple[str, ...] | None, held: tuple[str, ...] | None) -> bool:
    """Whether a database file already open with ``held`` can serve ``wanted``.

    DuckDB settings are per database file per process and cannot be widened after
    the latch, so the second source on a file gets the first one's allowlist or
    nothing. Equality is too strict for the ordinary "one warehouse file with
    project dirs beside it" layout: the file's folder is on both lists and each
    project dir sits under it, so neither source can see anything the other's
    reach did not already include. Those are served.

    Coverage one way round is not enough, because it also serves a source *nested
    inside* another project, handing it the parent project's files — a read the
    narrower confinement refused (#638 review). So each side's reach must sit
    inside the other's: then being served on the held allowlist grants the second
    source nothing it was not already going to have. Anything else is refused,
    including any mix of confined and ``external_access: true``.
    """
    if held is None or wanted is None:
        return held == wanted
    return dirs_within(wanted, held) and dirs_within(held, wanted)


def _latched_dirs(cursor, key: str) -> tuple[str, ...]:
    """The allowlist DuckDB reports for a database that is already locked down.

    The record is the usual answer to "what is this file confined to", but it is
    dropped when the last engine on the file goes, and the instance can outlive
    that if a connection is still open. DuckDB itself still knows, so ask it
    rather than refusing a source whose reach the live allowlist already covers.
    The reported list carries trailing separators and the database's own scratch
    directory, neither of which is a project dir.
    """
    try:
        cursor.execute("SELECT current_setting('allowed_directories')")
        reported = cursor.fetchone()[0] or []
    except Exception:
        return ()
    scratch = f"{key}.tmp"
    dirs = []
    for entry in reported:
        directory = str(entry).rstrip("/") or "/"
        if directory != scratch and directory not in dirs:
            dirs.append(directory)
    return tuple(dirs)


def _release_confinement(key: str) -> None:
    """Forget a database file's claim once no engine holds it any more.

    DuckDB keeps the database instance, and its access settings, alive while any
    connection to the file is open, so a claim is only safe to forget when every
    engine on it has been disposed. Holding it for the life of the process
    instead meant a source edited to add ``external_access: true`` was refused
    until restart, blamed on a second source that did not exist.

    If the instance somehow outlives its engines, the next connection finds
    external access already off with nothing claiming it and is refused by
    ``_confine_duckdb_connection`` rather than handed reach it did not ask for.
    """
    with _confinement_lock:
        holders = _confinement_holders.get(key, 0) - 1
        if holders > 0:
            _confinement_holders[key] = holders
            return
        _confinement_holders.pop(key, None)
        _confinements.pop(key, None)


def _confine_duckdb_connection(dbapi_conn, key: str, dirs: tuple[str, ...] | None) -> None:
    """Give one duckdb connection exactly the reach its source asked for, or refuse.

    ``enable_external_access`` and ``allowed_directories`` are *database-wide* in
    DuckDB, and DuckDB keeps one database instance per file per process, so two
    sources over one ``.duckdb`` file cannot be given different reach, however
    they are pooled. The first one to connect therefore decides for the file, and
    the live setting cannot be read as "nobody has claimed this yet" — an
    ``external_access: true`` source leaves it untouched, so a confined source
    that only looked at DuckDB would latch the file under it. What each database
    was claimed for is recorded here instead, and a source that disagrees is
    refused rather than handed reach it did not ask for: a confined source
    inheriting another project's allowlist, or an opted-out source silently
    confined and failing on DuckDB's raw permission error.

    The outcome does not depend on connection order. Whichever source connects
    first gets exactly what it asked for; the one that disagrees is told why,
    either way round.

    The SETs run only when this connection is the one making the claim, or when
    the database was reopened after every connection to it closed (DuckDB refuses
    to re-enable external access while a database is running, so external access
    reading true means the instance is fresh). The allowlist has to land before
    the latch; after it, DuckDB refuses both.

    The record says what was actually latched, so it is rewritten by whichever
    connection runs the SETs. A covered source that finds a fresh instance
    latches its own narrower list, and leaving the earlier, wider claim recorded
    would judge the first source covered when it came back, serve it without a
    SET, and then let DuckDB refuse it its own data (#639 review).
    """
    cursor = dbapi_conn.cursor()
    try:
        cursor.execute("SELECT current_setting('enable_external_access')")
        open_access = bool(cursor.fetchone()[0])
        with _confinement_lock:
            claimed = key in _confinements
            held = _confinements.get(key)
            if claimed and not _compatible(dirs, held):
                raise SharedFileConflict(
                    SHARED_FILE_CONFLICT.format(
                        database=key,
                        held=_held_description(held),
                        wanted=_held_description(dirs),
                    )
                )
            if not claimed and not open_access:
                live = _latched_dirs(cursor, key)
                if not _compatible(dirs, live):
                    raise ConnectorError(
                        STALE_CONFINEMENT.format(
                            database=key,
                            held=_held_description(live),
                            wanted=_held_description(dirs),
                        )
                    )
                _confinements[key] = live
            if open_access:
                _confinements[key] = dirs
        if dirs is None or not open_access:
            return
        quoted = ", ".join("'" + directory.replace("'", "''") + "'" for directory in dirs)
        # semgrep: SET cannot take binds; each directory is quote-escaped above
        # nosemgrep: formatted-sql-query, sqlalchemy-execute-raw-query
        cursor.execute(f"SET allowed_directories=[{quoted}]")
        cursor.execute("SET enable_external_access=false")
    finally:
        cursor.close()


def _confine(engine: "Engine", source: "Source", base_dir: Path | None, scan_dir: Path) -> "Engine":
    """Confine a duckdb engine's file access to the project, unless the author opted out.

    Ad-hoc SQL is caller-supplied and only read-guarded (`sqlguard`), and for a
    warehouse the credential's own grants bound what that read can reach. A
    duckdb source has no such credential: its reach is the server user's
    filesystem, which includes the owner's `~/.config/sqldash/profiles.yaml`
    (#622). Every surface that runs SQL shares one pooled engine per source, so
    the confinement belongs on the connection, not on one route.

    An ``external_access: true`` source registers its reach too, rather than
    skipping the handler: an opt-out that nothing records is one a later confined
    source can quietly revoke, since the setting is database-wide.
    """
    if engine.dialect.name != "duckdb":
        return engine
    dirs = (
        None if source.external_access else tuple(duckdb_allowed_dirs(source, base_dir, scan_dir))
    )
    key = _duckdb_database_key(engine, scan_dir)
    with _confinement_lock:
        _confinement_holders[key] = _confinement_holders.get(key, 0) + 1

    @event.listens_for(engine, "connect")
    def confine(dbapi_conn, record):
        try:
            _confine_duckdb_connection(dbapi_conn, key, dirs)
        except BaseException:
            with contextlib.suppress(Exception):
                dbapi_conn.close()
            raise

    @event.listens_for(engine, "engine_disposed")
    def release(disposed):
        _release_confinement(key)

    return engine


def _duckdb_database_key(engine: "Engine", scan_dir: Path) -> str:
    """What DuckDB shares a database instance by: the database file the engine
    will open, resolved. Read off the URL rather than the source, because that is
    what the driver actually receives.

    An in-memory database is never shared between connections, so it gets a key
    of its own and each one is confined on its own terms.
    """
    database = engine.url.database or ""
    if database in ("", ":memory:") or database.startswith("file:"):
        return f":memory:{token_hex(8)}"
    try:
        return str((scan_dir / database).resolve())
    except OSError:
        return database


def explain_confinement(message: str, source: "Source", base_dir: Path | None) -> str:
    """Turn DuckDB's bare permission error into one that says why, and how to allow it."""
    match = _DUCKDB_BLOCKED.search(message)
    if match is None or source.external_access:
        return message
    scan_dir = source_files_dir(source, Path(base_dir)) if base_dir else Path(".")
    allowed = ", ".join(duckdb_allowed_dirs(source, base_dir, scan_dir))
    return f"cannot read {match.group(1)!r} — it is outside {allowed}. {CONFINEMENT_HINT}"


def _duckdb_data_files(base_dir: Path, database: str) -> list[Path]:
    """Sibling csv/parquet files next to the database (and in its `data/` subdir), sorted."""
    data_dir = base_dir if database == ":memory:" else (base_dir / database).resolve().parent
    return attach_data_files(data_dir)


def _duckdb_view_name(path: Path) -> str:
    return re.sub(r"\W", "_", path.stem)


def _duckdb_attach_sql(base_dir: Path, database: str) -> list[str]:
    """CREATE VIEW statements exposing sibling csv/parquet files as queryable views."""
    statements = []
    for path in _duckdb_data_files(base_dir, database):
        reader = _ATTACH_READERS[path.suffix.lower()]
        escaped = str(path).replace("'", "''")
        statements.append(
            f'CREATE OR REPLACE VIEW "{_duckdb_view_name(path)}" '
            f"AS SELECT * FROM {reader}('{escaped}')"
        )
    return statements


def _duckdb_attach_snapshot(base_dir: Path, database: str) -> tuple[tuple[str, int], ...]:
    """What the data folder holds right now: (path, mtime_ns) per attachable file.

    Cheap enough to take on every pool checkout, which is what keeps a csv
    dropped in after the first query queryable without a restart (#279).
    """
    snapshot = []
    for path in _duckdb_data_files(base_dir, database):
        try:
            snapshot.append((str(path), path.stat().st_mtime_ns))
        except OSError:
            continue
    return tuple(snapshot)


def _duckdb_sync_views(dbapi_conn, record_info: dict, base_dir: Path, database: str) -> None:
    """Bring one connection's views in line with the folder; no-op when nothing changed.

    Views for files that disappeared are dropped so a stale name fails with
    DuckDB's plain "does not exist" rather than a read error naming a path
    the author already deleted.
    """
    snapshot = _duckdb_attach_snapshot(base_dir, database)
    previous = record_info.get("sqldash_attach")
    if snapshot == previous:
        return
    current_names = {_duckdb_view_name(Path(entry[0])) for entry in snapshot}
    cursor = dbapi_conn.cursor()
    try:
        for entry in previous or ():
            name = _duckdb_view_name(Path(entry[0]))
            if name not in current_names:
                # semgrep: view names are reduced to word characters by _duckdb_view_name
                # nosemgrep: formatted-sql-query, sqlalchemy-execute-raw-query
                cursor.execute(f'DROP VIEW IF EXISTS "{name}"')
        for statement in _duckdb_attach_sql(base_dir, database):
            cursor.execute(statement)
    finally:
        cursor.close()
    record_info["sqldash_attach"] = snapshot


def snowflake_session_gone(exc: BaseException) -> bool:
    """Whether a Snowflake error means the session itself is gone, not the statement.

    snowflake-sqlalchemy keeps the default ``is_disconnect``, which says no to
    everything, so a killed session failed every pooled checkout once
    (``390111: Session no longer exists``) instead of being replaced.
    """
    exc = getattr(exc, "orig", None) or exc
    errno = getattr(exc, "errno", None)
    sqlstate = str(getattr(exc, "sqlstate", None) or "")
    return (
        errno in SNOWFLAKE_SESSION_GONE
        or errno == SNOWFLAKE_CONNECTION_CLOSED
        or sqlstate.startswith("08")
    )


def connect_args_for(source: "Source") -> dict[str, Any]:
    """Author connect_args, plus a handshake timeout on drivers that accept one.

    `engine.connect()` is a blocking socket connect with nothing for
    `cancel()` to attach to, so an unreachable host wedged a pool worker
    until the OS TCP timeout (minutes). Snowflake already sets
    `login_timeout=30`. psycopg/pymysql get `connect_timeout`. A custom
    `driver:` is not assumed to accept the default driver's kwargs.
    redshift's `timeout` is a persistent socket deadline — do not
    default it. trino has no handshake knob.
    """
    args = interpolate_env(source.connect_args) if source.connect_args else {}
    key = _DRIVER_CONNECT_TIMEOUT.get(_effective_driver(source))
    if key is not None and key not in args:
        return {**args, key: CONNECT_TIMEOUT_S}
    return args


def _effective_driver(source: "Source") -> str:
    """The DBAPI name the engine will actually load — from the URL when
    there is one, otherwise from `driver:` / the type default.

    Type and `driver:` are not enough: `url: postgresql+pg8000://…` on a
    postgres source used to pick up psycopg's connect_timeout. A missing
    type (url-only sqlite) must not crash.
    """
    if source.url:
        try:
            name = source_url(source).drivername or ""
        except Exception:
            return ""
    else:
        name = source.driver or DRIVERS.get(source.type) or ""
    if "+" in name:
        return name.rsplit("+", 1)[-1]
    return _BARE_DIALECT_DRIVER.get(name, "")


class Cooldown:
    """Pause further login attempts for a while after one failed, so SSO does not
    open a browser tab per pooled connection."""

    def __init__(self) -> None:
        self.failed_at = 0.0
        self.failure: str | None = None

    def record(self, message: str) -> None:
        self.failed_at = time.monotonic()
        self.failure = message

    def clear(self) -> None:
        self.failure = None

    def check(self) -> None:
        if self.failure and time.monotonic() - self.failed_at < AUTH_FAILURE_COOLDOWN:
            raise ConnectorError(
                self.failure + "\n(auth just failed — further attempts paused for "
                f"{AUTH_FAILURE_COOLDOWN:.0f}s so SSO doesn't open a browser tab "
                "per connection)"
            )


def signin_failed(exc: BaseException) -> bool:
    """Whether a Snowflake login failed on who the user is, not on what the source asked for.

    Only these pause every source of the identity. A role, warehouse or database the
    user cannot use is one source's problem: sharing it rejected healthy dashboards
    with another dashboard's role error.
    """
    exc = getattr(exc, "orig", None) or exc
    errno = getattr(exc, "errno", None)
    message = str(exc).lower()
    if errno in SNOWFLAKE_SOURCE_ERRNOS or "does not exist or not authorized" in message:
        return False
    if errno in SNOWFLAKE_SIGNIN_ERRNOS:
        return True
    if isinstance(errno, int) and 390000 <= errno < 391000:
        return True
    return any(marker in message for marker in SNOWFLAKE_SIGNIN_MARKERS)


class SignInGate:
    """Login state one Snowflake identity shares across every engine in the process.

    Engines are cached per source config, so each role, warehouse and database a
    user picks, each warehouse-fallback probe and each dashboard directory gets its
    own. When the lock, the pending sign-in and the failure cooldown lived in the
    engine, every one of them raced its own browser sign-in and Keychain read. Keyed
    on (account, user, authenticator), they wait for the one sign-in in flight, and
    a failed sign-in pauses all of them. A failure specific to one source pauses
    only that source's own cooldown.
    """

    def __init__(self, user: Any) -> None:
        self.lock = threading.Lock()
        self.user = user
        self.cooldown = Cooldown()
        self.inflight: SignInAttempt | None = None

    def fail(self, exc: BaseException, local: Cooldown) -> ConnectorError:
        message = str(exc)
        if "differs from the user currently logged in" in message:
            message = (
                f"{message}\n\nattempted user: {self.user!r} — the "
                "'username' in your source/profile must exactly match the "
                "account you sign in with at your IdP (usually your work email)."
            )
        (self.cooldown if signin_failed(exc) else local).record(message)
        return ConnectorError(message)


class SignInAttempt(threading.Thread):
    """One login, off the request thread, so a browser sign-in nobody completes
    cannot hold a request (and the gate's lock) forever: the driver waits for the
    SSO callback with no timeout of its own."""

    def __init__(self, gate: SignInGate, local: Cooldown, login: Callable[[], Any]) -> None:
        super().__init__(daemon=True, name="snowflake-signin")
        self.gate = gate
        self.local = local
        self.login = login
        self.done = threading.Event()
        self.started = time.monotonic()
        self.orphaned = False
        self.conn = None
        self.error: Exception | None = None

    def abandoned(self) -> bool:
        """A sign-in nobody will finish (tab closed) must not block retries until
        restart. Its thread stays parked in the driver; it is a daemon."""
        return not self.done.is_set() and time.monotonic() - self.started > SIGNIN_ABANDON

    def run(self) -> None:
        try:
            self.conn = self.login()
        except Exception as exc:
            self.error = exc
        finally:
            self.done.set()
        with self.gate.lock:
            if self.gate.inflight is self:
                self.gate.inflight = None
            if not self.orphaned and self.error is not None:
                self.gate.fail(self.error, self.local)
            unclaimed = self.orphaned and self.conn is not None
        if unclaimed:
            with contextlib.suppress(Exception):
                self.conn.close()


_SIGNIN_GATES: dict[tuple[str, str, str], SignInGate] = {}
_SIGNIN_GATES_LOCK = threading.Lock()


def signin_gate(kwargs: dict[str, Any]) -> SignInGate:
    """The process-wide gate for the identity these Snowflake connect kwargs log in as."""
    key = (
        str(kwargs.get("account") or "").lower(),
        str(kwargs.get("user") or "").lower(),
        str(kwargs.get("authenticator") or "snowflake").lower(),
    )
    with _SIGNIN_GATES_LOCK:
        return _SIGNIN_GATES.setdefault(key, SignInGate(kwargs.get("user")))


def build_engine(source: "Source", base_dir: Path | None) -> "Engine":
    """Construct the pooled engine for a source; secrets resolve here, at build time."""
    common = {
        "pool_pre_ping": True,
        "pool_recycle": 3600,
        "pool_size": POOL_SIZE,
        "max_overflow": MAX_OVERFLOW,
        "pool_timeout": POOL_TIMEOUT,
    }
    if source.type != "snowflake":
        args = role_connect_args(source, connect_args_for(source))
        if args:
            common["connect_args"] = args
    if source.type == "snowflake":
        try:
            import snowflake.connector  # noqa: PLC0415 — optional [snowflake] extra
            import snowflake.sqlalchemy  # noqa: PLC0415 — optional [snowflake] extra
        except ImportError as exc:
            raise ConnectorError(
                "snowflake support is not installed — run: pip install 'sqldash[snowflake]'"
            ) from exc
        except AttributeError as exc:
            raise ConnectorError(
                f"snowflake-sqlalchemy does not support the installed SQLAlchemy "
                f"{sqlalchemy.__version__}; install SQLAlchemy<2.1: "
                "pip install 'sqlalchemy>=2.0,<2.1'"
            ) from exc
        kwargs = snowflake_connect_kwargs(source, resolve_credentials(source))
        browser = kwargs.get("authenticator") == "externalbrowser"
        if browser:
            from sqldash.connectors.snowflake_tokens import (  # noqa: PLC0415 — optional [snowflake] extra
                remember_snowflake_tokens,
            )

            remember_snowflake_tokens()
            common["max_overflow"] = 0
        gate = signin_gate(kwargs)
        local = Cooldown()
        mine: dict[str, SignInAttempt | None] = {"attempt": None}

        def login():
            return snowflake.connector.connect(**kwargs)

        def claim(attempt):
            mine["attempt"] = None
            if attempt.error is not None:
                raise gate.fail(attempt.error, local) from attempt.error
            gate.cooldown.clear()
            local.clear()
            return attempt.conn

        def connect():
            waiting = gate.inflight
            if (
                waiting is not None
                and not waiting.abandoned()
                and not waiting.done.wait(SIGNIN_RECHECK)
            ):
                raise ConnectorError(SIGNIN_PENDING)
            with gate.lock:
                attempt = mine["attempt"]
                if attempt is not None and attempt.orphaned:
                    mine["attempt"] = attempt = None
                if attempt is not None and attempt.done.is_set():
                    return claim(attempt)
                gate.cooldown.check()
                local.check()
                if not browser:
                    try:
                        conn = login()
                    except Exception as exc:
                        raise gate.fail(exc, local) from exc
                    gate.cooldown.clear()
                    local.clear()
                    return conn
                inflight = gate.inflight
                if inflight is not None and inflight.abandoned():
                    inflight.orphaned = True
                    gate.inflight = inflight = None
                if inflight is not None:
                    if not inflight.done.wait(SIGNIN_RECHECK):
                        raise ConnectorError(SIGNIN_PENDING)
                    if inflight is mine["attempt"]:
                        return claim(inflight)
                    if inflight.error is not None and signin_failed(inflight.error):
                        raise gate.fail(inflight.error, local) from inflight.error
                attempt = mine["attempt"] = gate.inflight = SignInAttempt(gate, local, login)
                attempt.start()
                if not attempt.done.wait(SIGNIN_WAIT):
                    raise ConnectorError(SIGNIN_PENDING)
                return claim(attempt)

        engine = create_engine("snowflake://sqldash", creator=connect, **common)

        @event.listens_for(engine, "handle_error")
        def session_gone(context):
            if not snowflake_session_gone(context.original_exception):
                return
            # A pre-ping that reports a disconnect makes the pool raise
            # InvalidatePoolError, which retires every pooled session and logs each
            # one in again (an SSO prompt apiece on externalbrowser). Raising
            # DisconnectionError instead replaces only the connection that died.
            if context.is_pre_ping:
                raise DisconnectionError(_clean(context.original_exception))
            context.is_disconnect = True
            context.invalidate_pool_on_disconnect = False

        return engine
    # Callers pass the dashboard dir. File databases must resolve `database:`
    # against the scan dir or `base_dir: data/csv` + `database: app.duckdb`
    # opens a decoy at the dashboard root.
    scan_dir = source_files_dir(source, Path(base_dir)) if base_dir else Path(".")
    url = build_url(
        source,
        scan_dir if source.type in FILE_DATABASES and base_dir is not None else base_dir,
    )
    if source.type in FILE_DATABASES and base_dir is not None:
        missing = database_file_missing(resolve_source_env(source, ("database",)), scan_dir)
        if missing:
            raise ConnectorError(missing)
    if source.type == "duckdb":
        try:
            engine = create_engine(url, poolclass=QueuePool, **common)
        except Exception as exc:
            raise ConnectorError(f"cannot create engine for source: {exc}") from exc
        if source.attach_files and not source.url:
            missing = attach_dir_missing(source, scan_dir)
            if missing:
                raise ConnectorError(f"{missing}; attached 0 files")
            # The resolved database, not the authored one: `data_dir` is derived
            # from it, so an unexpanded "${env:DB}" pointed the sibling-file scan
            # at the wrong directory and the views silently never appeared.
            database = interpolate_env(source.database) or ":memory:"

            @event.listens_for(engine, "checkout")
            def attach_views(dbapi_conn, record, proxy):
                _duckdb_sync_views(dbapi_conn, record.info, scan_dir, database)

        return _confine(engine, source, base_dir, scan_dir)
    try:
        engine = create_engine(url, **common)
    except TypeError:
        engine = create_engine(
            url,
            pool_pre_ping=True,
            pool_recycle=3600,
            connect_args=common.get("connect_args", {}),
        )
    except Exception as exc:
        raise ConnectorError(f"cannot create engine for source: {exc}") from exc
    return _confine(engine, source, base_dir, scan_dir)


def _cancel_for(dialect: str, dbapi_conn) -> Callable[[], None] | None:
    """Best-effort out-of-band canceller for the dialect, or None when the driver has none.

    Snowflake is not here: its canceller needs the query id, which only exists once
    the statement is submitted, so :func:`_run_snowflake` attaches its own.
    """
    if dialect == "duckdb":
        return lambda: dbapi_conn.interrupt()
    if dialect == "postgresql":
        driver = getattr(dbapi_conn, "driver_connection", dbapi_conn)
        return lambda: driver.cancel_safe()
    return None


def _snowflake_canceller(dbapi_conn, qid: str) -> Callable[[], None]:
    """SYSTEM$CANCEL_QUERY on the query's own connection, which the driver lets threads share."""
    qid = str(uuid.UUID(qid))

    def cancel() -> None:
        with contextlib.suppress(Exception):
            cursor = dbapi_conn.cursor()
            try:
                cursor.execute("SELECT SYSTEM$CANCEL_QUERY(%s)", (qid,))
            finally:
                cursor.close()

    return cancel


def _snowflake_description(description: list) -> list:
    """The cursor description with Snowflake's numeric field ids swapped for type
    names, so a VARIANT is typed from its column rather than from its JSON text.
    A FIXED column with a scale is a decimal."""
    from snowflake.connector import constants  # noqa: PLC0415 — optional [snowflake] extra

    named = []
    for column in description:
        native = constants.FIELD_ID_TO_NAME.get(column[1], str(column[1]))
        if native == "FIXED" and column[5]:
            native = "DECIMAL"
        named.append((column[0], native, *column[2:]))
    return named


def _run_snowflake(cursor, dbapi_conn, sql: str, params, cancel_token: CancelToken) -> None:
    """Submit asynchronously so the query id exists while it runs, then wait for it.

    The blocking ``execute()`` only learns the id when the server answers, which it
    does once the query is done, so a cancel had nothing to name until then.
    Results load through ``query_result`` into the usual fetch path; an error the
    query hits while running comes back from that endpoint with no SQLSTATE.
    """
    if params:
        cursor.execute_async(sql, params)
    else:
        cursor.execute_async(sql)
    qid = cursor.sfqid
    canceller = _snowflake_canceller(dbapi_conn, qid)
    cancel_token.attach(canceller)
    if cancel_token.cancelled:
        raise ConnectorError("query cancelled")
    timeout = getattr(dbapi_conn, "network_timeout", None)
    started = time.monotonic()
    pauses = itertools.chain(SNOWFLAKE_POLL_BACKOFF, itertools.repeat(SNOWFLAKE_POLL_BACKOFF[-1]))
    for pause in pauses:
        if not dbapi_conn.is_still_running(dbapi_conn.get_query_status(qid)):
            break
        if timeout and time.monotonic() - started >= timeout:
            canceller()
            raise ConnectorError(SNOWFLAKE_TIMEOUT)
        if cancel_token.wait(pause):
            raise ConnectorError("query cancelled")
    cursor.query_result(qid)


PARAMSTYLES = {
    "duckdb": "qmark",
    "sqlite": "qmark",
    "postgres": "pyformat",
    "postgresql": "pyformat",
    "snowflake": "pyformat",
    "mysql": "format",
    "mariadb": "format",
    "bigquery": "pyformat",
    "redshift": "format",
    "athena": "pyformat",
}


def paramstyle_for(source: "Source") -> str:
    """Bind-parameter style for the source, probing a throwaway engine for unknown dialects."""
    if source.type in PARAMSTYLES:
        return PARAMSTYLES[source.type]
    try:
        if source.url:
            engine = create_engine(source_url(source))
        else:
            dialect = source.driver or DRIVERS.get(source.type, source.type)
            engine = create_engine(f"{dialect}://")
        try:
            return engine.dialect.paramstyle
        finally:
            engine.dispose()
    except ConnectorError:
        raise
    except Exception as exc:
        raise ConnectorError(f"cannot resolve dialect for source: {exc}") from exc


class EngineConnector(Connector):
    """Shared connector over one engine-owned pool; the engine is built lazily on first use."""

    def __init__(self, source: Source, base_dir: Path | None) -> None:
        self.source = source
        self.base_dir = base_dir
        self._engine = None
        self._lock = threading.Lock()

    @property
    def engine(self):
        with self._lock:
            if self._engine is None:
                engine = build_engine(self.source, self.base_dir)
                try:
                    install_role(engine, self.source)
                except BaseException:
                    engine.dispose()
                    raise
                self._engine = engine
            return self._engine

    def connect(self) -> None:
        _ = self.engine

    def _explain(self, exc: BaseException) -> str:
        return explain_confinement(_clean(exc, self.source), self.source, self.base_dir)

    def execute(
        self, sql: str, bind: list[Any] | dict[str, Any], row_limit: int, cancel_token: CancelToken
    ) -> QueryResult:
        started = time.monotonic()
        try:
            try:
                pooled = self.engine.connect()
            except SQLAlchemyTimeoutError as exc:
                raise ConnectionBusy(
                    f"all pooled connections are busy (waited {POOL_TIMEOUT:.0f}s). "
                    "If sqldash sat idle, stale warehouse sessions are being replaced — "
                    "that can stall when the network/VPN isn't up yet or an SSO prompt "
                    "is pending. A refresh usually clears this once connectivity is back."
                ) from exc
            except ConnectorError:
                raise
            except Exception as exc:
                raise ConnectorError(self._explain(exc)) from None
            with pooled as conn:
                dbapi_conn = conn.connection.dbapi_connection
                cursor = dbapi_conn.cursor()
                dialect = self.engine.dialect.name
                cancel_token.attach(_cancel_for(dialect, dbapi_conn))
                try:
                    params = (tuple(bind) if isinstance(bind, list) else bind) if bind else None
                    if dialect == "snowflake":
                        _run_snowflake(cursor, dbapi_conn, sql, params, cancel_token)
                    elif params:
                        cursor.execute(sql, params)
                    else:
                        cursor.execute(sql)
                    description = list(cursor.description or [])
                    if not description:
                        with contextlib.suppress(Exception):
                            dbapi_conn.commit()
                        return QueryResult(columns=[], rows=[], row_count=0)
                    raw_rows: list[tuple] = []
                    while len(raw_rows) <= row_limit:
                        if cancel_token.cancelled:
                            raise ConnectorError("query cancelled")
                        batch = cursor.fetchmany(FETCH_BATCH)
                        if not batch:
                            break
                        raw_rows.extend(tuple(row) for row in batch)
                    if dialect == "snowflake":
                        description = _snowflake_description(description)
                    columns = infer_columns(description, raw_rows)
                    with contextlib.suppress(Exception):
                        dbapi_conn.commit()
                    return self.build_result(columns, raw_rows, row_limit, started)
                except BaseException as exc:
                    if not isinstance(exc, (ConnectorError, SQLAlchemyError)):
                        if self.engine.dialect.is_disconnect(exc, dbapi_conn, cursor) or (
                            dialect == "snowflake" and snowflake_session_gone(exc)
                        ):
                            conn.invalidate()
                            raise ConnectionLost(f"connection lost: {self._explain(exc)}") from exc
                        message = self._explain(exc)
                        lowered = message.lower()
                        if "timeout" in lowered:
                            if dialect == "snowflake":
                                raise ConnectorError(SNOWFLAKE_TIMEOUT) from exc
                            raise ConnectorError(message) from exc
                        if "cancel" in lowered or "interrupt" in lowered:
                            raise ConnectorError("query cancelled") from exc
                        raise ConnectorError(message) from exc
                    raise
                finally:
                    cancel_token.attach(None)
                    with contextlib.suppress(Exception):
                        cursor.close()
        except DBAPIError as exc:
            if exc.connection_invalidated:
                raise ConnectionLost(f"connection lost: {self._explain(exc)}") from exc
            raise ConnectorError(self._explain(exc)) from exc
        except ConnectorError:
            raise
        except SQLAlchemyError as exc:
            raise ConnectorError(self._explain(exc)) from exc

    def role_context(self) -> dict:
        try:
            return role_context(self.engine, self.source)
        except Exception as exc:
            raise ConnectorError(self._explain(exc)) from None

    def database_context(self) -> dict:
        if self.engine.dialect.name != "snowflake":
            return {"current": None, "databases": [], "comments": {}}
        try:
            with self.engine.connect() as conn:
                current = conn.exec_driver_sql("SELECT CURRENT_DATABASE()").scalar()
                rows = conn.exec_driver_sql("SHOW DATABASES").mappings().all()
            return {
                "current": current,
                "databases": sorted({row["name"] for row in rows}),
                "comments": {row["name"]: row["comment"] for row in rows if row.get("comment")},
                "kinds": {row["name"]: row["kind"] for row in rows if row.get("kind")},
            }
        except Exception as exc:
            raise ConnectorError(self._explain(exc)) from None

    def sql_identifier(self, name: str) -> str:
        """``name`` as it must be written in this dialect's SQL: bare when an unquoted
        reference resolves to it, otherwise quoted, so case-sensitive names survive."""
        dialect = self.engine.dialect
        folded = dialect.normalize_name(name) if dialect.requires_name_normalize else name
        quoted = dialect.identifier_preparer.quote(folded)
        return name if quoted == folded else quoted

    def introspect_database(self, database: str) -> list[TableInfo]:
        if self.engine.dialect.name != "snowflake":
            raise ConnectorError("Database browsing is only available for Snowflake")
        quote = self.engine.dialect.identifier_preparer.quote_identifier
        sql = INFORMATION_SCHEMA_PRECISION_SQL.replace(
            "information_schema.columns", f"{quote(database)}.information_schema.columns"
        )
        try:
            with self.engine.connect() as conn:
                rows = conn.exec_driver_sql(sql).fetchall()
            return _tables_from_rows(rows)
        except Exception as exc:
            raise ConnectorError(self._explain(exc)) from None

    def introspect(self) -> list[TableInfo]:
        """Prefer one information_schema round trip; fall back to the per-table inspector."""
        sql = (
            INFORMATION_SCHEMA_PRECISION_SQL
            if self.engine.dialect.name in PRECISION_DIALECTS
            else INFORMATION_SCHEMA_SQL
        )
        try:
            with self.engine.connect() as conn:
                rows = conn.exec_driver_sql(sql).fetchall()
            if rows:
                return _tables_from_rows(rows)
        except Exception:
            pass
        try:
            inspector = inspect(self.engine)
            tables = []
            for name in inspector.get_table_names():
                info = TableInfo(name=name, schema=None)
                for col in inspector.get_columns(name):
                    info.columns.append((col["name"], str(col["type"])))
                tables.append(info)
            return tables
        except Exception as exc:
            raise ConnectorError(self._explain(exc)) from None

    def close(self) -> None:
        with self._lock:
            if self._engine is not None:
                self._engine.dispose()
                self._engine = None


def _clean(exc: BaseException, source: Source | None = None) -> str:
    """Unwrap the driver's original error and drop SQLAlchemy's appended [SQL: ...] block."""
    message = str(getattr(exc, "orig", None) or exc).strip()
    message = message.split("\n[SQL", 1)[0].strip()
    message = _DOUBLED_ERRNO.sub(r"\1: ", message)
    message = SNOWFLAKE_RELOGIN.sub(SNOWFLAKE_SESSION_ENDED, message)
    if source is not None:
        message = scrub_error_text(message, source)
    return message
