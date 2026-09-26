"""LookML import (optional 'lkml' dependency): views become relations, measures
become metrics via aggregate templates, time dimension_groups become time_dimensions."""

import copy
import importlib.util
import json
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NamedTuple

from sqldash.models.semantics import parse_window
from sqldash.semantics.compiler import TRUNC_MACRO, dialect_kind, exported_sql
from sqldash.semantics.layer import SemanticError
from sqldash.semantics.naming import import_name, source_column

TABLE_REF = re.compile(r"\$\{TABLE\}\.([A-Za-z_][A-Za-z0-9_]*)")
FIELD_REF = re.compile(r"\$\{([^}]+)\}")
_LKML_LOAD = "import json,sys,lkml; json.dump(lkml.load(sys.stdin.read()), sys.stdout)"
_LKML_LOAD_TIMEOUT_S = 8

MEASURE_TEMPLATES = {
    "count": lambda col: "COUNT(*)",
    "count_distinct": lambda col: f"COUNT(DISTINCT {col})",
    "sum": lambda col: f"SUM({col})",
    "average": lambda col: f"AVG({col})",
    "min": lambda col: f"MIN({col})",
    "max": lambda col: f"MAX({col})",
    "median": lambda col: f"MEDIAN({col})",
}


# LookML strings escape quotes and backslashes, and the lkml parser hands them
# back still escaped. Unescaping on import and escaping on export is what lets a
# description containing a quote survive a round trip.
_LKML_ESCAPE = re.compile(r'\\(["\\])')


def _lkml_text(value: Any) -> str:
    return _LKML_ESCAPE.sub(r"\1", str(value))


def _lkml_quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _lkml_strings(values: list[str]) -> str:
    return "[" + ", ".join(_lkml_quote(value) for value in values) + "]"


def _lkml_synonyms(field: dict[str, Any]) -> list[str]:
    """A field's `synonyms:`. Looker takes a string or a list of them, and lkml
    hands each form back as it was written."""
    value = field.get("synonyms")
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    return [text for text in (_lkml_text(v).strip() for v in values) if text]


# sqldash format -> Looker value_format_name. `number` is Looker's default
# rendering, so it needs no name. `currency` follows each dashboard's currency
# (USD when none is set), which LookML cannot express, so it becomes usd with a
# warning. compact, date and other currencies have no named Looker format.
_FORMAT_TO_LOOKER = {"USD": "usd", "EUR": "eur", "GBP": "gbp", "percent": "percent_1"}
_LOOKER_TO_FORMAT = {
    **dict.fromkeys(("usd", "usd_0"), "USD"),
    **dict.fromkeys(("eur", "eur_0"), "EUR"),
    **dict.fromkeys(("gbp", "gbp_0"), "GBP"),
    **{f"percent_{n}": "percent" for n in range(5)},
    **{f"decimal_{n}": "number" for n in range(5)},
}

# `cumulative:` and `window:` have no LookML measure type, so the exported
# measure is the per-bucket sum and the semantics rides on two carriers instead.
# `tags:` is a real LookML field parameter holding consumer-defined strings, so
# Looker still validates the view and `import lookml` can restore the metric;
# the note on `description:` is the same loss stated in the one field a Looker
# user reads, and it is what keeps the re-imported metric honest when the tag
# does not survive a hand edit. Import strips the note only when it restored the
# semantics the note describes.
_TAG_PREFIX = "sqldash:"
_LOSS_NOTE = re.compile(r"\s*\(sqldash: [^()]*\)\s*$")


def _carried_loss(metric: Any) -> tuple[list[str], str | None]:
    """The tags and description note for what LookML cannot compute, if anything."""
    if metric.cumulative:
        return [f"{_TAG_PREFIX}cumulative"], "a running total"
    if metric.window:
        return [f"{_TAG_PREFIX}window={metric.window}"], f"a trailing window of {metric.window}"
    return [], None


# A dimension_group's timeframes, in the terms sqldash already compiles grains
# with. The import target is unknown here — the source in the output is a
# placeholder the user fills in, and a metrics.yaml can be pointed at another
# source later — so a timeframe becomes the dialect-neutral SQLDASH_TRUNC macro
# and the compiler spells it per dialect. `raw` and `time` are the untruncated
# column. Timeframes that extract rather than truncate — day_of_week, month_name
# — have no truncation equivalent, so they are deliberately absent and fall
# through to being skipped rather than guessed.
_TIMEFRAME_GRAINS = {
    "raw": None,
    "time": None,
    "date": "day",
    "hour": "hour",
    "week": "week",
    "month": "month",
    "quarter": "quarter",
    "year": "year",
}


def _lookup(match: "re.Match[str]", fields: dict[str, str], groups: dict[str, str]) -> str:
    """A field reference, or one of a dimension_group's generated timeframes."""
    name = match.group(1)
    if name in fields:
        return fields[name]
    # `dimension_group: ordered` generates ordered_date, ordered_week, ... and a
    # measure references those, never the bare group name. They read the same
    # column, but not at the same grain — resolving them all to the raw column
    # imports a metric that returns a different number than the one it came
    # from, wherever the column carries a time.
    for group, expr in groups.items():
        if not name.startswith(f"{group}_"):
            continue
        grain = _TIMEFRAME_GRAINS.get(name[len(group) + 1 :], "")
        if grain is None:
            return expr
        if grain:
            return f"{TRUNC_MACRO}('{grain}', {expr})"
    return match.group(0)


def _resolve_field_chain(
    fields: dict[str, str], groups: dict[str, str], vname: str, warnings: list[str]
) -> dict[str, str]:
    """Expand fields defined over other fields, so one substitution resolves a
    measure written over them.

    `net: ${gross} - ${tax}` is as ordinary as a measure over a bare dimension.
    Substituting once left `${gross}` behind, and that residue was then reported
    as a cross-field reference — naming a field this very view defines.
    """
    resolved = dict(fields)
    for _ in range(len(resolved) + 1):
        changed = False
        for name, expr in list(resolved.items()):
            others = {k: v for k, v in resolved.items() if k != name}
            expanded = FIELD_REF.sub(lambda m, others=others: _lookup(m, others, groups), expr)
            if expanded != expr:
                resolved[name] = expanded
                changed = True
        if not changed:
            return resolved
    warnings.append(
        f"view '{vname}': dimensions reference each other in a cycle — "
        f"measures over them will not import"
    )
    return resolved


def _unresolved_reason(
    ref: str, fields: dict[str, str], groups: dict[str, str], measures: set[str]
) -> str:
    """Why a `${...}` survived resolution.

    Only a name this view does not define is genuinely cross-field. Calling
    every leftover reference "cross-field" told an author their own view was
    somebody else's, which is the least useful thing a warning can say.
    """
    name = ref.strip("${}")
    if "." in name:
        return f"reference {ref} to another view is not supported"
    if name in fields or name in groups:
        return f"{ref} is part of a reference cycle in this view"
    if name in measures:
        return f"{ref} is another measure, and measures over measures are not supported"
    for group in groups:
        if name.startswith(f"{group}_"):
            timeframe = name[len(group) + 1 :]
            return f"timeframe '{timeframe}' of dimension_group '{group}' has no SQL equivalent"
    return f"reference {ref} names no field this view defines"


def _resolve_sql(
    sql: str | None,
    warnings: list[str],
    context: str,
    fields: dict[str, str] | None = None,
    groups: dict[str, str] | None = None,
    measures: set[str] | None = None,
) -> str | None:
    """Turn a LookML sql fragment into a plain column expression.

    `${TABLE}.col` is the table-qualified form. A bare `${name}` is a reference
    to another field *in the same view* — the dominant way a measure names the
    column it aggregates (`measure: revenue { type: sum sql: ${amount} }`) — so
    it resolves to whatever that field is defined as.
    """
    if sql is None:
        return None
    resolved = TABLE_REF.sub(lambda m: m.group(1), sql).strip().rstrip(";")
    if fields:
        resolved = FIELD_REF.sub(lambda m: _lookup(m, fields, groups or {}), resolved)
    unresolved = FIELD_REF.search(resolved)
    if unresolved:
        reason = _unresolved_reason(
            unresolved.group(0), fields or {}, groups or {}, measures or set()
        )
        warnings.append(f"skipped {context}: {reason}")
        return None
    return resolved


_NUMBER = re.compile(r"^\d+(\.\d+)?$")
_NUMBER_TERM = re.compile(r"^(>=|<=|!=|<>|>|<|=)?\s*(\d+(?:\.\d+)?)$")
_COLUMN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_STRING_TYPES = frozenset({"string", ""})
_GROUPING_TYPES = frozenset({"tier", "bin"})


class _Unsupported(Exception):
    pass


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _string_filter(col: str, expression: str) -> str:
    """Looker's string filter grammar, for the terms that have one reading:
    exact values and `%` patterns, a leading `-` to exclude, NULL. An excluded
    value keeps NULL rows, as Looker's own `<>` filter does."""
    if "^" in expression:
        raise _Unsupported
    include: list[str] = []
    exclude: list[str] = []
    for raw in expression.split(","):
        term = raw.strip()
        negated = term.startswith("-")
        term = term[1:].strip() if negated else term
        if not term or term.upper() == "EMPTY":
            raise _Unsupported
        if term.upper() == "NULL":
            (exclude if negated else include).append(
                f"{col} IS NOT NULL" if negated else f"{col} IS NULL"
            )
            continue
        op = "LIKE" if "%" in term else "="
        if negated:
            exclude.append(f"(NOT {col} {op} {_sql_string(term)} OR {col} IS NULL)")
        else:
            include.append(f"{col} {op} {_sql_string(term)}")
    parts = exclude
    if include:
        parts = [include[0] if len(include) == 1 else f"({' OR '.join(include)})", *exclude]
    return " AND ".join(parts)


def _number_filter(col: str, expression: str) -> str:
    terms = [t.strip() for t in expression.split(",")]
    if len(terms) > 1:
        if not all(_NUMBER.match(t) for t in terms):
            raise _Unsupported
        return f"{col} IN ({', '.join(terms)})"
    term = terms[0]
    if term.upper() == "NULL":
        return f"{col} IS NULL"
    if term.upper() == "NOT NULL":
        return f"{col} IS NOT NULL"
    if term.upper().startswith("NOT ") and _NUMBER.match(term[4:].strip()):
        return f"{col} <> {term[4:].strip()}"
    match = _NUMBER_TERM.match(term)
    if not match:
        raise _Unsupported
    op = {None: "=", "!=": "<>"}.get(match.group(1), match.group(1))
    return f"{col} {op} {match.group(2)}"


def _measure_filters(
    measure: dict[str, Any],
    vname: str,
    fields: dict[str, str],
    types: dict[str, str],
) -> tuple[list[str], str | None]:
    """A measure's `filters:` as the static SQL a metric's `filters:` holds, or
    the reason one of them cannot be carried.

    The filter is part of what the measure counts, so a filter that cannot be
    translated means the measure cannot be imported: without it the metric is a
    different, larger number under the same name.
    """
    snippets: list[str] = []
    for group in measure.get("filters__all", []):
        for item in group if isinstance(group, list) else [group]:
            if not isinstance(item, dict):
                return [], f"filter {item!r} is not a `field: value` pair"
            for field, raw in item.items():
                expression = _lkml_text(raw)
                shown = f'{field}: "{expression}"'
                name = field.removeprefix(f"{vname}.")
                if "." in name:
                    return [], f"filter {shown} is on another view"
                if name not in types:
                    return [], f"filter {shown} names no dimension this view defines"
                col = fields.get(name, "")
                if FIELD_REF.search(col) or not col:
                    return [], f"filter {shown} is on a field that did not resolve to SQL"
                kind = types[name]
                col = col if _COLUMN.match(col) else f"({col})"
                try:
                    if kind in _STRING_TYPES:
                        snippets.append(_string_filter(col, expression))
                    elif kind == "number":
                        snippets.append(_number_filter(col, expression))
                    elif kind == "yesno" and expression.lower() in ("yes", "no"):
                        snippets.append(col if expression.lower() == "yes" else f"NOT ({col})")
                    else:
                        raise _Unsupported
                except _Unsupported:
                    return [], (
                        f"filter {shown} has no sqldash equivalent, and dropping it "
                        "would change what the measure computes"
                    )
    return snippets, None


def _tagged_semantics(measure: dict[str, Any], has_time: bool) -> tuple[dict[str, Any], str | None]:
    """`cumulative:` / `window:` as `export lookml` tagged them, or the reason the
    measure cannot be imported at all.

    Same rule as a filter with no sqldash reading: the tag says this measure is a
    running total or a trailing window, so importing it as the bare aggregate
    would write a larger number under a name and a description that still promise
    the original. A missing metric is better than that.
    """
    carried: dict[str, Any] = {}
    for tag in measure.get("tags") or []:
        marker = _lkml_text(tag)
        if not marker.startswith(_TAG_PREFIX):
            continue
        body = marker[len(_TAG_PREFIX) :]
        if body == "cumulative":
            carried["cumulative"] = True
        elif body.startswith("window="):
            spec = body[len("window=") :]
            try:
                parse_window(spec)
            except ValueError as exc:
                return {}, f"tag '{marker}' is not a window sqldash can read: {exc}"
            carried["window"] = spec
        else:
            return {}, (
                f"tag '{marker}' is a sqldash marker this version cannot read, and "
                "importing the measure without it would change what it computes"
            )
    if len(carried) > 1:
        return {}, "tags carry both cumulative and a window, which cannot be combined"
    if carried and not has_time:
        return {}, (
            f"tags carry {next(iter(carried))}, which accumulates along a time "
            "dimension, and this view has none"
        )
    return carried, None


_UNTIL_DOUBLE_SEMI = frozenset({"sql", "html", "sql_table_name"})


def _expand_compact_fields(text: str) -> str:
    """Break a field-separating `; next_key:` onto the next line.

    lkml 1.3.7 hangs on `[a, b]; next:` and reads `type: sum;` as `sum;`.
    Those are separators. A `; word:` inside a `;;`-terminated value
    (sql, html, sql_table_name) is not — splitting those imported
    different SQL with no warning, quoted or not.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    quote: str | None = None
    until_dsemi = False
    while i < n:
        c = text[i]
        if quote is not None:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in "'\"":
            quote = c
            out.append(c)
            i += 1
            continue
        if c == "#":
            nl = text.find("\n", i)
            if nl < 0:
                out.append(text[i:])
                break
            out.append(text[i:nl])
            i = nl
            continue
        if c == ";" and i + 1 < n and text[i + 1] == ";":
            out.append(";;")
            until_dsemi = False
            i += 2
            continue
        if not until_dsemi and (c.isalpha() or c == "_"):
            k = i + 1
            while k < n and (text[k].isalnum() or text[k] == "_"):
                k += 1
            m = k
            while m < n and text[m] in " \t":
                m += 1
            if m < n and text[m] == ":":
                if text[i:k].lower() in _UNTIL_DOUBLE_SEMI:
                    until_dsemi = True
                out.append(text[i : m + 1])
                i = m + 1
                continue
        if c == ";" and not until_dsemi:
            j = i + 1
            while j < n and text[j] in " \t":
                j += 1
            k = j
            while k < n and (text[k].isalnum() or text[k] == "_"):
                k += 1
            m = k
            while m < n and text[m] in " \t":
                m += 1
            if j < k and m < n and text[m] == ":":
                out.append("\n")
                i += 1
                continue
        out.append(c)
        i += 1
    return "".join(out)


def _lkml_load(text: str) -> Any:
    """Parse LookML without letting a lexer hang take the process with it."""
    expanded = _expand_compact_fields(text)
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _LKML_LOAD],
            input=expanded,
            capture_output=True,
            text=True,
            timeout=_LKML_LOAD_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise SemanticError("LookML parse did not complete") from exc
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "lkml failed").strip()
        last = next((line.strip() for line in reversed(err.splitlines()) if line.strip()), "")
        raise SemanticError(last or "lkml failed")
    return json.loads(proc.stdout)


def _view_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return sorted(p for p in path.rglob("*.lkml") if ".model." not in p.name)


def _unquote_table_ref(value: str) -> str:
    """One outer pair of quotes off a `sql_table_name`, and only when the whole
    value is a single quoted identifier whose contents need no quoting.

    `str.strip('`"')` removed every leading and trailing quote character
    independently, so a reference that merely *starts* with a quoted identifier
    (`"PROD DB".ORDERS`) lost its opening quote and kept the inner one — a
    reference no engine can parse, written verbatim into `relations:` (#332).
    Anything that is not a redundantly quoted plain identifier is already valid
    SQL as the author wrote it, so it passes through untouched.
    """
    text = value.strip()
    for quote in ('"', "`"):
        if len(text) < 2 or not text.startswith(quote) or not text.endswith(quote):
            continue
        inner = text[1:-1]
        if quote in inner.replace(quote * 2, ""):
            continue
        return inner if _IDENT.match(inner) else text
    return text


_FIELD_LISTS = ("dimensions", "dimension_groups", "measures", "filters", "parameters", "sets")
_VIEW_BASES = ("sql_table_name", "derived_table")


def _readable_fields(view: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    """The view with every field the importer cannot read removed and named.

    `dimension: x extends: y { ... }` puts `extends:` outside the braces, and
    lkml reads that field as the bare string `x`. A field-level `extends:`
    inherits sql this importer does not resolve, so the field would come back
    as a column of its own name. Both are skipped with a warning rather than
    dropped silently or imported as a different field."""
    vname = view.get("name")
    cleaned = dict(view)
    for key in _FIELD_LISTS:
        entries = view.get(key)
        if entries is None:
            continue
        if not isinstance(entries, list):
            warnings.append(
                f"view '{vname}': skipped {key} lkml read only as {entries!r}; "
                "extends: belongs inside the field's braces"
            )
            cleaned[key] = []
            continue
        kind = key.removesuffix("s").replace("_", " ")
        kept = []
        for field in entries:
            if not isinstance(field, dict):
                warnings.append(
                    f"view '{vname}': skipped a {kind} lkml read only as {field!r}; "
                    "extends: belongs inside the field's braces"
                )
            elif field.get("extends__all"):
                warnings.append(
                    f"view '{vname}': skipped {kind} '{field.get('name')}': field-level "
                    "extends: is not supported, so it would import without the sql it inherits"
                )
            else:
                kept.append(field)
        cleaned[key] = kept
    return cleaned


def _merge_view(parent: dict[str, Any], child: dict[str, Any]) -> dict[str, Any]:
    """LookML's `extends:` merge: the extending view's parameters win, and a
    field it redefines keeps every parameter it does not restate."""
    merged = {k: v for k, v in parent.items() if k != "extension"}
    if any(child.get(k) for k in _VIEW_BASES):
        for key in _VIEW_BASES:
            merged.pop(key, None)
    for key, value in child.items():
        if key in _FIELD_LISTS and isinstance(value, list):
            fields = {f.get("name"): f for f in merged.get(key, []) if isinstance(f, dict)}
            for field in value:
                if isinstance(field, dict):
                    name = field.get("name")
                    fields[name] = {**fields.get(name, {}), **field}
            merged[key] = list(fields.values())
        else:
            merged[key] = value
    return merged


def _extends(view: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for entry in view.get("extends__all", []):
        for name in entry if isinstance(entry, list) else [entry]:
            if isinstance(name, str) and name not in names:
                names.append(name)
    return names


def _resolve_extends(views: list[dict[str, Any]], warnings: list[str]) -> list[dict[str, Any]]:
    """Every importable view with the fields it inherits through `extends:`
    merged in, left to right, then its own on top.

    A base can live in any imported file. A view marked `extension: required`
    only exists to be extended, so it is not a relation of its own.
    """
    by_name: dict[str, dict[str, Any]] = {}
    for view in views:
        by_name.setdefault(view.get("name"), view)
    resolved: dict[str, dict[str, Any] | str] = {}

    def resolve(name: str, stack: tuple[str, ...]) -> dict[str, Any] | str:
        if name in resolved:
            return resolved[name]
        view = by_name[name]
        merged: dict[str, Any] = {}
        for base in _extends(view):
            path = " -> ".join((*stack, name, base))
            if base in stack or base == name:
                return f"extends '{base}' in a cycle ({path})"
            if base not in by_name:
                return f"extends '{base}', which is not in the imported files ({path})"
            inherited = resolve(base, (*stack, name))
            if isinstance(inherited, str):
                return inherited
            merged = _merge_view(merged, inherited)
        own = {k: v for k, v in view.items() if k != "extends__all"}
        result = _merge_view(merged, own) if merged else own
        result["name"] = name
        resolved[name] = result
        return result

    out: list[dict[str, Any]] = []
    for view in views:
        name = view.get("name")
        if not name:
            continue
        if view.get("extension") == "required":
            warnings.append(
                f"skipped view '{name}': extension: required, so only the views "
                "that extend it are imported"
            )
            continue
        if by_name[name] is not view:
            if _extends(view):
                warnings.append(
                    f"skipped view '{name}': another view with the same name was imported "
                    "first, and extends: resolves by name (Looker rejects duplicate view names)"
                )
                continue
            out.append(view)
            continue
        merged = resolve(name, ())
        if isinstance(merged, str):
            warnings.append(f"skipped view '{name}': {merged}")
            continue
        out.append(merged)
    return out


def _load_views(files: list[Path], warnings: list[str]) -> list[dict[str, Any]]:
    loaded: list[dict[str, Any]] = []
    for file in files:
        try:
            parsed = _lkml_load(file.read_text())
        except Exception as exc:
            warnings.append(f"skipped {file.name}: parse error: {exc}")
            continue
        for view in parsed.get("views", []):
            if isinstance(view, dict):
                loaded.append(view)
            elif parsed.get("extends__all"):
                warnings.append(
                    f"skipped view '{view}' in {file.name}: `extends:` goes inside the view's "
                    f"braces, as in `view: {view} {{ extends: [base_view] ... }}`"
                )
            else:
                warnings.append(f"skipped view '{view}' in {file.name}: not a view block")
    return loaded


def _view_base(view: dict[str, Any], vname: str, warnings: list[str]) -> dict[str, str] | None:
    derived = view.get("derived_table")
    if derived is not None and not isinstance(derived, dict):
        warnings.append(
            f"skipped view '{vname}': derived_table lkml read only as {derived!r}; "
            "extends: belongs inside its braces"
        )
        return None
    if view.get("sql_table_name"):
        return {"table": _unquote_table_ref(view["sql_table_name"])}
    if (derived or {}).get("sql"):
        return {"sql": view["derived_table"]["sql"].strip()}
    warnings.append(f"skipped view '{vname}': no sql_table_name or derived_table")
    return None


class _ViewFields(NamedTuple):
    fields: dict[str, str]
    groups: dict[str, str]
    measures: set[str]
    types: dict[str, str]


def _view_fields(view: dict[str, Any], vname: str, warnings: list[str]) -> _ViewFields:
    """What a same-view `${name}` resolves to: the field's own sql if it has
    one, otherwise the column named after it. Built from every dimension, not
    just the ones that become metric dimensions, since a measure may aggregate
    a time field too."""
    fields: dict[str, str] = {}
    for d in view.get("dimensions", []):
        if not d.get("name"):
            continue
        expr = TABLE_REF.sub(lambda m: m.group(1), d.get("sql") or "").strip().rstrip(";")
        fields[d["name"]] = expr or d["name"]
    groups: dict[str, str] = {}
    for g in view.get("dimension_groups", []):
        if not g.get("name"):
            continue
        expr = TABLE_REF.sub(lambda m: m.group(1), g.get("sql") or "").strip().rstrip(";")
        groups[g["name"]] = expr or g["name"]
    fields.update(groups)
    fields = _resolve_field_chain(fields, groups, vname, warnings)
    measures = {m["name"] for m in view.get("measures", []) if m.get("name")}
    types = {
        d["name"]: str(d.get("type") or "string").lower()
        for d in view.get("dimensions", [])
        if d.get("name")
    }
    return _ViewFields(fields, groups, measures, types)


def _import_dimensions(
    view: dict[str, Any], vname: str, scope: _ViewFields, warnings: list[str]
) -> list[dict[str, Any]]:
    dims: list[dict[str, Any]] = []
    for d in view.get("dimensions", []):
        if d.get("type") in ("time", "date"):
            continue
        if d.get("type") in _GROUPING_TYPES:
            warnings.append(
                f"skipped dimension '{vname}.{d.get('name')}': type '{d['type']}' "
                "buckets values into ranges, which sqldash dimensions cannot express"
            )
            continue
        expr = _resolve_sql(
            d.get("sql"),
            warnings,
            f"dimension '{vname}.{d.get('name')}'",
            scope.fields,
            scope.groups,
            scope.measures,
        )
        if d.get("sql") is not None and expr is None:
            continue
        dname = import_name(
            d["name"],
            f"view '{vname}': dimension",
            warnings,
            taken=[existing["name"] for existing in dims],
        )
        item = {"name": dname}
        expr = expr or source_column(d["name"])
        if expr != dname:
            item["expr"] = expr
        if d.get("description"):
            item["description"] = _lkml_text(d["description"])
        synonyms = _lkml_synonyms(d)
        if synonyms:
            item["synonyms"] = synonyms
        dims.append(item)
    return dims


def _import_time_dimension(
    view: dict[str, Any], vname: str, warnings: list[str]
) -> dict[str, Any] | None:
    time_dimension = None
    kept = ""
    dropped: list[str] = []
    for group in view.get("dimension_groups", []):
        if group.get("type") not in ("time", "date"):
            continue
        expr = _resolve_sql(
            group.get("sql"), warnings, f"dimension_group '{vname}.{group.get('name')}'"
        )
        if expr is None:
            continue
        if time_dimension is not None:
            dropped.append(group["name"])
            continue
        kept = group["name"]
        tdname = import_name(kept, f"view '{vname}': time dimension", warnings)
        if _lkml_synonyms(group):
            warnings.append(
                f"view '{vname}': dimension_group '{kept}' has synonyms, which a "
                "sqldash time dimension has no field for; they were dropped"
            )
        time_dimension = {"name": tdname, "grain": "day"}
        expr = expr or source_column(kept)
        if expr != tdname:
            time_dimension["expr"] = expr
    if dropped:
        warnings.append(
            f"view '{vname}': only the first time dimension "
            f"('{kept}') was kept per metric; "
            f"dropped: {', '.join(dropped)}"
        )
    return time_dimension


def _import_measure(
    measure: dict[str, Any],
    vname: str,
    rname: str,
    scope: _ViewFields,
    time_dimension: dict[str, Any] | None,
    dims: list[dict[str, Any]],
    warnings: list[str],
) -> dict[str, Any] | None:
    mname = measure.get("name")
    mtype = measure.get("type", "count")
    template = MEASURE_TEMPLATES.get(mtype)
    if template is None:
        warnings.append(f"skipped measure '{vname}.{mname}': type '{mtype}' not supported")
        return None
    context = f"measure '{vname}.{mname}'"
    had_sql = measure.get("sql") is not None
    # COUNT(*) ignores the column, so a `type: count` measure's sql
    # is never used — resolving it produced a "skipped" warning for a
    # measure that then shipped anyway.
    col = (
        None
        if mtype == "count"
        else _resolve_sql(
            measure.get("sql"), warnings, context, scope.fields, scope.groups, scope.measures
        )
    )
    if mtype != "count" and col is None:
        # _resolve_sql already said why when there was sql to reject;
        # this is only for a measure that supplied none at all.
        if not had_sql:
            warnings.append(f"skipped {context}: no sql to aggregate")
        return None
    filters, reason = _measure_filters(measure, vname, scope.fields, scope.types)
    if reason:
        warnings.append(f"skipped {context}: {reason}")
        return None
    carried, reason = _tagged_semantics(measure, time_dimension is not None)
    if reason:
        warnings.append(f"skipped {context}: {reason}")
        return None
    entry: dict[str, Any] = {"relation": rname, "expr": template(col)}
    entry.update(carried)
    if filters:
        entry["filters"] = filters
    if measure.get("label"):
        entry["title"] = _lkml_text(measure["label"])
    description = _lkml_text(measure.get("description") or "")
    if carried:
        description = _LOSS_NOTE.sub("", description)
    if description:
        entry["description"] = description
    format_name = measure.get("value_format_name")
    if format_name:
        if format_name in _LOOKER_TO_FORMAT:
            entry["format"] = _LOOKER_TO_FORMAT[format_name]
        else:
            warnings.append(
                f"{context}: value_format_name {format_name} has no sqldash format; it was dropped"
            )
    synonyms = _lkml_synonyms(measure)
    if synonyms:
        entry["synonyms"] = synonyms
    if time_dimension:
        entry["time_dimension"] = dict(time_dimension)
    if dims:
        entry["dimensions"] = copy.deepcopy(dims)
    return entry


def _import_view(view: dict[str, Any], out: dict[str, Any], warnings: list[str]) -> None:
    vname = view.get("name")
    if not vname:
        return
    base = _view_base(view, vname, warnings)
    if base is None:
        return
    rname = import_name(
        vname,
        "view",
        warnings,
        kind="relation",
        allow_reserved=True,
        taken=out["relations"],
    )
    out["relations"][rname] = base
    scope = _view_fields(view, vname, warnings)
    dims = _import_dimensions(view, vname, scope, warnings)
    time_dimension = _import_time_dimension(view, vname, warnings)
    for measure in view.get("measures", []):
        entry = _import_measure(measure, vname, rname, scope, time_dimension, dims, warnings)
        if entry is None:
            continue
        slug = import_name(measure.get("name"), f"view '{vname}': measure", warnings, kind="metric")
        key = slug if slug not in out["metrics"] else f"{rname}_{slug}"
        out["metrics"][key] = entry


def import_lookml(path: Path) -> tuple[dict, list[str]]:
    """Translate .lkml view files under a path into a metrics.yaml mapping,
    collecting warnings for anything skipped (unsupported measure types,
    cross-field ``${...}`` references)."""
    if importlib.util.find_spec("lkml") is None:
        raise SemanticError(
            "LookML import needs the 'lkml' parser — install with: pip install 'sqldash[lookml]'"
        )

    warnings: list[str] = []
    out: dict[str, Any] = {
        "source": {
            "type": "snowflake",
            "account": "<your-account>",
            "warehouse": "<your-warehouse>",
            "database": "<database>",
            "schema": "<schema>",
            "authentication": "externalbrowser",
            "username": "${env:SNOWFLAKE_USER}",
        },
        "relations": {},
        "metrics": {},
    }

    files = _view_files(path)
    if not files:
        raise SemanticError(f"no .lkml view files found under {path}")

    loaded = _load_views(files, warnings)
    for view in _resolve_extends([_readable_fields(v, warnings) for v in loaded], warnings):
        _import_view(view, out, warnings)

    if not out["metrics"]:
        # The warnings are the whole explanation when nothing came through — a
        # file that failed to parse is recorded there, and reporting only "no
        # importable measures" sends the user looking at their measures instead
        # of at the syntax error that stopped the file being read at all.
        detail = "".join(f"\n  - {w}" for w in warnings)
        raise SemanticError(f"no importable measures found in the LookML{detail}")
    return out, warnings


_CARRIED = (
    "the measure carries the semantics on tags: and says so in its description:, "
    "so import lookml restores it"
)
_NUMBER_DROP = (
    "LookML emits type: number, and import lookml skips that type, "
    "so a round-trip drops the measure"
)
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NON_IDENT = re.compile(r"[^A-Za-z0-9_]+")
_AGG_PATTERNS = (
    (re.compile(r"^COUNT\s*\(\s*\*\s*\)$", re.I), "count", None),
    (re.compile(r"^COUNT\s*\(\s*DISTINCT\s+(.+)\)$", re.I | re.S), "count_distinct", 1),
    (re.compile(r"^SUM\s*\(\s*(.+)\)$", re.I | re.S), "sum", 1),
    (re.compile(r"^AVG\s*\(\s*(.+)\)$", re.I | re.S), "average", 1),
    (re.compile(r"^MIN\s*\(\s*(.+)\)$", re.I | re.S), "min", 1),
    (re.compile(r"^MAX\s*\(\s*(.+)\)$", re.I | re.S), "max", 1),
    (re.compile(r"^MEDIAN\s*\(\s*(.+)\)$", re.I | re.S), "median", 1),
)


def _lookml_ident(name: str) -> str:
    """LookML `view:` names must match [A-Za-z_][A-Za-z0-9_]*. A warehouse
    last-segment can be quoted, spaced, or start with a digit."""
    if _IDENT.match(name):
        return name
    text = _NON_IDENT.sub("_", name.strip().strip("\"'`")).strip("_")
    if not text:
        return "view"
    if text[0].isdigit():
        return f"t_{text}"
    return text


def _table_tail(table: str) -> str:
    """Last dotted segment, without splitting inside quoted identifiers."""
    tail: list[str] = []
    quote = ""
    for char in table:
        if quote:
            tail.append(char)
            if char == quote:
                quote = ""
            continue
        if char in "\"'`":
            quote = char
            tail.append(char)
            continue
        if char == ".":
            tail = []
            continue
        tail.append(char)
    return "".join(tail) or table


def _table_sql(expr: str) -> str:
    text = expr.strip()
    if _IDENT.match(text):
        return f"${{TABLE}}.{text}"
    return text


def _parens_balanced(text: str) -> bool:
    depth = 0
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _lookml_sql(dialect: str, text: str | None) -> str | None:
    """Author SQL as a LookML `sql:` body, through the export seam.

    A LookML sql block is warehouse SQL, so the macro has to become a real
    function. Which one depends on the warehouse Looker connects to, and the
    only evidence sqldash has is the metric's own source: a project on BigQuery
    exports the view it already queries, and hardcoding one dialect handed a
    BigQuery or MySQL project a `DATE_TRUNC` those two spell differently or not
    at all. Every sql body goes through here, a derived_table's included."""
    if text is None:
        return None
    return exported_sql(dialect, text)


def _measure_from_expr(expr: str) -> tuple[str, str | None]:
    text = expr.strip()
    for pat, mtype, group in _AGG_PATTERNS:
        match = pat.fullmatch(text)
        if match:
            col = match.group(group).strip() if group else None
            if col is not None and not _parens_balanced(col):
                continue
            return mtype, col
    return "number", text


_SYMBOL_OPS = ("!=", "<>", ">=", "<=", "=", ">", "<")
_WORD_OPS = re.compile(r"\s+(IS\s+NOT\s+NULL|IS\s+NULL|NOT\s+LIKE|LIKE|IN)\b", re.I)
_STRING_LITERAL = re.compile(r"^'((?:[^']|'')*)'$")
_EXCLUDED_TERM = re.compile(
    r"^NOT\s+(?P<col>.+?)\s+(?P<op>=|LIKE)\s+(?P<value>'(?:[^']|'')*')"
    r"\s+OR\s+(?P<same>.+?)\s+IS\s+NULL$",
    re.I | re.S,
)
_FILTER_DROP = "the exported measure counts rows the metric excludes"


def _top_level(text: str) -> Iterator[tuple[int, str]]:
    """Each character outside a string literal and outside nested parentheses,
    with the parentheses that open and close at the top level."""
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        char = text[i]
        if char == "'":
            i += 1
            while i < n:
                if text[i] == "'":
                    if text[i + 1 : i + 2] != "'":
                        break
                    i += 1
                i += 1
        elif char == "(":
            if depth == 0:
                yield i, char
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                yield i, char
        elif depth == 0:
            yield i, char
        i += 1


def _unwrap(text: str) -> str:
    """A term without the parentheses that wrap the whole of it. `import lookml`
    parenthesises a dimension's sql before comparing it, so `(amount >= 150)`
    has to find the dimension defined as `amount >= 150`."""
    text = text.strip()
    while text.startswith("(") and text.endswith(")"):
        edges = list(_top_level(text))
        if edges != [(0, "("), (len(text) - 1, ")")]:
            return text
        text = text[1:-1].strip()
    return text


def _split_comparison(text: str) -> tuple[str, str, str] | None:
    """A term as (left, operator, right), splitting on the first operator that
    is not inside a string literal or a nested expression."""
    for index, _char in _top_level(text):
        for op in _SYMBOL_OPS:
            if text.startswith(op, index):
                return text[:index].strip(), op, text[index + len(op) :].strip()
        word = _WORD_OPS.match(text, index)
        if word:
            return (
                text[:index].strip(),
                " ".join(word.group(1).upper().split()),
                text[word.end() :].strip(),
            )
    return None


def _looker_literal(literal: str) -> tuple[str, str]:
    """A SQL literal as (kind, text) in Looker's filter grammar.

    The grammar has no escaping, so a value that spells one of its own operators
    reads back as that operator rather than as itself: a comma separates terms,
    `-` excludes, `^` starts a character escape, NULL and EMPTY are keywords, and
    surrounding spaces are stripped. Those have no filter expression that means
    the literal, which is a different thing from having no expression at all.
    """
    match = _STRING_LITERAL.match(literal)
    if match:
        value = match.group(1).replace("''", "'")
        if not value or value != value.strip() or value.startswith("-"):
            raise _Unsupported
        if any(char in value for char in ",^") or value.upper() in ("NULL", "EMPTY"):
            raise _Unsupported
        return "string", value
    if _NUMBER.match(literal):
        return "number", literal
    raise _Unsupported


def _looker_pattern(value: str) -> str:
    """A LIKE pattern Looker's `%` wildcard means the same thing by. `_` is a
    SQL wildcard Looker reads as a literal underscore, so a pattern using one
    would come back matching fewer rows."""
    if "%" not in value or "_" in value:
        raise _Unsupported
    return value


def _split_list(right: str) -> list[str]:
    inner = _unwrap(right)
    if inner == right:
        raise _Unsupported
    parts: list[str] = []
    start = 0
    for index, char in _top_level(inner):
        if char == ",":
            parts.append(inner[start:index])
            start = index + 1
    parts.append(inner[start:])
    return [part.strip() for part in parts if part.strip()]


def _looker_expression(op: str, right: str) -> tuple[str, str]:
    """The Looker filter expression for a comparison, with the dimension type it
    has to be read as.

    The type is what the expression means: `>100` on a string dimension (Looker's
    default) is text rather than a bound, and `-NULL` has no reading as a number
    at all, so every expression declares the type it needs and a field whose
    filters disagree about it keeps only the first.

    `!=` has no expression here: it drops NULL rows and Looker's own exclusion
    keeps them, so `-value` would be a wider number under the same name.
    """
    if op in ("IS NULL", "IS NOT NULL"):
        if right:
            raise _Unsupported
        return ("NULL" if op == "IS NULL" else "-NULL"), "string"
    if op == "IN":
        read = [_looker_literal(_unwrap(part)) for part in _split_list(right)]
        kinds = {kind for kind, _ in read}
        if len(kinds) != 1 or any("%" in value for _, value in read):
            raise _Unsupported
        return ", ".join(value for _, value in read), kinds.pop()
    kind, value = _looker_literal(right)
    if op == "LIKE":
        if kind != "string":
            raise _Unsupported
        return _looker_pattern(value), "string"
    if "%" in value:
        raise _Unsupported
    if op == "=":
        return value, kind
    if kind == "number" and op in (">", ">=", "<", "<="):
        return f"{op}{value}", "number"
    raise _Unsupported


def _filter_field(term: str, dimensions: dict[str, str]) -> str:
    """The view dimension a filter term is about.

    A LookML filter names a field, so a filter on a column the metric never
    declared as a dimension needs one: the column is already part of the metric's
    definition, and the alternative is a `filters:` line Looker cannot resolve.

    Both sides are unwrapped before they are compared: `import lookml`
    parenthesises a dimension's sql to splice it into a filter, so a dimension
    already written as `(amount + 1)` arrives as `((amount + 1)) > 100` and would
    otherwise never match the dimension it is a filter on.
    """
    for name, expr in dimensions.items():
        if expr == term or _unwrap(expr) == term:
            return name
    if not _IDENT.match(term):
        raise _Unsupported
    if term in dimensions:
        raise _Unsupported("the view's dimension of that name is defined over other SQL")
    dimensions[term] = term
    return term


def _looker_filter(text: str, dimensions: dict[str, str]) -> tuple[str, str, str]:
    """A metric's static filter as (field, Looker filter expression, dimension
    type), or `_Unsupported` when the term has no reading as one.

    The inverse of `_measure_filters`: the terms it translates on the way in are
    the terms that survive on the way out, so an imported measure's filter comes
    back as the filter it was rather than as a wider number under the same name.
    """
    term = _unwrap(text)
    excluded = _EXCLUDED_TERM.match(term)
    if excluded and _unwrap(excluded.group("col")) == _unwrap(excluded.group("same")):
        kind, value = _looker_literal(excluded.group("value"))
        if kind != "string":
            raise _Unsupported
        if excluded.group("op").upper() == "LIKE":
            value = _looker_pattern(value)
        elif "%" in value:
            raise _Unsupported
        return _filter_field(_unwrap(excluded.group("col")), dimensions), f"-{value}", "string"
    if term.upper().startswith("NOT "):
        return _filter_field(_unwrap(term[4:]), dimensions), "no", "yesno"
    split = _split_comparison(term)
    if split is None:
        return _filter_field(term, dimensions), "yes", "yesno"
    expression, dim_type = _looker_expression(split[1], split[2])
    return _filter_field(_unwrap(split[0]), dimensions), expression, dim_type


def _authored_metrics(layer: Any, store: Any) -> dict[str, Any]:
    """Original MetricDefs, keyed the same way `all_metrics` names them.

    `expand_derived` clears `derived` on the resolved definition, so export
    looks the original up to warn. A workspace keys `repo/metric`; falling
    back to the leaf assigned one repo's flags to another's same-named metric.
    """
    layers = getattr(layer, "layers", None)
    if isinstance(layers, dict):
        authored: dict[str, Any] = {}
        for repo, sub in layers.items():
            mf = sub.metrics_file()
            if mf is not None:
                for name, definition in mf.metrics.items():
                    authored[f"{repo}/{name}"] = definition
            for _, dashboard in sub.store.iter_loaded():
                for name, definition in dashboard.metrics.items():
                    authored.setdefault(f"{repo}/{name}", definition)
        return authored
    authored = {}
    for _, dashboard in store.iter_loaded():
        authored.update(dashboard.metrics)
    mf = layer.metrics_file()
    if mf is not None:
        authored.update(mf.metrics)
    return authored


def _lossy_warnings(resolved: Any, src: Any, warnings: list[str]) -> None:
    metric = resolved.definition
    if metric.cumulative:
        warnings.append(
            f"metric '{resolved.name}' is cumulative — LookML's running_total is "
            f"computed over the returned rows rather than in the warehouse, so the "
            f"exported measure is the per-bucket sum in Looker; {_CARRIED}"
        )
    if metric.window:
        warnings.append(
            f"metric '{resolved.name}' has window: {metric.window} — LookML has no "
            f"trailing window, so the exported measure is the per-bucket sum in "
            f"Looker; {_CARRIED}"
        )
    if src is not None and src.derived is not None:
        warnings.append(f"metric '{resolved.name}' is derived — {_NUMBER_DROP}")


def _looker_format(resolved: Any, warnings: list[str]) -> str | None:
    metric = resolved.definition
    format_name = _FORMAT_TO_LOOKER.get(metric.format or "")
    if metric.format == "currency":
        format_name = "usd"
        warnings.append(
            f"metric '{resolved.name}' has format: currency, which follows each "
            "dashboard's currency; LookML has no equivalent, so it was exported as usd"
        )
    elif metric.format and metric.format != "number" and format_name is None:
        warnings.append(
            f"metric '{resolved.name}' has format: {metric.format}, which has no "
            "named LookML format; it was dropped"
        )
    return format_name


def _view_for(
    views: dict[object, dict[str, Any]], resolved: Any, kind: str, warnings: list[str]
) -> dict[str, Any]:
    metric = resolved.definition
    if resolved.relation.table:
        key: object = ("table", resolved.relation.table)
        view_name = metric.relation or _table_tail(resolved.relation.table)
        sql_table = resolved.relation.table
        derived_sql = None
    else:
        key = ("sql", resolved.relation.sql)
        view_name = metric.relation or f"{resolved.name.split('/')[-1]}_base"
        sql_table = None
        derived_sql = _lookml_sql(kind, resolved.relation.sql)
    created = key not in views
    view = views.setdefault(
        key,
        {
            "name": view_name,
            "sql_table_name": sql_table,
            "derived_sql": derived_sql,
            "dimensions": {},
            "descriptions": {},
            "dimension_types": {},
            "synonyms": {},
            "time": None,
            "measures": [],
        },
    )
    if not created and metric.relation and metric.relation != view["name"]:
        warnings.append(
            f"relation '{metric.relation}' shares a table with view '{view['name']}' — "
            f"exported as '{view['name']}'"
        )
    return view


def _merge_dimensions(view: dict[str, Any], metric: Any, kind: str, warnings: list[str]) -> None:
    for dim in metric.dimensions:
        expr = _lookml_sql(kind, dim.expr) if dim.expr else dim.name
        existing = view["dimensions"].get(dim.name)
        if existing is not None and existing != expr:
            warnings.append(
                f"view '{view['name']}' defines dimension '{dim.name}' over both "
                f"{existing} and {expr} — only the first is exported"
            )
            continue
        view["dimensions"].setdefault(dim.name, expr)
        if dim.synonyms:
            kept_synonyms = view["synonyms"].setdefault(dim.name, list(dim.synonyms))
            if kept_synonyms != list(dim.synonyms):
                warnings.append(
                    f"view '{view['name']}' dimension '{dim.name}' has different "
                    "synonyms across metrics; only the first is exported"
                )
        if dim.description:
            kept = view["descriptions"].setdefault(dim.name, dim.description)
            if kept != dim.description:
                warnings.append(
                    f"view '{view['name']}' dimension '{dim.name}' has different "
                    "descriptions across metrics; only the first is exported"
                )


def _merge_time_dimension(
    view: dict[str, Any], metric: Any, kind: str, warnings: list[str]
) -> None:
    if metric.time_dimension is None:
        return
    td = (
        metric.time_dimension.name,
        _lookml_sql(kind, metric.time_dimension.expr)
        if metric.time_dimension.expr
        else metric.time_dimension.name,
    )
    if view["time"] is None:
        view["time"] = td
    elif view["time"] != td:
        warnings.append(
            f"view '{view['name']}' has metrics with different time dimensions "
            f"({view['time'][0]}, {td[0]}) — only the first is exported"
        )


def _export_filters(
    resolved: Any, kind: str, view: dict[str, Any], warnings: list[str]
) -> list[tuple[str, str]]:
    filters: list[tuple[str, str]] = []
    for snippet in resolved.definition.filters:
        try:
            field, expression, dim_type = _looker_filter(
                _lookml_sql(kind, snippet) or "", view["dimensions"]
            )
            if any(field == taken_field for taken_field, _ in filters):
                raise _Unsupported(
                    f"dimension '{field}' already carries another of the metric's "
                    "filters, and LookML takes one expression per field"
                )
            held = view["dimension_types"].setdefault(field, dim_type)
            if held != dim_type:
                raise _Unsupported(
                    f"dimension '{field}' is already read as {held} by another filter"
                )
        except _Unsupported as exc:
            reason = str(exc) or "it is not a field/value comparison Looker can express"
            warnings.append(
                f"metric '{resolved.name}' filter `{snippet}` — {reason}; {_FILTER_DROP}"
            )
            continue
        filters.append((field, expression))
    return filters


def _export_measure(
    resolved: Any,
    src: Any,
    kind: str,
    filters: list[tuple[str, str]],
    format_name: str | None,
    warnings: list[str],
) -> dict[str, Any]:
    metric = resolved.definition
    mtype, col = _measure_from_expr(_lookml_sql(kind, metric.expr or "") or "")
    if mtype == "number" and (src is None or src.derived is None):
        warnings.append(f"metric '{resolved.name}' is not a LookML aggregate — {_NUMBER_DROP}")
    tags, lost = _carried_loss(metric)
    description = metric.description
    if lost:
        note = f"(sqldash: this measure is the per-bucket sum; {lost} is not expressible in LookML)"
        description = f"{description} {note}" if description else note
    return {
        "name": resolved.name.split("/")[-1],
        "resolved": resolved.name,
        "type": mtype,
        "sql": col,
        "filters": filters,
        "label": metric.title,
        "description": description,
        "format_name": format_name,
        "synonyms": list(metric.synonyms),
        "tags": tags,
    }


def _add_metric(
    views: dict[object, dict[str, Any]], resolved: Any, src: Any, warnings: list[str]
) -> None:
    if resolved.ambiguous_with:
        warnings.append(
            f"skipped metric '{resolved.name}' — defined inline by "
            f"{', '.join(resolved.ambiguous_with)}, so it has no single definition; "
            "rename one or move it to metrics.yaml"
        )
        return
    metric = resolved.definition
    kind = dialect_kind(resolved.source)
    _lossy_warnings(resolved, src, warnings)
    if not (metric.expr or "").strip():
        warnings.append(f"skipped metric '{resolved.name}' — expr is empty")
        return
    if metric.owners:
        warnings.append(
            f"metric '{resolved.name}' has owners, which LookML has no field "
            "parameter for; they were dropped"
        )
    format_name = _looker_format(resolved, warnings)
    view = _view_for(views, resolved, kind, warnings)
    _merge_dimensions(view, metric, kind, warnings)
    _merge_time_dimension(view, metric, kind, warnings)
    filters = _export_filters(resolved, kind, view, warnings)
    view["measures"].append(_export_measure(resolved, src, kind, filters, format_name, warnings))


def _dedupe_view_names(views: dict[object, dict[str, Any]], warnings: list[str]) -> None:
    taken: set[str] = set()
    for view in views.values():
        original = view["name"]
        name = _lookml_ident(original)
        if name != original:
            warnings.append(f"view '{original}' is not a LookML identifier — exported as '{name}'")
            view["name"] = name
        if name not in taken:
            taken.add(name)
            continue
        table = view["sql_table_name"] or name
        base = _lookml_ident(str(table).replace(".", "_"))
        candidate = base
        suffix = 2
        while candidate in taken:
            candidate = f"{base}_{suffix}"
            suffix += 1
        warnings.append(
            f"view '{original}' collides with another relation — exported as '{candidate}'"
        )
        view["name"] = candidate
        taken.add(candidate)


def _dedupe_measure_names(view: dict[str, Any], warnings: list[str]) -> None:
    taken_m: set[str] = set()
    for measure in view["measures"]:
        original = measure["name"]
        name = _lookml_ident(original)
        if name != original:
            warnings.append(
                f"measure '{original}' is not a LookML identifier — exported as '{name}'"
            )
            measure["name"] = name
        if name not in taken_m:
            taken_m.add(name)
            continue
        base = _lookml_ident(str(measure["resolved"]).replace("/", "_"))
        candidate = base
        suffix = 2
        while candidate in taken_m:
            candidate = f"{base}_{suffix}"
            suffix += 1
        warnings.append(
            f"measure '{original}' collides in view '{view['name']}' — exported as '{candidate}'"
        )
        measure["name"] = candidate
        taken_m.add(candidate)


def _drop_colliding_fields(view: dict[str, Any], warnings: list[str]) -> None:
    taken_f = {m["name"] for m in view["measures"]}
    if view["time"] is not None:
        tname, texpr = view["time"]
        ident = _lookml_ident(tname)
        if ident in taken_f:
            candidate = ident
            suffix = 2
            while candidate in taken_f:
                candidate = f"{ident}_{suffix}"
                suffix += 1
            warnings.append(
                f"view '{view['name']}' dimension_group '{tname}' collides with another "
                f"field — exported as '{candidate}'"
            )
            view["time"] = (candidate, texpr)
            taken_f.add(candidate)
        else:
            if ident != tname:
                warnings.append(
                    f"view '{view['name']}' dimension_group '{tname}' is not a LookML "
                    f"identifier — exported as '{ident}'"
                )
                view["time"] = (ident, texpr)
            taken_f.add(view["time"][0])
    for dname in [n for n in view["dimensions"] if n in taken_f]:
        warnings.append(
            f"view '{view['name']}' dimension '{dname}' collides with another field — not exported"
        )
        del view["dimensions"][dname]
        for measure in view["measures"]:
            if any(field == dname for field, _ in measure["filters"]):
                measure["filters"] = [f for f in measure["filters"] if f[0] != dname]
                warnings.append(
                    f"measure '{measure['name']}' filters on '{dname}', which the view "
                    f"does not export — {_FILTER_DROP}"
                )


def _render_measure(measure: dict[str, Any]) -> list[str]:
    lines = ["", f"  measure: {measure['name']} {{", f"    type: {measure['type']}"]
    if measure["type"] != "count":
        lines.append(f"    sql: {_table_sql(measure['sql'])} ;;")
    if measure["filters"]:
        terms = ", ".join(f"{f}: {_lkml_quote(v)}" for f, v in measure["filters"])
        lines.append(f"    filters: [{terms}]")
    if measure["label"]:
        lines.append(f"    label: {_lkml_quote(measure['label'])}")
    if measure["description"]:
        lines.append(f"    description: {_lkml_quote(measure['description'])}")
    if measure["format_name"]:
        lines.append(f"    value_format_name: {measure['format_name']}")
    if measure["synonyms"]:
        lines.append(f"    synonyms: {_lkml_strings(measure['synonyms'])}")
    if measure["tags"]:
        lines.append(f"    tags: {_lkml_strings(measure['tags'])}")
    lines.append("  }")
    return lines


def _render_view(view: dict[str, Any]) -> str:
    lines = [f"view: {view['name']} {{"]
    if view["sql_table_name"]:
        lines.append(f"  sql_table_name: {view['sql_table_name']} ;;")
    else:
        sql = (view["derived_sql"] or "").rstrip()
        lines.append("  derived_table: {")
        lines.append(f"    sql: {sql} ;;")
        lines.append("  }")
    for dname, dexpr in view["dimensions"].items():
        lines.append("")
        lines.append(f"  dimension: {dname} {{")
        if view["dimension_types"].get(dname) in ("number", "yesno"):
            lines.append(f"    type: {view['dimension_types'][dname]}")
        lines.append(f"    sql: {_table_sql(dexpr)} ;;")
        if view["descriptions"].get(dname):
            lines.append(f"    description: {_lkml_quote(view['descriptions'][dname])}")
        if view["synonyms"].get(dname):
            lines.append(f"    synonyms: {_lkml_strings(view['synonyms'][dname])}")
        lines.append("  }")
    if view["time"]:
        tname, texpr = view["time"]
        lines.append("")
        lines.append(f"  dimension_group: {tname} {{")
        lines.append("    type: time")
        lines.append("    timeframes: [raw, date, week, month, quarter, year]")
        lines.append(f"    sql: {_table_sql(texpr)} ;;")
        lines.append("  }")
    for measure in view["measures"]:
        lines.extend(_render_measure(measure))
    lines.append("}")
    return "\n".join(lines)


def export_lookml(layer: Any, store: Any) -> tuple[str, list[str]]:
    """Render the semantic layer as LookML views. Lossy: derived / cumulative /
    window become a plain measure; joins/explores are not reconstructed."""
    warnings: list[str] = []
    authored = _authored_metrics(layer, store)

    views: dict[object, dict[str, Any]] = {}
    for resolved in layer.all_metrics():
        _add_metric(views, resolved, authored.get(resolved.name), warnings)

    if not views:
        detail = "".join(f"\n  - {w}" for w in warnings)
        raise SemanticError(f"no exportable metrics{detail}")

    _dedupe_view_names(views, warnings)
    for view in views.values():
        _dedupe_measure_names(view, warnings)
        _drop_colliding_fields(view, warnings)

    return "\n\n".join(_render_view(view) for view in views.values()) + "\n", warnings
