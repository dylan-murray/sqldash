"""Everything about turning DBAPI results into wire-typed columns and JSON-safe
values, generically across dialects: name-based mapping from cursor descriptions,
value-based inference for drivers with unhelpful descriptions, and serialization."""

import json
import math
import re
from datetime import date, datetime
from datetime import time as time_of_day
from decimal import Decimal
from typing import Any

from sqldash.models.results import Execution, ResultColumn, WireType

JS_MAX_SAFE_INTEGER = 2**53 - 1

NATIVE_NAMES: dict[str, WireType] = {
    **dict.fromkeys(
        [
            "TINYINT",
            "SMALLINT",
            "INTEGER",
            "INT",
            "BIGINT",
            "HUGEINT",
            "UTINYINT",
            "USMALLINT",
            "UINTEGER",
            "UBIGINT",
            "FIXED",
            "SERIAL",
        ],
        "integer",
    ),
    **dict.fromkeys(["FLOAT", "REAL", "DOUBLE", "DOUBLE PRECISION"], "float"),
    **dict.fromkeys(["NUMERIC", "NUMBER"], "decimal"),
    **dict.fromkeys(["BOOLEAN", "BOOL"], "boolean"),
    "DATE": "date",
    **dict.fromkeys(["BLOB", "BYTEA", "BINARY", "VARBINARY"], "binary"),
}

NATIVE_PREFIXES: tuple[tuple[str, WireType], ...] = (
    ("DECIMAL", "decimal"),
    ("TIMESTAMP", "timestamp"),
    ("DATETIME", "timestamp"),
    ("TIME", "time"),
    ("STRUCT", "json"),
    ("MAP", "json"),
    ("LIST", "json"),
    ("JSON", "json"),
    ("ARRAY", "json"),
    ("OBJECT", "json"),
    ("VARIANT", "json"),
    ("GEOGRAPHY", "json"),
    ("GEOMETRY", "json"),
)

PYTHON_TYPES: tuple[tuple[type | tuple[type, ...], WireType], ...] = (
    (bool, "boolean"),
    (int, "integer"),
    (float, "float"),
    (Decimal, "decimal"),
    (datetime, "timestamp"),
    (date, "date"),
    (time_of_day, "time"),
    ((bytes, bytearray), "binary"),
    ((list, dict), "json"),
)


def wire_type_from_native(native: str) -> WireType:
    name = native.upper()
    if name in NATIVE_NAMES:
        return NATIVE_NAMES[name]
    if name.endswith("[]"):
        return "json"
    for prefix, wire in NATIVE_PREFIXES:
        if name.startswith(prefix):
            return wire
    return "string"


def wire_type_from_value(value: Any) -> WireType:
    for python_type, wire in PYTHON_TYPES:
        if isinstance(value, python_type):
            return wire
    return "string"


def infer_columns(description: list, raw_rows: list[tuple]) -> list[ResultColumn]:
    columns = [
        ResultColumn(name=str(d[0]), type=wire_type_from_native(str(d[1]))) for d in description
    ]
    for idx, col in enumerate(columns):
        if col.type != "string":
            continue
        sample = next((row[idx] for row in raw_rows if row[idx] is not None), None)
        if sample is not None and not isinstance(sample, str):
            col.type = wire_type_from_value(sample)
    return columns


NON_FINITE_TEXT = {math.inf: "Infinity", -math.inf: "-Infinity"}

UNDEFINED_OR_STRING = re.compile(r'"(?:[^"\\]|\\.)*"|\bundefined\b')


def to_jsonable(value: Any) -> Any:
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return "NaN" if math.isnan(value) else NON_FINITE_TEXT[value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time_of_day)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex().upper()
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    return str(value)


def parse_json_text(value: Any) -> Any:
    """A JSON column's cell as the structure it holds. Snowflake hands VARIANT,
    ARRAY and OBJECT over as indented JSON text, spelling a SQL NULL inside an
    array `undefined`, which is not JSON; that becomes null. Only an object,
    array or string is parsed: a driver that already decoded the JSON (psycopg)
    can hand over a string scalar like `123`, which must not become a number.
    Text that still is not JSON (a GEOGRAPHY in WKT output) stays text."""
    if not isinstance(value, str) or not value.lstrip().startswith(("{", "[", '"')):
        return value
    for text in (value, UNDEFINED_OR_STRING.sub(_undefined_as_null, value)):
        try:
            return json.loads(text)
        except ValueError:
            continue
    return value


def _undefined_as_null(match: re.Match) -> str:
    return "null" if match[0] == "undefined" else match[0]


def to_cell(value: Any, wire_type: WireType) -> Any:
    return to_jsonable(parse_json_text(value) if wire_type == "json" else value)


def flat_text(value: Any) -> Any:
    """A cell for one line of CSV or a CLI table: JSON stays JSON, on one line,
    spaced the way the browser's JSON.stringify writes it."""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return value


def js_exact(value: Any) -> Any:
    """A JSON value a browser parses without rounding: an integer JavaScript
    would read as a lossy double travels as its decimal text instead."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return str(value) if abs(value) > JS_MAX_SAFE_INTEGER else value
    if isinstance(value, list):
        return [js_exact(v) for v in value]
    if isinstance(value, dict):
        return {k: js_exact(v) for k, v in value.items()}
    return value


def for_browser(execution: Execution) -> Execution:
    """The execution as the web UI receives it. The cached result is left as the
    driver returned it, so CSV export, the CLI and MCP keep exact JSON integers."""
    if execution.result is None:
        return execution
    rows = [[js_exact(v) for v in row] for row in execution.result.rows]
    result = execution.result.model_copy(update={"rows": rows})
    return execution.model_copy(update={"result": result})
