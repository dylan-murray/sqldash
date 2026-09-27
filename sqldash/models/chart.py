"""Chart contract: the `chart:` block on tiles and the shared format vocabulary."""

import math
import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

ChartType = Literal["line", "bar", "area", "scatter", "pie", "big_number", "table"]
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

    @model_validator(mode="after")
    def check_one_position(self) -> "ReferenceLine":
        given = [k for k in ("y", "x", "metric") if getattr(self, k) is not None]
        if len(given) != 1:
            raise ValueError(
                "a reference takes exactly one of 'y' (a value), 'x' (a date or category) "
                f"or 'metric' (a scalar metric), got {', '.join(given) or 'none'}"
            )
        if self.y is not None:
            values = self.y if isinstance(self.y, list) else [self.y]
            if not all(math.isfinite(v) for v in values):
                raise ValueError("a reference 'y' must be a finite number")
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
