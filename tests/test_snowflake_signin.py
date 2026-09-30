"""Snowflake externalbrowser sign-in across engines, with a fake driver and a fake Keychain.

The fake ``connect`` does what the connector does on macOS: read the cached ID token
through ``KeyringTokenCache`` (so through whatever keyring backend is active), sign in
in the browser when there is none, and store the new token. Each Keychain read is a
chance for macOS to prompt, so the tests count them.
"""

import subprocess
import sys
import textwrap
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import ClassVar

import pytest

from sqldash.connectors import engine as engine_module
from sqldash.connectors.base import ConnectorError
from sqldash.execution import ExecutionRegistry
from sqldash.models.source import Source

snowflake_connector = pytest.importorskip("snowflake.connector")
token_cache = pytest.importorskip("snowflake.connector.token_cache")
snowflake_errors = pytest.importorskip("snowflake.connector.errors")
snowflake_tokens = pytest.importorskip("sqldash.connectors.snowflake_tokens")

SERVICE = token_cache.KeyringTokenCache.SERVICE_NAME
BASE_DIR = Path("/tmp/dash")


class FakeCursor:
    description: ClassVar[list] = [
        (name, 0, None, None, None, None, None) for name in ("a", "b", "c")
    ]

    def execute(self, sql, *args, **kwargs):
        return self

    def fetchone(self):
        return ("8.40.1", "DB", "SCH")

    def fetchall(self):
        return [("8.40.1", "DB", "SCH")]

    def close(self):
        pass


class FakeConn:
    messages: ClassVar[list] = []

    def cursor(self):
        return FakeCursor()

    def autocommit(self, value=None):
        pass

    def close(self):
        pass

    def rollback(self):
        pass

    def commit(self):
        pass


class FakeDriver:
    def __init__(self, signin_seconds=0.3, fail_users=()):
        self.signin_seconds = signin_seconds
        self.fail_users = set(fail_users)
        self.revoked_roles: set[str] = set()
        self.lock = threading.Lock()
        self.stats: Counter = Counter()
        self.spans: list[tuple[float, float]] = []
        self.active = 0

    def connect(self, **kwargs):
        start = time.monotonic()
        with self.lock:
            self.stats["logins"] += 1
            self.active += 1
            self.stats["max_parallel"] = max(self.stats["max_parallel"], self.active)
        try:
            if kwargs["user"] in self.fail_users:
                raise RuntimeError("differs from the user currently logged in at the IDP.")
            if kwargs.get("role") in self.revoked_roles:
                raise snowflake_errors.DatabaseError(
                    msg=f"Role '{kwargs['role']}' specified in the connect string does not "
                    "exist or not authorized.",
                    errno=390189,
                )
            cache = token_cache.KeyringTokenCache()
            key = token_cache.TokenKey(
                f"{kwargs['account']}.snowflakecomputing.com",
                kwargs["user"],
                token_cache.TokenType.ID_TOKEN,
            )
            if cache.retrieve(key) is None:
                with self.lock:
                    self.stats["browser_signins"] += 1
                time.sleep(self.signin_seconds)
                cache.store(key, f"id-token-for-{kwargs['user']}")
            time.sleep(0.02)
            return FakeConn()
        finally:
            with self.lock:
                self.active -= 1
                self.spans.append((start, time.monotonic()))


@pytest.fixture
def driver(monkeypatch, keychain):
    fake = FakeDriver()
    monkeypatch.setattr(snowflake_connector, "connect", fake.connect)
    monkeypatch.setattr(snowflake_tokens, "USES_KEYCHAIN", True)
    return fake


@pytest.fixture
def registry():
    registry = ExecutionRegistry(max_workers=8)
    yield registry
    registry.shutdown()


def source(user="me@example.com", **fields):
    return Source(type="snowflake", account="acct", username=user, **fields)


def run_tiles(registry, sources, tiles=12):
    errors: Counter = Counter()

    def tile(i):
        for _ in range(40):
            try:
                with (
                    registry.connection(sources[i % len(sources)], BASE_DIR) as connector,
                    connector.engine.connect() as conn,
                ):
                    conn.exec_driver_sql("SELECT 1")
                    time.sleep(0.2)
                return
            except ConnectorError as exc:
                errors[str(exc)[:40]] += 1
                time.sleep(0.25)
        raise AssertionError(f"tile {i} never connected: {dict(errors)}")

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(tile, range(tiles)))
    return errors


def test_twelve_concurrent_tiles_sign_in_once_and_read_the_keychain_once(
    driver, keychain, registry
):
    run_tiles(registry, [source()])
    assert driver.stats["browser_signins"] == 1
    assert keychain.reads[SERVICE] <= 1, keychain.reads
    assert keychain.writes[SERVICE] == 1, keychain.writes
    assert driver.stats["max_parallel"] == 1, "logins must not overlap"
    first_end = min(driver.spans)[1]
    assert all(start >= first_end - 0.01 for start, _ in sorted(driver.spans)[1:])
    assert driver.stats["logins"] <= engine_module.POOL_SIZE, driver.stats


def test_a_token_already_in_the_keychain_is_read_once_per_process(driver, keychain, registry):
    """The case that prompts: an item another interpreter wrote. Every session
    used to read it again, and each read could ask for access."""
    key = token_cache.TokenKey(
        "acct.snowflakecomputing.com", "me@example.com", token_cache.TokenType.ID_TOKEN
    )
    keychain.items[(SERVICE, key.hash_key())] = "id-token-from-yesterday"
    run_tiles(registry, [source()])
    assert driver.stats["browser_signins"] == 0
    assert sum(keychain.reads.values()) == 1, keychain.reads
    assert keychain.writes[SERVICE] == 0
    assert driver.stats["logins"] > 1, "several sessions, so the memo was exercised"


def test_warehouse_variants_of_one_user_share_one_signin(driver, keychain, registry):
    variants = [source(), source(warehouse="WH_BIG"), source(warehouse="WH_SMALL")]
    run_tiles(registry, variants)
    assert driver.stats["browser_signins"] == 1, driver.stats
    assert keychain.reads[SERVICE] <= 1, keychain.reads
    assert driver.stats["max_parallel"] == 1


def test_role_database_and_dashboard_dir_variants_share_one_signin(driver, registry):
    variants = [source(), source(role="ANALYST"), source(database="PROD")]
    errors: list = []

    def open_one(item):
        index, variant = item
        try:
            with registry.connection(variant, BASE_DIR / str(index)) as connector:
                connector.engine.connect().close()
        except ConnectorError as exc:
            errors.append(exc)

    with ThreadPoolExecutor(3) as pool:
        list(pool.map(open_one, enumerate(variants)))
    assert not errors
    assert driver.stats["browser_signins"] == 1, driver.stats


def test_a_different_user_gets_its_own_signin(driver, keychain, registry):
    run_tiles(registry, [source(), source(user="ada@example.com")])
    assert driver.stats["browser_signins"] == 2, driver.stats
    assert keychain.writes[SERVICE] == 2
    assert keychain.reads[SERVICE] <= 2, keychain.reads


def test_the_failure_cooldown_is_per_account_and_user(driver, registry):
    driver.fail_users = {"me@example.com"}
    with (
        pytest.raises(ConnectorError, match=r"attempted user: 'me@example\.com'"),
        registry.connection(source(), BASE_DIR) as connector,
    ):
        connector.engine.connect()
    with (
        pytest.raises(ConnectorError, match="attempts paused"),
        registry.connection(source(warehouse="WH_BIG"), BASE_DIR) as connector,
    ):
        connector.engine.connect()
    assert driver.stats["logins"] == 1, "a warehouse variant must not retry the failed sign-in"
    with registry.connection(source(user="ada@example.com"), BASE_DIR) as connector:
        connector.engine.connect().close()
    assert driver.stats["logins"] == 2


def test_a_revoked_role_on_one_source_does_not_pause_the_others(driver, registry):
    """A bad role, warehouse or database is that source's problem, not the
    identity's. Sharing its cooldown rejected a healthy dashboard for 30s with
    another dashboard's role error, without even trying."""
    driver.revoked_roles = {"REVOKED"}
    with (
        pytest.raises(ConnectorError, match="does not exist or not authorized"),
        registry.connection(source(role="REVOKED"), BASE_DIR) as connector,
    ):
        connector.engine.connect()
    start = time.monotonic()
    with registry.connection(source(role="ANALYST"), BASE_DIR) as connector:
        connector.engine.connect().close()
    assert time.monotonic() - start < 1
    assert driver.stats["logins"] == 2
    with (
        pytest.raises(ConnectorError, match="attempts paused"),
        registry.connection(source(role="REVOKED"), BASE_DIR) as connector,
    ):
        connector.engine.connect()
    assert driver.stats["logins"] == 2, "the failing source keeps its own cooldown"


@pytest.mark.parametrize(
    ("errno", "message"),
    [
        (390100, "Incorrect username or password was specified."),
        (390190, "There was an error related to the SAML Identity Provider account parameter."),
        (250006, "Failed to connect to the identity provider."),
        (None, "The user you were trying to authenticate as differs from the user at the IdP."),
    ],
)
def test_sign_in_failures_pause_every_source_of_the_identity(
    driver, registry, monkeypatch, errno, message
):
    def failing(**kwargs):
        driver.stats["logins"] += 1
        raise snowflake_errors.DatabaseError(msg=message, errno=errno)

    monkeypatch.setattr(snowflake_connector, "connect", failing)
    with pytest.raises(ConnectorError), registry.connection(source(), BASE_DIR) as connector:
        connector.engine.connect()
    with (
        pytest.raises(ConnectorError, match="attempts paused"),
        registry.connection(source(role="ANALYST"), BASE_DIR) as connector,
    ):
        connector.engine.connect()
    assert driver.stats["logins"] == 1


def test_the_memo_follows_the_connector_when_it_drops_an_expired_token(keychain, monkeypatch):
    monkeypatch.setattr(snowflake_tokens, "USES_KEYCHAIN", True)
    snowflake_tokens.remember_snowflake_tokens()
    snowflake_tokens.remember_snowflake_tokens()
    cache = token_cache.KeyringTokenCache()
    key = token_cache.TokenKey(
        "acct.snowflakecomputing.com", "me@example.com", token_cache.TokenType.ID_TOKEN
    )
    cache.store(key, "old")
    assert cache.retrieve(key) == "old"
    cache.remove(key)
    assert (SERVICE, key.hash_key()) not in keychain.items
    assert cache.retrieve(key) is None
    cache.store(key, "new")
    assert cache.retrieve(key) == "new"
    assert keychain.reads[SERVICE] == 1, "the read after the delete, and nothing else"


def test_the_memo_passes_other_services_through(keychain, monkeypatch):
    monkeypatch.setattr(snowflake_tokens, "USES_KEYCHAIN", True)
    snowflake_tokens.remember_snowflake_tokens()
    memo = snowflake_tokens.keyring.get_keyring()
    keychain.items[("other-app", "me")] = "secret"
    assert memo.get_password("other-app", "me") == "secret"
    assert memo.get_password("other-app", "me") == "secret"
    assert keychain.reads["other-app"] == 2


def test_the_memo_is_not_installed_where_the_connector_uses_no_keychain(keychain, monkeypatch):
    monkeypatch.setattr(snowflake_tokens, "USES_KEYCHAIN", False)
    snowflake_tokens.remember_snowflake_tokens()
    assert snowflake_tokens.keyring.get_keyring() is keychain


@pytest.mark.parametrize("uses_keychain", [True, False])
def test_a_cache_class_without_a_service_name_never_raises(keychain, monkeypatch, uses_keychain):
    """Resolving SERVICE_NAME at import time broke every externalbrowser source
    on a connector without it, Linux included, before the platform check ran."""

    class OldKeyringTokenCache:
        pass

    monkeypatch.setattr(token_cache, "KeyringTokenCache", OldKeyringTokenCache)
    monkeypatch.setattr(snowflake_tokens, "USES_KEYCHAIN", uses_keychain)
    snowflake_tokens.remember_snowflake_tokens()
    assert snowflake_tokens.snowflake_service() == "com.snowflake.connector.python"
    backend = snowflake_tokens.keyring.get_keyring()
    backend.set_password("com.snowflake.connector.python", "k", "v")
    assert backend.get_password("com.snowflake.connector.python", "k") == "v"
    assert backend.get_password("com.snowflake.connector.python", "k") == "v"
    assert keychain.reads["com.snowflake.connector.python"] == (0 if uses_keychain else 2)


def test_importing_against_a_cache_class_without_a_service_name_never_raises():
    script = textwrap.dedent(
        """
        import keyring
        from keyring.backends import fail
        from snowflake.connector import token_cache

        keyring.set_keyring(fail.Keyring())
        del token_cache.KeyringTokenCache.SERVICE_NAME
        from sqldash.connectors import snowflake_tokens

        snowflake_tokens.USES_KEYCHAIN = True
        snowflake_tokens.remember_snowflake_tokens()
        print(snowflake_tokens.snowflake_service())
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "com.snowflake.connector.python"


def test_an_externalbrowser_pool_has_no_overflow(driver):
    engine = engine_module.build_engine(source(), None)
    try:
        assert engine.pool.size() == engine_module.POOL_SIZE
        assert engine.pool._max_overflow == 0
    finally:
        engine.dispose()
    engine = engine_module.build_engine(source(token="pat-value", authentication="pat"), None)
    try:
        assert engine.pool._max_overflow == engine_module.MAX_OVERFLOW
    finally:
        engine.dispose()
