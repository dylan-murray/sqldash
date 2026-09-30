"""HTTP-only request shaping: strict request bodies and the JSON payload the browser renders."""

import difflib
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, model_validator

from sqldash.models.dashboard import Dashboard
from sqldash.models.source import redact_source
from sqldash.params import filter_ui_default
from sqldash.project.drill import plan_drills
from sqldash.project.store import Store

if TYPE_CHECKING:
    from sqldash.semantics import SemanticLayer


class StrictBody(BaseModel):
    """A misspelled field is refused by name, with the fields that exist.

    Every HTTP request body subclasses this: dropping an unknown key made
    PATCH /meta 200 on a no-op and POST /api/repos lose the chosen name.
    """

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def known_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        fields = list(cls.model_fields)
        unknown = [key for key in data if key not in fields]
        if unknown:
            key = str(unknown[0])
            close = difflib.get_close_matches(key, fields, n=1)
            hint = f" (did you mean '{close[0]}'?)" if close else ""
            raise ValueError(f"unknown field '{key}'{hint}; valid fields: {', '.join(fields)}")
        return data


def _jsonable(value: Any) -> Any:
    """Unquoted YAML dates parse as datetime.date; json.dumps cannot encode them."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


def client_payload(
    name: str,
    dashboard: Dashboard,
    etag: str,
    layer: "SemanticLayer | None" = None,
    store: Store | None = None,
) -> dict:
    """Dashboard JSON for the browser, with sources redacted and metric formats attached.
    With the store, each drill tile also gets the resolved plan its links are built from."""
    data = dashboard.model_dump(mode="json", exclude={"source", "sources"})
    data["source"] = redact_source(dashboard.source)
    data["sources"] = {k: redact_source(v) for k, v in dashboard.sources.items()}
    for f, fdef in zip(data["filters"], dashboard.filters, strict=True):
        f["resolved_default"] = _jsonable(filter_ui_default(fdef))
    formats: dict[str, str] = {}
    has_time: dict[str, bool] = {}
    if layer is not None:
        try:
            for metric_name, resolved in layer.metrics_for_dashboard(name).items():
                if resolved.definition.format:
                    formats[metric_name] = resolved.definition.format
                has_time[metric_name] = resolved.definition.time_dimension is not None
        except Exception:
            pass
    data["metric_formats"] = formats
    data["metric_has_time"] = has_time
    # `today` is the server's date, the same one `resolved_default` was resolved
    # against. The filter bar resolves presets against it instead of the client
    # clock, whose UTC date can be a different day (#673).
    payload = {"name": name, "etag": etag, "today": date.today().isoformat(), "dashboard": data}
    if store is not None:
        payload["drills"] = plan_drills(store, name, dashboard)
    return payload
