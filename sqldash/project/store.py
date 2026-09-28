"""Dashboard persistence: comment-preserving YAML writes with etag concurrency."""

import errno
import hashlib
import io
import os
import re
import secrets
import stat
import threading
from abc import ABC, abstractmethod
from collections.abc import Iterator
from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any, NamedTuple

from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.composer import ComposerError
from ruamel.yaml.error import CommentMark
from ruamel.yaml.scalarstring import FoldedScalarString, LiteralScalarString
from ruamel.yaml.tokens import CommentToken

from sqldash.models.chart import ChartSpec, ReferenceLine
from sqldash.models.dashboard import (
    TILE_LEVEL_DIMENSIONS_HINT,
    Dashboard,
    Tile,
    authored_filter,
    dashboard_stem,
    derive_tile_ids,
    slugify,
)
from sqldash.models.drill import DrillSpec
from sqldash.models.semantics import MetricRef
from sqldash.models.source import (
    DEFAULT_MARK,
    is_named_source_map,
    source_as_project_yaml,
)

_tls = threading.local()


def _make_yaml() -> YAML:
    inst = YAML(typ="rt")
    inst.preserve_quotes = True
    inst.width = 4096
    inst.indent(mapping=2, sequence=4, offset=2)
    return inst


class _ThreadYAML:
    def _inst(self) -> YAML:
        inst = getattr(_tls, "yaml", None)
        if inst is None:
            inst = _make_yaml()
            _tls.yaml = inst
        return inst

    def load(self, stream: Any) -> Any:
        return self._inst().load(stream)

    def dump(self, data: Any, stream: Any) -> None:
        self._inst().dump(data, stream)


yaml = _ThreadYAML()

SKIP_NAMES = {"profiles", "config", "metrics", "agents"}
_DASHBOARD_KEYS = frozenset(Dashboard.model_fields)
_DASHBOARD_BODY = frozenset({"source", "tiles", "queries"})


def _looks_like_dashboard(path: Path) -> bool:
    """True when a sibling YAML should be treated as a dashboard.

    Parsed top-level keys: a file whose keys are all dashboard fields
    (including `title:` alone — the scaffold's first line) is a dashboard,
    possibly broken. A foreign key (`format`, `services`, `chapters`) means
    it is not. Multi-doc streams (`---`) are never dashboards — compose
    files use them and `parse_dashboard` cannot. Any other parse/OS/UTF-8
    failure stays visible as broken so a half-save cannot vanish.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return True
    try:
        data = yaml.load(io.StringIO(text))
    except ComposerError:
        return False
    except Exception:
        return True
    if not isinstance(data, dict):
        return False
    keys = frozenset(data)
    return keys <= _DASHBOARD_KEYS or bool(keys & _DASHBOARD_BODY)


def _open_private_sibling(directory: Path) -> tuple[int, Path]:
    while True:
        temporary = directory / f".sqldash-{secrets.token_hex(8)}.tmp"
        try:
            return os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666), temporary
        except FileExistsError:
            continue


def atomic_write(path: Path, text: str) -> None:
    """Write via a same-directory replace so a concurrent reader never sees
    a truncated file. ``write_text`` opens with 'w' (truncates first); the
    two-deletes smoke test read d.yaml in that window and got ''.

    The temp file is created fresh with ``O_EXCL`` under a random name, never
    opened by a fixed name: a planted ``<name>.yaml.tmp`` symlink used to take
    the write outside the project and leave the dashboard a symlink to it.
    ``O_EXCL`` rather than ``mkstemp`` so a new file gets the umask's mode, not
    0600; an existing file keeps its own mode."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = None
    fd, temporary = _open_private_sibling(path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


NAME_MAX = 255
_LONGEST_SUFFIX = ".yaml.tmp"


def bound_file_stem(stem: str) -> str:
    """Trim a dashboard file stem to what one directory entry can hold.

    NAME_MAX counts bytes, not characters, so the trim happens on the encoded
    form and a multi-byte stem cannot slip back over the limit. The budget still
    reserves `.yaml.tmp` although `atomic_write` now stages under a short random
    name, so stems trimmed before that change keep their length. Slugs end on a
    word, not on the underscore a cut run would leave behind.
    """
    budget = NAME_MAX - len(_LONGEST_SUFFIX.encode())
    encoded = stem.encode()
    if len(encoded) <= budget:
        return stem
    return encoded[:budget].decode(errors="ignore").rstrip("_")


_BLOCK_SCALARS = (LiteralScalarString, FoldedScalarString)


class _Comment(NamedTuple):
    """Own-line comment text on its way between ruamel slots: each line
    stripped ('' for a blank line) plus the column it was written at."""

    lines: list[str]
    column: int


def _is_block_collection(node: Any) -> bool:
    return (
        isinstance(node, (CommentedMap, CommentedSeq))
        and node.fa.flow_style() is not True
        and len(node) > 0
    )


def _trailing_slot(node: Any) -> tuple[Any, Any, int, Any] | None:
    """Where ruamel keeps the comment lines that follow a block node.

    A `#` line between two tiles is stored as the post-comment of the
    *previous* tile's deepest last entry: slot 2 of its last key, or slot 0
    of a nested block sequence's last item. Returns (container, key, slot,
    value). A flow value keeps its post-comment on the container's key, and
    the emitter renders it there.
    """
    if not _is_block_collection(node):
        return None
    if isinstance(node, CommentedMap):
        key = list(node.keys())[-1]
        return _trailing_slot(node[key]) or (node, key, 2, node[key])
    return _trailing_slot(node[-1]) or (node, len(node) - 1, 0, node[-1])


def _split_lines(token: CommentToken) -> list[str]:
    lines = token.value.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line.strip() for line in lines]


def _token_column(token: CommentToken) -> int:
    """The column the comment lines were written at.

    `start_mark` is only the column of where the token *starts*. A token whose
    value opens with a newline — the shape ruamel parks after a block scalar —
    starts at column 0 on the line the scalar ended, and carries the real
    indent inside its own text instead. Trusting the mark there re-indented the
    next tile's comment to column 0 on every appended key (#393).
    """
    raw = token.value.split("\n")
    if raw and raw[0] == "":
        for line in raw[1:]:
            if line.strip():
                return len(line) - len(line.lstrip())
    return token.start_mark.column


def _embed(lines: list[str], column: int) -> str:
    return "".join((" " * column + line if line else "") + "\n" for line in lines)


def _fresh_line_token(comment: _Comment) -> CommentToken:
    """The form the emitter wants at column 0 (after a block scalar, or as an
    item pre-comment): it indents the first line itself."""
    lines, column = comment
    value = (lines[0] + "\n" + _embed(lines[1:], column)) if lines else "\n"
    return CommentToken(value, CommentMark(column), None)


def _open_line_token(comment: _Comment) -> CommentToken:
    """The form the emitter wants while a value line is still open (a plain
    scalar or a flow collection): the leading newline ends that line."""
    return CommentToken("\n" + _embed(*comment), CommentMark(comment.column), None)


def _detach_after(container: Any, key: Any, slot: int, value: Any) -> _Comment | None:
    """Take the own-line comment lines out of one post-comment slot. An
    end-of-line comment on the entry's own line stays with the entry."""
    entry = container.ca.items.get(key)
    if not entry or len(entry) <= slot or entry[slot] is None:
        return None
    token = entry[slot]
    lines = _split_lines(token)
    eol = None
    if not isinstance(value, _BLOCK_SCALARS):
        first, lines = lines[0], lines[1:]
        eol = first or None
    if eol is None:
        entry[slot] = None
        if all(part is None for part in entry):
            container.ca.items.pop(key, None)
    else:
        token.value = eol + "\n"
    return _Comment(lines, _token_column(token)) if lines else None


def _attach_after(container: Any, key: Any, slot: int, value: Any, comment: _Comment) -> None:
    """Append comment lines to one post-comment slot, after whatever is there."""
    entry = container.ca.items.get(key)
    if entry is None:
        entry = [None, None, None, None]
        container.ca.items[key] = entry
    while len(entry) <= slot:
        entry.append(None)
    if entry[slot] is not None:
        entry[slot].value += _embed(*comment)
    elif isinstance(value, _BLOCK_SCALARS):
        entry[slot] = _fresh_line_token(comment)
    else:
        entry[slot] = _open_line_token(comment)


def _take_trailing(node: Any) -> _Comment | None:
    """Detach the comment lines that follow a block node (see `_trailing_slot`)."""
    slot = _trailing_slot(node)
    return _detach_after(*slot) if slot else None


def _give_trailing(node: Any, comment: _Comment | None) -> None:
    """Append comment lines after a block node's deepest last entry."""
    slot = _trailing_slot(node)
    if slot is not None and comment is not None and comment.lines:
        _attach_after(*slot, comment)


def _put_above(mapping: CommentedMap, comment: _Comment) -> None:
    """Park comment lines above a sequence item's dash: the only slot ruamel
    keeps stable for text that preceded the item's first key (`- # note` is
    rewritten there on the next dump anyway)."""
    token = _fresh_line_token(_Comment(comment.lines, max(comment.column - 2, 0)))
    pre = mapping.ca.comment
    if pre and len(pre) > 1 and pre[1]:
        pre[1].append(token)
    else:
        mapping.ca.comment = [None, [token]]


def _pre_comments(seq: CommentedSeq, index: int) -> list[_Comment]:
    """The comment lines ruamel keys on a sequence index: those directly above
    the item, when the item before it ended in a flow value."""
    entry = seq.ca.items.get(index)
    if not entry or len(entry) < 2 or not entry[1]:
        return []
    return [_Comment(_split_lines(token), _token_column(token)) for token in entry[1]]


def _ends_blank(node: Any) -> bool:
    slot = _trailing_slot(node)
    if slot is None:
        return False
    container, key, position, value = slot
    entry = container.ca.items.get(key)
    if not entry or len(entry) <= position or entry[position] is None:
        return False
    lines = _split_lines(entry[position])
    if not isinstance(value, _BLOCK_SCALARS):
        lines = lines[1:]
    return bool(lines) and lines[-1] == ""


def _without_leading_blanks(comments: list[_Comment]) -> list[_Comment]:
    kept: list[_Comment] = []
    for comment in comments:
        lines = comment.lines
        if not kept:
            while lines and not lines[0]:
                lines = lines[1:]
            if not lines:
                continue
        kept.append(_Comment(lines, comment.column))
    return kept


def _carry_between(seq: CommentedSeq, index: int, comments: list[_Comment]) -> None:
    """Re-home comment lines that sat around a removed or rewritten item so the
    text between its neighbours is what it was, directly above the item now at
    ``index`` or after the last item when there is none. The removed item's
    own blank-line separator goes with it when the seam already has one, so a
    delete reads like a hand edit rather than stacking blank lines."""
    if index == 0 or _ends_blank(seq[index - 1]):
        comments = _without_leading_blanks(comments)
    if not comments:
        return
    if index < len(seq):
        entry = seq.ca.items.get(index)
        if entry is None:
            entry = [None, None, None, None]
            seq.ca.items[index] = entry
        while len(entry) < 2:
            entry.append(None)
        entry[1] = [_fresh_line_token(c) for c in comments] + list(entry[1] or [])
        return
    if seq:
        for comment in comments:
            _give_trailing(seq[-1], comment)


def _take_value_trailing(container: Any, key: Any, value: Any) -> _Comment | None:
    """Detach the comment lines that follow a value in its mapping (``key``)
    or sequence (index): the lines between it and whatever comes next. ruamel
    keeps them at the end of the value's subtree, on its deepest last node,
    for a block collection, and on the container's slot for the key
    otherwise. The value's own end-of-line comment stays where it is."""
    if _is_block_collection(value):
        return _take_trailing(value)
    slot = 2 if isinstance(container, CommentedMap) else 0
    entry = container.ca.items.get(key)
    token = entry[slot] if entry and len(entry) > slot else None
    raw = token.value.split("\n")[1:] if token is not None else []
    comment = _detach_after(container, key, slot, value)
    if comment is None or isinstance(value, _BLOCK_SCALARS):
        return comment
    column = next((len(line) - len(line.lstrip()) for line in raw if line.strip()), None)
    return comment if column is None else _Comment(comment.lines, column)


def _give_value_trailing(container: Any, key: Any, value: Any, comment: _Comment | None) -> None:
    """Put comment lines back after a value: at the end of its subtree for a
    block collection, or on the container's slot for the key otherwise."""
    if comment is None:
        return
    if _is_block_collection(value):
        _give_trailing(value, comment)
    else:
        slot = 2 if isinstance(container, CommentedMap) else 0
        _attach_after(container, key, slot, value, comment)


def _replace_value(container: Any, key: Any, value: Any) -> None:
    """Replace a value in place, moving the comment lines that followed the
    old one to after the new one, so a note over the next key or item is not
    lost with the subtree it happened to be stored in."""
    comment = _take_value_trailing(container, key, container[key])
    container[key] = value
    _give_value_trailing(container, key, value, comment)


def _delete_key(mapping: CommentedMap, key: str) -> None:
    """Delete a key, keeping the comment lines under it where they are: on the
    key above, or above the item's dash when the key was first, so they still
    precede the key they sat over. Its own end-of-line comment goes with it."""
    keys = list(mapping.keys())
    position = keys.index(key)
    comment = _take_value_trailing(mapping, key, mapping[key])
    mapping.ca.items.pop(key, None)
    del mapping[key]
    if comment is None or len(keys) == 1:
        return
    if position == 0:
        _put_above(mapping, comment)
        return
    neighbour = keys[position - 1]
    slot = _trailing_slot(mapping[neighbour]) or (mapping, neighbour, 2, mapping[neighbour])
    _attach_after(*slot, comment)


def _seq_append(seq: CommentedSeq, item: Any) -> None:
    """Append without leaving a trailing tile comment in front of the new item.

    A `#` or blank line between the last tile and the next top-level key is
    stored as an after-comment on the last tile's last key. `seq.append`
    then dumps the new tile after that comment (#296).
    """
    trailing = _take_trailing(seq[-1]) if seq else None
    seq.append(item)
    _give_trailing(item, trailing)


def _put_key(mapping: CommentedMap, key: str, value: Any) -> None:
    """Set a key without pushing it past trailing comments or blank lines.

    ruamel stores a blank line (or a `#` comment) between the last tile key
    and the next top-level key as a post-comment on that last key.
    `mapping[key] =` then dumps the new key after that comment (#296).
    """
    if key in mapping:
        _replace_value(mapping, key, value)
        return
    trailing = _take_trailing(mapping)
    mapping[key] = value
    _give_trailing(mapping, trailing)


def _set_position(target: Any, pos: dict[str, int]) -> None:
    """Write a flow-style ``position`` onto one tile, replacing a legacy ``size``
    in place so the key keeps its slot and its comment. A ``size`` beside an
    existing ``position`` is dead config (dimensions_hint prefers position),
    so it goes too (#338)."""
    existing = target.get("position")
    if existing is None:
        existing = CommentedMap()
        existing.fa.set_flow_style()
        if "size" in target:
            index = list(target.keys()).index("size")
            comment = target.ca.items.pop("size", None)
            del target["size"]
            target.insert(index, "position", existing)
            if comment is not None:
                target.ca.items["position"] = comment
        else:
            _put_key(target, "position", existing)
    elif "size" in target:
        _delete_key(target, "size")
    for key in ("x", "y", "w", "h"):
        existing[key] = int(pos[key])


def _same_text(a: Any, b: Any) -> bool:
    return isinstance(a, str) and isinstance(b, str) and a.strip() == b.strip()


def _drill_node(value: Any) -> Any:
    """A tile's `drill:` in its tersest form: a bare dashboard name when it maps
    nothing, otherwise a block with each `{filter: name}` on one line."""
    spec = DrillSpec.model_validate(value)
    if spec.dashboard and not spec.filters and spec.column is None and not spec.new_tab:
        return spec.dashboard
    node = CommentedMap()
    if spec.dashboard:
        node["dashboard"] = spec.dashboard
    if spec.filters:
        node["filters"] = CommentedMap(
            (key, v if isinstance(v, str) else _flow(v.model_dump()))
            for key, v in spec.filters.items()
        )
    if spec.column:
        node["column"] = spec.column
    if spec.new_tab:
        node["new_tab"] = True
    return node


def _tile_model(raw: CommentedMap) -> Tile | None:
    try:
        return Tile.model_validate(dict(raw))
    except ValidationError:
        return None


def _implied_chart_type(tile: Tile) -> str:
    """The type the browser renders a chart-less tile with (`defaultChartSpec`)."""
    if tile.metric is None:
        return "table"
    if tile.metric.grain:
        return "area"
    if tile.metric.dimensions:
        return "table"
    return "big_number"


def _slim_chart(chart: dict[str, Any]) -> dict[str, Any]:
    slim = {k: v for k, v in chart.items() if v not in (None, [], {})}
    if slim.get("stacked") is False:
        del slim["stacked"]
    if slim.get("legend") is True:
        del slim["legend"]
    return slim


def _chart_spec(slim: dict[str, Any]) -> ChartSpec | None:
    try:
        return ChartSpec.model_validate(slim)
    except ValidationError:
        return None


def _chart_unchanged(current: Tile | None, slim: dict[str, Any]) -> bool:
    """Whether the incoming chart means what the tile already renders: its own
    `chart:` (with a tile-level `format:` folded in), or the implied default
    for a tile that has none. A bare metric tile's payload carries the
    metric's format, which is not the tile's to store."""
    incoming = _chart_spec(slim)
    if current is None or incoming is None:
        return False
    if current.chart is not None:
        return _typed(incoming.model_dump()) == _typed(current.chart.model_dump())
    implied = ChartSpec(type=_implied_chart_type(current))
    if current.metric is not None:
        incoming = incoming.model_copy(update={"format": {}})
    return _typed(incoming.model_dump()) == _typed(implied.model_dump())


def _typed(value: Any) -> Any:
    """Python counts `True == 1`, but a chart does not: `x_order: [true]` and
    `x_order: [1]` pin different categories, so compare booleans as their own kind."""
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, dict):
        return {k: _typed(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_typed(v) for v in value]
    return value


def _same_chart_value(key: str, written: Any, incoming: Any) -> bool:
    """Compare the way `ChartSpec` reads the file: `y: revenue` and `y: [revenue]`
    are the same chart, so neither spelling is rewritten into the other."""
    if key == "y":
        return ([written] if isinstance(written, str) else written) == (
            [incoming] if isinstance(incoming, str) else incoming
        )
    return _typed(written) == _typed(incoming)


def _shares_yaml(node: Any, seen: set[int] | None = None, ancestors: tuple = ()) -> bool:
    """Whether a subtree holds an anchor, an alias or a `<<` merge key, any of
    which makes an in-place edit reach past the tile being saved. A mapping
    above it counts too: a tile that is anchored, or that takes its keys from
    another through `<<`, can share the very chart it seems to own."""
    for above in ancestors:
        if getattr(above.anchor, "value", None) is not None or above.merge:
            return True
    seen = set() if seen is None else seen
    anchor = getattr(node, "anchor", None)
    if anchor is not None and getattr(anchor, "value", None) is not None:
        return True
    if not isinstance(node, (CommentedMap, CommentedSeq)):
        return False
    if id(node) in seen or (isinstance(node, CommentedMap) and node.merge):
        return True
    seen.add(id(node))
    children = node.values() if isinstance(node, CommentedMap) else node
    return any(_shares_yaml(child, seen) for child in children)


def _plain_scalar(value: Any) -> Any:
    anchor = getattr(value, "anchor", None)
    if anchor is None or getattr(anchor, "value", None) is None:
        return value
    for kind in (bool, int, float, str):
        if isinstance(value, kind):
            return kind(value)
    return value


def _materialized(node: Any) -> Any:
    """A fully resolved, unshared copy of a subtree: every alias expanded into
    its own nodes, every `<<` merge written out as the keys it contributed,
    no anchors left, and each mapping and sequence keeping its flow or block
    style and its own comments. Editing the copy touches nothing else."""
    if isinstance(node, CommentedMap):
        out = CommentedMap()
        for key, value in node.items():
            out[key] = _materialized(value)
        out.ca.items.update({k: deepcopy(v) for k, v in node.ca.items.items() if k in out})
    elif isinstance(node, CommentedSeq):
        out = CommentedSeq(_materialized(value) for value in node)
        out.ca.items.update(deepcopy(node.ca.items))
    else:
        return _plain_scalar(node)
    out.ca.comment = deepcopy(node.ca.comment)
    out.ca.end = deepcopy(node.ca.end)
    if node.fa.flow_style():
        out.fa.set_flow_style()
    else:
        out.fa.set_block_style()
    return out


def _write_chart(existing: CommentedMap, slim: dict[str, Any]) -> None:
    """Write a changed chart in the tersest form that still says it: `chart: bar`
    when only the type is set, otherwise key-by-key into the mapping the author
    wrote (keeping its style) or as a new flow mapping."""
    node = existing.get("chart")
    if set(slim) == {"type"}:
        _put_key(existing, "chart", slim["type"])
        return
    if not isinstance(node, CommentedMap):
        _put_key(existing, "chart", _flow(slim))
        return
    if _shares_yaml(node, ancestors=(existing,)):
        node = existing["chart"] = _materialized(node)
    for key, value in slim.items():
        if key == "references" and isinstance(node.get(key), CommentedSeq):
            _write_references(node[key], value)
        elif key not in node:
            _put_key(node, key, _flow(value))
        elif not _same_chart_value(key, node[key], value):
            _replace_value(node, key, _flow(value))
    for key in list(node.keys()):
        field = ChartSpec.model_fields.get(key)
        if key not in slim and field is not None and node[key] != field.default:
            _delete_key(node, key)


def _reference(raw: Any) -> ReferenceLine | None:
    try:
        return ReferenceLine.model_validate(dict(raw))
    except (TypeError, ValueError):
        return None


def _write_references(seq: CommentedSeq, incoming: list[Any]) -> None:
    """Edit an authored `references:` list item by item: a reference that still
    means the same thing keeps its node (and its style, a `2026-09-01` date
    staying unquoted), a changed one is edited key by key in the node at its
    position, a removed one goes, and a new one is written as a flow mapping.
    Comments stay with the references they sit against."""
    refs = [_reference(item) for item in seq]
    free = set(range(len(seq)))
    wanted = [_reference(value) for value in incoming]
    kept: list[Any] = [None] * len(incoming)
    for j, ref in enumerate(wanted):
        match = next((i for i in sorted(free) if ref is not None and refs[i] == ref), None)
        if match is not None:
            free.discard(match)
            kept[j] = seq[match]
    for j, value in enumerate(incoming):
        if kept[j] is not None:
            continue
        if j in free and refs[j] is not None and wanted[j] is not None:
            free.discard(j)
            below = _take_item_trailing(seq, j)
            _edit_reference(seq[j], refs[j], wanted[j], value)
            _give_item_trailing(seq, j, below)
            kept[j] = seq[j]
        else:
            kept[j] = _flow(value)
    for index in sorted(free, reverse=True):
        _give_item_trailing(seq, index - 1, _take_item_trailing(seq, index))
    _rearrange(seq, kept)


def _edit_reference(node: CommentedMap, old: ReferenceLine, new: ReferenceLine, raw: Any) -> None:
    before = old.model_dump(exclude_none=True)
    after = new.model_dump(exclude_none=True)
    for key, value in after.items():
        if key not in node:
            _put_key(node, key, _flow(raw[key]))
        elif before.get(key) != value:
            _replace_value(node, key, _flow(raw[key]))
    for key in [k for k in node if k not in after]:
        _delete_key(node, key)


def _take_item_trailing(seq: CommentedSeq, index: int) -> _Comment | None:
    return _take_value_trailing(seq, index, seq[index])


def _give_item_trailing(seq: CommentedSeq, index: int, comment: _Comment | None) -> None:
    """Put comment lines below item ``index``, or at the head of the list when
    ``index`` is -1: where lines taken from an edited item go back, and where
    a removed item's go, onto the item above it."""
    if comment is None:
        return
    if index >= 0:
        _give_value_trailing(seq, index, seq[index], comment)
        return
    token = _fresh_line_token(comment)
    head = seq.ca.comment
    if head and len(head) > 1 and head[1]:
        head[1].append(token)
    else:
        seq.ca.comment = [None, [token]]


def _rearrange(seq: CommentedSeq, wanted: list[Any]) -> None:
    """Make ``seq`` hold ``wanted`` in order by popping and inserting, never by
    slice assignment, which wipes the positional comment table where a comment
    or blank line *between* items lives. Each entry is keyed by index, so it is
    carried with the node it sits against and re-keyed by the new positions.

    A comment that lands at index 0 stops travelling: YAML has no way to say
    "this comment belongs to the first item" rather than "to the block", so
    ruamel reads it back as the sequence's own head comment. It stays at the
    top: never lost, but no longer moving with its item."""
    if [id(n) for n in seq] == [id(n) for n in wanted]:
        return
    carried = {
        id(node): seq.ca.items[index] for index, node in enumerate(seq) if index in seq.ca.items
    }
    keep = {id(n) for n in wanted}
    for index in range(len(seq) - 1, -1, -1):
        if id(seq[index]) not in keep:
            seq.pop(index)
    for index, node in enumerate(wanted):
        if index < len(seq) and seq[index] is node:
            continue
        for later in range(index, len(seq)):
            if seq[later] is node:
                seq.pop(later)
                break
        seq.insert(index, node)
    seq.ca.items.clear()
    for index, node in enumerate(seq):
        if id(node) in carried:
            seq.ca.items[index] = carried[id(node)]


def _slim_metric(metric: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in metric.items() if v not in (None, [], "")}


def _metric_ref(slim: dict[str, Any]) -> MetricRef | None:
    try:
        return MetricRef.model_validate(slim)
    except ValidationError:
        return None


def _fold_tile_level(
    nested: dict[str, Any], tile: dict[str, Any], keys: tuple[str, ...]
) -> dict[str, Any]:
    """Read a payload's tile-level shorthand the way `Tile.check_tile_shape` reads
    the file: it fills a key the nested metric/chart leaves empty, so
    `{"metric": "revenue", "grain": "month"}` sets the grain instead of clearing it."""
    folded = dict(nested)
    for key in keys:
        if folded.get(key) in (None, "", {}) and tile.get(key) not in (None, "", {}):
            folded[key] = tile[key]
    return folded


def _supersede_tile_level(
    existing: CommentedMap,
    slim: dict[str, Any],
    keys: tuple[str, ...],
    superseded: set[str],
    *,
    can_clear: bool,
) -> None:
    """A tile-level shorthand the incoming value still agrees with keeps carrying
    it; one it disagrees with — a new value, or a cleared one (#508) — is dead.
    A bare-string `metric`/`chart` cannot express the key at all, so it never
    clears one."""
    for key in keys:
        if existing.get(key) is None:
            continue
        if existing[key] == slim.get(key):
            del slim[key]
        elif key in slim or can_clear:
            superseded.add(key)


def _edit_tile_in_place(
    doc: CommentedMap,
    tiles: CommentedSeq,
    existing: CommentedMap,
    old_id: str,
    tile: dict[str, Any],
    sql: str | None,
    showing: dict[str, dict[str, int]],
) -> bool:
    """Apply an editor payload to the tile node the author wrote, touching only
    the keys whose meaning changed and keeping every unchanged key in its
    authored form: inline `sql:` stays inline, `chart: bar` stays a string, a
    tile-level `format:`/`grain:` keeps carrying its value, a derived id stays
    derived. Returns whether a position was written (#360)."""
    current = _tile_model(existing)
    superseded: set[str] = set()

    title = tile.get("title")
    hoisted = "sql" in existing and not existing.get("query")
    if (
        hoisted
        and not existing.get("id")
        and title
        and slugify(str(title)) != old_id
        and any(w is not existing and w.get("query") == old_id for w in tiles)
    ):
        _put_key(existing, "id", old_id)
    if title is None:
        if "title" in existing:
            _delete_key(existing, "title")
    elif existing.get("title") != title:
        _put_key(existing, "title", title)

    incoming_type = tile.get("type") or "chart"
    if incoming_type == "chart":
        if existing.get("type") not in (None, "chart"):
            _delete_key(existing, "type")
    elif existing.get("type") not in (None, incoming_type):
        existing["type"] = incoming_type

    query_name = tile.get("query")
    if query_name and hoisted and query_name == old_id:
        # `query` naming this tile's own hoisted block is the GET+PUT shape:
        # GET returns `sql: null`, so the caller has no `sql` to send back and
        # pruning the block left the tile pointing at nothing (#392).
        if sql is not None and not _same_text(existing["sql"], sql):
            existing["sql"] = LiteralScalarString(sql.strip() + "\n")
    elif query_name:
        if hoisted and any(w is not existing and w.get("query") == old_id for w in tiles):
            doc.setdefault("queries", CommentedMap())[old_id] = existing["sql"]
        if sql is not None:
            queries = doc.setdefault("queries", CommentedMap())
            if not _same_text(queries.get(query_name), sql):
                queries[query_name] = LiteralScalarString(sql.strip() + "\n")
        if "sql" in existing:
            trailing = _take_trailing(existing)
            index = list(existing.keys()).index("sql")
            _delete_key(existing, "sql")
            existing.pop("query", None)
            existing.insert(index, "query", query_name)
            _give_trailing(existing, trailing)
        elif existing.get("query") != query_name:
            _put_key(existing, "query", query_name)
    else:
        for key in ("query", "sql"):
            if key in existing:
                _delete_key(existing, key)

    metric = tile.get("metric")
    bare_metric = isinstance(metric, str)
    if bare_metric:
        metric = {"name": metric}
    if isinstance(metric, dict):
        slim = _slim_metric(_fold_tile_level(metric, tile, ("grain", "compare")))
        if current is None or current.metric != _metric_ref(slim):
            _supersede_tile_level(
                existing, slim, ("grain", "compare"), superseded, can_clear=not bare_metric
            )
            _put_key(existing, "metric", slim["name"] if set(slim) == {"name"} else _flow(slim))
    else:
        if "metric" in existing:
            _delete_key(existing, "metric")
        superseded.update(("grain", "compare"))

    source = tile.get("source") or None
    if source is None:
        if "source" in existing:
            _delete_key(existing, "source")
    elif existing.get("source") != source:
        _put_key(existing, "source", source)

    markdown = tile.get("markdown")
    if markdown is None:
        if "markdown" in existing:
            _delete_key(existing, "markdown")
    elif not _same_text(existing.get("markdown"), markdown):
        value: Any = markdown
        if "\n" in markdown or isinstance(existing.get("markdown"), LiteralScalarString):
            value = LiteralScalarString(markdown.rstrip("\n") + "\n")
        _put_key(existing, "markdown", value)

    chart = tile.get("chart")
    bare_chart = isinstance(chart, str)
    if bare_chart:
        chart = {"type": chart}
    if isinstance(chart, dict):
        slim = _slim_chart(_fold_tile_level(chart, tile, ("format",)))
        if not _chart_unchanged(current, slim):
            _supersede_tile_level(existing, slim, ("format",), superseded, can_clear=not bare_chart)
            _write_chart(existing, slim)
    else:
        if "chart" in existing:
            _delete_key(existing, "chart")
        superseded.add("format")

    if "drill" in tile:
        wanted = None if tile["drill"] is None else DrillSpec.model_validate(tile["drill"])
        if current is None or current.drill != wanted:
            if wanted is None:
                if "drill" in existing:
                    _delete_key(existing, "drill")
            else:
                _put_key(existing, "drill", _drill_node(tile["drill"]))

    wrote_position = False
    pos = tile.get("position")
    if isinstance(pos, dict):
        wanted = {key: int(pos[key]) for key in ("x", "y", "w", "h")}
        if wanted != showing.get(old_id):
            _set_position(existing, wanted)
            wrote_position = True

    for key in superseded:
        if key in existing:
            _delete_key(existing, key)
    return wrote_position


def pin_positions(tiles: list, computed: dict[str, dict[str, int]]) -> None:
    """Write ``position`` on every tile that is still flowing.

    A flowed tile's position is computed at parse time and never recorded, and
    the flow cursor starts *below* every pinned tile — so the moment one tile
    gains a `position:`, every remaining tile silently moves. Editing one tile
    re-laid-out the whole dashboard, and each subsequent edit pinned a tile at
    its already-drifted spot, so the damage accumulated.

    Recording the layout the author is looking at makes a single-tile edit mean
    only what it says.
    """
    ids = derive_tile_ids(list(tiles))
    for raw, tile_id in zip(tiles, ids, strict=True):
        if raw.get("position"):
            continue
        pos = computed.get(tile_id)
        if pos:
            _set_position(raw, pos)


def pin_derived_ids(tiles: list) -> None:
    """Write ``id:`` on tiles whose derived id is not just slugify(title).

    Untitled tiles and collision suffixes are positional. After a UI mutation
    they must stop being derived, or the next delete targets the wrong tile.
    """
    ids = derive_tile_ids(list(tiles))
    for raw, derived in zip(tiles, ids, strict=True):
        if raw.get("id"):
            continue
        title = raw.get("title")
        if title and derived == slugify(str(title)):
            continue
        _put_key(raw, "id", derived)


class StoreError(Exception):
    pass


class NotFoundError(StoreError):
    pass


class ConflictError(StoreError):
    pass


class InvalidDashboardError(StoreError):
    pass


def read_dashboard_text(path: Path) -> str:
    """Read a dashboard file as UTF-8, turning a non-UTF-8 file into an
    InvalidDashboardError so the serving side shows the friendly broken-file
    page instead of a 500 (the file is undecodable, same class as unparseable)."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidDashboardError(f"file is not valid UTF-8: {exc}") from exc


def compute_etag(text: str) -> str:
    """Content hash used for If-Match optimistic concurrency."""
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _entity_tag(token: str) -> str:
    """The opaque value inside one RFC 7232 entity-tag: `"abc"` and `W/"abc"`
    are both the etag `abc`. A bare token is returned as it came, because the
    browser sends the hash unquoted."""
    token = token.strip()
    if token[:2].upper() == "W/":
        token = token[2:].lstrip()
    if len(token) > 1 and token.startswith('"') and token.endswith('"'):
        token = token[1:-1]
    return token


def if_match_wildcard(if_match: str) -> bool:
    """`If-Match: *` — any existing representation matches (RFC 7232)."""
    return any(token.strip() == "*" for token in if_match.split(","))


def etag_matches(current: str, if_match: str) -> bool:
    """Whether an If-Match header names the etag on disk.

    RFC 7232 spells an entity-tag quoted, so every conforming HTTP client sends
    `If-Match: "abc"` while our own pages send `abc`; comparing the raw header
    meant a spec-correct client could never write (#651). A comma list matches
    if any member does.
    """
    return if_match_wildcard(if_match) or current in [
        _entity_tag(token) for token in if_match.split(",")
    ]


def etag_mismatch(current: str, if_match: str) -> str:
    """Both sides quoted, so a quoting difference is readable as one.
    `etag abc, expected "abc"` printed the same value twice and read as a
    concurrent edit that never happened (#651)."""
    return f"(on disk {current!r}, If-Match {if_match!r})"


def etag_conflict(subject: str, current: str, if_match: str) -> str:
    return f"{subject} changed on disk {etag_mismatch(current, if_match)}"


# Keys the tile editor owns. Absent (or null) in a PUT payload means "remove it".
# Everything else on a tile is authored config the editor never sees and must
# survive an edit: per-tile `source`, and the shorthands `format`/`grain`/`compare`,
# which validation folds into `chart`/`metric` and blanks before the browser is ever
# handed the tile — so the browser cannot round-trip them even in principle.
# Pruning those was silently destroying them on every save. `size` is managed
# rather than preserved: it is shorthand for `position`, which the editor does
# send, and dimensions_hint() prefers position — so a surviving `size` would be
# dead config that a later hand-edit changes nothing.
EDITOR_MANAGED_TILE_KEYS = frozenset(
    {
        "type",
        "title",
        "query",
        "metric",
        "position",
        "chart",
        "markdown",
        "sql",
        "size",
        "source",
    }
)


PROMOTED_SOURCE_NAME = "main"


def _put_named_source(doc, sname: str, source) -> None:
    """Name one more connection in this file. Does not overwrite an authored entry.

    A file already naming its connections grows an entry under `source:`. A file
    still carrying the legacy `sources:` block keeps that shape — rewriting an
    author's file into the merged one is not what "save this tile" asked for. A
    single connection is promoted into the merged mapping: the one key is where
    named connections live now, so growing a second `sources:` key would write
    back the shape this format merged away.
    """
    node = CommentedMap(source_as_project_yaml(source))
    node.fa.set_flow_style()
    block = doc.get("source")
    if is_named_source_map(block):
        if sname not in block:
            block[sname] = node
        return
    sources = doc.get("sources")
    if isinstance(sources, dict) or not isinstance(block, dict | str):
        if not isinstance(sources, dict):
            sources = CommentedMap()
            keys = list(doc.keys())
            idx = keys.index("source") + 1 if "source" in doc else 1
            doc.insert(idx, "sources", sources)
        if sname not in sources:
            sources[sname] = node
        return
    default_name = PROMOTED_SOURCE_NAME if sname != PROMOTED_SOURCE_NAME else "default"
    if isinstance(block, str):
        promoted = CommentedMap({"url": block})
        promoted.fa.set_flow_style()
    else:
        promoted = block if isinstance(block, CommentedMap) else CommentedMap(block)
    promoted[DEFAULT_MARK] = True
    merged = CommentedMap()
    merged[default_name] = promoted
    merged[sname] = node
    doc["source"] = merged


def _flow(obj: Any) -> Any:
    """Deep-copy into flow-style ruamel nodes so mappings render as one-line ``{...}``."""
    if isinstance(obj, dict):
        out = CommentedMap({k: _flow(v) for k, v in obj.items()})
        out.fa.set_flow_style()
        return out
    if isinstance(obj, list):
        out = CommentedSeq([_flow(v) for v in obj])
        out.fa.set_flow_style()
        return out
    return obj


def build_dashboard_text(title: str, source: dict[str, Any]) -> str:
    """Minimal starter YAML for a new dashboard: title, flow-style source, empty tiles."""
    doc = CommentedMap()
    doc["title"] = title
    doc["source"] = _flow(source)
    doc["tiles"] = []
    buffer = io.StringIO()
    yaml.dump(doc, buffer)
    return buffer.getvalue()


def plain_validation_message(error: dict) -> str:
    """One pydantic error without the "Value error, " prefix pydantic wraps
    validator messages in. Re-raising a prefixed message from inside another
    validator makes pydantic prefix it a second time."""
    return error["msg"].removeprefix("Value error, ")


def format_validation_error(error: dict) -> str:
    """One pydantic error as a person would write it: no empty location prefix
    for model-level validators, and without pydantic's "Value error," noise."""
    message = plain_validation_message(error)
    loc = error["loc"]
    if (
        error.get("type") == "extra_forbidden"
        and len(loc) == 3
        and loc[0] == "tiles"
        and loc[2] == "dimensions"
    ):
        message = f"{message} — {TILE_LEVEL_DIMENSIONS_HINT}"
    location = ".".join(str(p) for p in loc)
    return f"{location}: {message}" if location else message


DATE_LITERAL = re.compile(r"(?<![\w-])\d{4}-\d{1,2}-\d{1,2}(?![\w-])")


def yaml_error(text: str, exc: Exception) -> str:
    """`invalid YAML: ...` with a location, even when the loader has no mark.

    A syntax error carries ruamel's own line and column in its text. A value the
    loader refuses to *construct* does not: an impossible date (`2026-02-30`) is
    well-formed YAML that raises a bare ValueError from the date constructor, so
    the author was told "day 30 must be in range 1..28 for month 2" with nothing
    to search for but the message (#672). The offending scalar is findable, so
    find it: the first date literal in the file that is not a real date.
    """
    message = f"invalid YAML: {exc}"
    if getattr(exc, "problem_mark", None) is not None:
        return message
    for number, line in enumerate(text.splitlines(), start=1):
        for match in DATE_LITERAL.finditer(line):
            try:
                date.fromisoformat(match.group(0))
            except ValueError:
                return (
                    f"{message}\n  in line {number}, column {match.start() + 1}: {match.group(0)}"
                )
    return message


def parse_dashboard(text: str) -> Dashboard:
    """Parse and validate, flattening pydantic errors into one readable message."""
    try:
        data = yaml.load(io.StringIO(text))
    except Exception as exc:
        raise InvalidDashboardError(yaml_error(text, exc)) from exc
    if not isinstance(data, dict):
        raise InvalidDashboardError("dashboard file must be a YAML mapping")
    try:
        return Dashboard.model_validate(data)
    except ValidationError as exc:
        errors = "; ".join(format_validation_error(e) for e in exc.errors())
        raise InvalidDashboardError(errors) from exc


class Store(ABC):
    """The store contract: a namespace of dashboards addressed by name.

    DashboardStore serves one root; WorkspaceStore federates several under
    ``repo/name`` prefixes. Everything above this layer (API, CLI, MCP,
    semantic layer) depends only on this interface.
    """

    @abstractmethod
    def discover(self) -> dict[str, Path]:
        """Map every dashboard name to its file path."""

    @abstractmethod
    def load(self, name: str) -> tuple[Dashboard, str, str]:
        """Return (parsed dashboard, raw text, etag) or raise NotFoundError."""

    @abstractmethod
    def path_for(self, name: str) -> Path:
        """The file behind a dashboard name, or raise NotFoundError."""

    @abstractmethod
    def save_text(self, name: str, text: str, if_match: str | None = None) -> str:
        """Validate and write full file text, returning the new etag."""

    @abstractmethod
    def delete(self, name: str) -> None:
        """Remove the dashboard's file."""

    def iter_loaded(self) -> Iterator[tuple[str, Dashboard]]:
        """Yield (name, dashboard) for every dashboard that parses; skip the rest."""
        for name in self.discover():
            try:
                dashboard, _, _ = self.load(name)
            except Exception:
                continue
            yield name, dashboard

    def _etag_on_disk(self, path: Path) -> str:
        """Content hash, or '' when the file is not valid UTF-8 (the index
        serves that so a broken file stays deletable)."""
        try:
            return compute_etag(path.read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            return ""

    def _check_etag(self, path: Path, if_match: str | None) -> str:
        try:
            text = path.read_text(encoding="utf-8")
            current = compute_etag(text)
        except UnicodeDecodeError:
            # An undecodable file has no computable etag; the index serves it
            # with data-etag="". Let that match so a broken file stays deletable.
            text, current = "", ""
        if if_match is not None and not etag_matches(current, if_match):
            raise ConflictError(etag_conflict(f"'{path.name}'", current, if_match))
        return text


class DashboardStore(Store):
    """Reads and surgically edits the dashboard YAML under one root.

    A root is a directory, its ``.sqldash/`` subfolder when present, or a
    single file (which then refuses new dashboards). Every mutation is a
    ruamel round-trip touching only the keys it changes, so comments,
    quoting, and layout survive — a UI edit must diff like a hand edit.
    """

    def __init__(self, path: Path) -> None:
        path = path.resolve()
        if path.is_file():
            self.root = path.parent
            self.single_file: Path | None = path
        elif path.is_dir():
            nested = path / ".sqldash"
            self.root = nested if nested.is_dir() else path
            self.single_file = None
        else:
            raise StoreError(f"path does not exist: {path}")

    def discover(self) -> dict[str, Path]:
        """Map name -> path for every dashboard file; ``.yaml`` wins a stem collision."""
        if self.single_file is not None:
            if self.single_file.stem in SKIP_NAMES:
                return {}
            return {self.single_file.stem: self.single_file}
        found: dict[str, Path] = {}
        for pattern in ("*.yaml", "*.yml"):
            for path in sorted(self.root.glob(pattern)):
                if path.stem in SKIP_NAMES or not _looks_like_dashboard(path):
                    continue
                found.setdefault(path.stem, path)
        return found

    def path_for(self, name: str) -> Path:
        paths = self.discover()
        if name not in paths:
            raise NotFoundError(f"no dashboard named '{name}'")
        return paths[name]

    def load(self, name: str) -> tuple[Dashboard, str, str]:
        """Return (parsed dashboard, raw text, etag)."""
        text = read_dashboard_text(self.path_for(name))
        return parse_dashboard(text), text, compute_etag(text)

    def _confined_path(self, name: str) -> Path:
        """A dashboard path that cannot leave the store root.

        Names are a single path segment. ``repo/dashboard`` is a WorkspaceStore
        address — it splits and hands the remainder here. Accepting a slash
        wrote a file discover() cannot see and skipped If-Match (#334).
        """
        if not name or "/" in name or name in (".", "..") or name in SKIP_NAMES:
            raise InvalidDashboardError(
                f"invalid dashboard name '{name}'"
                + (" — a dashboard name cannot contain '/'" if "/" in name else "")
            )
        path = (self.root / f"{name}.yaml").resolve()
        try:
            path.relative_to(self.root.resolve())
        except ValueError:
            raise InvalidDashboardError(f"invalid dashboard name '{name}'") from None
        return path

    def _reject_unwritable_name(self, name: str) -> None:
        """Refuse a name no directory entry can hold, before touching the disk.

        `Path.exists()` only swallows ENOENT/ENOTDIR/EBADF/ELOOP, so on Linux a
        too-long name raises out of the existence check before the guarded write
        is reached, while macOS answered False and reached it. Checking the
        length first makes the 422 the same verdict on both.
        """
        if self.single_file is not None:
            return
        if len(f"{name}{_LONGEST_SUFFIX}".encode()) > NAME_MAX:
            raise InvalidDashboardError(
                f"dashboard name '{name[:40]}...' is too long for this filesystem "
                f"({len(name.encode())} bytes) — use a shorter title"
            )

    def _reject_unslugged_name(self, name: str) -> None:
        """A new dashboard's name is the stem POST would derive from it.

        Anything else reaches disk as a file whose index link cannot route
        (`we ird?q#h%` 404s), and a decomposed spelling of an existing name
        would look identical to it in the index.
        """
        stem = bound_file_stem(dashboard_stem(name))
        if stem != name:
            hint = f"; try '{stem}'" if stem else ""
            raise InvalidDashboardError(
                f"invalid dashboard name '{name}': a new dashboard name is lowercase "
                f"letters and numbers joined by single underscores{hint}"
            )

    def save_text(self, name: str, text: str, if_match: str | None = None) -> str:
        """Validate then write the whole file; a stale etag raises ConflictError.

        A new file is created only by ``if_match=None`` (POST, which picked the
        name) or ``If-Match: *``. Any other etag names a version the caller saw,
        so a missing file means it was deleted, and writing would resurrect it.
        """
        parse_dashboard(text)
        self._reject_unwritable_name(name)
        try:
            path = self.path_for(name)
        except NotFoundError:
            if self.single_file is not None:
                raise
            path = self._confined_path(name)
            if path.exists():
                if if_match is None:
                    raise ConflictError(f"dashboard '{name}' already exists") from None
                current = self._etag_on_disk(path)
                if not etag_matches(current, if_match):
                    raise ConflictError(
                        etag_conflict(f"dashboard '{name}'", current, if_match)
                    ) from None
            else:
                if if_match is not None and not if_match_wildcard(if_match):
                    raise NotFoundError(
                        f"no dashboard named '{name}'; send If-Match: * to create it"
                    ) from None
                self._reject_unslugged_name(name)
        else:
            if if_match is not None:
                current = self._etag_on_disk(path)
                if not etag_matches(current, if_match):
                    raise ConflictError(etag_conflict(f"dashboard '{name}'", current, if_match))
        try:
            atomic_write(path, text)
        except OSError as exc:
            if exc.errno != errno.ENAMETOOLONG:
                raise
            raise InvalidDashboardError(
                f"dashboard name '{name}' is too long for this filesystem "
                f"({len(name.encode())} bytes) — use a shorter title"
            ) from exc
        return compute_etag(text)

    def delete(self, name: str) -> None:
        self.path_for(name).unlink()

    def _mutate(self, name: str, if_match: str | None, fn) -> str:
        """Etag-check, round-trip load, apply ``fn`` to the ruamel doc in place,
        re-validate, write, and return the new etag. ``fn`` must edit only the
        keys it means to change."""
        path = self.path_for(name)
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise InvalidDashboardError(
                f"file is not valid UTF-8: {exc} — replace the whole file instead of a partial edit"
            ) from exc
        current = compute_etag(text)
        if if_match is not None and not etag_matches(current, if_match):
            raise ConflictError(etag_conflict(f"'{path.name}'", current, if_match))
        doc = yaml.load(io.StringIO(text))
        fn(doc)
        buffer = io.StringIO()
        yaml.dump(doc, buffer)
        new_text = buffer.getvalue()
        parse_dashboard(new_text)
        atomic_write(path, new_text)
        return compute_etag(new_text)

    def update_positions(
        self, name: str, positions: dict[str, dict[str, int]], if_match: str | None
    ) -> str:
        """Set each tile's flow-style ``position``, upgrading a legacy ``size:`` in place."""

        def apply(doc) -> None:
            tiles = doc.get("tiles") or []
            by_id = dict(zip(derive_tile_ids(list(tiles)), tiles, strict=True))
            for tile_id, pos in positions.items():
                if tile_id not in by_id:
                    raise InvalidDashboardError(f"unknown tile '{tile_id}'")
                _set_position(by_id[tile_id], pos)
            pin_derived_ids(tiles)

        return self._mutate(name, if_match, apply)

    def upsert_tile(
        self,
        name: str,
        tile: dict[str, Any],
        sql: str | None,
        if_match: str | None,
        named_source: tuple[str, Any] | None = None,
    ) -> str:
        """Insert or replace a tile by derived id, written in the tersest shorthand forms.

        `named_source` is `(sname, Source)` copied into `sources:` in the same
        write so a tile can land on a project source that was not yet named here.
        """
        # The layout as it stands *before* this edit. Pinning one tile moves
        # every tile that is still flowing, so the rest have to be written down
        # at what they are showing now, in the same write.
        try:
            current, _, _ = self.load(name)
            showing = {
                w.id: {"x": w.position.x, "y": w.position.y, "w": w.position.w, "h": w.position.h}
                for w in current.tiles
                if w.position is not None
            }
        except (InvalidDashboardError, NotFoundError, FileNotFoundError):
            showing = {}

        def apply(doc) -> None:
            if named_source is not None:
                _put_named_source(doc, named_source[0], named_source[1])
            target_id = tile.get("id")
            tiles = doc.setdefault("tiles", [])
            derived = derive_tile_ids(list(tiles))
            for index, (existing, existing_id) in enumerate(zip(tiles, derived, strict=True)):
                if existing_id == target_id:
                    moved = _edit_tile_in_place(
                        doc, tiles, existing, existing_id, tile, sql, showing
                    )
                    after = derive_tile_ids(list(tiles))
                    title = existing.get("title")
                    own = slugify(str(title)) if title else existing_id
                    shifted = [
                        i for i, (a, b) in enumerate(zip(derived, after, strict=True)) if a != b
                    ]
                    if any(i != index for i in shifted) or after[index] not in (own, existing_id):
                        pin_derived_ids(tiles)
                    if moved:
                        pin_positions(tiles, showing)
                    return
            if sql is not None and tile.get("query"):
                queries = doc.setdefault("queries", CommentedMap())
                normalized = sql.strip() + "\n"
                queries[tile["query"]] = LiteralScalarString(normalized)
            ordered = {"id": tile.get("id")}
            for key in (
                "type",
                "title",
                "query",
                "source",
                "metric",
                "position",
                "chart",
                "markdown",
                "drill",
            ):
                if key in tile:
                    ordered[key] = tile[key]
            clean = {k: v for k, v in ordered.items() if v is not None}
            if "drill" in clean:
                clean["drill"] = _drill_node(clean["drill"])
            if clean.get("type") == "chart":
                del clean["type"]
            if clean.get("id") and clean.get("title") and clean["id"] == slugify(clean["title"]):
                del clean["id"]
            metric = clean.get("metric")
            if isinstance(metric, dict):
                slim = _slim_metric(metric)
                clean["metric"] = slim["name"] if set(slim) == {"name"} else _flow(slim)
            if "position" not in clean:
                # Below *every* tile, not only the ones already carrying a
                # `position:`. pin_positions is about to write the flowing tiles
                # down where they are, so a new tile placed below only the
                # pinned ones lands on top of a flowing one — two tiles sharing
                # every cell of their footprint.
                bottoms = [pos["y"] + pos["h"] for pos in showing.values()]
                bottoms += [
                    w["position"].get("y", 0) + w["position"].get("h", 4)
                    for w in tiles
                    if w.get("position")
                ]
                if bottoms:
                    clean["position"] = {"x": 0, "y": max(bottoms), "w": 6, "h": 4}
            if isinstance(clean.get("position"), dict):
                clean["position"] = _flow(clean["position"])
            chart = clean.get("chart")
            if isinstance(chart, dict):
                slim = _slim_chart(chart)
                clean["chart"] = slim["type"] if set(slim) == {"type"} else _flow(slim)
            _seq_append(tiles, CommentedMap(clean))
            pin_derived_ids(tiles)
            pin_positions(tiles, showing)

        return self._mutate(name, if_match, apply)

    def update_meta(
        self,
        name: str,
        if_match: str | None,
        title: str | None = None,
        description: str | None = None,
    ) -> str:
        """Set the title and set/clear the description (inserted right after title)."""

        def apply(doc) -> None:
            if title is not None:
                if not title.strip():
                    raise InvalidDashboardError("title cannot be empty")
                doc["title"] = title.strip()
            if description is not None:
                if description.strip():
                    if "description" in doc:
                        doc["description"] = description.strip()
                    else:
                        items = list(doc.keys())
                        doc.insert(items.index("title") + 1, "description", description.strip())
                else:
                    doc.pop("description", None)

        return self._mutate(name, if_match, apply)

    def update_filters(self, name: str, filters: list[dict[str, Any]], if_match: str | None) -> str:
        """Update the filters list in place, placed up top on first write.

        Matched by name into the nodes already in the file, and only the keys
        that actually changed are written — so a filter the author wrote in
        block form stays in block form, keeps its quoting, and keeps any comment
        attached to it. Rebuilding the list as flow items instead meant editing
        one filter reflowed every filter, burying the real change in noise. New
        filters are still written terse, since there is no authored form to keep.
        """

        def apply(doc) -> None:
            if not filters:
                doc.pop("filters", None)
                return
            existing = doc.get("filters")
            nodes = existing if isinstance(existing, list) else []
            # Only names that identify exactly one node on each side. Nothing
            # forbids two filters sharing a name, and matching them by it would
            # point both at one node — ruamel then writes an anchor and an alias,
            # and the next edit to either silently changes both.
            seen: dict[str, Any] = {}
            for node in nodes:
                if isinstance(node, dict) and node.get("name"):
                    seen[node["name"]] = None if node["name"] in seen else node
            incoming = [f.get("name") for f in filters]
            by_name = {
                name: node
                for name, node in seen.items()
                if node is not None and incoming.count(name) == 1
            }
            rendered = []
            for f in filters:
                filter_name = f.get("name")
                node = by_name.get(filter_name)
                authored = set(node) if node is not None else set()
                # Three fields the model fills in when the author omitted
                # them: a daterange's bind, an options_sql select's default, and
                # `type`, whose default is text. The editor round-trips the
                # parsed model, so writing them back turns every terse authored
                # filter into a verbose one on the first edit.
                #
                # Dropped only when the node does not already carry the key —
                # what the author wrote stays, whatever it says. Dropping on
                # equality instead reached into filters nobody edited: one save
                # deleted an authored `type: text` from a filter across the
                # file, which is the noise this exists to prevent, produced by
                # the fix for it.
                item = CommentedMap(
                    authored_filter({k: v for k, v in f.items() if v not in ([], "")}, authored)
                )
                bind = item.get("bind")
                if isinstance(bind, dict):
                    item["bind"] = _flow(bind)
                options = item.get("options")
                if isinstance(options, list):
                    item["options"] = _flow(options)
                if node is None:
                    item.fa.set_flow_style()
                    rendered.append(item)
                    continue
                # Write only what differs: an untouched key keeps the exact node
                # the author wrote, quoting and all.
                for key, value in item.items():
                    if node.get(key) != value:
                        node[key] = value
                for key in [k for k in node if k not in item]:
                    del node[key]
                rendered.append(node)
            if isinstance(existing, list):
                _rearrange(existing, rendered)
            else:
                keys = list(doc.keys())
                anchor = next(
                    (
                        keys.index(k) + 1
                        for k in ("source", "refresh", "description", "title")
                        if k in keys
                    ),
                    len(keys),
                )
                doc.insert(anchor, "filters", rendered)

        return self._mutate(name, if_match, apply)

    def delete_tile(self, name: str, tile_id: str, if_match: str | None) -> str:
        """Remove a tile by derived id, garbage-collecting its named query if now unused.

        An emptied `queries:` mapping is dropped so the dump does not leave
        `queries: {}` (#462).
        """

        def apply(doc) -> None:
            tiles = doc.get("tiles") or []
            pin_derived_ids(tiles)
            derived = derive_tile_ids(list(tiles))
            index = next((i for i, d in enumerate(derived) if d == tile_id), None)
            if index is None:
                raise InvalidDashboardError(f"unknown tile '{tile_id}'")
            removed = tiles[index]
            around = _pre_comments(tiles, index)
            trailing = _take_trailing(removed)
            tiles.pop(index)
            _carry_between(tiles, index, around + ([trailing] if trailing else []))
            query = removed.get("query")
            if query and not any(w.get("query") == query for w in tiles):
                queries = doc.get("queries")
                if isinstance(queries, dict):
                    queries.pop(query, None)
                    if not queries:
                        doc.pop("queries", None)

        return self._mutate(name, if_match, apply)


class WorkspaceStore(Store):
    """Serves several DashboardStores at once; dashboard names are 'repo/name'."""

    single_file = None

    def __init__(self, repos: dict[str, "DashboardStore"]) -> None:
        self.repos = repos

    def split(self, name: str) -> tuple["DashboardStore", str, str]:
        """Resolve 'repo/dashboard' to (store, repo, bare dashboard name)."""
        repo, _, rest = name.partition("/")
        if repo not in self.repos or not rest:
            known = ", ".join(sorted(self.repos))
            raise NotFoundError(
                f"no dashboard named '{name}' — workspace names are 'repo/dashboard' "
                f"(repos: {known})"
            )
        return self.repos[repo], repo, rest

    def add_repo(self, name: str, store: "DashboardStore") -> None:
        self.repos[name] = store

    def remove_repo(self, name: str) -> "DashboardStore":
        return self.repos.pop(name)

    def discover(self) -> dict[str, Path]:
        return {
            f"{repo}/{name}": path
            for repo, store in self.repos.items()
            for name, path in store.discover().items()
        }

    def path_for(self, name: str) -> Path:
        store, _, rest = self.split(name)
        return store.path_for(rest)

    def load(self, name: str) -> tuple[Dashboard, str, str]:
        store, _, rest = self.split(name)
        return store.load(rest)

    def save_text(self, name: str, text: str, if_match: str | None = None) -> str:
        store, _, rest = self.split(name)
        return store.save_text(rest, text, if_match)

    def delete(self, name: str) -> None:
        store, _, rest = self.split(name)
        store.delete(rest)

    def __getattr__(self, attr: str):
        if attr in (
            "update_positions",
            "upsert_tile",
            "update_meta",
            "update_filters",
            "delete_tile",
        ):

            def delegate(name: str, *args, **kwargs):
                store, _, rest = self.split(name)
                return getattr(store, attr)(rest, *args, **kwargs)

            return delegate
        raise AttributeError(attr)
