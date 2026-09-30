"""Keep the Snowflake connector's SSO token in memory once the Keychain has handed it over.

On macOS the connector caches the externalbrowser ID token in the Keychain through
``keyring`` and reads it back on every ``snowflake.connector.connect()``. Each read is
a chance for macOS to ask the user to allow Python access again, and keyring's macOS
store is delete and re-add, which drops an earlier "Always Allow". A pool of sessions
per engine, times the role, warehouse and database variants of one source, turned one
sign-in into a stack of prompts.

:class:`SnowflakeTokenMemo` wraps whatever keyring backend is active and remembers only
the connector's own service. Every other service passes straight through, so no other
library's keyring use changes. Writes and deletes still reach the Keychain, so the
token survives a restart as before; they also update the memo, so an expired token the
connector deletes is not served again. Only imported with the ``[snowflake]`` extra.
"""

import threading

import keyring
from keyring.backend import KeyringBackend
from snowflake.connector import token_cache
from snowflake.connector.compat import IS_MACOS

DEFAULT_SERVICE = "com.snowflake.connector.python"
USES_KEYCHAIN = IS_MACOS
_install_lock = threading.Lock()


def snowflake_service() -> str:
    """The keyring service the connector stores tokens under, looked up when used.

    Read at import time, a connector whose cache class names it differently (or not
    at all) failed every externalbrowser source, on every platform, before connecting.
    """
    cache = getattr(token_cache, "KeyringTokenCache", None)
    service = getattr(cache, "SERVICE_NAME", None)
    return service if isinstance(service, str) and service else DEFAULT_SERVICE


class SnowflakeTokenMemo(KeyringBackend):
    def __init__(self, inner: KeyringBackend) -> None:
        super().__init__()
        self.inner = inner
        self._lock = threading.Lock()
        self._tokens: dict[str, str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        if service != snowflake_service():
            return self.inner.get_password(service, username)
        with self._lock:
            token = self._tokens.get(username)
            if token is None:
                token = self.inner.get_password(service, username)
                if token is not None:
                    self._tokens[username] = token
            return token

    def set_password(self, service: str, username: str, password: str) -> None:
        if service != snowflake_service():
            self.inner.set_password(service, username, password)
            return
        with self._lock:
            self.inner.set_password(service, username, password)
            self._tokens[username] = password

    def delete_password(self, service: str, username: str) -> None:
        if service != snowflake_service():
            self.inner.delete_password(service, username)
            return
        with self._lock:
            self._tokens.pop(username, None)
            self.inner.delete_password(service, username)

    def get_credential(self, service: str, username: str | None):
        return self.inner.get_credential(service, username)


def remember_snowflake_tokens() -> None:
    """Wrap the active keyring backend once per process; later calls are no-ops."""
    if not USES_KEYCHAIN:
        return
    with _install_lock:
        current = keyring.get_keyring()
        if not isinstance(current, SnowflakeTokenMemo):
            keyring.set_keyring(SnowflakeTokenMemo(current))
