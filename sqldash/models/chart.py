"""Chart contract: the `chart:` block on tiles and the shared format vocabulary."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

ChartType = Literal["line", "bar", "area", "scatter", "pie", "big_number", "table"]
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
