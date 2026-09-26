"""Data source contract: one flat connection shape for every engine, secrets by reference."""

import difflib
import re
from secrets import token_hex
from typing import Annotated, Any
from urllib.parse import unquote

from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from sqlalchemy.engine import make_url

from sqldash.redact import REDACTED
from sqldash.secrets import ENV_REF, SecretError, interpolate_env

SECRET_FIELDS = ("password", "token", "private_key_passphrase")
# Extra connect_args/options names that carry secrets but are not on Source.
SECRET_BAG_KEYS = frozenset(SECRET_FIELDS) | {
    "passwd",
    "pwd",
    "secret",
    "secret_key",
    "api_key",
    "apikey",
    "access_token",
    "access_key",
    "key",
}
SECRET_BAG_PATH_KEYS = frozenset({"credentials_path", "keyfile", "key_file"})

AUTHENTICATION_METHODS = ("externalbrowser", "password", "pat", "keypair")


class Source(BaseModel):
    """A database connection: `type` names the engine (postgres, snowflake, duckdb, ...)
    and the flat fields describe it, or `url` bypasses URL building entirely. Fields not
    listed here are rejected with a did-you-mean hint. Secrets belong in `${env:VAR}`
    references or a named `profile`, never as literals in a tracked file.

    `external_access` is the one field that widens what SQL can reach rather than
    describing where to connect: a duckdb source is confined to its project
    directory unless it is set, because there the effective credential is the
    server user's filesystem (#622)."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    type: str | None = None
    url: str | None = None
    host: str | None = None
    port: int | None = None
    database: str | None = None
    db_schema: str | None = Field(default=None, alias="schema")
    account: str | None = None
    warehouse: str | None = None
    role: str | None = None
    secondary_roles: bool | None = None
    username: str | None = Field(default=None, validation_alias=AliasChoices("username", "user"))
    password: str | None = None
    token: str | None = None
    private_key_path: str | None = None
    private_key_passphrase: str | None = None
    authentication: str | None = None
    profile: str | None = None
    driver: str | None = None
    project: str | None = None
    catalog: str | None = None
    http_path: str | None = None
    attach_files: bool = False
    base_dir: str | None = None
    external_access: bool = False
    options: dict[str, str] = {}
    connect_args: dict[str, Any] = {}

    @field_validator("options", mode="before")
    @classmethod
    def stringify_option_scalars(cls, value: Any) -> Any:
        """YAML `connect_timeout: 10` is an int. Rejecting it killed the dashboard. #293."""
        if not isinstance(value, dict):
            return value
        out = {}
        for key, item in value.items():
            if isinstance(item, str) or item is None:
                out[key] = item
            elif isinstance(item, bool):
                out[key] = "true" if item else "false"
            else:
                out[key] = str(item)
        return out

    @model_validator(mode="after")
    def check_shape(self) -> "Source":
        if not self.type and not self.url:
            raise ValueError("source needs a 'type' (database name) or a 'url'")
        if self.type and not re.match(r"^[a-z][a-z0-9_+]*$", self.type):
            raise ValueError(f"source type '{self.type}' is not a valid database name")
        if self.authentication and self.authentication not in AUTHENTICATION_METHODS:
            raise ValueError(
                f"authentication '{self.authentication}' is not valid — use one of "
                f"{', '.join(AUTHENTICATION_METHODS)}"
            )
        return self


KNOWN_SOURCE_KEYS = frozenset(Source.model_fields) | {"schema", "user"}

RENAMED_SOURCE_KEYS = {"attach_csv": "attach_files"}

DEFAULT_MARK = "default"


def is_named_source_map(value: Any) -> bool:
    """True when a `source:` block names connections instead of describing one.

    Every entry of the named form is a mapping, and one connection always carries
    `type` or `url`: those two tests separate the shapes on *values*, which is what
    lets a connection be named `warehouse:` or `database:` — names that are also
    Source fields. Reading the names instead would make `source: {typ: duckdb}` a
    connection named "typ" rather than the typo it is.
    """
    if not isinstance(value, dict) or not value:
        return False
    if "type" in value or "url" in value:
        return False
    return all(isinstance(entry, dict) for entry in value.values())


def split_named_sources(block: dict) -> tuple[str, dict[str, Any]]:
    """The default connection's name and every connection with the mark stripped.

    One entry needs no mark. Several need exactly one `default: true` — guessing
    (first entry, alphabetical) would let a reordering silently repoint every tile
    that names no source.
    """
    entries: dict[str, Any] = {}
    marked: list[str] = []
    for sname, raw in block.items():
        if isinstance(raw, dict) and DEFAULT_MARK in raw:
            raw = dict(raw)
            flag = raw.pop(DEFAULT_MARK)
            if not isinstance(flag, bool):
                raise ValueError(f"source '{sname}': default must be true or false")
            if flag:
                marked.append(sname)
        if isinstance(raw, dict) and not raw:
            raise ValueError(
                f"source '{sname}': names a connection but declares none — give it a "
                "'type' (database name) or a 'url'"
            )
        entries[sname] = raw
    if len(marked) > 1:
        raise ValueError(
            f"source: {', '.join(marked)} are all marked 'default: true' — mark exactly one"
        )
    if marked:
        return marked[0], entries
    if len(entries) == 1:
        return next(iter(entries)), entries
    raise ValueError(
        f"source: names {len(entries)} connections and none is the default — add "
        "'default: true' to the one tiles use when they name no source"
    )


def default_source_name(block: Any) -> str | None:
    """The default entry of a named `source:` map, or None when the map is not one
    or does not say. For writers, which see files the parser has already accepted."""
    if not is_named_source_map(block):
        return None
    try:
        return split_named_sources(block)[0]
    except ValueError:
        return None


def _coerce_source(value: Any) -> Any:
    if isinstance(value, str):
        return {"url": value}
    if isinstance(value, dict):
        for key in value:
            if not isinstance(key, str) or key in KNOWN_SOURCE_KEYS:
                continue
            if key in RENAMED_SOURCE_KEYS:
                raise ValueError(
                    f"source field '{key}' was renamed — use '{RENAMED_SOURCE_KEYS[key]}'"
                )
            if key == "auth":
                raise ValueError(
                    "the nested 'auth:' block is gone — put authentication:, username:, "
                    "password: directly on the source"
                )
            if isinstance(value[key], dict):
                raise ValueError(
                    f"unknown source field '{key}' — a named connection cannot sit beside "
                    "connection fields: under source:, name every connection or none"
                )
            close = difflib.get_close_matches(key, KNOWN_SOURCE_KEYS, n=1)
            hint = f" — did you mean '{close[0]}'?" if close else ""
            raise ValueError(f"unknown source field '{key}'{hint}")
    return value


SourceConfig = Annotated[Source, BeforeValidator(_coerce_source)]


_REDACT_SENTINEL = "sqldash_redacted_password"


def _redact_query(query: str) -> str:
    """Mask every query-string value. Same stance as connect_args: redact the whole
    bag rather than guess which keys hold secrets — `?token=`, `?password=` and
    friends are common, and a denylist only has to miss once to leak."""
    out = []
    for pair in query.split("&"):
        if not pair:
            continue
        key, sep, _value = pair.partition("=")
        out.append(f"{key}={REDACTED}" if sep else key)
    return "&".join(out)


def _redact_url(url: str) -> str:
    """Mask the password and every query-string value in a connection URL.

    The password is whatever **sqlalchemy** says it is. That is not a stylistic
    choice: sqlalchemy is what parses this string at connect time, so matching it
    means the masked span is exactly the span that functions as a credential —
    including passwords containing `/`, `?`, `#`, `=` or `@`, which hand-rolled
    splitting gets wrong in a different way for each character.

    A URL sqlalchemy cannot parse is not one sqldash could connect with either,
    so it is replaced wholesale rather than shown on a guess."""
    try:
        parsed = make_url(url)
    except Exception:
        return REDACTED

    redacted = url
    if parsed.password:
        literal = f":{parsed.password}@"
        if literal in redacted:
            # Splice, so the rest of the URL stays byte-identical for the reader.
            redacted = redacted.replace(literal, f":{REDACTED}@", 1)
        else:
            # Percent-encoded, so it is not in the string literally. Re-render
            # from the parse instead; the sentinel avoids the marker itself being
            # percent-encoded on the way out.
            redacted = (
                parsed.set(password=_REDACT_SENTINEL)
                .render_as_string(hide_password=False)
                .replace(_REDACT_SENTINEL, REDACTED)
            )

    # With the password gone, the first `?` is unambiguously the query separator.
    base, question, query = redacted.partition("?")
    if question:
        redacted = f"{base}?{_redact_query(query)}"
    return redacted


def source_label(source: Source) -> str:
    """Display name for a source: its `type`, else the URL scheme, else 'sql'."""
    if source.type:
        return source.type
    scheme = (source.url or "").split("://", 1)[0]
    name = scheme.split("+", 1)[0]
    if name and not any(ch in name for ch in "/@:"):
        return name
    return "sql"


def _is_env_ref(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("${env:")


_PORT_TOKEN = re.compile(r"(?<=:)(sd\d+x[0-9a-f]+)(?=/|\?|$|:)")


def _holds_secret(value: str, secret: str) -> bool:
    """True when a query value is the password, or the password as a :/@ token.

    Substring containment dropped `application_name=administration` when the
    password is `admin`. Equality + `secret@` / `:secret@` missed `foo:hunter2`
    and `hunter2:`. Bound the secret with string edges or `:` / `@` so it has
    to be a token, not a prefix.
    """
    for candidate in (value, unquote(value)):
        if not candidate:
            continue
        padded = f":{candidate}:"
        if f":{secret}:" in padded or f":{secret}@" in padded or f"@{secret}:" in padded:
            return True
        if f"@{secret}@" in f"@{candidate}@":
            return True
    return False


def _scrub_query(url: str, *, stripped: str | None = None, tokens: set[str] | None = None) -> str:
    """Drop secret-named query keys and query values that reuse ``stripped``."""
    tokens = tokens or set()
    base, question, query = url.partition("?")
    if not question:
        return url
    kept = []
    for pair in query.split("&"):
        if not pair:
            continue
        key, sep, value = pair.partition("=")
        if (
            sep
            and is_secret_bag_key(key)
            and value
            and value not in tokens
            and not _is_env_ref(value)
        ):
            continue
        if stripped and _holds_secret(value, stripped):
            continue
        kept.append(pair)
    return f"{base}?{'&'.join(kept)}" if kept else base


def _drop_raw_userinfo_secret(url: str) -> str | None:
    """Best-effort drop of a literal userinfo password when make_url cannot parse.

    Username is optional so ``://:password@`` is stripped, not copied.
    """
    stripped = None
    match = re.match(r"^([^:/?#]+://[^/@:]*):([^@]+)(@.*)$", url)
    if match is not None:
        prefix, password, rest = match.groups()
        if not (_is_env_ref(password) or "${env:" in password):
            url = f"{prefix}{rest}"
            stripped = password
        return _scrub_query(url, stripped=stripped)
    _, sep, rest = url.partition("://")
    if sep:
        authority = rest.split("/", 1)[0].split("?", 1)[0]
        userinfo = authority.rsplit("@", 1)[0] if "@" in authority else ""
        if ":" in userinfo:
            return None
    return _scrub_query(url)


def _restore_port_tokens(url: str, port_nums: dict[str, str]) -> str:
    """Put env-ref tokens back in the authority only — not path or query."""
    if not port_nums:
        return url
    head, qsep, query = url.partition("?")
    scheme, sep, rest = head.partition("://")
    if not sep:
        return url
    authority, slash, path = rest.partition("/")
    for n, token in reversed(list(port_nums.items())):
        suffix = f":{n}"
        if authority.endswith(suffix):
            authority = authority[: -len(n)] + token
            break
    head = f"{scheme}://{authority}{slash}{path}"
    return head + (qsep + query if qsep else "")


def _parse_substituted(substituted: str):
    """Parse after env tokenization. Port tokens are not ints — retry those as numbers."""
    try:
        return make_url(substituted), {}
    except Exception as exc:
        port_nums: dict[str, str] = {}

        def port_repl(match: re.Match[str]) -> str:
            token = match.group(1)
            n = str(61000 + len(port_nums))
            port_nums[n] = token
            return n

        port_safe = _PORT_TOKEN.sub(port_repl, substituted)
        if port_safe == substituted:
            raise exc
        return make_url(port_safe), port_nums


def _project_url(url: str) -> str | None:
    """Copy a URL with plaintext passwords and secret query keys removed.

    ``make_url`` cannot parse ``${env:HOST}``. Substitute each env ref with a
    token that cannot collide with ``:1@`` in the original string, strip
    secrets on that parseable URL, then put the refs back. Unparseable URLs
    with env refs keep the refs but drop a literal userinfo password;
    unparseable URLs without them are dropped.
    """
    mapping: list[tuple[str, str]] = []

    def repl(match: re.Match[str]) -> str:
        token = f"sd{len(mapping)}x{token_hex(4)}"
        mapping.append((token, match.group(0)))
        return token

    substituted = ENV_REF.sub(repl, url)
    try:
        parsed, port_nums = _parse_substituted(substituted)
    except Exception:
        return _drop_raw_userinfo_secret(url) if "${env:" in url else None
    tokens = {token for token, _ in mapping}
    out = substituted
    stripped = None
    if parsed.password and parsed.password not in tokens:
        stripped = parsed.password
        literal = f":{parsed.password}@"
        head, qsep, rest = out.partition("?")
        if literal in head:
            out = head.replace(literal, "@", 1) + (qsep + rest if qsep else "")
        else:
            # Percent-encoded: `URL.set(password=None)` is a no-op, so re-render
            # with a sentinel (same as `_redact_url`) and splice that out.
            rendered = parsed.set(password=_REDACT_SENTINEL).render_as_string(hide_password=False)
            out = rendered.replace(f":{_REDACT_SENTINEL}@", "@", 1)
    out = _scrub_query(out, stripped=stripped, tokens=tokens)
    out = _restore_port_tokens(out, port_nums)
    for token, ref in mapping:
        out = out.replace(token, ref)
    return out


def is_secret_bag_key(key: str) -> bool:
    """True for connect_args/options keys that should never be copied as literals."""
    k = key.lower()
    if k in SECRET_BAG_PATH_KEYS:
        return False
    if k in SECRET_BAG_KEYS:
        return True
    bits = ("password", "token", "secret", "private_key", "credential", "passphrase")
    return any(bit in k for bit in bits)


def _project_bag(bag: dict) -> dict:
    """Keep env refs and non-secret keys; drop plaintext secret-named values."""
    out = {}
    for key, value in bag.items():
        if _is_env_ref(value):
            out[key] = value
            continue
        if isinstance(key, str) and is_secret_bag_key(key):
            continue
        if isinstance(value, dict):
            cleaned = _project_bag(value)
            if cleaned:
                out[key] = cleaned
            continue
        out[key] = value
    return out


def source_as_project_yaml(source: Source) -> dict:
    """Shape to write into a dashboard file: no plaintext secrets, env refs stay.

    Profile *names* travel (AWS-style); profile values never do. A literal
    password/token/url-password/connect_args secret is dropped so copying
    another dashboard's source cannot smuggle a secret into git.
    """
    data = source.model_dump(by_alias=True, exclude_none=True, exclude_defaults=True)
    for field in SECRET_FIELDS:
        val = data.get(field)
        if not val:
            continue
        if _is_env_ref(val):
            continue
        del data[field]
    if data.get("url"):
        cleaned = _project_url(data["url"])
        if cleaned:
            data["url"] = cleaned
        else:
            del data["url"]
    for bag in ("connect_args", "options"):
        if data.get(bag):
            cleaned = _project_bag(data[bag])
            if cleaned:
                data[bag] = cleaned
            else:
                del data[bag]
    return data


def redact_source(source: Source) -> dict:
    """Dump a source for API responses with secret fields, URL passwords and query
    strings, and connect_args/options values masked — every outbound source
    representation goes through this."""
    data = source.model_dump(by_alias=True, exclude_none=True, exclude_defaults=True)
    data["type"] = source_label(source)
    for field in SECRET_FIELDS:
        if data.get(field):
            data[field] = REDACTED
    if data.get("url"):
        data["url"] = _redact_url(data["url"])
    if data.get("connect_args"):
        data["connect_args"] = dict.fromkeys(data["connect_args"], REDACTED)
    if data.get("options"):
        data["options"] = dict.fromkeys(data["options"], REDACTED)
    return data


def _resolved_secret(value: Any) -> str | None:
    """Plaintext, including the value of a `${env:VAR}` ref once interpolated."""
    if isinstance(value, (int, float)):
        text = str(value)
        return text if len(text) >= 4 else None
    if not isinstance(value, str) or not value:
        return None
    if _is_env_ref(value) or "${env:" in value:
        try:
            value = interpolate_env(value)
        except SecretError:
            return None
        if not isinstance(value, str) or not value:
            return None
    return value


def _secret_fragments(source: Source) -> list[str]:
    """Credential strings that must never appear in an error message.

    Includes interpolated env-ref values: the driver sees the resolved secret
    and will echo it even when the YAML only stored `${env:…}`.
    """
    found: list[str] = []
    for field in SECRET_FIELDS:
        resolved = _resolved_secret(getattr(source, field, None))
        if resolved:
            found.append(resolved)
    for bag in (source.connect_args, source.options):
        if not bag:
            continue
        try:
            resolved_bag = interpolate_env(bag)
        except SecretError:
            resolved_bag = bag
        found.extend(_bag_secret_values(resolved_bag))
    if source.url:
        url = source.url
        try:
            url = interpolate_env(url)
        except SecretError:
            url = source.url
        try:
            parsed = make_url(url)
        except Exception:
            parsed = None
        if parsed is not None and parsed.password:
            pw = _resolved_secret(parsed.password) or parsed.password
            if pw and not _is_env_ref(pw):
                found.append(pw)
                found.append(unquote(pw))
    return found


def _bag_secret_values(bag: dict) -> list[str]:
    out: list[str] = []
    for key, value in bag.items():
        if isinstance(value, dict):
            out.extend(_bag_secret_values(value))
            continue
        if not isinstance(key, str) or not is_secret_bag_key(key):
            continue
        resolved = _resolved_secret(value)
        if resolved:
            out.append(resolved)
    return out


def scrub_error_text(message: str, source: Source) -> str:
    """Replace plaintext secret values in a driver error with the redact sentinel."""
    fragments = [f for f in _secret_fragments(source) if len(f) >= 4]
    fragments.sort(key=len, reverse=True)
    for frag in fragments:
        message = message.replace(frag, REDACTED)
    return message
