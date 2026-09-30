"""Semantic layer contract: metrics.yaml and inline dashboard `metrics:` blocks."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from sqldash.models.chart import validate_format, validate_identifier
from sqldash.models.source import SourceConfig

Grain = Literal["hour", "day", "week", "month", "quarter", "year"]

_RESERVED_WORDS = """
all analyse analyze and any anti array as asc asof asymmetric at authorization between binary
both by case cast check collate collation column constraint create cross current
current_catalog current_date current_role current_schema current_time current_timestamp
current_user default deferrable desc describe distinct do else end except exists false fetch
following for foreign from full glob grant group having ilike in initially inner intersect into
is isnull join lateral leading left like limit localtime localtimestamp natural not notnull
null of offset on only or order outer overlaps pivot placing positional primary qualify
references returning right rows select semi session_user set show similar some summarize
symmetric system_user table tablesample then to trailing true union unique unpivot user using
variadic when where window with
"""
SQL_RESERVED = frozenset(_RESERVED_WORDS.split())
WINDOW_UNITS = ("hour", "day", "week", "month")
_WINDOW_ALIASES = {
    "h": "hour",
    "hour": "hour",
    "hours": "hour",
    "d": "day",
    "day": "day",
    "days": "day",
    "w": "week",
    "week": "week",
    "weeks": "week",
    "mo": "month",
    "month": "month",
    "months": "month",
}
_WINDOW_RE = re.compile(r"^(\d+)\s*([A-Za-z]+)$")


def parse_window(value: str) -> tuple[int, str]:
    """'<n> <unit>' → (count, canonical unit). Count is inlined into SQL as an int."""
    match = _WINDOW_RE.fullmatch(value.strip())
    if match is None:
        raise ValueError(f"window {value!r} must be '<n> <unit>' — e.g. '28 days', '4 weeks'")
    count = int(match[1])
    if count < 1:
        raise ValueError("window count must be at least 1")
    unit = _WINDOW_ALIASES.get(match[2].lower())
    if unit is None:
        raise ValueError(f"window unit '{match[2]}' must be one of hours, days, weeks, months")
    return count, unit


def validate_name(value: str, label: str, *, column: bool = False) -> str:
    """A name the compiler will emit into SQL: an identifier that is not a reserved word.

    The compiler quotes the aliases it invents, but a dimension's default expr is the
    bare name in author position, and a word like `user` or `current_timestamp` is not
    a parse error there: Postgres silently resolves it to the session function.
    """
    validate_identifier(value, label)
    if value.lower() in SQL_RESERVED:
        hint = " and point expr at the column" if column else ""
        raise ValueError(f"{label} '{value}' is a SQL reserved word; rename it{hint}")
    return value


class RelationDef(BaseModel):
    """A named base that metrics build on: exactly one of `table` or `sql`."""

    model_config = ConfigDict(extra="forbid")

    table: str | None = None
    sql: str | None = None
    description: str | None = None

    @model_validator(mode="after")
    def exactly_one_base(self) -> "RelationDef":
        if bool(self.table) == bool(self.sql):
            raise ValueError("relation requires exactly one of 'table' or 'sql'")
        return self


class DimensionDef(BaseModel):
    """A groupable/filterable attribute of a metric; `expr` defaults to the column
    named `name`. Only declared dimensions are queryable — the compiler rejects others."""

    model_config = ConfigDict(extra="forbid")

    name: str
    expr: str | None = None
    description: str | None = None
    synonyms: list[str] = []

    @field_validator("name")
    @classmethod
    def name_is_identifier(cls, v: str) -> str:
        return validate_name(v, "dimension name", column=True)

    @property
    def sql_expr(self) -> str:
        return self.expr or self.name


class TimeDimensionDef(BaseModel):
    """The time axis of a metric; `grain` is the default truncation when a query names none.

    `timezone: session` cuts buckets in the session time zone. Snowflake's DATE_TRUNC
    keeps each TIMESTAMP_TZ row's own offset, so one month became a bucket per offset;
    Postgres and DuckDB already truncate a timestamptz in the session zone."""

    model_config = ConfigDict(extra="forbid")

    name: str
    expr: str | None = None
    grain: Grain = "day"
    timezone: Literal["session"] | None = None
    description: str | None = None

    @field_validator("name")
    @classmethod
    def name_is_identifier(cls, v: str) -> str:
        return validate_name(v, "time dimension name", column=True)

    @property
    def sql_expr(self) -> str:
        return self.expr or self.name


class MetricDef(BaseModel):
    """A governed metric: either `expr` (an aggregate over exactly one of
    `relation`/`table`/`sql`) or `derived` (a brace-ref expression over sibling
    metrics on the same relation, e.g. `"{a} / NULLIF({b}, 0)"`).
    `cumulative: true` makes plain metrics a running total along the required
    `time_dimension`. `window: 28 days` is a trailing aggregate per time bucket
    (needs a date spine). `filters` are static SQL snippets always ANDed in."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    description: str | None = None
    relation: str | None = None
    table: str | None = None
    sql: str | None = None
    expr: str | None = None
    derived: str | None = None
    cumulative: bool = False
    window: str | None = None
    format: str | None = None

    @field_validator("format")
    @classmethod
    def check_format(cls, v):
        if v is not None:
            validate_format(v)
        return v

    synonyms: list[str] = []
    owners: list[str] = []
    time_dimension: TimeDimensionDef | None = None
    dimensions: list[DimensionDef] = []
    filters: list[str] = []

    @field_validator("window", mode="before")
    @classmethod
    def window_is_count_unit(cls, v):
        if v is None:
            return v
        if not isinstance(v, str):
            raise ValueError("window must be a string like '28 days', not a bare number")
        parse_window(v)
        return v

    @model_validator(mode="after")
    def exactly_one_base(self) -> "MetricDef":
        if (self.expr is None) == (self.derived is None):
            raise ValueError("metric requires exactly one of 'expr' or 'derived'")
        if self.cumulative and self.time_dimension is None:
            raise ValueError(
                "cumulative metrics need a time_dimension — the running total accumulates along it"
            )
        if self.cumulative and self.derived is not None:
            raise ValueError("cumulative applies to plain metrics, not derived ones")
        if self.window and self.cumulative:
            raise ValueError(
                "window and cumulative cannot be combined — "
                "a trailing window is not a running total"
            )
        if self.window and self.time_dimension is None:
            raise ValueError(
                "window metrics need a time_dimension — the trailing aggregate walks it"
            )
        if self.window and self.derived is not None:
            raise ValueError("window applies to plain metrics, not derived ones")
        set_count = sum(1 for v in (self.relation, self.table, self.sql) if v)
        if self.derived is not None:
            if set_count != 0:
                raise ValueError(
                    "a derived metric inherits its relation from the metrics it references "
                    "— drop 'relation'/'table'/'sql'"
                )
        elif set_count != 1:
            raise ValueError("metric requires exactly one of 'relation', 'table', or 'sql'")
        if self.relation:
            validate_identifier(self.relation, "relation reference")
        names = [d.name for d in self.dimensions]
        if self.time_dimension:
            names.append(self.time_dimension.name)
        if len(names) != len(set(names)):
            raise ValueError("dimension names must be unique within a metric")
        return self

    def dimension(self, name: str) -> DimensionDef | None:
        """The declared dimension called `name`, if any."""
        return next((d for d in self.dimensions if d.name == name), None)

    @property
    def window_spec(self) -> tuple[int, str] | None:
        if not self.window:
            return None
        return parse_window(self.window)


class MetricsFile(BaseModel):
    """The project-level metrics.yaml: one source for the whole file, plus relations
    and metrics keyed by identifier. Dashboard-inline metrics override these by name."""

    model_config = ConfigDict(extra="forbid")

    source: SourceConfig
    relations: dict[str, RelationDef] = {}
    metrics: dict[str, MetricDef] = {}

    @model_validator(mode="after")
    def check_references(self) -> "MetricsFile":
        for name in self.relations:
            validate_identifier(name, "relation name")
        for name in self.metrics:
            validate_name(name, "metric name")
        for name, metric in self.metrics.items():
            if metric.relation and metric.relation not in self.relations:
                declared = ", ".join(sorted(self.relations)) or "(none)"
                raise ValueError(
                    f"metric '{name}' references unknown relation '{metric.relation}' "
                    f"— relations declared in this file: {declared}. Declare it under "
                    f"'relations:', or write 'table:' on the metric naming the table it "
                    f"reads (the relation's name is not always the table's)"
                )
        return self


class MetricRef(BaseModel):
    """A tile's `metric:` reference — names only, never SQL: which metric, which
    declared dimensions to group by, at what grain, with an optional period comparison.
    A bare string in YAML coerces to just the name."""

    model_config = ConfigDict(extra="forbid")

    name: str
    dimensions: list[str] = []
    grain: Grain | None = None
    compare: Literal["previous_period", "yoy"] | None = None

    @field_validator("name")
    @classmethod
    def name_is_identifier(cls, v: str) -> str:
        return validate_identifier(v, "metric name")
