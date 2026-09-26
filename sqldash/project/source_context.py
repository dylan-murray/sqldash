"""Portable source references with a per-query role, database and warehouse override."""

import json

from sqldash.secrets import ENV_REF

PREFIX = "@role:"
CONTEXT_PREFIX = "@context:"


def _valid(value):
    return isinstance(value, str) and 0 < len(value) <= 1000 and "\x00" not in value


FIELDS = ("role", "database", "warehouse")


def _checked(base, role, database, warehouse):
    if not _valid(base) or base.startswith((PREFIX, CONTEXT_PREFIX)):
        raise ValueError
    overrides = (role, database, warehouse)
    for override in overrides:
        if override is not None and (not _valid(override) or ENV_REF.search(override)):
            raise ValueError
    if all(override is None for override in overrides):
        raise ValueError
    return base, role, database, warehouse


def split_source_context(key):
    """(base, role, database, warehouse) for a picker key; overrides are None when absent."""
    if not key or not key.startswith((PREFIX, CONTEXT_PREFIX)):
        return key, None, None, None
    try:
        if key.startswith(PREFIX):
            value = json.loads(key[len(PREFIX) :])
            if not isinstance(value, list) or len(value) != 2:
                raise ValueError
            return _checked(value[0], value[1], None, None)
        value = json.loads(key[len(CONTEXT_PREFIX) :])
        if not isinstance(value, dict) or set(value) - {"source", *FIELDS}:
            raise ValueError
        return _checked(value.get("source"), *(value.get(field) for field in FIELDS))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("Invalid role-qualified source reference") from exc


def split_role_source(key):
    base, role, _, _ = split_source_context(key)
    return base, role


def context_source(base, role=None, database=None, warehouse=None):
    """The picker key for `base` with overrides, or `base` itself when there are none.

    A role alone keeps the `@role:` form so keys saved before databases could be
    chosen stay identical.
    """
    if role is None and database is None and warehouse is None:
        return base
    if database is None and warehouse is None:
        key = PREFIX + json.dumps([base, role], ensure_ascii=False, separators=(",", ":"))
    else:
        fields = {"source": base, "role": role, "database": database, "warehouse": warehouse}
        key = CONTEXT_PREFIX + json.dumps(
            {k: v for k, v in fields.items() if v is not None},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    split_source_context(key)
    return key


def role_source(base, role):
    return context_source(base, role)
