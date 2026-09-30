"""Guarantee `tests/` is importable however pytest is invoked.

`from helpers import ...` relies on pytest's default prepend import mode
putting this directory on sys.path — which stops being true the moment anyone
adds tests/__init__.py or switches to --import-mode=importlib. conftest.py is
always loaded first, so pinning the path here keeps the shared helpers working
under any import mode.

Snowflake sign-in state is process-wide, so every test starts with none, and the
keyring backend is an in-memory fake: no test can read or write the real Keychain.
"""

import sys
from collections import Counter
from pathlib import Path

import pytest

from sqldash.connectors import engine as engine_module

try:
    import keyring.core
    from keyring.backend import KeyringBackend
except ImportError:
    keyring = None
    KeyringBackend = object

sys.path.insert(0, str(Path(__file__).parent))


class FakeKeychain(KeyringBackend):
    """Counts reads and writes per service, the way the Keychain would see them."""

    def __init__(self):
        self.items: dict[tuple[str, str], str] = {}
        self.reads: Counter = Counter()
        self.writes: Counter = Counter()

    def get_password(self, service, username):
        self.reads[service] += 1
        return self.items.get((service, username))

    def set_password(self, service, username, password):
        self.writes[service] += 1
        self.items[(service, username)] = password

    def delete_password(self, service, username):
        self.items.pop((service, username), None)


@pytest.fixture(autouse=True)
def _isolated_snowflake_signin(monkeypatch):
    monkeypatch.setattr(engine_module, "_SIGNIN_GATES", {})
    if keyring is not None:
        monkeypatch.setattr(keyring.core, "_keyring_backend", FakeKeychain())


@pytest.fixture
def keychain():
    if keyring is None:
        pytest.skip("keyring ships with the [snowflake] extra")
    return keyring.core.get_keyring()
