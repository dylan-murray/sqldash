"""Record the README walkthrough: one continuous session through the product.

Library → dashboard → filter → + Explore, a tile from SQL → three AI Studio turns
(theme, headings, a second theme) → the shared metric's page. The labeled Demo
agent is a deterministic local subprocess, not an LLM response; the file edits
and the live refresh go through the real Studio lifecycle. See assets/README.md
for re-recording.
"""

import argparse
import io
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import duckdb
from PIL import Image, ImageDraw
from ruamel.yaml import YAML

from scripts.showcase_repos import write_repos
from sqldash.server import create_app
from sqldash.snapshot import _start_server
from sqldash.studio.entrypoints import AgentEntrypoint, save_entrypoint

ROOT = Path(__file__).resolve().parent.parent
WIDTH, HEIGHT = 1600, 1000
SCALE = 2
FPS_MS = 100
MOVE_MS = 80
NEW_TILE_SQL = (
    "SELECT dayname(order_date) AS weekday, COUNT(*) AS orders\n"
    "FROM orders\nGROUP BY 1\nORDER BY MIN(dayofweek(order_date))"
)
SET_SQL = "(editor, sql) => ace.edit(editor).setValue(sql, 1)"
WORKSPACE_READY = (
    "() => { const frame = document.querySelector('#workspace-frames iframe');"
    " const doc = frame && frame.contentDocument;"
    " return !document.getElementById('library-save').disabled"
    " && document.getElementById('schema-message').hidden"
    " && !document.getElementById('workspace-role').selectedOptions[0].text.startsWith('Loading')"
    " && doc && doc.querySelector('.workspace-resultbar') && doc.querySelector('.ace_editor'); }"
)
SCROLL_ELEMENT = "(el, top) => { el.scrollTop = top; }"
SCROLL_RANGE = (
    "() => { const m = document.querySelector('main.container');"
    " const d = document.scrollingElement;"
    " const inner = m.scrollHeight - m.clientHeight, outer = d.scrollHeight - d.clientHeight;"
    " return inner > outer ? [m.scrollTop, inner] : [d.scrollTop, outer]; }"
)
SCROLL_TO = (
    "top => { const m = document.querySelector('main.container');"
    " m.scrollTop = top; document.scrollingElement.scrollTop = top; }"
)
AGENT = ROOT / "scripts" / "studio_demo_agent.py"
REPO = "commerce"
GALLERY = (
    ("neon", "dark", "Neon observatory", "Dark and glassy, violet accents, cards that glow."),
    ("quartz", "light", "Rose quartz", "Softer. Rose and lavender on paper, a serif title."),
    ("citrus", "dark", "Electric citrus", "Brighter and punchier: acid lime on black."),
    ("broadsheet", "light", "Morning broadsheet", "Editorial. Cream newsprint, black ink, serif."),
    ("ember", "dark", "Ember", "Warm it up: sunset oranges and coral on deep plum."),
    (
        "terminal",
        "dark",
        "Amber terminal",
        "Make it feel like a terminal: black, amber, monospace.",
    ),
)


def ease(t):
    return t * t * (3 - 2 * t)


class Recording:
    """Frames with a drawn cursor, eased pointer motion and typing."""

    def __init__(self, page):
        self.page = page
        self.frames = []
        self.durations = []
        self.cursor = (WIDTH * 0.55, HEIGHT * 0.6)

    def _draw_cursor(self, image):
        x, y = self.cursor[0] * SCALE, self.cursor[1] * SCALE
        pts = [(0, 0), (0, 17), (4.5, 13), (7.5, 20), (10.5, 18.5), (7.5, 11.5), (13, 11.5)]
        pts = [(px * SCALE, py * SCALE) for px, py in pts]
        draw = ImageDraw.Draw(image)
        shadow = [(x + px + 1, y + py + 2) for px, py in pts]
        draw.polygon(shadow, fill=(0, 0, 0, 90))
        draw.polygon([(x + px, y + py) for px, py in pts], fill="white", outline="black")
        return image

    def frame(self, milliseconds=FPS_MS, cursor=True):
        raw = self.page.screenshot(type="png")
        image = Image.open(io.BytesIO(raw)).convert("RGB")
        if cursor:
            self._draw_cursor(image)
        self.frames.append(image)
        self.durations.append(milliseconds)
        return image

    def hold(self, seconds):
        self.frame(round(seconds * 1000))

    def animate(self, seconds):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            start = time.monotonic()
            self.frame()
            self.page.wait_for_timeout(max(0, FPS_MS - (time.monotonic() - start) * 1000))

    def move(self, x, y, seconds=0.5):
        sx, sy = self.cursor
        steps = max(3, round(seconds * 1000 / MOVE_MS))
        for i in range(1, steps + 1):
            k = ease(i / steps)
            self.cursor = (sx + (x - sx) * k, sy + (y - sy) * k)
            self.page.mouse.move(*self.cursor)
            self.frame(MOVE_MS)

    def locate(self, target):
        return self.page.locator(target) if isinstance(target, str) else target

    def click(self, selector, offset=(0.5, 0.5), seconds=0.5, settle=0.3, beat=0.2):
        box = self.locate(selector).first.bounding_box()
        x = box["x"] + box["width"] * offset[0]
        y = box["y"] + box["height"] * offset[1]
        self.move(x, y, seconds)
        self.frame(round(beat * 1000))
        self.page.mouse.click(x, y)
        if settle:
            self.page.wait_for_timeout(round(settle * 1000))
            self.frame()

    def scroll_to_top(self, seconds=0.6):
        start, _ = self.page.evaluate(SCROLL_RANGE)
        self._scroll(start, 0, seconds)

    def scroll_to_bottom(self, seconds=0.9):
        start, target = self.page.evaluate(SCROLL_RANGE)
        self._scroll(start, target, seconds)

    def scroll_element(self, element, seconds=0.6):
        start, target = element.evaluate("el => [el.scrollTop, el.scrollHeight - el.clientHeight]")
        steps = max(3, round(seconds * 1000 / MOVE_MS))
        for i in range(1, steps + 1):
            element.evaluate(SCROLL_ELEMENT, start + (target - start) * ease(i / steps))
            self.page.wait_for_timeout(20)
            self.frame(MOVE_MS)

    def _scroll(self, start, target, seconds):
        steps = max(3, round(seconds * 1000 / MOVE_MS))
        for i in range(1, steps + 1):
            k = ease(i / steps)
            self.page.evaluate(SCROLL_TO, start + (target - start) * k)
            self.page.wait_for_timeout(20)
            self.frame(MOVE_MS)

    def type(self, selector, value, chunk=4):
        field = self.locate(selector)
        field.fill("")
        for end in range(0, len(value), chunk):
            field.press_sequentially(value[end : end + chunk], delay=12)
            self.frame(110)

    def copy_from(self, other, count, milliseconds=FPS_MS):
        self.frames.extend(other.frames[-count:])
        self.durations.extend([milliseconds] * count)

    def save(self, output):
        common = {"save_all": True, "duration": self.durations, "loop": 0}
        first, rest = self.frames[0], self.frames[1:]
        if output.suffix == ".gif":
            method = Image.Quantize.FASTOCTREE
            quantized = [frame.quantize(colors=256, method=method) for frame in self.frames]
            quantized[0].save(output, append_images=quantized[1:], disposal=2, **common)
        elif output.suffix == ".webp":
            first.save(output, append_images=rest, quality=80, method=6, **common)
        else:
            first.save(output, append_images=rest, **common)
        seconds = sum(self.durations) / 1000
        size = output.stat().st_size / 1e6
        print(f"{output}: {len(self.frames)} frames, {seconds:.1f}s, {size:.2f} MB")


def weekday_size(project):
    doc = YAML().load((project / "revenue.yaml").read_text())
    tile = next(t for t in doc["tiles"] if str(t.get("title", "")).startswith("Orders by weekday"))
    return tile["position"]["w"], tile["position"]["h"]


def ready(page):
    page.wait_for_function("!document.querySelector('.tile-status .skeleton')", timeout=30000)
    page.wait_for_timeout(400)
    assert not page.locator(".tile-status .err").all_text_contents()


def git_command(project):
    git = ["git", "-C", str(project)]
    return [*git, "-c", "user.name=Demo", "-c", "user.email=demo@example.invalid"]


def prepare_project(work):
    project = work / "studio"
    shutil.copytree(ROOT / "examples/studio", project)
    yaml = YAML()
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.width = 4096
    metrics_path = project / "metrics.yaml"
    metrics = yaml.load(metrics_path.read_text())
    with duckdb.connect(str(project / "studio.duckdb")) as connection:
        connection.execute("CREATE TABLE orders AS " + metrics["relations"]["orders"]["sql"])
    metrics["relations"]["orders"] = {"table": "orders"}
    for path in sorted(project.glob("*.yaml")):
        doc = metrics if path == metrics_path else yaml.load(path.read_text())
        doc["source"] = {"type": "duckdb", "database": "studio.duckdb"}
        with path.open("w") as stream:
            yaml.dump(doc, stream)
    git = git_command(project)
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "Baseline"], check=True)
    return project


def capture(output_dir, gif=False):
    from playwright.sync_api import sync_playwright  # noqa: PLC0415 — optional snapshot extra

    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sqldash-showcase-") as temp:
        work = Path(temp)
        project = prepare_project(work)
        git = git_command(project)
        with patch(
            "sqldash.studio.entrypoints.entrypoints_path", return_value=work / "studio.json"
        ):
            save_entrypoint(
                AgentEntrypoint(name="Demo agent", command=[sys.executable, str(AGENT), "{prompt}"])
            )
            workspace = [(REPO, project), *write_repos(work / "repos")]
            app = create_app(
                workspace=workspace, studio=True, allowed_hosts=["127.0.0.1", "localhost"]
            )
            server, thread, port = _start_server(app)
            base = f"http://127.0.0.1:{port}"
            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch()
                    context = browser.new_context(
                        viewport={"width": WIDTH, "height": HEIGHT},
                        device_scale_factor=SCALE,
                        color_scheme="dark",
                    )
                    context.add_init_script(
                        "localStorage.setItem('sqldash-ai-studio-tour-v1','seen');"
                        "if (!localStorage.getItem('sqldash-theme'))"
                        " localStorage.setItem('sqldash-theme','dark')"
                    )
                    page = context.new_page()
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    hero = Recording(page)

                    # 1. The library: every dashboard and metric is a file in the repo.
                    page.goto(base)
                    page.wait_for_selector("#browser-filter")
                    page.wait_for_timeout(400)
                    hero.hold(0.8)
                    hero.click(f'a[href="/d/{REPO}/revenue"]', settle=0)
                    ready(page)
                    hero.animate(0.4)
                    hero.hold(1.1)

                    # 2. Filters re-run every tile.
                    total = page.locator('.tile[data-tile-id="revenue"] .value').inner_text()
                    hero.click("#filter-region", settle=0.35)
                    box = page.locator("#filter-region").bounding_box()
                    hero.move(
                        box["x"] + box["width"] * 0.3, box["y"] + box["height"] + 12 + 2 * 30, 0.5
                    )
                    hero.frame(350)
                    page.locator("#filter-region").select_option("Europe")
                    page.wait_for_function(
                        "old => document.querySelector('[data-tile-id=revenue] .value')"
                        ".textContent !== old",
                        arg=total,
                    )
                    ready(page)
                    hero.hold(1.1)

                    # 3. + Explore: type SQL, run it, pick a chart, add it, back to the dashboard.
                    hero.click(f'a[href="/d/{REPO}/revenue/workspace"]', settle=0)
                    page.wait_for_function(WORKSPACE_READY, timeout=30000)
                    page.wait_for_timeout(300)
                    editor = page.frame_locator("#workspace-frames iframe").first
                    hero.hold(0.4)
                    length = len(NEW_TILE_SQL)
                    for step in range(1, 7):
                        editor.locator(".ace_editor").evaluate(
                            SET_SQL, NEW_TILE_SQL[: length * step // 6]
                        )
                        hero.frame(150)
                    hero.hold(0.3)
                    hero.click(editor.locator("#run-btn"), seconds=0.45)
                    try:
                        editor.locator("#results-body table").wait_for(timeout=15000)
                    except Exception:
                        print("workspace said:", editor.locator("#results-meta").all_inner_texts())
                        raise
                    page.wait_for_timeout(250)
                    hero.hold(0.5)
                    hero.click(editor.locator("#qb-type button:has-text('Bar')"), seconds=0.45)
                    editor.locator("#qb-preview .chart-mount").wait_for()
                    page.wait_for_timeout(400)
                    hero.scroll_element(editor.locator("#workspace-chart-builder"), seconds=0.5)
                    hero.hold(0.5)
                    hero.click(editor.locator(".workspace-chart-footer .btn-primary"), settle=0.35)
                    hero.type(editor.locator("#qb-title"), "Orders by weekday", chunk=6)
                    page.wait_for_timeout(150)
                    hero.hold(0.3)
                    page.screenshot(path=str(output_dir / "query-preview.png"))
                    hero.click(editor.locator("#qb-add"), seconds=0.35, settle=0)
                    editor.locator("#qb-add:has-text('Added')").wait_for(timeout=15000)
                    page.wait_for_timeout(150)
                    hero.hold(0.6)
                    hero.click(".workspace-back", seconds=0.6, settle=0)
                    page.wait_for_url(f"{base}/d/{REPO}/revenue", timeout=15000)
                    ready(page)
                    hero.hold(0.6)
                    hero.scroll_to_bottom()
                    hero.hold(1.2)

                    # 4. AI Studio: pin the new tile and revenue, ask for a theme, keep chatting.
                    before = (project / "revenue.yaml").read_bytes()
                    hero.click("#studio-open", settle=0.5)
                    page.locator("#studio-entrypoint").select_option("Demo agent")
                    page.wait_for_timeout(300)
                    hero.hold(1.0)
                    hero.click("#studio-pick", settle=0.4)
                    hero.scroll_to_bottom(seconds=0.7)
                    hero.hold(0.4)
                    hero.click(".tile:has-text('Orders by weekday')", offset=(0.3, 0.2), settle=0.5)
                    hero.hold(0.25)
                    hero.type("#studio-note", "Make this full width so the weekdays read.", chunk=9)
                    hero.hold(0.25)
                    hero.click('#studio-note-form button[type="submit"]', settle=0.3)
                    hero.hold(0.5)
                    hero.scroll_to_top()
                    hero.hold(0.4)
                    hero.click('.tile[data-tile-id="revenue"]', offset=(0.3, 0.3), settle=0.5)
                    hero.hold(0.25)
                    hero.type("#studio-note", "Make revenue the focal point.", chunk=8)
                    hero.hold(0.25)
                    hero.click('#studio-note-form button[type="submit"]', settle=0.3)
                    hero.hold(0.8)
                    hero.click("#studio-message", offset=(0.25, 0.5), seconds=0.7, settle=0.4)
                    hero.hold(0.4)
                    hero.type(
                        "#studio-message",
                        "Give it a dark, glassy look with violet accents and glowing cards.",
                        chunk=10,
                    )
                    hero.hold(0.4)
                    hero.click("#studio-send", settle=0)
                    hero.animate(3.8)
                    page.locator("#studio-undo-last").wait_for(state="visible", timeout=30000)
                    page.wait_for_function(
                        "document.querySelector('#dash-title').textContent === 'Neon observatory'"
                    )
                    ready(page)
                    after = (project / "revenue.yaml").read_bytes()
                    assert before != after
                    assert b"css:" in after
                    assert weekday_size(project) == (12, 3)
                    page.screenshot(path=str(output_dir / "studio-preview.png"))
                    hero.hold(1.6)

                    # 5. Second turn: structure. The agent adds section headings.
                    hero.click("#studio-message", offset=(0.25, 0.5), seconds=0.7, settle=0.4)
                    hero.hold(0.4)
                    hero.type(
                        "#studio-message",
                        "Break it into sections: short headings with a line of context.",
                        chunk=10,
                    )
                    hero.hold(0.4)
                    hero.click("#studio-send", settle=0)
                    hero.animate(3.4)
                    page.wait_for_function(
                        "document.querySelectorAll('.tile-text').length >= 3", timeout=30000
                    )
                    ready(page)
                    assert (project / "revenue.yaml").read_text().count("markdown:") == 3
                    hero.hold(1.4)
                    hero.scroll_to_bottom(seconds=0.9)
                    hero.hold(1.3)
                    hero.scroll_to_top(seconds=0.5)

                    # 6. Third turn: a different direction, straight to the agent.
                    hero.click("#studio-message", offset=(0.25, 0.5), seconds=0.7, settle=0.4)
                    hero.hold(0.4)
                    hero.type(
                        "#studio-message",
                        "Too dark. Something brighter and punchier, acid lime on black.",
                        chunk=10,
                    )
                    hero.hold(0.4)
                    hero.click("#studio-send", settle=0)
                    hero.animate(3.4)
                    page.wait_for_function(
                        "document.querySelector('#dash-title').textContent === 'Electric citrus'",
                        timeout=30000,
                    )
                    ready(page)
                    final = (project / "revenue.yaml").read_bytes()
                    assert b"Electric citrus" in final
                    assert weekday_size(project) == (12, 3)
                    (output_dir / "studio-demo.diff").write_text(
                        subprocess.check_output(
                            ["git", "-C", str(project), "diff", "--", "revenue.yaml"], text=True
                        )
                    )
                    hero.hold(2.0)

                    # 7. Close on the shared metric: open the info card, follow Revenue.
                    hero.click("#dash-info-dd .dd-btn", settle=0.5)
                    hero.hold(0.9)
                    hero.click(f'#dash-info-menu a[href="/m/{REPO}/revenue"]', settle=0)
                    page.wait_for_url(f"{base}/m/{REPO}/revenue", timeout=15000)
                    ready(page)
                    page.wait_for_function("!document.querySelector('#metric-previews .loading')")
                    hero.animate(0.4)
                    hero.hold(2.2)
                    page.screenshot(path=str(output_dir / "metric-preview.png"))

                    # 8. Theme gallery: three looks asked for in plain words, chat left open.
                    page.close()
                    with app.state.studio.lock:
                        for session_id in list(app.state.studio.sessions):
                            app.state.studio.sessions.pop(session_id).close()
                    gallery = Recording(page)
                    for slug, mode, title, ask in GALLERY:
                        subprocess.run([*git, "checkout", "--", "revenue.yaml"], check=True)
                        tab = context.new_page()
                        tab.on("pageerror", lambda error: errors.append(str(error)))
                        tab.goto(f"{base}/")
                        tab.evaluate("mode => localStorage.setItem('sqldash-theme', mode)", mode)
                        tab.goto(f"{base}/d/{REPO}/revenue")
                        ready(tab)
                        assert tab.evaluate("document.documentElement.dataset.theme") == mode
                        tab.click("#studio-open")
                        tab.locator("#studio-entrypoint").select_option("Demo agent")
                        tab.fill("#studio-message", ask)
                        tab.click("#studio-send")
                        tab.wait_for_function(
                            f"document.querySelector('#dash-title').textContent === {title!r}",
                            timeout=30000,
                        )
                        tab.locator("#studio-undo-last").wait_for(state="visible", timeout=30000)
                        ready(tab)
                        tab.wait_for_timeout(600)
                        gallery.page = tab
                        tab.screenshot(path=str(output_dir / f"theme-{slug}.png"))
                        gallery.frame(3000, cursor=False)
                        tab.click("#studio-close")
                        tab.locator("#studio-close-sheet").wait_for(state="visible")
                        tab.click("#studio-close-keep")
                        tab.locator("#studio-close-sheet").wait_for(state="hidden")
                        assert not app.state.studio.sessions
                        tab.close()

                    hero.save(output_dir / "demo.webp")
                    if gif:
                        hero.save(output_dir / "demo.gif")
                    gallery.save(output_dir / "demo-themes.webp")
                    if gif:
                        gallery.save(output_dir / "demo-themes.gif")
                    assert not errors, errors
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                assert not thread.is_alive(), "capture server did not stop"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "assets")
    parser.add_argument(
        "--gif",
        action="store_true",
        help="Also export GIF versions of every recording (large; the README embeds WebP)",
    )
    args = parser.parse_args()
    capture(args.out, args.gif)
