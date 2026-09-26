"""Build assets/wordmark-{light,dark}.svg: the logo beside "sqldash" set in vendored Geist.

The name is converted to outlines so the SVG renders identically everywhere GitHub
shows it, with no font available to the viewer. Needs two libraries the project does
not depend on, so run it as:

    uv run --with fonttools --with brotli --with uharfbuzz python scripts/build_wordmark.py
"""

from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FONT = ROOT / "sqldash" / "static" / "vendor" / "geist-var.woff2"
LOGO = ROOT / "assets" / "logo.svg"

TEXT = "sqldash"
WEIGHT = 640
FONT_SIZE = 68
TRACKING = -0.035
LOGO_SIZE = 72
GAP = 18
PAD = 4
INK = {"light": "#1f2328", "dark": "#e6edf3"}


def shape(font_path: Path) -> tuple[list[tuple[str, float, float]], float, float]:
    import uharfbuzz as hb  # noqa: PLC0415 — build-only dependency, see docstring
    from fontTools.pens.boundsPen import BoundsPen  # noqa: PLC0415
    from fontTools.pens.svgPathPen import SVGPathPen  # noqa: PLC0415
    from fontTools.pens.transformPen import TransformPen  # noqa: PLC0415
    from fontTools.ttLib import TTFont  # noqa: PLC0415 — build-only dependency
    from fontTools.varLib.instancer import instantiateVariableFont  # noqa: PLC0415

    woff2 = TTFont(font_path)
    woff2.flavor = None
    raw = BytesIO()
    woff2.save(raw)
    hb_font = hb.Font(hb.Face(raw.getvalue()))
    hb_font.set_variations({"wght": WEIGHT})
    buf = hb.Buffer()
    buf.add_str(TEXT)
    buf.guess_segment_properties()
    hb.shape(hb_font, buf)

    ttf = instantiateVariableFont(TTFont(BytesIO(raw.getvalue())), {"wght": WEIGHT})
    upem = ttf["head"].unitsPerEm
    scale = FONT_SIZE / upem
    glyph_order = ttf.getGlyphOrder()
    glyph_set = ttf.getGlyphSet()

    x = 0.0
    paths: list[tuple[str, float, float]] = []
    bounds = BoundsPen(glyph_set)
    for info, pos in zip(buf.glyph_infos, buf.glyph_positions, strict=True):
        name = glyph_order[info.codepoint]
        gx = x + pos.x_offset * scale
        gy = -pos.y_offset * scale
        pen = SVGPathPen(glyph_set, ntos=lambda v: f"{v:.2f}".rstrip("0").rstrip("."))
        glyph_set[name].draw(TransformPen(pen, (scale, 0, 0, -scale, gx, gy)))
        glyph_set[name].draw(TransformPen(bounds, (scale, 0, 0, -scale, gx, gy)))
        paths.append((pen.getCommands(), gx, gy))
        x += pos.x_advance * scale + TRACKING * FONT_SIZE
    _, ymin, xmax, ymax = bounds.bounds
    return paths, xmax, (ymin + ymax) / 2


def logo_markup() -> str:
    svg = LOGO.read_text()
    inner = svg[svg.index(">") + 1 : svg.rindex("</svg>")].strip()
    scale = LOGO_SIZE / 24
    return f'<g transform="translate({PAD} {PAD}) scale({scale:g})">{inner}</g>'


def main() -> None:
    paths, text_width, text_mid_y = shape(FONT)
    text_x = PAD + LOGO_SIZE + GAP
    logo_mid_y = PAD + LOGO_SIZE / 2
    text_y = logo_mid_y - text_mid_y
    width = text_x + text_width + PAD
    height = LOGO_SIZE + 2 * PAD
    d = " ".join(p for p, _, _ in paths)
    for theme, ink in INK.items():
        out = ROOT / "assets" / f"wordmark-{theme}.svg"
        out.write_text(
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.1f} {height}" '
            f'width="{width:.1f}" height="{height}" role="img" aria-label="sqldash">\n'
            f"  {logo_markup()}\n"
            f'  <path fill="{ink}" transform="translate({text_x} {text_y:.2f})" d="{d}"/>\n'
            "</svg>\n"
        )
        print(f"wrote {out.relative_to(ROOT)} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
