"""Capture the README demo GIF.

Builds a throwaway project, drives the real UI in headless chromium, and
assembles the frames. Each frame is written as a full opaque RGB image with
GIF disposal=2 — partial frames or transparency produce the ghosting artifact
where two pages bleed through each other.

    uv run python scripts/capture_demo.py [--out assets/demo.png]

Format is chosen by the output suffix. Default is APNG (.png): full 24-bit
colour and it renders anywhere a PNG does. `.webp` is ~7x smaller at the same
colour depth; `.gif` is 8-bit and bands badly on the UI's dark gradients.
"""

import argparse
import io
import shutil
import tempfile
import time
from pathlib import Path

from PIL import Image

from scripts.showcase_repos import CHANNEL_MIX, REPOS, repo_dashboard
from sqldash.scaffold import create_demo
from sqldash.server import create_app
from sqldash.snapshot import _start_server

WIDTH, HEIGHT = 1280, 800
README_WIDTH = 946
SCALE = README_WIDTH * 2 / WIDTH
HOLD = 1.6


def _shot(page, frames, hold=HOLD):
    """One frame held for `hold` seconds — duplicate frames confuse GIF optimizers."""
    frames.append((page.screenshot(type="png"), int(hold * 1000)))


def capture(out: Path) -> None:
    from playwright.sync_api import sync_playwright  # noqa: PLC0415 — optional [snapshot] extra

    work = Path(tempfile.mkdtemp(prefix="sqldash-gif-"))
    try:
        demo_root = work / "acme_dashboards"
        create_demo(demo_root)
        for slug, title, description, seed in (
            ("marketing_funnel", "Marketing Funnel", "Top-of-funnel through first purchase.", 7),
            ("inventory_health", "Inventory Health", "Stock coverage across warehouses.", 11),
        ):
            (demo_root / ".sqldash" / f"{slug}.yaml").write_text(
                repo_dashboard(title, description, seed)
            )
        workspace = [("acme_dashboards", demo_root / ".sqldash")]
        for index, (repo, dashboards) in enumerate(REPOS.items()):
            root = work / repo / ".sqldash"
            root.mkdir(parents=True)
            for offset, (slug, title, description) in enumerate(dashboards):
                seed = 3 + index * 5 + offset
                body = (
                    CHANNEL_MIX
                    if slug == "channel_mix"
                    else repo_dashboard(title, description, seed)
                )
                (root / f"{slug}.yaml").write_text(body)
            workspace.append((repo, root))

        app = create_app(allowed_hosts=["127.0.0.1", "localhost"], workspace=workspace)
        server, thread, port = _start_server(app)
        base = f"http://127.0.0.1:{port}"
        frames: list[tuple[bytes, int]] = []

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_context(
                viewport={"width": WIDTH, "height": HEIGHT},
                device_scale_factor=SCALE,
                color_scheme="dark",
            ).new_page()

            page.goto(f"{base}/", wait_until="networkidle")
            page.wait_for_selector("#browser-filter", state="visible")
            _collapse_all_but_first(page)
            time.sleep(0.6)
            _shot(page, frames, 2.4)

            page.goto(f"{base}/d/acme_dashboards/demo", wait_until="networkidle")
            _wait_tiles(page)
            time.sleep(0.8)
            _shot(page, frames, 2.6)

            _new_tile_frame(page, base, frames)

            page.goto(f"{base}/d/growth_marketing/channel_mix", wait_until="networkidle")
            _wait_tiles(page)
            time.sleep(0.8)
            _shot(page, frames, 2.6)

            page.goto(f"{base}/m/acme_dashboards/revenue", wait_until="networkidle")
            _wait_tiles(page)
            time.sleep(0.8)
            _shot(page, frames, 2.8)

            browser.close()

        server.should_exit = True
        thread.join(timeout=10)
        _assemble(frames, out)
    finally:
        shutil.rmtree(work, ignore_errors=True)


NEW_TILE_SQL = """SELECT region,
       SUM(amount)  AS revenue,
       COUNT(*)     AS orders
FROM orders
WHERE order_date >= CURRENT_DATE - INTERVAL 60 DAY
GROUP BY 1
ORDER BY revenue DESC"""


def _new_tile_frame(page, base: str, frames: list) -> None:
    """The '+ New tile' page with a query entered, run, and previewed as a bar
    chart, scrolled so the result table, the preview, and the add row are all
    in frame. The SQL goes in through ace's API: typed newlines accept the
    autocomplete popup's suggestion and mangle the query. Nothing is added; the
    frame is the editor, the dashboard frames stay as scaffolded."""
    page.goto(f"{base}/d/acme_dashboards/demo/query", wait_until="load")
    page.wait_for_selector("#qb-preview", timeout=30_000)
    page.evaluate(
        "sql => ace.edit(document.querySelector('.ace_editor')).setValue(sql, -1)", NEW_TILE_SQL
    )
    page.locator("#run-btn").click()
    page.wait_for_selector("#qb-preview .chart-mount, #qb-preview table", timeout=30_000)
    page.locator("#qb-type button", has_text="Bar").first.click()
    page.locator("#qb-title").fill("Revenue by region")
    page.wait_for_function("() => !document.getElementById('qb-add').disabled", timeout=5_000)
    page.evaluate("() => document.getElementById('qb-add').scrollIntoView({block: 'end'})")
    time.sleep(0.8)
    _shot(page, frames, 3.2)


def _collapse_all_but_first(page) -> None:
    """Collapse every dashboard repo folder but the first, so the frame shows the
    folder structure and the expanded metrics list together rather than one long
    run of dashboard rows. Metric folders stay open — the metric names are the
    semantic-layer story."""
    folders = page.query_selector_all("#dash-browser .dfolder")
    for folder in folders[1:]:
        folder.click()
        time.sleep(0.05)
    page.evaluate("() => window.scrollTo(0, 0)")


def _wait_tiles(page, timeout=30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pending = page.evaluate(
            "() => document.querySelectorAll("
            "'.tile-status .skeleton, .widget-status .skeleton').length"
        )
        if pending == 0:
            return
        time.sleep(0.2)


def _assemble(frames: list[tuple[bytes, int]], out: Path) -> None:
    """GIF is 8-bit: smooth dark gradients band badly. WebP and APNG keep the
    full 24-bit render, so pick the encoder from the output suffix."""
    suffix = out.suffix.lower()
    images, durations = [], []
    for raw, hold in frames:
        images.append(Image.open(io.BytesIO(raw)).convert("RGB"))
        durations.append(hold)
    out.parent.mkdir(parents=True, exist_ok=True)
    if suffix == ".webp":
        images[0].save(
            out,
            save_all=True,
            append_images=images[1:],
            duration=durations,
            loop=0,
            quality=92,
            method=6,
        )
    elif suffix == ".png":
        images[0].save(
            out,
            save_all=True,
            append_images=images[1:],
            duration=durations,
            loop=0,
            disposal=1,
        )
    else:
        _assemble_gif(images, durations, out)
    print(f"wrote {out} — {len(images)} frames, {out.stat().st_size / 1_000_000:.2f} MB")


def _assemble_gif(images: list, durations: list[int], out: Path) -> None:
    """FASTOCTREE keeps rare accent colours (the brand mark, row icons) that
    MEDIANCUT and ADAPTIVE merge into the dominant greys."""
    quantized = [im.quantize(colors=256, method=Image.FASTOCTREE) for im in images]
    quantized[0].save(
        out,
        save_all=True,
        append_images=quantized[1:],
        duration=durations,
        loop=0,
        disposal=2,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("assets/demo.png"),
        help="Output path; the suffix picks the encoder (.png=APNG, .webp, .gif)",
    )
    args = parser.parse_args()
    capture(args.out)
