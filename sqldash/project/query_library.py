"""Project queries are copied into dashboards; library files are never tile dependencies."""

import hashlib
import io
import re
import threading
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from ruamel.yaml.scalarstring import LiteralScalarString

from sqldash.models.dashboard import FilterDef, authored_filter
from sqldash.project.store import (
    ConflictError,
    InvalidDashboardError,
    NotFoundError,
    atomic_write,
    compute_etag,
    etag_matches,
    etag_mismatch,
    if_match_wildcard,
)

_LOCK = threading.RLock()
_ID = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")
_MAX_BYTES = 1_000_000


class LibraryQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    title: str = Field(min_length=1, max_length=160)
    sql: str = Field(min_length=1, max_length=500_000)
    source: str = Field(min_length=1, max_length=4096)
    parameters: list[FilterDef] = Field(default_factory=list)


class QueryLibrary:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def path(self, query_id: str) -> Path:
        if not _ID.fullmatch(query_id):
            raise InvalidDashboardError("invalid query id")
        path = self.root / "queries" / f"{query_id}.yaml"
        if not path.resolve().is_relative_to(self.root):
            raise InvalidDashboardError("query path leaves the project")
        return path

    def existing(self, query_id: str) -> Path:
        path = self.path(query_id)
        if not path.is_file():
            raise NotFoundError(f"no library query '{query_id}'")
        return path

    def etag(self, query_id: str) -> str:
        """The entry's etag without parsing it, so a malformed entry can still
        be overwritten or deleted. Matches load() for every readable file."""
        path = self.existing(query_id)
        if path.stat().st_size > _MAX_BYTES:
            # Oversized files are never loaded, so hash them in chunks rather
            # than reading the whole file just to name it in the listing.
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(65536), b""):
                    digest.update(chunk)
            return digest.hexdigest()[:16]
        try:
            return compute_etag(path.read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            return hashlib.sha256(path.read_bytes()).hexdigest()[:16]

    def load(self, query_id: str) -> tuple[LibraryQuery, str]:
        path = self.existing(query_id)
        if path.stat().st_size > _MAX_BYTES:
            raise InvalidDashboardError("query file is too large")
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise InvalidDashboardError("query file is not valid UTF-8") from exc
        try:
            query = LibraryQuery.model_validate(YAML(typ="safe").load(text))
        except (ValidationError, YAMLError, ValueError) as exc:
            raise InvalidDashboardError("invalid library query file") from exc
        if query.id != query_id:
            raise InvalidDashboardError("query id must match its filename")
        return query, compute_etag(text)

    def list(self) -> tuple[list[dict], list[dict]]:
        entries, errors = [], []
        directory = self.path("probe").parent
        for path in sorted(directory.glob("*.yaml")):
            try:
                query, etag = self.load(path.stem)
                entries.append({**query.model_dump(mode="json"), "etag": etag})
            except (InvalidDashboardError, OSError, NotFoundError) as exc:
                reason = str(exc) if isinstance(exc, InvalidDashboardError) else "unreadable"
                try:
                    etag = self.etag(path.stem)
                except (InvalidDashboardError, OSError, NotFoundError):
                    etag = ""
                errors.append(
                    {
                        "id": path.stem,
                        "message": "Could not read query file",
                        "reason": reason,
                        "etag": etag,
                    }
                )
        return entries, errors

    def save(self, query: LibraryQuery, if_match: str) -> str:
        with _LOCK:
            path = self.path(query.id)
            exists = path.exists()
            if exists:
                current = self.etag(query.id)
                if not etag_matches(current, if_match):
                    raise ConflictError(
                        "library query changed; reload before saving "
                        + etag_mismatch(current, if_match)
                    )
            elif not if_match_wildcard(if_match):
                raise ConflictError("library query was deleted; save a new copy")
            yaml = YAML()
            yaml.indent(mapping=2, sequence=4, offset=2)
            doc = _round_trip(yaml, path) if exists else {}
            data = query.model_dump(mode="json")
            data["parameters"] = [authored_filter(p) for p in data["parameters"]]
            if not data["parameters"]:
                del data["parameters"]
            for key in [k for k in doc if k not in data]:
                del doc[key]
            doc.update(data)
            doc["sql"] = LiteralScalarString(query.sql.strip() + "\n")
            stream = io.StringIO()
            yaml.dump(doc, stream)
            text = stream.getvalue()
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, text)
            return compute_etag(text)

    def delete(self, query_id: str, if_match: str) -> None:
        with _LOCK:
            current = self.etag(query_id)
            if not etag_matches(current, if_match):
                raise ConflictError(
                    "library query changed; reload before deleting "
                    + etag_mismatch(current, if_match)
                )
            self.path(query_id).unlink()


def _round_trip(yaml: YAML, path: Path) -> dict:
    """The existing document, to keep its comments; a malformed one is replaced.

    An entry over the size cap is replaced unread: reading it would pull the
    whole file in, and keeping its comments would keep the file oversized."""
    if path.stat().st_size > _MAX_BYTES:
        return {}
    try:
        doc = yaml.load(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, YAMLError):
        return {}
    return doc if isinstance(doc, dict) else {}
