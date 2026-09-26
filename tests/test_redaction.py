"""`redact_source` is the chokepoint for "secrets never surface".

It had no tests, which is how three leaks reached a release candidate. Every
outbound representation — `source list`, `dashboard show`, the page payload, MCP
`list_sources`, `export context` — goes through this one function, so these cases
stand in for all of them.
"""

from __future__ import annotations

import pytest

from sqldash.models.source import REDACTED, Source, redact_source, scrub_error_text

SECRET = "hunter2"


def _dump(**kwargs) -> str:
    return repr(redact_source(Source.model_validate(kwargs)))


@pytest.mark.parametrize(
    "url",
    [
        f"postgres://user:{SECRET}@db.internal:5432/analytics",
        # No username: the old regex required one before the colon and let this by.
        f"postgres://:{SECRET}@db.internal:5432/analytics",
        # Secrets in the query string, which the old regex never looked at.
        f"postgres://user:x@db.internal:5432/analytics?token={SECRET}",
        f"clickhouse://u:x@host:9000/db?password={SECRET}",
        f"trino://host:8080/hive?access_token={SECRET}",
        # Belt and braces: secret in both places at once.
        f"postgres://user:{SECRET}@host/db?password={SECRET}",
    ],
)
def test_url_secrets_never_survive_redaction(url: str):
    assert SECRET not in _dump(url=url)


def test_options_values_are_masked():
    dumped = _dump(
        type="athena",
        options={"s3_staging_dir": "s3://b/p", "access_key": "AKIA123", "secret_key": SECRET},
    )
    assert SECRET not in dumped
    assert "AKIA123" not in dumped


def test_connect_args_values_are_masked():
    assert SECRET not in _dump(url="postgres://h/db", connect_args={"password": SECRET})


def test_scrub_error_text_strips_connect_args_secrets():
    src = Source.model_validate(
        {
            "type": "duckdb",
            "connect_args": {"private_key": "CLEAN_PK_SECRET_99", "session_token": SECRET},
        }
    )
    raw = (
        "Invoked with: kwargs: database=':memory:', "
        "private_key='CLEAN_PK_SECRET_99', session_token='hunter2'"
    )
    out = scrub_error_text(raw, src)
    assert "CLEAN_PK_SECRET_99" not in out
    assert SECRET not in out
    assert REDACTED in out


def test_scrub_error_text_strips_interpolated_env_secrets(monkeypatch):
    monkeypatch.setenv("TEST_PW_REAL_SECRET", "envpw_super_secret_123")
    src = Source.model_validate(
        {
            "type": "duckdb",
            "connect_args": {"private_key": "${env:TEST_PW_REAL_SECRET}"},
        }
    )
    raw = "Invoked with: kwargs: private_key='envpw_super_secret_123'"
    out = scrub_error_text(raw, src)
    assert "envpw_super_secret_123" not in out
    assert REDACTED in out


@pytest.mark.parametrize("field", ["password", "token"])
def test_declared_secret_fields_are_masked(field: str):
    assert SECRET not in _dump(type="postgres", host="h", database="d", **{field: SECRET})


def test_redaction_keeps_the_url_readable():
    """Masking must not destroy the parts an operator needs to identify a source."""
    out = redact_source(
        Source.model_validate({"url": f"postgres://user:{SECRET}@db:5432/analytics"})
    )
    assert out["url"] == f"postgres://user:{REDACTED}@db:5432/analytics"


def test_url_without_credentials_is_untouched():
    url = "duckdb:///data/warehouse.db"
    assert redact_source(Source.model_validate({"url": url}))["url"] == url


def test_unparseable_url_fails_closed():
    """If it cannot be parsed it cannot be shown to be safe, so it is not shown."""
    out = redact_source(Source.model_validate({"url": "postgres://user:pw@[oops:/bad]:99/db"}))
    assert "pw" not in out["url"]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # sqlalchemy reads `x=y` here as query, so it is masked rather than shown.
        ("postgres://h/db#frag?x=y", f"postgres://h/db#frag?x={REDACTED}"),
        ("postgres://h/db?token=t#frag", f"postgres://h/db?token={REDACTED}"),
    ],
)
def test_query_values_after_a_fragment_are_still_masked(url: str, expected: str):
    assert redact_source(Source.model_validate({"url": url}))["url"] == expected


def test_percent_encoded_password_is_masked():
    """The correct spelling of a password containing `@`. It is not in the string
    literally, so this is the path that re-renders from sqlalchemy's parse."""
    out = redact_source(Source.model_validate({"url": "postgres://user:p%40ss@host/db"}))
    assert out["url"] == f"postgres://user:{REDACTED}@host/db"


def test_unencoded_at_sign_masks_what_sqlalchemy_calls_the_password():
    """`user:p@ss@host` is ambiguous, and sqlalchemy resolves it as password `p`,
    host `ss@host` — so that is what it authenticates with, and masking `p` is
    what actually hides the credential. Anyone intending `p@ss` must
    percent-encode it, which the test above covers. Asserted so the residue is a
    recorded decision rather than something that looks like a miss."""
    out = redact_source(Source.model_validate({"url": "postgres://user:p@ss@host/db"}))
    assert out["url"] == f"postgres://user:{REDACTED}@ss@host/db"


def test_host_and_path_survive_an_at_sign_in_the_path():
    out = redact_source(Source.model_validate({"url": "postgres://user:pw@host/db/a@b.db"}))
    assert out["url"] == f"postgres://user:{REDACTED}@host/db/a@b.db"


def test_scheme_less_url_still_masks_its_password():
    """Not a usable connection string, but a plausible typo — and it must not display."""
    out = redact_source(Source.model_validate({"url": f"user:{SECRET}@host/db?token=x"}))
    assert SECRET not in out["url"]


def test_an_at_sign_in_a_path_is_not_treated_as_credentials():
    url = "duckdb:///data/a@b.db"
    assert redact_source(Source.model_validate({"url": url}))["url"] == url


@pytest.mark.parametrize("password", ["p/w", "p?w", "p#w", "p=w", "p:w", "pw"])
def test_passwords_containing_url_delimiters_are_masked(password: str):
    """Every one of these is a working connection string as far as sqlalchemy is
    concerned, and each breaks a different hand-rolled splitting rule: `/`, `?`
    and `#` truncate urlsplit's netloc, and `=` defeats any "this looks like a
    query parameter" heuristic. Redacting whatever sqlalchemy calls the password
    sidesteps the whole category."""
    out = redact_source(Source.model_validate({"url": f"postgres://user:{password}@host/db"}))
    assert password not in out["url"], out["url"]
    assert out["url"] == f"postgres://user:{REDACTED}@host/db"


def test_ipv6_host_survives_redaction():
    out = redact_source(Source.model_validate({"url": f"postgres://u:{SECRET}@[::1]:5432/db"}))
    assert out["url"] == f"postgres://u:{REDACTED}@[::1]:5432/db"


def test_an_at_sign_in_a_query_value_does_not_swallow_the_url():
    """The `@` belongs to the query, not to userinfo — masking the value is enough."""
    out = redact_source(Source.model_validate({"url": "postgres://h/db?email=a@b.com"}))
    assert out["url"] == f"postgres://h/db?email={REDACTED}"


def test_url_with_username_but_no_password_is_untouched():
    url = "postgres://user@host/db"
    assert redact_source(Source.model_validate({"url": url}))["url"] == url
