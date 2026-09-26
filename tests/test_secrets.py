import pytest

from sqldash.redact import mask_url_userinfo
from sqldash.secrets import SecretError, interpolate_env, resolve_auth


def test_interpolate_env(monkeypatch):
    monkeypatch.setenv("SF_USER", "ada")
    assert interpolate_env("${env:SF_USER}") == "ada"
    assert interpolate_env({"user": "${env:SF_USER}", "n": 1}) == {"user": "ada", "n": 1}


def test_interpolate_missing_env_names_variable():
    with pytest.raises(SecretError, match="NOPE_MISSING"):
        interpolate_env("${env:NOPE_MISSING}")


def test_resolve_auth_profile_merge():
    profiles = {"acme": {"user": "svc", "password": "hunter2"}}
    resolved = resolve_auth({"profile": "acme", "method": "password"}, profiles)
    assert resolved == {"user": "svc", "password": "hunter2", "method": "password"}


def test_resolve_auth_explicit_overrides_profile():
    profiles = {"acme": {"user": "svc"}}
    resolved = resolve_auth({"profile": "acme", "user": "override"}, profiles)
    assert resolved["user"] == "override"


def test_resolve_auth_unknown_profile():
    with pytest.raises(SecretError, match="profile 'ghost' not found"):
        resolve_auth({"profile": "ghost"}, {})


def test_load_profiles_yaml(tmp_path):
    from sqldash.secrets import load_profiles

    path = tmp_path / "profiles.yaml"
    path.write_text("acme-prod:\n  user: ada@acme.com\n  method: externalbrowser\n")
    profiles = load_profiles(path)
    assert profiles["acme-prod"]["user"] == "ada@acme.com"


def test_load_profiles_rejects_non_mapping(tmp_path):
    from sqldash.secrets import SecretError, load_profiles

    path = tmp_path / "profiles.yaml"
    path.write_text("- just\n- a list\n")
    with pytest.raises(SecretError, match="mapping of profile name"):
        load_profiles(path)


def test_profile_unknown_keys_rejected():
    from sqldash.models.source import Source
    from sqldash.secrets import resolve_credentials

    source = Source(type="snowflake", account="a", profile="acme")
    with pytest.raises(SecretError, match="unknown key"):
        resolve_credentials(source, {"acme": {"method": "externalbrowser", "user": "u"}})


def test_profile_user_alias_maps_to_username():
    from sqldash.models.source import Source
    from sqldash.secrets import resolve_credentials

    source = Source(type="snowflake", account="a", profile="acme")
    creds = resolve_credentials(source, {"acme": {"user": "svc", "token": "t"}})
    assert creds["username"] == "svc"


@pytest.mark.parametrize(
    ("text", "masked"),
    [
        ("https://alice:ghp_x@github.com/a/b.git", "https://•••@github.com/a/b.git"),
        ("https://ghp_x@github.com/a/b", "https://•••@github.com/a/b"),
        ("ssh://git:pw@host/x.git", "ssh://•••@host/x.git"),
        (
            "see 'https://u:p@h/x.git' and https://h2/q@r",
            "see 'https://•••@h/x.git' and https://h2/q@r",
        ),
        ("https://github.com/a/b.git", "https://github.com/a/b.git"),
        ("git@github.com:a/b.git", "git@github.com:a/b.git"),
        ("/srv/repos/a@b", "/srv/repos/a@b"),
    ],
)
def test_mask_url_userinfo(text, masked):
    assert mask_url_userinfo(text) == masked
