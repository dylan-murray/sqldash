"""Drill-down contract: the `drill:` block that makes a tile's marks and rows links."""

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class CurrentFilter(BaseModel):
    """A mapping value that carries this dashboard's current value of one of its
    own filters, instead of a column of the clicked row."""

    model_config = ConfigDict(extra="forbid")

    filter: str


DrillValue = str | CurrentFilter


class DrillSpec(BaseModel):
    """Where a click on a tile's bar, point, slice or table row goes.

    `dashboard` names the destination the way `/d/<name>` does, resolved inside the
    source dashboard's own repo; `repo/name` reaches another repo of a workspace, and
    leaving it out drills into this same dashboard. `filters` maps each destination
    filter to the clicked row's column (a bare string) or to `{filter: name}`, this
    dashboard's current value. `column` picks the table column that holds the link,
    defaulting to the first mapped column. A bare string is shorthand for
    `{dashboard: name}`."""

    model_config = ConfigDict(extra="forbid")

    dashboard: str | None = None
    filters: dict[str, DrillValue] = {}
    column: str | None = None
    new_tab: bool = False

    @model_validator(mode="before")
    @classmethod
    def coerce_shorthand(cls, data):
        if isinstance(data, str):
            return {"dashboard": data}
        return data

    @field_validator("dashboard")
    @classmethod
    def check_dashboard(cls, v):
        if v is None:
            return v
        name = v.strip()
        parts = name.split("/")
        if not name or len(parts) > 2 or any(p in ("", ".", "..") for p in parts):
            raise ValueError(
                f"drill dashboard '{v}' must be a dashboard name like 'customer_detail', "
                "or 'repo/customer_detail' in a workspace"
            )
        return name

    @field_validator("filters", mode="before")
    @classmethod
    def check_filters(cls, v):
        if not isinstance(v, dict):
            return v
        for target, value in v.items():
            if isinstance(value, str) and not value.strip():
                raise ValueError(f"drill filter '{target}' maps to an empty column name")
            if value is None:
                raise ValueError(f"drill filter '{target}' needs a column name or {{filter: name}}")
        return v

    def mapped_columns(self) -> list[str]:
        """Clicked-row columns the mappings read, in file order."""
        return [value for value in self.filters.values() if isinstance(value, str)]

    def link_column(self) -> str | None:
        """The table column that carries the link: `column`, else the first mapped one."""
        if self.column:
            return self.column
        columns = self.mapped_columns()
        return columns[0] if columns else None
