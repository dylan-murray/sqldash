"""Build assets/how-it-works-{light,dark}.svg: repo → sqldash → warehouse, text as outlines.

Vector so GitHub can show it at any column width without resampling. Run with the
build-only dependencies (see scripts/svgtext.py):

    uv run --with fonttools --with brotli --with uharfbuzz python scripts/build_how_it_works.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import svgtext
from svgtext import Doc, Font, Run

ROOT = Path(__file__).resolve().parent.parent
W, H = 946, 352
TOP = 40
COLS = ((32, 236), (328, 292), (680, 236))
PAD_X, PAD_Y = 20, 18

THEMES = {
    "light": {
        "page": "#f4f3ef",
        "surface": "#fdfdfc",
        "raised": "#ffffff",
        "ink1": "#0b0b0b",
        "ink2": "#52514e",
        "muted": "#898781",
        "border": "rgba(11,11,11,.08)",
        "border_strong": "rgba(11,11,11,.14)",
        "accent": "#2a78d6",
        "accent_soft": "rgba(42,120,214,.09)",
        "violet": "#4a3aa7",
        "green": "#1baf7a",
        "green_soft": "rgba(27,175,122,.14)",
        "shadow": '<feDropShadow dx="0" dy="3" stdDeviation="5" flood-color="#181610" flood-opacity=".08"/>',
        "glow": "",
        "mark_glow": "",
    },
    "dark": {
        "page": "#0c0c0d",
        "surface": "#161617",
        "raised": "#1d1d1f",
        "ink1": "#f5f5f4",
        "ink2": "#c3c2b7",
        "muted": "#8a8880",
        "border": "rgba(255,255,255,.09)",
        "border_strong": "rgba(255,255,255,.16)",
        "accent": "#3987e5",
        "accent_soft": "rgba(57,135,229,.14)",
        "violet": "#7c6bdc",
        "green": "#2fc98f",
        "green_soft": "rgba(47,201,143,.14)",
        "shadow": '<feDropShadow dx="0" dy="12" stdDeviation="14" flood-color="#000" flood-opacity=".45"/>',
        "glow": (
            '<radialGradient id="glow" gradientUnits="userSpaceOnUse" cx="473" cy="-35" r="450" '
            'gradientTransform="translate(0 -35) scale(1 .47) translate(0 35)">'
            '<stop offset="0" stop-color="rgba(57,135,229,.16)"/>'
            '<stop offset=".7" stop-color="rgba(57,135,229,0)"/></radialGradient>'
        ),
        "mark_glow": '<feDropShadow dx="0" dy="2" stdDeviation="4" flood-color="#3987e5" flood-opacity=".35"/>',
    },
}

FOLDER = '<path fill="currentColor" opacity=".55" d="M1.5 2.5A1.5 1.5 0 0 1 3 1h3.6l1.5 1.5H13A1.5 1.5 0 0 1 14.5 4v8A1.5 1.5 0 0 1 13 13.5H3A1.5 1.5 0 0 1 1.5 12z"/>'
DOCFILE = '<rect x="2.5" y="2.5" width="11" height="11" rx="2" fill="none" stroke="currentColor" stroke-width="1.2" opacity=".6"/><path d="M5 8h6M5 10.5h4" stroke="currentColor" stroke-width="1.2" opacity=".6"/>'
ROBOT = '<g fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" opacity=".7"><rect x="3" y="5" width="10" height="8" rx="2"/><path d="M8 2.5V5M1.5 8v2M14.5 8v2M6.5 11h3"/></g><g fill="currentColor" opacity=".7"><circle cx="8" cy="2" r="1"/><circle cx="6" cy="8" r=".8"/><circle cx="10" cy="8" r=".8"/></g>'

BARS = '<path d="M3 12.5V6M8 12.5V3.5M13 12.5V8" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" opacity=".7"/>'
DB = '<ellipse cx="8" cy="4" rx="5.5" ry="2.2" fill="none" stroke="currentColor" stroke-width="1.2" opacity=".6"/><path d="M2.5 4v8c0 1.2 2.5 2.2 5.5 2.2s5.5-1 5.5-2.2V4M2.5 8c0 1.2 2.5 2.2 5.5 2.2s5.5-1 5.5-2.2" fill="none" stroke="currentColor" stroke-width="1.2" opacity=".6"/>'
SPARK = '<path d="M3 11.5 6.5 7.5 9 9.5 13 4.5" fill="none" stroke="#fff" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>'


def icon(inner: str, x: float, y: float, color: str, size: float = 14) -> str:
    return (
        f'<svg x="{x}" y="{y}" width="{size}" height="{size}" viewBox="0 0 16 16" '
        f'color="{color}">{inner}</svg>'
    )


def card_repo(doc: Doc, sans: Font, mono: Font, t: dict, x: float, w: float) -> tuple[str, float]:
    inner = w - 2 * PAD_X
    cx, y = x + PAD_X, TOP + PAD_Y
    out = [doc.text(sans, "YOUR REPO", cx, y + 10, 11, 600, t["muted"], tracking=0.08)]
    y += 13 + 12
    rows = (
        (FOLDER, 0, ".sqldash/"),
        (DOCFILE, 22, "revenue.yaml"),
        (ROBOT, 22, "agents.yaml"),
        (BARS, 22, "metrics.yaml"),
    )
    for ic, indent, name in rows:
        out.append(icon(ic, cx + indent, y + 6.5, t["ink1"]))
        out.append(doc.text(mono, name, cx + indent + 23, y + 18.5, 14, 400, t["ink1"]))
        y += 27
    y += 12
    for line in doc.wrap(
        sans,
        "Dashboards, metrics, and agents—reviewed in pull requests and versioned with your code.",
        12.5,
        400,
        inner,
    ):
        out.append(doc.text(sans, line, cx, y + 12.5, 12.5, 400, t["ink2"]))
        y += 18.1
    return "".join(out), y - 18.1 + 16 + PAD_Y - TOP


def card_sqldash(
    doc: Doc, sans: Font, mono: Font, t: dict, x: float, w: float
) -> tuple[str, float]:
    inner = w - 2 * PAD_X
    cx, y = x + PAD_X, TOP + PAD_Y
    out = [
        f'<rect x="{cx}" y="{y}" width="30" height="30" rx="8" fill="url(#mark)" filter="url(#markglow)"/>',
        f'<svg x="{cx + 7}" y="{y + 7}" width="16" height="16" viewBox="0 0 16 16">{SPARK}</svg>',
        doc.text(sans, "sqldash", cx + 40, y + 21, 17, 600, t["ink1"], tracking=-0.02),
    ]
    y += 30 + 14
    for cmd, who, color, soft in (
        ("sqldash serve", "you, in the browser", t["accent"], t["accent_soft"]),
        ("sqldash mcp", "agents, over MCP", t["violet"], "rgba(124,107,220,.18)"),
    ):
        out.append(
            f'<rect x="{cx}" y="{y}" width="{inner}" height="33" rx="16.5" fill="{t["raised"]}" stroke="{t["border_strong"]}"/>'
        )
        out.append(
            f'<circle cx="{cx + 17}" cy="{y + 16.5}" r="7" fill="{soft}"/><circle cx="{cx + 17}" cy="{y + 16.5}" r="4" fill="{color}"/>'
        )
        out.append(doc.text(mono, cmd, cx + 30, y + 21.3, 13.5, 500, t["ink1"]))
        out.append(
            doc.text(sans, who, cx + inner - 12, y + 20.5, 11, 400, t["muted"], anchor="end")
        )
        y += 33 + 8
    y += 4
    for line in doc.wrap(
        sans,
        "Runs on your machine. Both read the same files, compile the same metrics, and bind every parameter.",
        12.5,
        400,
        inner,
    ):
        out.append(doc.text(sans, line, cx, y + 12.5, 12.5, 400, t["ink2"]))
        y += 18.1
    return "".join(out), y - 18.1 + 16 + PAD_Y - TOP


def card_warehouse(
    doc: Doc, sans: Font, mono: Font, t: dict, x: float, w: float
) -> tuple[str, float]:
    inner = w - 2 * PAD_X
    cx, y = x + PAD_X, TOP + PAD_Y
    out = [doc.text(sans, "YOUR WAREHOUSE", cx, y + 10, 11, 600, t["muted"], tracking=0.08)]
    y += 13 + 12
    out.append(icon(DB, cx, y + 2, t["ink1"]))
    lines = doc.wrap(sans, "Snowflake, BigQuery, Postgres …", 14, 400, inner - 23)
    for i, line in enumerate(lines):
        if i == len(lines) - 1 and line.endswith(" …"):
            base = line[:-2]
            out.append(
                doc.runs(
                    sans, [Run(base, t["ink1"]), Run(" …", t["muted"])], cx + 23, y + 13, 14, 400
                )
            )
        else:
            out.append(doc.text(sans, line, cx + 23, y + 13, 14, 400, t["ink1"]))
        y += 17
    y += 7
    for line in doc.wrap(sans, "or DuckDB and a folder of Parquet", 12.5, 400, inner):
        out.append(doc.text(sans, line, cx, y + 12, 12.5, 400, t["muted"]))
        y += 17
    y += 8
    tag = "your credentials, never in the file"
    size = 11.5 if doc.width(sans, tag, 11.5, 600) + 18 <= inner else 11
    tw = doc.width(sans, tag, size, 600) + 18
    out.append(
        f'<rect x="{cx}" y="{y}" width="{tw:.1f}" height="20" rx="10" fill="{t["green_soft"]}"/>'
    )
    out.append(doc.text(sans, tag, cx + 9, y + 14, size, 600, t["green"]))
    y += 20 + 12
    for line in doc.wrap(
        sans, "Every query is compiled and bound locally, then run as you.", 12.5, 400, inner
    ):
        out.append(doc.text(sans, line, cx, y + 12.5, 12.5, 400, t["ink2"]))
        y += 18.1
    return "".join(out), y - 18.1 + 16 + PAD_Y - TOP


def build(theme: str) -> str:
    t = THEMES[theme]
    sans, mono = svgtext.sans(), svgtext.mono()
    doc = Doc()
    cards = []
    for (x, w), fn in zip(COLS, (card_repo, card_sqldash, card_warehouse), strict=True):
        body, h = fn(doc, sans, mono, t, x, w)
        cards.append(
            f'<rect x="{x}" y="{TOP}" width="{w}" height="{h:.1f}" rx="14" fill="{t["surface"]}" '
            f'stroke="{t["border_strong"]}" filter="url(#shadow)"/>{body}'
        )
    arrows = (
        f'<path d="M269 150H327" stroke="{t["border_strong"]}" stroke-width="1.5" fill="none" marker-end="url(#head)"/>'
        + doc.text(sans, "reads", 298, 140, 11.5, 400, t["muted"], anchor="middle")
        + f'<path d="M621 133H679" stroke="{t["accent"]}" stroke-width="1.5" fill="none" marker-end="url(#head-accent)"/>'
        + f'<path d="M621 175H679" stroke="{t["violet"]}" stroke-width="1.5" fill="none" marker-end="url(#head-violet)"/>'
        + doc.text(sans, "bound SQL", 650, 158, 11.5, 400, t["muted"], anchor="middle")
    )
    foot = doc.text(
        sans,
        "a dashboard is a file · a metric is defined once · secrets stay local",
        W / 2,
        H - 22,
        12,
        400,
        t["muted"],
        anchor="middle",
    )
    glow = f'<rect width="{W}" height="{H}" fill="url(#glow)"/>' if t["glow"] else ""

    def marker(mid: str, color: str) -> str:
        return (
            f'<marker id="{mid}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
            f'orient="auto"><path d="M0 0L10 5 0 10z" fill="{color}"/></marker>'
        )

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
        f'role="img" aria-label="How sqldash works: YAML dashboards, metrics, and agent definitions in your repo, served locally '
        f'to the browser and to agents over MCP, querying your warehouse with your own credentials">'
        f"<defs>{doc.defs_svg()}"
        f'<linearGradient id="mark" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#2a78d6"/><stop offset="1" stop-color="#6b4ee6"/></linearGradient>'
        f'<filter id="shadow" x="-20%" y="-20%" width="140%" height="160%">{t["shadow"]}</filter>'
        f'<filter id="markglow" x="-50%" y="-50%" width="200%" height="200%">{t["mark_glow"] or "<feOffset/>"}</filter>'
        f"{t['glow']}{marker('head', t['border_strong'])}{marker('head-accent', t['accent'])}{marker('head-violet', t['violet'])}"
        f"</defs>"
        f'<rect width="{W}" height="{H}" fill="{t["page"]}"/>{glow}'
        f"{''.join(cards)}{arrows}{foot}</svg>\n"
    )


def main() -> None:
    for theme in THEMES:
        out = ROOT / "assets" / f"how-it-works-{theme}.svg"
        out.write_text(build(theme))
        print(f"wrote {out.relative_to(ROOT)} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
