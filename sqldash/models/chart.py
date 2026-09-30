"""Chart contract: the `chart:` block on tiles and the shared format vocabulary."""

import math
import re
from datetime import date
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    field_validator,
    model_validator,
)

ChartType = Literal[
    "line", "bar", "area", "scatter", "pie", "histogram", "heatmap", "big_number", "table"
]
REFERENCE_CHART_TYPES = ("line", "bar", "area", "scatter")
ReferenceColor = Literal[
    "ink",
    "muted",
    "accent",
    "good",
    "bad",
    "series-1",
    "series-2",
    "series-3",
    "series-4",
    "series-5",
    "series-6",
    "series-7",
    "series-8",
]
NAMED_FORMATS = ("number", "currency", "percent", "compact", "date")
CURRENCY_CODE = re.compile(r"^[A-Z]{3}$")
HEATMAP_FIELDS = ("aggregate", "palette", "midpoint", "x_order", "y_order")
MAX_HISTOGRAM_BINS = 200
HISTOGRAM_FIELDS = ("bins", "bin_width", "bin_start", "measure")
EXACT_IN_BROWSER = 2**53 - 1
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_identifier(value: str, label: str) -> str:
    """Require a SQL-safe identifier; the compiler relies on names never being fragments."""
    if not IDENTIFIER.match(value):
        raise ValueError(f"{label} '{value}' must match [A-Za-z_][A-Za-z0-9_]*")
    return value


def validate_format(value: str, label: str = "format") -> str:
    """Accept a named format role or a bare ISO 4217 currency code like EUR."""
    if value in NAMED_FORMATS or CURRENCY_CODE.match(value):
        return value
    raise ValueError(
        f"{label} '{value}' is not valid — use one of {', '.join(NAMED_FORMATS)} "
        "or an ISO 4217 currency code like EUR, JPY, GBP"
    )


def _date_to_text(value):
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, list):
        return [_date_to_text(v) for v in value]
    return value


class ReferenceLine(BaseModel):
    """A target, threshold or marker drawn over an xy chart. `y` is a value on the
    value axis (a pair is a band), `x` a date or category on the x axis (a pair is
    a band), and `metric` a scalar metric evaluated with the dashboard's filters."""

    model_config = ConfigDict(extra="forbid")

    y: float | list[float] | None = None
    x: str | float | list[str | float] | None = None
    metric: str | None = None
    label: str | None = None
    color: ReferenceColor | None = None
    style: Literal["dashed", "solid", "dotted"] | None = None
    format: str | None = None

    @field_validator("x", mode="before")
    @classmethod
    def dates_as_text(cls, v):
        return _date_to_text(v)

    @field_validator("format")
    @classmethod
    def check_format(cls, v):
        if v is not None:
            validate_format(v)
        return v

    @field_validator("metric")
    @classmethod
    def metric_is_identifier(cls, v):
        return v if v is None else validate_identifier(v, "reference metric")

    @model_validator(mode="after")
    def check_one_position(self) -> "ReferenceLine":
        given = [k for k in ("y", "x", "metric") if getattr(self, k) is not None]
        if len(given) != 1:
            raise ValueError(
                "a reference takes exactly one of 'y' (a value), 'x' (a date or category) "
                f"or 'metric' (a scalar metric), got {', '.join(given) or 'none'}"
            )
        for key in ("y", "x"):
            value = getattr(self, key)
            values = value if isinstance(value, list) else [value]
            if any(isinstance(v, float) and not math.isfinite(v) for v in values):
                raise ValueError(f"a reference '{key}' must be a finite number")
        for key in ("y", "x"):
            value = getattr(self, key)
            if isinstance(value, list) and len(value) != 2:
                raise ValueError(
                    f"a reference band takes two values, [from, to], got {len(value)} in '{key}'"
                )
        return self


class ChartSpec(BaseModel):
    """How a tile renders its result. A bare string (`chart: bar`) coerces to a spec
    with only `type`; unset encodings are inferred from the result columns client-side.
    `format` maps columns to format names, or one string applied to the value/y columns."""

    model_config = ConfigDict(extra="forbid")

    type: ChartType = "table"
    x: str | None = None
    y: list[str] | None = None
    group_by: str | None = None
    stacked: bool = False
    orientation: Literal["horizontal"] | None = None
    color_by: Literal["value"] | None = None
    value: str | None = None
    label: str | None = None
    format: dict[str, str] | str = {}
    legend: bool = True
    bins: int | None = Field(default=None, ge=1, le=MAX_HISTOGRAM_BINS, strict=True)
    bin_width: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    bin_start: float | None = Field(default=None, allow_inf_nan=False)
    measure: Literal["count", "percent"] | None = None
    aggregate: Literal["sum", "avg", "count", "min", "max"] | None = None
    palette: Literal["sequential", "diverging"] | None = None
    midpoint: float | None = Field(default=None, allow_inf_nan=False)
    x_order: list[str | int | FiniteFloat | bool] | None = None
    y_order: list[str | int | FiniteFloat | bool] | None = None
    references: list[ReferenceLine] = []

    @field_validator("y", mode="before")
    @classmethod
    def coerce_y_to_list(cls, v):
        if isinstance(v, str):
            return [v]
        return v

    @field_validator("format")
    @classmethod
    def check_formats(cls, v):
        if isinstance(v, str):
            validate_format(v)
        else:
            for column, fmt in v.items():
                validate_format(fmt, f"format for '{column}'")
        return v

    @field_validator("x_order", "y_order")
    @classmethod
    def check_order_integers(cls, order, info):
        for v in order or []:
            if isinstance(v, int) and not isinstance(v, bool) and abs(v) > EXACT_IN_BROWSER:
                raise ValueError(
                    f"{info.field_name} entry {v} is too big for the browser to match exactly; "
                    f"quote it as a string: '{v}'"
                )
        return order

    @model_validator(mode="after")
    def check_midpoint(self):
        if self.midpoint is not None and self.palette != "diverging":
            raise ValueError(
                "midpoint is where a diverging palette turns, so it needs palette: diverging"
            )
        return self

    @model_validator(mode="after")
    def check_binning(self):
        if self.bins is not None and self.bin_width is not None:
            raise ValueError(
                "set bins (how many) or bin_width (how wide), not both; "
                "they are two ways to say the same thing"
            )
        if self.bin_start is not None and self.bin_width is None:
            raise ValueError("bin_start aligns bin_width edges, so it needs a bin_width")
        return self
