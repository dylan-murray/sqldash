"""Set text as SVG outlines in the vendored Geist fonts, for README assets.

GitHub shows README SVGs as images, so text has to be paths: the viewer has no
Geist, and a raster gets resampled at whatever width the README column is.
Glyphs are defined once in <defs> and placed with <use> in font units inside a
scaled group, which keeps files small. Build-only dependencies:

    uv run --with fonttools --with brotli --with uharfbuzz python scripts/<builder>.py
"""

from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "sqldash" / "static" / "vendor"


def _num(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


class Font:
    def __init__(self, path: Path, tag: str) -> None:
        import uharfbuzz as hb  # noqa: PLC0415
        from fontTools.ttLib import TTFont  # noqa: PLC0415 — build-only, see module docstring

        woff2 = TTFont(path)
        woff2.flavor = None
        raw = BytesIO()
        woff2.save(raw)
        self.raw = raw.getvalue()
        self.tag = tag
        self.hb = hb.Font(hb.Face(self.raw))
        self.upem = woff2["head"].unitsPerEm
        os2 = woff2["OS/2"]
        self.cap = os2.sCapHeight / self.upem
        self.xheight = os2.sxHeight / self.upem
        self.cmap = woff2.getBestCmap()
        self._instances: dict[int, tuple] = {}

    def has(self, ch: str) -> bool:
        return ord(ch) in self.cmap

    def instance(self, weight: int) -> tuple:
        from fontTools.ttLib import TTFont  # noqa: PLC0415
        from fontTools.varLib.instancer import instantiateVariableFont  # noqa: PLC0415

        if weight not in self._instances:
            ttf = instantiateVariableFont(TTFont(BytesIO(self.raw)), {"wght": weight})
            self._instances[weight] = (ttf.getGlyphSet(), ttf.getGlyphOrder())
        return self._instances[weight]

    def shape(self, text: str, weight: int) -> list[tuple[str, int, int, int]]:
        import uharfbuzz as hb  # noqa: PLC0415

        self.hb.set_variations({"wght": weight})
        buf = hb.Buffer()
        buf.add_str(text)
        buf.guess_segment_properties()
        hb.shape(self.hb, buf)
        _, order = self.instance(weight)
        return [
            (order[i.codepoint], p.x_advance, p.x_offset, p.y_offset)
            for i, p in zip(buf.glyph_infos, buf.glyph_positions, strict=True)
        ]

    def advance(self, text: str, weight: int) -> int:
        return sum(a for _, a, _, _ in self.shape(text, weight))


@dataclass
class Run:
    text: str
    fill: str
    weight: int | None = None
    font: Font | None = None


@dataclass
class Doc:
    """Collects glyph definitions while runs are placed; emit defs() once at the end."""

    defs: dict[str, str] = field(default_factory=dict)

    def glyph_id(self, font: Font, weight: int, name: str) -> str:
        gid = f"{font.tag}{weight}-{name}"
        if gid not in self.defs:
            from fontTools.pens.svgPathPen import SVGPathPen  # noqa: PLC0415

            glyph_set, _ = font.instance(weight)
            pen = SVGPathPen(glyph_set, ntos=_num)
            glyph_set[name].draw(pen)
            self.defs[gid] = pen.getCommands()
        return gid

    def defs_svg(self) -> str:
        return "".join(f'<path id="{gid}" d="{d}"/>' for gid, d in self.defs.items() if d)

    def width(self, font: Font, text: str, size: float, weight: int, tracking: float = 0) -> float:
        n = len(text)
        return font.advance(text, weight) * size / font.upem + tracking * size * max(n - 1, 0)

    def text(
        self,
        font: Font,
        text: str,
        x: float,
        y: float,
        size: float,
        weight: int,
        fill: str,
        anchor: str = "start",
        tracking: float = 0,
        opacity: float | None = None,
    ) -> str:
        """One run at baseline y; glyph x offsets are font units inside a scaled group."""
        if not text:
            return ""
        width = self.width(font, text, size, weight, tracking)
        if anchor == "middle":
            x -= width / 2
        elif anchor == "end":
            x -= width
        scale = size / font.upem
        track_units = tracking * font.upem
        uses = []
        pen_x = 0.0
        for name, adv, dx, dy in font.shape(text, weight):
            gid = self.glyph_id(font, weight, name)
            if self.defs[gid]:
                uses.append(f'<use href="#{gid}" x="{_num(pen_x + dx)}" y="{_num(dy)}"/>')
            pen_x += adv + track_units
        op = f' opacity="{opacity}"' if opacity is not None else ""
        return (
            f'<g transform="translate({_num(x)} {_num(y)}) scale({scale:.6f} {-scale:.6f})" '
            f'fill="{fill}"{op}>{"".join(uses)}</g>'
        )

    def runs(
        self, font: Font, runs: list[Run], x: float, y: float, size: float, weight: int
    ) -> str:
        out, pen = [], x
        for r in runs:
            f = r.font or font
            w = r.weight or weight
            out.append(self.text(f, r.text, pen, y, size, w, r.fill))
            pen += self.width(f, r.text, size, w)
        return "".join(out)

    def wrap(self, font: Font, text: str, size: float, weight: int, max_width: float) -> list[str]:
        lines, line = [], ""
        for word in text.split(" "):
            cand = f"{line} {word}" if line else word
            if line and self.width(font, cand, size, weight) > max_width:
                lines.append(line)
                line = word
            else:
                line = cand
        if line:
            lines.append(line)
        return lines


def sans() -> Font:
    return Font(VENDOR / "geist-var.woff2", "s")


def mono() -> Font:
    return Font(VENDOR / "geist-mono-var.woff2", "m")
