import warnings
from pathlib import Path

from sqldash.connectors.base import CancelToken, Connector, ConnectorError, TableInfo

__all__ = [
    "CancelToken",
    "Connector",
    "ConnectorError",
    "TableInfo",
    "create_connector",
    "get_connector_class",
]


def get_connector_class(source_type: str | None = None) -> type:
    """Deprecated: there is one connector class — import EngineConnector directly."""
    warnings.warn(
        "get_connector_class is deprecated; use sqldash.connectors.engine.EngineConnector",
        DeprecationWarning,
        stacklevel=2,
    )
    from sqldash.connectors.engine import EngineConnector  # noqa: PLC0415 — deprecation shim

    return EngineConnector


def create_connector(source, base_dir: Path | None) -> Connector:
    """Deprecated: construct sqldash.connectors.engine.EngineConnector directly."""
    warnings.warn(
        "create_connector is deprecated; use sqldash.connectors.engine.EngineConnector",
        DeprecationWarning,
        stacklevel=2,
    )
    from sqldash.connectors.engine import EngineConnector  # noqa: PLC0415 — deprecation shim

    return EngineConnector(source, base_dir)
