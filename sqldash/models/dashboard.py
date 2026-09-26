"""Dashboard YAML contract: the document, its tiles, filters, and grid layout."""

import re
import unicodedata
from collections.abc import Collection
from typing import Any, Literal, NamedTuple

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from sqldash.models.chart import ChartSpec, validate_format
from sqldash.models.semantics import Grain, MetricDef, MetricRef, RelationDef, validate_name
from sqldash.models.source import (
    DEFAULT_MARK,
    Source,
    SourceConfig,
    is_named_source_map,
    split_named_sources,
)

SIZE_PATTERN = re.compile(r"^(\d+)\s*[xX×]\s*(\d+)$")
DEFAULT_SIZE = (6, 4)
# A markdown tile is nearly always a section heading or a caption. Defaulting it
# to the chart footprint reserved 320px for one line of prose.
DEFAULT_TEXT_SIZE = (12, 1)


def slugify(text: str) -> str:
    """Lowercase text into a snake_case id fragment.

    Stays ASCII on purpose: a tile whose id equals slugify(title) never has
    the id written to disk, so widening this would silently rename every
    existing tile with an accented title. New dashboard names go through
    `dashboard_stem` instead.
    """
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")


def dashboard_stem(title: str) -> str:
    """File stem for a new dashboard, keeping letters, marks and digits from any script.

    An ASCII title gives exactly what slugify gives. Anything else keeps its
    characters instead of losing them (`Übersicht` stayed `bersicht`, and a
    title in Japanese had no stem at all). Marks are kept so scripts that
    write vowels as combining marks are not split apart, but only on a letter
    or digit: a mark with nothing to attach to is dropped like punctuation, so
    a marks-only title has no stem instead of one that renders as nothing.
    NFC comes after lowering, since lowering can decompose (`J̌` has no
    precomposed capital, so it lowers to j + caron, not the `ǰ` a file on disk
    would hold).
    """
    text = unicodedata.normalize("NFC", (title or "").lower())
    kept: list[str] = []
    for ch in text:
        kind = unicodedata.category(ch)[0]
        attached = kind == "M" and bool(kept) and kept[-1] != "_"
        kept.append(ch if kind in "LN" or attached else "_")
    return re.sub(r"_+", "_", "".join(kept)).strip("_")


def derive_tile_id(raw: dict) -> str | None:
    """Id for one raw tile dict: explicit id, else slug of title, query, or metric name."""
    if raw.get("id"):
        return str(raw["id"])
    for source in (raw.get("title"), raw.get("query")):
        if source:
            slug = slugify(str(source))
            if slug:
                return slug
    metric = raw.get("metric")
    if isinstance(metric, str):
        return metric
    if isinstance(metric, dict) and metric.get("name"):
        return str(metric["name"])
    return None


def derive_tile_ids(raw_tiles: list[dict]) -> list[str]:
    """Ids for a tile list in file order, deduped with `_2` suffixes — the one
    derivation shared by parsing and surgical file writes, so both target the same ids.

    Explicit `id:` values are reserved first. An untitled tile at index 1 would
    otherwise derive `tile_2` and then `setdefault` would keep a later
    `id: tile_2`, producing two tiles the store cannot mutate.
    """
    reserved = {str(raw["id"]) for raw in raw_tiles if raw.get("id")}
    taken = set(reserved)
    ids: list[str] = []
    for index, raw in enumerate(raw_tiles):
        if raw.get("id"):
            ids.append(str(raw["id"]))
            continue
        base = derive_tile_id(raw) or f"tile_{index + 1}"
        candidate = base
        n = 2
        while candidate in taken:
            candidate = f"{base}_{n}"
            n += 1
        taken.add(candidate)
        ids.append(candidate)
    return ids


class Position(BaseModel):
    """Explicit grid placement in layout units: column offset, row offset, width, height."""

    model_config = ConfigDict(extra="forbid")

    x: int = 0
    y: int = 0
    w: int = 6
    h: int = 4


class Layout(BaseModel):
    """Grid geometry the tiles are placed on: column count and pixel row height."""

    model_config = ConfigDict(extra="forbid")

    columns: int = 12
    row_height: int = 80


FilterType = Literal["date", "daterange", "select", "text", "number"]


class FilterDef(BaseModel):
    """A dashboard-level control whose value binds to `{{ name }}` query params.
    Daterange filters auto-bind `<name>_start`/`<name>_end` unless `bind:` is explicit;
    `options_sql` populates a select's choices from a query and defaults it to `all`."""

    model_config = ConfigDict(extra="forbid")

    name: str
    type: FilterType = "text"
    label: str | None = None
    default: Any = None
    options: list[Any] | None = None
    options_sql: str | None = None
    bind: dict[str, str] | None = None

    @model_validator(mode="after")
    def default_daterange_bind(self) -> "FilterDef":
        if self.options_sql is not None:
            if self.type != "select":
                raise ValueError(
                    f"filter '{self.name}': options_sql only applies to select filters"
                )
            if self.options is not None:
                raise ValueError(
                    f"filter '{self.name}': use either 'options' or 'options_sql', not both"
                )
            if self.default is None:
                self.default = "all"
        if self.type == "daterange":
            if self.bind is None:
                self.bind = {"start": f"{self.name}_start", "end": f"{self.name}_end"}
            elif set(self.bind) != {"start", "end"}:
                raise ValueError(
                    f"filter '{self.name}': daterange bind must have 'start' and 'end' keys"
                )
        return self


def authored_filter(raw: dict[str, Any], authored: Collection[str] = ()) -> dict[str, Any]:
    """A filter dict as an author would write it: without the keys FilterDef fills in.

    None-valued keys go, and so do the three values the model derives when they are
    omitted: a daterange's `<name>_start`/`<name>_end` bind, an options_sql select's
    `all` default, and `type: text`. Keys named in ``authored`` stay whatever they say.
    Reloading the result gives back an equal FilterDef.
    """
    item = {k: v for k, v in raw.items() if v is not None}
    name = item.get("name")
    if item.get("type") == "daterange":
        derived = {"start": f"{name}_start", "end": f"{name}_end"}
        if item.get("bind") == derived and "bind" not in authored:
            del item["bind"]
    elif item.get("options_sql") and item.get("default") == "all" and "default" not in authored:
        del item["default"]
    if item.get("type") == "text" and "type" not in authored:
        del item["type"]
    return item


TILE_LEVEL_DIMENSIONS_HINT = "dimensions: belongs inside metric: {name, dimensions, grain}"


class Tile(BaseModel):
    """One tile on the grid. Chart tiles take exactly one of `query`, `sql`, or `metric`
    (inline `sql:` is hoisted into the dashboard's `queries` at parse); text tiles take
    `markdown`. `id`, `position`, and the chart spec may all be omitted and derived."""

    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    type: Literal["chart", "text"] = "chart"
    title: str | None = None
    query: str | None = None
    sql: str | None = None
    metric: MetricRef | None = None
    grain: Grain | None = None
    compare: Literal["previous_period", "yoy"] | None = None
    source: str | None = None
    size: str | list[int] | None = None
    position: Position | None = None
    chart: ChartSpec | None = None
    format: str | dict[str, str] | None = None
    markdown: str | None = None

    @field_validator("metric", mode="before")
    @classmethod
    def coerce_metric_ref(cls, v):
        if isinstance(v, str):
            return MetricRef(name=v)
        return v

    @field_validator("chart", mode="before")
    @classmethod
    def coerce_chart_type(cls, v):
        if isinstance(v, str):
            return {"type": v}
        return v

    @model_validator(mode="after")
    def check_tile_shape(self) -> "Tile":
        label = self.id or self.title or "?"
        if (
            self.type == "chart"
            and self.markdown is not None
            and not (self.query or self.sql or self.metric)
        ):
            self.type = "text"
        if self.type == "chart":
            set_count = sum(1 for v in (self.query, self.sql, self.metric) if v)
            if set_count != 1:
                raise ValueError(
                    f"tile '{label}': chart tiles require exactly one of "
                    "'query', 'sql', or 'metric'"
                )
        if self.type == "text" and self.markdown is None:
            raise ValueError(f"tile '{label}': text tiles require 'markdown'")
        if self.grain is not None:
            if self.metric is None:
                raise ValueError(f"tile '{label}': 'grain' requires a 'metric'")
            if self.metric.grain is None:
                self.metric.grain = self.grain
            self.grain = None
        if self.compare is not None:
            if self.metric is None:
                raise ValueError(f"tile '{label}': 'compare' requires a 'metric'")
            if self.metric.compare is None:
                self.metric.compare = self.compare
            self.compare = None
        if self.format is not None:
            if isinstance(self.format, str):
                validate_format(self.format)
            else:
                for column, fmt in self.format.items():
                    validate_format(fmt, f"format for '{column}'")
            if self.chart is None:
                if self.metric is not None:
                    default_type = "area" if self.metric.grain else "big_number"
                else:
                    default_type = "table"
                self.chart = ChartSpec(type=default_type)
            if not self.chart.format:
                self.chart.format = self.format
            self.format = None
        return self

    def dimensions_hint(self) -> tuple[int, int]:
        """Footprint (w, h) from `position` or the `size:` shorthand, defaulting to 6x4."""
        if self.position is not None:
            return self.position.w, self.position.h
        if isinstance(self.size, str):
            match = SIZE_PATTERN.match(self.size.strip())
            if not match:
                raise ValueError(f"tile '{self.id}': size must look like '6x4', got {self.size!r}")
            return int(match.group(1)), int(match.group(2))
        if isinstance(self.size, list):
            if len(self.size) != 2:
                raise ValueError(f"tile '{self.id}': size list must be [w, h]")
            return int(self.size[0]), int(self.size[1])
        return DEFAULT_TEXT_SIZE if self.type == "text" else DEFAULT_SIZE


PAGE_TOKENS = frozenset(
    [
        "page",
        "page-glow",
        "surface",
        "surface-raised",
        "glass",
        "ink-1",
        "ink-2",
        "ink-muted",
        "grid-line",
        "baseline",
        "border",
        "border-strong",
        "accent",
        "accent-soft",
        "accent-glow",
        "accent-ink",
        *[f"series-{n}" for n in range(1, 9)],
    ]
)
PAGE_SELECTOR = ':root, :root[data-theme="light"], :root[data-theme="dark"]'
_THEME_SELECTORS = {"dark": ':root[data-theme="dark"]', "light": ':root[data-theme="light"]'}
_UNSAFE_TOKEN_VALUE = re.compile(r"[;{}<>\\@\r\n]|url\s*\(|expression\s*\(|/\*", re.IGNORECASE)
_PAGE_SELECTORS = frozenset([":root", "html", "body", ":scope"])
_PAGE_ELEMENT = re.compile(
    r"(?::root|html|body)(?:\[\s*data-theme\s*(?:=\s*([\"']?)(dark|light)\1(?:\s+[is])?\s*)?\])?"
)
_CUSTOM_TOKEN = re.compile(r"--[A-Za-z0-9_-]+")
_VAR_REFERENCE = re.compile(r"var\(\s*(--[A-Za-z0-9_-]+)\s*(?:,[^()]*)?\)")
_PAGE_COMPOUND = re.compile(_PAGE_ELEMENT.pattern, re.IGNORECASE)
_TOKEN_WRAPPER = re.compile(r"@(?:media|supports)\b")
_Place = tuple[tuple[str, ...], str | None]
_PLAIN_PAGE_PROPERTIES = {"background": "page", "background-color": "page", "color": "ink-1"}


class PageStyle(NamedTuple):
    page: str | None
    dashboard: str | None
    dropped: tuple[str, ...]
    ignored: tuple[str, ...] = ()
    unmatched: tuple[str, ...] = ()


def _strip_comments(text: str) -> str:
    out: list[str] = []
    i, quote = 0, None
    while i < len(text):
        char = text[i]
        if quote:
            if char == "\\" and i + 1 < len(text):
                out.append(text[i : i + 2])
                i += 2
                continue
            if char == quote:
                quote = None
            out.append(char)
        elif char in "\"'":
            quote = char
            out.append(char)
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = len(text) if end < 0 else end + 2
            continue
        else:
            out.append(char)
        i += 1
    return "".join(out)


def _statements(text: str) -> list[tuple[str, str | None]]:
    """Top-level CSS statements as (prelude, body): `body` is None for a bare
    declaration or block-less at-rule. Strings are honoured, so braces inside
    `content: "}"` do not open or close anything."""
    found: list[tuple[str, str | None]] = []
    i, start, depth, quote, open_at = 0, 0, 0, None, -1
    while i < len(text):
        char = text[i]
        if quote:
            if char == "\\":
                i += 1
            elif char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "{":
            if depth == 0:
                open_at = i
            depth += 1
        elif char == "}":
            if depth > 0:
                depth -= 1
                if depth == 0:
                    found.append((text[start:open_at].strip(), text[open_at + 1 : i]))
                    start = i + 1
            else:
                start = i + 1
        elif char == ";" and depth == 0:
            found.append((text[start:i].strip(), None))
            start = i + 1
        i += 1
    tail = text[start:].strip()
    if tail:
        found.append((tail, None))
    return [(prelude, body) for prelude, body in found if prelude or body]


def _brief(prelude: str, limit: int = 60) -> str:
    flat = " ".join(prelude.split())
    return flat if len(flat) <= limit else flat[:limit] + "..."


def _selector_parts(prelude: str) -> list[str]:
    return [" ".join(part.split()).lower() for part in prelude.split(",")]


def _page_themes(parts: list[str]) -> frozenset[str | None] | None:
    """Which themes a `:root`/`html`/`body` block applies in, `[data-theme]` variants
    included: `{None}` for both, otherwise `dark`, `light` or both. None when any part of
    the selector list is something else."""
    themes: set[str | None] = set()
    for part in parts:
        match = _PAGE_ELEMENT.fullmatch(part)
        if match is None:
            return None
        themes.add(match.group(2))
    return frozenset([None]) if None in themes else frozenset(themes)


def _is_page_selector(prelude: str) -> bool:
    parts = _selector_parts(prelude)
    return all(part in _PAGE_SELECTORS for part in parts) or _page_themes(parts) is not None


def _nested_page_selectors(body: str, where: str) -> list[str]:
    found = []
    for prelude, block in _statements(body):
        if block is None:
            continue
        if _is_page_selector(prelude):
            found.append(f"{prelude} inside {where}")
        elif prelude.startswith("@"):
            found.extend(_nested_page_selectors(block, prelude))
    return found


def _selector_list(prelude: str) -> list[str]:
    parts: list[str] = []
    depth, quote, start = 0, None, 0
    for i, char in enumerate(prelude):
        if quote:
            if char == quote and prelude[i - 1] != "\\":
                quote = None
        elif char in "\"'":
            quote = char
        elif char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(prelude[start:i])
            start = i + 1
    parts.append(prelude[start:])
    return parts


def _page_prefix(selector: str) -> tuple[bool, int] | None:
    """Where a leading run of `:root`/`html`/`body` compounds ends in a selector, and
    whether a descendant combinator follows it. None when the selector does not start
    with one, or already names `:scope` or `&` and so means what it says."""
    if ":scope" in selector.lower() or "&" in selector:
        return None
    pos, end = len(selector) - len(selector.lstrip()), 0
    while match := _PAGE_COMPOUND.match(selector, pos):
        after = match.end()
        if after < len(selector) and not (selector[after].isspace() or selector[after] in ">+~"):
            return None
        end = after
        pos = after + len(selector[after:]) - len(selector[after:].lstrip())
        if pos == len(selector) or selector[pos] in ">+~":
            return False, end
    return (True, end) if end else None


def _rewrite_selectors(prelude: str) -> tuple[str, list[str]]:
    """`:root[data-theme="dark"] .tile` inside `@scope` reads as `:scope :root… .tile`
    and matches nothing, so a descendant page prefix gets `:scope` put back after it.
    Other page prefixes are returned so lint can name them."""
    parts, stuck, changed = [], [], False
    for part in _selector_list(prelude):
        found = _page_prefix(part)
        if found is None:
            parts.append(part)
        elif found[0]:
            parts.append(f"{part[: found[1]]} :scope{part[found[1] :]}")
            changed = True
        else:
            parts.append(part)
            stuck.append(_brief(part))
    return (",".join(parts) if changed else prelude), stuck


def _needs_split(body: str) -> bool:
    for prelude, block in _statements(body):
        if block is None:
            continue
        if _TOKEN_WRAPPER.match(prelude):
            if _needs_split(block):
                return True
        elif not prelude.startswith("@") and (
            _page_themes(_selector_parts(prelude)) is not None
            or any(_page_prefix(part) for part in _selector_list(prelude))
        ):
            return True
    return False


def _scope_selector(themes: Collection[str | None]) -> str:
    return ", ".join(
        ":scope" if theme is None else f"{_THEME_SELECTORS[theme]} :scope"
        for theme in sorted(themes, key=str)
    )


def _unsafe_token_value(value: str) -> bool:
    return not value or len(value) > 400 or bool(_UNSAFE_TOKEN_VALUE.search(value))


def _resolve_references(value: str, names: dict[str, str]) -> str:
    resolved = value
    for _ in range(8):
        expanded = _VAR_REFERENCE.sub(lambda m: names.get(m.group(1), m.group(0)), resolved)
        if expanded == resolved:
            break
        if _unsafe_token_value(expanded):
            return value
        resolved = expanded
    return resolved


def split_page_tokens(css: str | None) -> PageStyle:
    """Design tokens written at page level in author CSS mean the page, not the dashboard:
    bare `--page: …;` lines outside any block, `--token` declarations inside `:root`,
    `html`, `body` (each optionally with a `[data-theme]` or `[data-theme="dark"|"light"]`
    condition) or `:scope` blocks, and a plain `background` or `color` on those blocks.
    Allowlisted `PAGE_TOKENS` are hoisted into a page-wide block, per theme when the block
    named one; everything else stays inside the dashboard scope, so a token set on one
    tile still scopes to that tile. Declarations that cannot leave the scope safely are
    dropped and named so lint can point at them.

    Any other `--token` in a `:root`/`html`/`body` block (or bare at the top level) is
    the author's own variable. It never reaches the page: it is re-emitted in the
    dashboard scope on `:scope`, behind the block's theme ancestor when it had one, where
    it was written, after the same value check. A page token that reads one through
    `var()` has the value substituted, since the variable does not exist at page level.

    A `@media` or `@supports` block holding a page block is split the same way, its
    prelude wrapped around both halves. Page selectors nested in any other at-rule are
    left alone and named.

    A rule or at-rule nested inside a page selector goes the same way: inside `:scope` it
    is kept verbatim in the dashboard scope, because `:scope` is the dashboard's own box
    and the nested selectors already mean what they say there. Inside `:root`, `html` or
    `body` it is dropped and named, because those never match inside `@scope
    (main.container)` and making them apply would mean rewriting the author's selectors.

    The one selector rewrite is narrow: a scoped rule whose selector starts with page
    compounds and a descendant combinator (`:root[data-theme="dark"] .tile`, `html body
    .x`) gets `:scope` inserted after that prefix, the rest kept byte for byte, because
    inside `@scope` the selector otherwise reads as `:scope :root… .tile` and matches
    nothing. A page prefix followed by `>`, `+` or `~`, or a page compound alone in a
    mixed list, is left as written and named in `unmatched`.

    What `:scope` keeps is emitted in the author's source order, declarations and nested
    blocks interleaved, because a bare `&` or a nested at-rule has the same specificity as
    the declarations around it and the later one wins. A token hoisted out of `:scope`
    also stays in place there, so a later `--accent` still beats an earlier `& { --accent
    }` on the dashboard itself instead of losing to it from `:root`."""
    if not css:
        return PageStyle(None, None, ())
    tokens: dict[_Place, dict[str, str]] = {}
    variables: dict[_Place, dict[str, str]] = {}
    dropped: list[str] = []
    ignored: list[str] = []
    unmatched: list[str] = []

    def take(declaration: str, plain: bool, places: list[_Place], scoped: list[str]) -> str | None:
        name, sep, value = declaration.partition(":")
        name, value = name.strip(), value.strip()
        key = _PLAIN_PAGE_PROPERTIES.get(name.lower()) if plain and sep else None
        if key is None:
            if not sep or not name.startswith("--"):
                if plain:
                    dropped.append(f"'{declaration}' is not a --token")
                return "dropped" if plain else None
            key = name[2:].lower()
            if key not in PAGE_TOKENS:
                if not plain:
                    return None
                if not _CUSTOM_TOKEN.fullmatch(name):
                    dropped.append(f"'{name}' is not a valid --token name")
                    return "dropped"
                if _unsafe_token_value(value):
                    dropped.append(f"{name} must be a plain value")
                    return "dropped"
                scoped.append(f"{name}: {value};")
                for place in places:
                    variables.setdefault(place, {})[name] = value
                return "scoped"
        if _unsafe_token_value(value):
            dropped.append(f"{name} must be a plain colour or gradient")
            return "dropped"
        for place in places:
            tokens.setdefault(place, {})[key] = value
        return "hoisted"

    def walk(statements: list[tuple[str, str | None]], wrappers: tuple[str, ...]) -> list[str]:
        kept: list[str] = []
        for prelude, body in statements:
            if body is None:
                scoped: list[str] = []
                if not prelude.startswith("--") or wrappers:
                    kept.append(f"{prelude};")
                elif take(prelude, True, [((), None)], scoped) == "scoped":
                    kept.append(f":scope {{ {scoped[0]} }}")
                continue
            parts = _selector_parts(prelude)
            themes = _page_themes(parts)
            if themes is not None or all(part in _PAGE_SELECTORS for part in parts):
                plain = themes is not None
                places = [(wrappers, theme) for theme in sorted(themes or [None], key=str)]
                chunks: list[str] = []
                scoped = []
                for inner, block in _statements(body):
                    if block is None:
                        taken = take(inner, plain, places, scoped) if inner else "dropped"
                        if taken is None or (taken == "hoisted" and not plain):
                            chunks.append(f"{inner};")
                    elif plain:
                        dropped.append(f"'{_brief(inner)}' is a rule nested in {_brief(prelude)}")
                    else:
                        chunks.append(f"{inner} {{{block}}}")
                if not plain:
                    ignored.extend(_nested_page_selectors(body, prelude))
                if chunks:
                    kept.append(f"{prelude} {{ {' '.join(chunks)} }}")
                if scoped:
                    kept.append(f"{_scope_selector(themes or [None])} {{ {' '.join(scoped)} }}")
                continue
            if _TOKEN_WRAPPER.match(prelude) and "</" not in prelude and _needs_split(body):
                inner_kept = walk(_statements(body), (*wrappers, prelude))
                if inner_kept:
                    kept.append(f"{prelude} {{ {' '.join(inner_kept)} }}")
                continue
            if prelude.startswith("@"):
                ignored.extend(_nested_page_selectors(body, prelude))
            else:
                prelude, stuck = _rewrite_selectors(prelude)
                unmatched.extend(stuck)
            kept.append(f"{prelude} {{{body}}}")
        return kept

    kept = walk(_statements(_strip_comments(css)), ())

    def visible(wrappers: tuple[str, ...], theme: str | None) -> dict[str, str]:
        names: dict[str, str] = {}
        for place in dict.fromkeys([((), None), ((), theme), (wrappers, None), (wrappers, theme)]):
            names.update(variables.get(place, {}))
        return names

    for (wrappers, theme), declared in list(tokens.items()):
        raw = dict(declared)
        names = visible(wrappers, theme)
        for key, value in raw.items():
            declared[key] = _resolve_references(value, names)
        if theme is not None:
            continue
        for other in _THEME_SELECTORS:
            themed = tokens.setdefault((wrappers, other), {})
            for key, value in raw.items():
                variant = _resolve_references(value, visible(wrappers, other))
                if key not in themed and variant != declared[key]:
                    themed[key] = variant

    blocks = []
    for wrappers in sorted(dict.fromkeys(wrappers for wrappers, _ in tokens), key=bool):
        for theme in (None, *_THEME_SELECTORS):
            declared = tokens.get((wrappers, theme))
            if not declared:
                continue
            declarations = " ".join(f"--{key}: {value};" for key, value in declared.items())
            block = f"{_THEME_SELECTORS.get(theme, PAGE_SELECTOR)} {{ {declarations} }}"
            for prelude in reversed(wrappers):
                block = f"{prelude} {{ {block} }}"
            blocks.append(block)
    return PageStyle(
        "\n".join(blocks) or None,
        "\n".join(kept) or None,
        tuple(dropped),
        tuple(ignored),
        tuple(unmatched),
    )


class Dashboard(BaseModel):
    """One YAML file, fully normalized at parse: missing tile ids are derived and deduped,
    inline `sql:` is hoisted into `queries`, tiles without `position` auto-flow
    left-to-right in file order below any explicitly positioned ones, and the one
    `source:` key is split into the default connection and the named ones."""

    model_config = ConfigDict(extra="forbid")

    def page_style(self) -> PageStyle:
        return split_page_tokens(self.css)

    title: str
    description: str | None = None
    refresh: str | None = None
    currency: str | None = None
    locale: str | None = None
    css: str | None = None
    source: SourceConfig
    sources: dict[str, SourceConfig] = {}
    default_source_name: str | None = Field(default=None, exclude=True)
    filters: list[FilterDef] = []
    queries: dict[str, str] = {}
    relations: dict[str, RelationDef] = {}
    metrics: dict[str, MetricDef] = {}
    layout: Layout = Field(default_factory=Layout)
    tiles: list[Tile] = Field(default_factory=list)

    @field_validator("currency")
    @classmethod
    def check_currency(cls, v):
        if v is not None and not re.match(r"^[A-Z]{3}$", v):
            raise ValueError(f"currency '{v}' must be an ISO 4217 code like USD, EUR, JPY")
        return v

    @model_validator(mode="before")
    @classmethod
    def merge_source_block(cls, data):
        """Split the one authoring key into the default connection and the named ones.

        `source:` is either a connection or a mapping of names to connections with
        one marked `default: true`. The legacy sibling `sources:` still parses,
        so every file written before the merge stays valid.
        """
        if not isinstance(data, dict):
            return data
        if "default_source_name" in data:
            raise ValueError(
                "default_source_name is not a dashboard key — mark the default connection "
                "with 'default: true' under source:"
            )
        block = data.get("source")
        if not is_named_source_map(block):
            if isinstance(block, dict) and DEFAULT_MARK in block:
                raise ValueError(
                    "source: 'default: true' marks one entry of a named source mapping — "
                    "a single connection is already the default"
                )
            return data
        if data.get("sources"):
            raise ValueError(
                "source: already names every connection — remove the separate 'sources:' block"
            )
        default_name, entries = split_named_sources(block)
        data = dict(data)
        data["source"] = entries[default_name]
        data["sources"] = {k: v for k, v in entries.items() if k != default_name}
        data["default_source_name"] = default_name
        return data

    @model_validator(mode="before")
    @classmethod
    def assign_tile_ids(cls, data):
        if isinstance(data, dict) and isinstance(data.get("tiles"), list):
            raw_tiles = [w for w in data["tiles"] if isinstance(w, dict)]
            explicit = [str(w["id"]) for w in raw_tiles if w.get("id")]
            if len(explicit) != len(set(explicit)):
                dupes = sorted({i for i in explicit if explicit.count(i) > 1})
                raise ValueError(f"duplicate tile ids: {', '.join(dupes)}")
            for raw, derived in zip(raw_tiles, derive_tile_ids(raw_tiles), strict=True):
                raw.setdefault("id", derived)
            assigned = [str(w["id"]) for w in raw_tiles]
            if len(assigned) != len(set(assigned)):
                dupes = sorted({i for i in assigned if assigned.count(i) > 1})
                raise ValueError(f"duplicate tile ids: {', '.join(dupes)}")
        return data

    @model_validator(mode="after")
    def normalize_and_check(self) -> "Dashboard":
        for tile in self.tiles:
            if tile.sql is not None:
                query_name = tile.query or tile.id
                existing = self.queries.get(query_name)
                if existing is not None and existing.strip() != tile.sql.strip():
                    raise ValueError(
                        f"tile '{tile.id}': inline sql collides with query '{query_name}'"
                    )
                self.queries[query_name] = tile.sql
                tile.query = query_name
                tile.sql = None

        cursor_x, cursor_y, row_h = 0, 0, 0
        explicit = [w for w in self.tiles if w.position is not None]
        if explicit:
            cursor_y = max(w.position.y + w.position.h for w in explicit)
        for tile in self.tiles:
            if tile.position is not None:
                continue
            w, h = tile.dimensions_hint()
            w = min(w, self.layout.columns)
            if cursor_x + w > self.layout.columns:
                cursor_x, cursor_y, row_h = 0, cursor_y + row_h, 0
            tile.position = Position(x=cursor_x, y=cursor_y, w=w, h=h)
            cursor_x += w
            row_h = max(row_h, h)

        for tile in self.tiles:
            if tile.query and tile.query not in self.queries:
                raise ValueError(f"tile '{tile.id}' references unknown query '{tile.query}'")
            if tile.source and self.named_source(tile.source) is None:
                named = ", ".join(sorted(self.source_names)) or "(none defined)"
                raise ValueError(
                    f"tile '{tile.id}' references unknown source '{tile.source}' "
                    f"— named sources: {named}"
                )
        for name, metric in self.metrics.items():
            validate_name(name, "metric name")
            if metric.relation and metric.relation not in self.relations:
                declared = ", ".join(sorted(self.relations)) or "none declared here"
                raise ValueError(
                    f"metric '{name}' references unknown relation '{metric.relation}' "
                    f"— an inline metric resolves 'relation:' against this dashboard's "
                    f"own 'relations:' ({declared}), never the project's metrics.yaml, "
                    f"because it runs on this dashboard's source. Declare "
                    f"'relations: {{{metric.relation}: {{table: ...}}}}' in this file, "
                    f"write 'table:' naming the table that relation points at (the "
                    f"relation's name is not always the table's), or keep the metric "
                    f"in metrics.yaml and reference it by name"
                )
        return self

    @property
    def source_names(self) -> list[str]:
        """Every name a tile's `source:` may use, the default's own name included."""
        names = list(self.sources)
        if self.default_source_name:
            names.append(self.default_source_name)
        return names

    def query_owner_source(self, query: str) -> str | None:
        """The `source:` name of the tiles that run a named query, so addressing
        the query by name answers what its tile shows. None means the default,
        which a tile naming the default outright also means.
        Raises ValueError when the owning tiles disagree: the browser runs each
        tile against its own source, a single run cannot, so the caller must pick."""
        owners = {
            None if not tile.source or tile.source == self.default_source_name else tile.source
            for tile in self.tiles
            if tile.query == query
        }
        if len(owners) > 1:
            named = sorted(name or "(dashboard default)" for name in owners)
            raise ValueError(
                f"query '{query}' is used by tiles on different sources ({', '.join(named)})"
            )
        return next(iter(owners), None)

    def named_source(self, name: str | None) -> Source | None:
        """The connection a tile's `source:` names — the default when it names nothing,
        or when it names the default by its own name. None means no such connection."""
        if not name or name == self.default_source_name:
            return self.source
        return self.sources.get(name)
