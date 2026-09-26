"""Credential resolution: per-user profiles.yaml plus ``${env:VAR}`` interpolation.

Dashboards reference a profile *name* (AWS-style); each user defines the
matching entry in ``~/.config/sqldash/profiles.yaml`` with their own
credentials. Secrets resolve in memory at connect time and are never written
back, so nothing secret can land in a project file or a git diff.
"""

import io
import os
import re
from pathlib import Path
from typing import Any

from platformdirs import user_config_dir
from ruamel.yaml import YAML

ENV_REF = re.compile(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}")


def sub_env_refs(text: str, repl: str) -> str:
    """Replace every ${env:VAR} reference in text with repl."""
    return ENV_REF.sub(repl, text)


_yaml = YAML(typ="safe")


class SecretError(ValueError):
    pass


def profiles_path() -> Path:
    return Path(user_config_dir("sqldash")) / "profiles.yaml"


def save_profile(name: str, fields: dict[str, Any], path: Path | None = None) -> Path:
    """Insert or replace one named profile, leaving every other entry alone.

    Setup re-runs through here so a typo'd first pass is not a new file. The
    rest of the document stays as ruamel loaded it — comments and sibling
    profiles survive.
    """
    path = path or profiles_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    rt = YAML(typ="rt")
    rt.indent(mapping=2, sequence=4, offset=2)
    if path.exists() and path.read_text().strip():
        try:
            data = rt.load(path.read_text())
        except Exception as exc:
            raise SecretError(f"{path}: invalid YAML: {exc}") from exc
        if data is None:
            data = {}
        elif not isinstance(data, dict):
            raise SecretError(
                f"{path} must be a mapping of profile name -> settings, e.g.\n"
                "acme-prod:\n  user: you@acme.com\n  authentication: externalbrowser"
            )
    else:
        data = {}
    existing = data.get(name)
    if isinstance(existing, dict):
        for key, value in fields.items():
            existing[key] = value
        data[name] = existing
    else:
        data[name] = dict(fields)
    buffer = io.StringIO()
    rt.dump(data, buffer)
    path.write_text(buffer.getvalue())
    path.chmod(0o600)
    return path


def load_profiles(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Load profiles.yaml, tolerating a missing file but never a malformed one."""
    path = path or profiles_path()
    if not path.exists():
        return {}
    try:
        data = _yaml.load(io.StringIO(path.read_text()))
    except Exception as exc:
        raise SecretError(f"{path}: invalid YAML: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict) or not all(isinstance(v, dict) for v in data.values()):
        raise SecretError(
            f"{path} must be a mapping of profile name -> settings, e.g.\n"
            "acme-prod:\n  user: you@acme.com\n  method: externalbrowser"
        )
    return data


def interpolate_env(value: Any) -> Any:
    """Recursively expand ``${env:VAR}`` references, failing loudly on unset variables."""
    if isinstance(value, str):

        def replace(match: re.Match) -> str:
            name = match.group(1)
            resolved = os.environ.get(name)
            if resolved is None:
                raise SecretError(
                    f"environment variable '{name}' is not set (referenced as ${{env:{name}}})"
                )
            return resolved

        return ENV_REF.sub(replace, value)
    if isinstance(value, dict):
        return {k: interpolate_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [interpolate_env(v) for v in value]
    return value


CREDENTIAL_FIELDS = (
    "username",
    "password",
    "token",
    "authentication",
    "private_key_path",
    "private_key_passphrase",
)

# The subset that must not appear as literals in a project file. username,
# authentication, and private_key_path are credentials-adjacent but not secrets.
SECRET_FIELDS = ("password", "token", "private_key_passphrase")


PROFILE_KEY_ALIASES = {"user": "username"}


def resolve_credentials(
    source, profiles: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Layer a source's own credential fields over its profile entry; unknown
    profile keys are rejected by name so typos fail before a connect attempt."""
    auth = {field: getattr(source, field) for field in CREDENTIAL_FIELDS}
    auth["profile"] = source.profile
    if source.profile:
        loaded = profiles if profiles is not None else load_profiles()
        entry = loaded.get(source.profile)
        if entry is not None:
            allowed = set(CREDENTIAL_FIELDS) | set(PROFILE_KEY_ALIASES)
            unknown = sorted(set(entry) - allowed)
            if unknown:
                raise SecretError(
                    f"profile '{source.profile}': unknown key(s) {', '.join(unknown)} — "
                    f"valid keys: {', '.join(sorted(allowed))}"
                )
        profiles = loaded
    resolved = resolve_auth(auth, profiles)
    for alias, canonical in PROFILE_KEY_ALIASES.items():
        if alias in resolved and canonical not in resolved:
            resolved[canonical] = resolved.pop(alias)
    return resolved


def resolve_auth(
    auth: dict[str, Any], profiles: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Merge explicit auth values over the named profile, then interpolate env refs."""
    profiles = profiles if profiles is not None else load_profiles()
    resolved: dict[str, Any] = {}
    profile_name = auth.get("profile")
    if profile_name:
        if profile_name not in profiles:
            raise SecretError(
                f"profile '{profile_name}' not found in {profiles_path()} — "
                f"add a '{profile_name}:' entry there with your own credentials"
            )
        resolved.update(profiles[profile_name])
    for key, value in auth.items():
        if key == "profile" or value is None:
            continue
        resolved[key] = value
    return interpolate_env(resolved)
