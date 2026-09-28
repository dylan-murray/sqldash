"""Turning a BI tool's identifiers into sqldash ones at import time.

`order status` is a legal Snowflake dimension and `select` is a legal LookML
measure; sqldash refuses both when it loads the file. Importers used to write
them through untouched, so an import could produce a metrics.yaml that
`sqldash lint` rejected, with no diagnostic at the point of the write (#337).
Every rename is warned, naming the original and the new name, because the
metric an author goes looking for is under the name their BI tool used.
"""

import re
from collections.abc import Container

from sqldash.models.chart import IDENTIFIER
from sqldash.models.semantics import SQL_RESERVED

_NON_IDENT = re.compile(r"[^A-Za-z0-9_]+")


def slugify_name(value: str, kind: str = "field", *, allow_reserved: bool = False) -> str:
    """The nearest sqldash name to `value`: non-identifier runs collapse to `_`,
    a leading digit takes an `f_` prefix, and a reserved word takes a `_<kind>`
    suffix (relation keys pass `allow_reserved`, the one place the model allows
    a reserved word because nothing emits it into SQL)."""
    text = _NON_IDENT.sub("_", str(value).strip().strip("\"'`")).strip("_")
    if not text:
        text = kind
    if text[0].isdigit():
        text = f"f_{text}"
    if not allow_reserved and text.lower() in SQL_RESERVED:
        text = f"{text}_{kind}"
    return text


def import_name(
    original: str,
    label: str,
    warnings: list[str],
    *,
    kind: str = "field",
    allow_reserved: bool = False,
    taken: Container[str] = (),
) -> str:
    """`slugify_name`, made unique against `taken`, with a warning when it moved."""
    slug = slugify_name(original, kind, allow_reserved=allow_reserved)
    name = slug
    suffix = 2
    while name in taken:
        name = f"{slug}_{suffix}"
        suffix += 1
    if name == original:
        return name
    if slug != original:
        reason = (
            "is a SQL reserved word"
            if IDENTIFIER.match(original)
            else "is not a valid sqldash identifier"
        )
    else:
        reason = "is already taken"
    warnings.append(f"{label} '{original}' {reason} — imported as '{name}'")
    return name


def source_column(name: str) -> str:
    """The renamed field's `expr`: the column the source actually named.

    A dimension's expr defaults to its own name, so renaming one without an expr
    would repoint it at a column that does not exist. A reserved word is quoted
    too: bare, it does not parse at all — `SELECT order AS "x"` is a syntax
    error on DuckDB and on Postgres, both checked live — so the import wrote an
    expr every query of that metric would die on while lint stayed clean.

    Quoting is right for Snowflake as well, which is the objection to weigh
    since Cortex is a Snowflake importer: a semantic view declares a column in
    the form Snowflake stored it (the fixtures are all `REGION`, `CLAIM_DATE`),
    so a reserved column arrives as `ORDER` and goes out as `"ORDER"`, which is
    the stored identifier. Quoting preserves whatever case the source declared,
    and the declared case is the stored case; it is only bare identifiers that
    fold. Not verified against a live Snowflake — nothing in this repo is.
    """
    if IDENTIFIER.match(name) and name.lower() not in SQL_RESERVED:
        return name
    return '"' + name.replace('"', '""') + '"'
