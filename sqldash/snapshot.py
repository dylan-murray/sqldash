"""Render dashboards to static PNGs (plus a self-contained index.html) with a
headless browser — the wallboard/stakeholder path: no warehouse credentials, no
server, just files you can commit or drop anywhere."""

import html
import socket
import threading
import time
from datetime import datetime
from pathlib import Path

import uvicorn

INSTALL_HINT = (
    "snapshot needs a headless browser:\n"
    "  pip install 'sqldash[snapshot]'   (or: uv pip install 'sqldash[snapshot]')\n"
    "  playwright install chromium"
)

SETTLE_SECONDS = 1.2
TILE_TIMEOUT_SECONDS = 45


class SnapshotError(RuntimeError):
    pass


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _start_server(app):
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            raise SnapshotError("embedded server failed to start")
        time.sleep(0.05)
    return server, thread, port


def _wait_for_tiles(page) -> bool:
    deadline = time.monotonic() + TILE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        pending = page.evaluate("() => document.querySelectorAll('.tile-status .skeleton').length")
        if pending == 0:
            time.sleep(SETTLE_SECONDS)
            return True
        time.sleep(0.25)
    return False


def _index_html(entries: list[dict], generated: str) -> str:
    def card(e: dict) -> str:
        if e.get("error"):
            return (
                '<div class="shot failed">'
                f'<div class="reason">failed to load: {html.escape(e["error"])}</div>'
                f"<span>{html.escape(e['title'])}</span></div>"
            )
        href = html.escape(e["file"], quote=True)
        alt = html.escape(e["title"], quote=True)
        return (
            f'<a class="shot" href="{href}">'
            f'<img src="{href}" alt="{alt}">'
            f"<span>{html.escape(e['title'])}</span></a>"
        )

    cards = "\n".join(card(e) for e in entries)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>sqldash snapshots</title>
<style>
  body {{ margin: 0; background: #0c0c0d; color: #f5f5f4;
         font: 14px/1.5 -apple-system, system-ui, sans-serif; padding: 32px; }}
  h1 {{ font-size: 18px; margin: 0 0 4px; }}
  p {{ color: #8a8880; margin: 0 0 24px; font-size: 12.5px; }}
  .grid {{ display: grid; gap: 18px;
          grid-template-columns: repeat(auto-fill, minmax(420px, 1fr)); }}
  .shot {{ display: block; color: inherit; text-decoration: none;
           border: 1px solid rgba(255,255,255,.1); border-radius: 12px;
           overflow: hidden; background: #161617; }}
  .shot img {{ width: 100%; display: block; }}
  .failed .reason {{ padding: 28px 14px; color: #f3a39b; font-size: 12.5px;
                     border-bottom: 1px solid rgba(255,255,255,.1); }}
  .shot span {{ display: block; padding: 10px 14px; font-weight: 570; font-size: 13.5px; }}
</style>
</head>
<body>
<h1>sqldash snapshots</h1>
<p>generated {generated} — static renders; no credentials required to view.</p>
<div class="grid">
{cards}
</div>
</body>
</html>
"""


def snapshot_dashboards(
    app,
    out_dir: Path,
    names: list[str] | None = None,
    theme: str = "dark",
    width: int = 1440,
    write_index: bool = True,
) -> list[dict]:
    store = app.state.store
    targets = list(names) if names else sorted(store.discover())
    unknown = [n for n in targets if n not in store.discover()]
    if unknown:
        available = ", ".join(sorted(store.discover())) or "(none)"
        raise SnapshotError(f"no dashboard named {', '.join(unknown)} — available: {available}")
    if not targets:
        raise SnapshotError("no dashboards to snapshot")

    loaded = []
    failed: list[dict] = []
    for name in targets:
        try:
            dashboard, _, _ = store.load(name)
        except Exception as exc:
            reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            failed.append({"name": name, "title": name, "file": None, "error": reason})
            continue
        loaded.append((name, dashboard))
    if not loaded:
        reasons = "\n".join(f"  {f['name']}: {f['error']}" for f in failed)
        raise SnapshotError(f"no snapshots written: every dashboard failed to load\n{reasons}")

    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415 — optional [snapshot] extra
    except ImportError as exc:
        raise SnapshotError(INSTALL_HINT) from exc

    out_dir.mkdir(parents=True, exist_ok=True)

    server, thread, port = _start_server(app)
    entries: list[dict] = []
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except Exception as exc:
                raise SnapshotError(f"{exc}\n\n{INSTALL_HINT}") from exc
            context = browser.new_context(
                viewport={"width": width, "height": 900},
                device_scale_factor=2,
                color_scheme=theme,
            )
            context.add_init_script(f"localStorage.setItem('sqldash-theme', '{theme}')")
            page = context.new_page()
            for name, dashboard in loaded:
                page.goto(f"http://127.0.0.1:{port}/d/{name}", wait_until="networkidle")
                settled = _wait_for_tiles(page)
                filename = name.replace("/", "__") + ".png"
                page.screenshot(path=str(out_dir / filename), full_page=True)
                entries.append(
                    {
                        "name": name,
                        "title": dashboard.title,
                        "file": filename,
                        "settled": settled,
                        "error": None,
                    }
                )
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)

    entries.extend(failed)
    entries.sort(key=lambda e: targets.index(e["name"]))
    if write_index:
        generated = datetime.now().strftime("%Y-%m-%d %H:%M")
        (out_dir / "index.html").write_text(_index_html(entries, generated))
    return entries
