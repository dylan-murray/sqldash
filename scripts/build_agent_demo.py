"""Build assets/demo-agent.svg: the README agent transcript as an animated SVG.

The text is a replay of one real Claude Code run against `sqldash mcp` (the file
it wrote is scripts/agent_demo_fulfillment.yaml). Five steps appear on a CSS
keyframe loop and the terminal scrolls as it fills, like the old APNG did, but as
outlines at any column width. Build-only dependencies (see scripts/svgtext.py):

    uv run --with fonttools --with brotli --with uharfbuzz python scripts/build_agent_demo.py
    ... --frame 3 out.svg   # a static preview of one step, for checking layout
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import svgtext
from svgtext import Doc

ROOT = Path(__file__).resolve().parent.parent
W, H = 946, 591
TERM = (47, 30, 852, 532)
BAR = 30
PAD_X, PAD_Y = 19, 16
SIZE, LH, CELL = 12.5, 20.25, 7.5
HOLDS = (1.8, 2.8, 3.0, 3.2, 3.8)

C = {
    "page": "#0c0c0d",
    "surface": "#131314",
    "ink1": "#f5f5f4",
    "ink2": "#c3c2b7",
    "muted": "#8a8880",
    "border": "rgba(255,255,255,.09)",
    "border_strong": "rgba(255,255,255,.16)",
    "accent": "#3987e5",
    "violet": "#7c6bdc",
    "green": "#2fc98f",
}
I1, I2, MU, AC, VI, GR = C["ink1"], C["ink2"], C["muted"], C["accent"], C["violet"], C["green"]

# (step, kind, runs). step 0 = always, 1 = only the first frame, k = from frame k on.
# kind: p = prompt, a = assistant prose, t = tool call, r = tool result, "" = plain, cursor.
LINES = [
    (0, "p", [("claude mcp add sqldash -- uvx sqldash mcp .", I1)]),
    (0, "", [("Added stdio MCP server sqldash", GR)]),
    (0, "", []),
    (0, "p", [('claude "build a fulfillment dashboard from the orders data"', I1)]),
    (1, "cursor", []),
    (2, "a", [("I'll look at the source schema and the existing metrics first.", I2)]),
    (2, "", []),
    (2, "t", [("sqldash", VI, 600), (" · get_schema", I1)]),
    (2, "r", [("orders (order_date DATE, region VARCHAR, category VARCHAR, amount DOUBLE)", MU)]),
    (2, "t", [("sqldash", VI, 600), (" · list_metrics", I1)]),
    (
        2,
        "r",
        [
            (
                "revenue SUM(amount) · order_count COUNT(*) · avg_order_value · cumulative_revenue · trailing_28d_revenue",
                MU,
            )
        ],
    ),
    (
        3,
        "a",
        [
            (
                "No ship or delivery columns here, so I'll build this as fulfillment volume and say so in the description. Validating a candidate before writing anything:",
                I2,
            )
        ],
    ),
    (3, "", []),
    (3, "t", [("sqldash", VI, 600), (" · validate_dashboard", I1)]),
    (3, "r", [("valid: true", GR), (" · errors: 0 · lint: 0 · ", MU), ("sql_checked: true", GR)]),
    (
        3,
        "r",
        [
            (
                "tiles: orders_to_fulfill metric 6x2 · fulfilled_order_value metric 6x2 · daily_fulfillment_volume metric 12x4 · fulfillment_by_region query 12x4",
                MU,
            )
        ],
    ),
    (4, "t", [("Write", VI, 600), (" .sqldash/fulfillment.yaml", I1)]),
    (4, "r", [("50 lines", MU)]),
    (4, "", []),
    (
        4,
        "a",
        [
            ("Written. Two big numbers on the governed ", I2),
            ("order_count", AC),
            (" and ", I2),
            ("revenue", AC),
            (" metrics with ", I2),
            ("compare: previous_period", AC),
            (
                ", a daily area chart, and a by-region table. Same source and filters as demo.yaml, so both dashboards slice identically.",
                I2,
            ),
        ],
    ),
    (4, "", []),
    (4, "p", [("sqldash lint", I1)]),
    (4, "", [("✓ fulfillment.yaml", GR), ("   0 errors, 0 warnings", MU)]),
    (5, "", []),
    (5, "p", [('claude "how did revenue by region do vs the previous period?"', I1)]),
    (5, "", []),
    (
        5,
        "t",
        [
            ("sqldash", VI, 600),
            (" · query_metric ", I1),
            (
                "revenue · dimensions: [region] · compare: previous_period · dashboard: fulfillment",
                MU,
            ),
        ],
    ),
    (5, "r", [("apac      95,281.60   prev    75,573.15   ", MU), ("▲ 26.1%", GR)]),
    (5, "r", [("eu       154,719.34   prev   117,405.84   ", MU), ("▲ 31.8%", GR)]),
    (5, "r", [("us       217,330.24   prev   175,736.18   ", MU), ("▲ 23.7%", GR)]),
    (
        5,
        "a",
        [
            (
                "Every region is up. us leads on volume, eu grew fastest. The numbers come from the governed ",
                I2,
            ),
            ("revenue", AC),
            (" metric, compiled and bound by sqldash, not SQL I wrote.", I2),
        ],
    ),
]
PREFIX = {"p": ("❯ ", AC), "a": ("● ", I1), "t": ("● ", VI), "r": ("⎿ ", MU)}
SPECIAL = {
    "❯": '<path d="M2 2.2 5.6 6.2 2 10.2" fill="none" stroke="{c}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>',
    "●": '<circle cx="3.75" cy="6.2" r="3" fill="{c}"/>',
    "⎿": '<path d="M1.6 .5V9.2H6.5" fill="none" stroke="{c}" stroke-width="1.2" stroke-linecap="round"/>',
    "▲": '<path d="M3.75 2 7 9H.5z" fill="{c}"/>',
    "✓": '<path d="M1.2 6.4 3.3 8.6 6.8 3.2" fill="none" stroke="{c}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>',
}


def wrap_runs(runs: list[tuple], first: int, rest: int) -> list[list[tuple]]:
    """Greedy word wrap over colored runs at fixed cell widths (columns)."""
    words: list[tuple] = []
    for run in runs:
        text, color, weight = run[0], run[1], run[2] if len(run) > 2 else 500
        parts = text.split(" ")
        for i, part in enumerate(parts):
            if i:
                words.append((" ", color, weight))
            if part:
                words.append((part, color, weight))
    lines: list[list[tuple]] = [[]]
    width, limit = 0, first
    for word in words:
        n = len(word[0])
        if width + n > limit and width and word[0] != " ":
            while lines[-1] and lines[-1][-1][0] == " ":
                lines[-1].pop()
            lines.append([])
            width, limit = 0, rest
        if word[0] == " " and width == 0 and len(lines) > 1:
            continue
        lines[-1].append(word)
        width += n
    return lines


def piece(doc: Doc, mono, text: str, color: str, weight: int, x: float, y: float) -> str:
    out, col, buf = [], 0, ""

    def flush() -> None:
        nonlocal buf
        if buf:
            out.append(doc.text(mono, buf, x + (col - len(buf)) * CELL, y, SIZE, weight, color))
            buf = ""

    for ch in text:
        if ch in SPECIAL:
            flush()
            out.append(
                f'<g transform="translate({x + col * CELL:.2f} {y - 9.5:.2f})">'
                f"{SPECIAL[ch].format(c=color)}</g>"
            )
        else:
            buf += ch
        col += 1
    flush()
    return "".join(out)


def build(static_frame: int | None = None) -> str:
    mono, sans = svgtext.mono(), svgtext.sans()
    doc = Doc()
    tx, ty, tw, th = TERM
    x0, y0 = tx + PAD_X, ty + BAR + PAD_Y
    cols = int((tw - 2 * PAD_X) // CELL)
    groups: dict[int, list[str]] = {k: [] for k in range(6)}
    row, ends = 0, {}
    for step, kind, runs in LINES:
        if kind == "cursor":
            y = y0 + row * LH
            groups[1].append(
                f'<rect x="{x0}" y="{y + 3:.2f}" width="6" height="14" rx="1" fill="{I1}"/>'
            )
            row += 1
            continue
        indent = 2.2 if kind == "r" else 0
        prefix = PREFIX.get(kind)
        first = cols - int(indent) - (2 if prefix else 0)
        rest = cols - int(indent)
        wrapped = wrap_runs(runs, first, rest) if runs else [[]]
        for i, line in enumerate(wrapped):
            y = y0 + row * LH + 14
            col = indent
            if i == 0 and prefix:
                groups[step].append(piece(doc, mono, prefix[0], prefix[1], 500, x0 + col * CELL, y))
                col += 2
            for text, color, weight in line:
                groups[step].append(piece(doc, mono, text, color, weight, x0 + col * CELL, y))
                col += len(text)
            row += 1
        ends[step] = max(ends.get(step, 0), row)
    visible_h = th - BAR - 2 * PAD_Y
    scroll = []
    last = 0
    for k in range(1, 6):
        last = max(last, ends.get(k, 0), ends.get(0, 0))
        scroll.append(max(0.0, last * LH - visible_h))

    total = sum(HOLDS)
    marks = [sum(HOLDS[:i]) / total * 100 for i in range(1, 5)]
    css = [f".body{{animation:scroll {total}s steps(1,end) infinite}}"]
    frames = ["0%{transform:translateY(0)}"]
    for i, m in enumerate(marks):
        frames.append(f"{m:.2f}%{{transform:translateY(-{scroll[i + 1]:.1f}px)}}")
    css.append("@keyframes scroll{" + "".join(frames) + "}")
    css.append(
        f".s1{{animation:s1 {total}s steps(1,end) infinite}}@keyframes s1{{0%{{opacity:1}}{marks[0]:.2f}%{{opacity:0}}}}"
    )
    for k in range(2, 6):
        css.append(
            f".s{k}{{opacity:0;animation:s{k} {total}s steps(1,end) infinite}}"
            f"@keyframes s{k}{{0%{{opacity:0}}{marks[k - 2]:.2f}%{{opacity:1}}}}"
        )
    style = "".join(css)
    if static_frame is not None:
        k = static_frame
        style = (
            f".s1{{opacity:{1 if k == 1 else 0}}}"
            + "".join(f".s{j}{{opacity:{1 if j <= k else 0}}}" for j in range(2, 6))
            + f".body{{transform:translateY(-{scroll[k - 1]:.1f}px)}}"
        )

    bar = (
        f'<line x1="{tx}" y1="{ty + BAR}" x2="{tx + tw}" y2="{ty + BAR}" stroke="{C["border"]}"/>'
        f'<rect x="{tx + 12}" y="{ty + 8}" width="14" height="14" rx="4" fill="url(#mark)"/>'
        f'<svg x="{tx + 15}" y="{ty + 11}" width="8" height="8" viewBox="0 0 16 16"><path d="M3 11.5 6.5 7.5 9 9.5 13 4.5" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>'
        + doc.runs(
            sans,
            [
                svgtext.Run("sqldash ", MU),
                svgtext.Run("/", "rgba(138,136,128,.5)"),
                svgtext.Run(" acme_dashboards ", MU),
                svgtext.Run("·", "rgba(138,136,128,.5)"),
                svgtext.Run(" claude code", MU),
            ],
            tx + 34,
            ty + 19,
            11.5,
            400,
        )
        + doc.text(mono, "sqldash mcp .", tx + tw - 14, ty + 19, 10.5, 500, MU, anchor="end")
    )
    body = "".join(
        f'<g class="s{k}">{"".join(v)}</g>' if k else "".join(v) for k, v in groups.items()
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" role="img" '
        f'aria-label="An agent building a dashboard over MCP: it reads the schema and metrics, validates the YAML, and writes the file">'
        f"<style>{style}</style><defs>{doc.defs_svg()}"
        f'<linearGradient id="mark" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#2a78d6"/><stop offset="1" stop-color="#6b4ee6"/></linearGradient>'
        f'<radialGradient id="glow" gradientUnits="userSpaceOnUse" cx="473" cy="-59" r="470" gradientTransform="translate(0 -59) scale(1 .53) translate(0 59)"><stop offset="0" stop-color="rgba(57,135,229,.16)"/><stop offset=".7" stop-color="rgba(57,135,229,0)"/></radialGradient>'
        f'<filter id="shadow" x="-20%" y="-20%" width="140%" height="160%"><feDropShadow dx="0" dy="18" stdDeviation="24" flood-color="#000" flood-opacity=".55"/></filter>'
        f'<clipPath id="clip"><rect x="{tx}" y="{ty + BAR + 1}" width="{tw}" height="{th - BAR - 1}"/></clipPath>'
        f"</defs>"
        f'<rect width="{W}" height="{H}" fill="{C["page"]}"/><rect width="{W}" height="{H}" fill="url(#glow)"/>'
        f'<rect x="{tx}" y="{ty}" width="{tw}" height="{th}" rx="14" fill="{C["surface"]}" stroke="{C["border_strong"]}" filter="url(#shadow)"/>'
        f'{bar}<g clip-path="url(#clip)"><g class="body">{body}</g></g></svg>\n'
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frame", type=int, help="write a static preview of this step (1-5)")
    parser.add_argument("out", nargs="?", type=Path, default=ROOT / "assets" / "demo-agent.svg")
    args = parser.parse_args()
    args.out.write_text(build(args.frame))
    print(f"wrote {args.out} ({args.out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
