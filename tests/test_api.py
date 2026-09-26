import csv
import importlib
import io
import json
import pkgutil
import re
import sqlite3
import unicodedata
from datetime import date
from pathlib import Path
from urllib.parse import quote

import duckdb
import pytest
from fastapi.responses import HTMLResponse
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from helpers import run_to_completion, warm_up

import sqldash
import sqldash.api
from sqldash import workspace
from sqldash.api import routes_pages
from sqldash.api.helpers import StrictBody
from sqldash.csv_safe import spreadsheet_safe
from sqldash.models.dashboard import PAGE_SELECTOR, Dashboard, PageStyle, split_page_tokens
from sqldash.scaffold import create_demo
from sqldash.server import (
    app_stylesheets,
    asset_url,
    content_security_policy,
    create_app,
    js_importmap,
)


@pytest.fixture(scope="module")
def demo_dir(tmp_path_factory):
    target = tmp_path_factory.mktemp("demo")
    create_demo(target)
    return target


@pytest.fixture(scope="module")
def client(demo_dir):
    app = create_app(demo_dir, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        warm_up(c, "demo")
        yield c


def test_index_page(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "Order Analytics" in res.text


def test_dashboard_page(client):
    res = client.get("/d/demo")
    assert res.status_code == 200
    assert "dashboard-data" in res.text
    assert "•••" not in res.text


def test_dashboard_page_accepts_unquoted_iso_date_default(tmp_path):
    """YAML `default: 2024-03-15` is a date object; the HTML pages json.dumps
    the payload and used to 500. Quoted strings and the JSON API were fine."""
    (tmp_path / "d.yaml").write_text(
        "title: Dates\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "filters:\n"
        "  - {name: d, type: date, default: 2024-03-15}\n"
        "  - {name: r, type: daterange, default: {start: 2024-02-29, end: 2024-03-31}}\n"
        "tiles: [{title: W, sql: 'SELECT 1'}]\n"
    )
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        page = c.get("/d/d")
        query = c.get("/d/d/query")
        api = c.get("/api/dashboards/d")
    assert page.status_code == 200, page.text
    assert query.status_code == 200, query.text
    assert api.status_code == 200
    assert "2024-03-15" in page.text


def test_dashboard_css_is_injected_and_cannot_break_out_of_style(tmp_path):
    (tmp_path / "d.yaml").write_text(
        "title: Styled\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "css: |\n"
        "  .tile { outline: 2px solid red; }\n"
        "  </style><script>alert(1)</script>\n"
        "tiles: [{title: W, sql: 'SELECT 1'}]\n"
    )
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        page = c.get("/d/d").text
    assert 'id="dash-css"' in page
    assert ".tile { outline: 2px solid red; }" in page
    assert "</style><script>" not in page
    assert "<\\/style>" in page or "<\\/script>" in page


def test_root_tokens_in_css_are_hoisted_page_wide_and_the_rest_stays_scoped(tmp_path):
    (tmp_path / "d.yaml").write_text(
        "title: Themed\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "css: |\n"
        "  :root {\n"
        "    --page: #0c0918;\n"
        "    --page-glow: radial-gradient(ellipse at 15% 0%, #702fc950, transparent 55%);\n"
        "    --accent: var(--series-7);\n"
        "  }\n"
        "  .tile { outline: 2px solid red; }\n"
        "tiles: [{title: W, sql: 'SELECT 1'}]\n"
    )
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        page = c.get("/d/d").text
    start = page.index('<style id="dash-page">')
    tokens = page[start : page.index("</style>", start)]
    assert tokens.startswith('<style id="dash-page">' + PAGE_SELECTOR + " { ")
    assert "--page: #0c0918;" in tokens
    assert "--page-glow: radial-gradient(ellipse at 15% 0%, #702fc950, transparent 55%);" in tokens
    assert "--accent: var(--series-7);" in tokens
    start = page.index('<style id="dash-css">')
    scoped = page[start : page.index("</style>", start)]
    assert "@scope (main.container)" in scoped
    assert ".tile { outline: 2px solid red; }" in scoped
    assert ":root" not in scoped
    assert page.index('id="dash-page"') < page.index('id="dash-css"')


def test_a_rule_nested_inside_scope_reaches_the_rendered_scope_block(tmp_path):
    (tmp_path / "d.yaml").write_text(
        "title: Nested\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "css: |\n"
        "  :scope {\n"
        "    --page: #0c0918;\n"
        "    @media (min-width: 1px) { .tile { outline: 2px solid rgb(1, 2, 3); } }\n"
        "  }\n"
        "tiles: [{title: W, sql: 'SELECT 1'}]\n"
    )
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        page = c.get("/d/d").text
    start = page.index('<style id="dash-css">')
    scoped = page[start : page.index("</style>", start)]
    assert "@scope (main.container)" in scoped
    assert (
        ":scope { --page: #0c0918; @media (min-width: 1px) "
        "{ .tile { outline: 2px solid rgb(1, 2, 3); } } }"
    ) in scoped
    assert "--page: #0c0918;" in page[page.index('<style id="dash-page">') :]


@pytest.mark.parametrize(
    ("css", "reason"),
    [
        (":root { --topbar: url(x); }", "--topbar must be a plain value"),
        (":root { --top\\bar: red; }", "is not a valid --token name"),
        (":root { --page: url(https://evil.test/x); }", "--page must be a plain colour"),
        (":root { --page: red; --accent: blue</style><script>x()</script>; }", "--accent must be"),
        (":root { --page: red\n--accent: blue; }", "--page must be a plain colour"),
        (":root { --page: red @import 'x'; }", "--page must be a plain colour"),
        (":root { margin: 0; }", "'margin: 0' is not a --token"),
    ],
)
def test_root_declarations_that_cannot_leave_the_scope_are_dropped(css, reason):
    style = split_page_tokens(css)
    assert any(reason in dropped for dropped in style.dropped), style.dropped
    for bad in ("--topbar", "url(", "</style>", "--accent: blue", "/*", "@import", "margin"):
        assert bad not in (style.page or "")


def test_comments_inside_the_root_block_are_stripped_not_hoisted():
    style = split_page_tokens(":root { /* page */ --page: red /* dark */; }")
    assert style.page == PAGE_SELECTOR + " { --page: red; }"
    assert style.dropped == ()


@pytest.mark.parametrize(
    ("css", "page", "rest"),
    [
        (
            "--page: #0c0918;\n--accent: #be91ff;\n.tile { x: y }",
            "--page: #0c0918; --accent: #be91ff;",
            ".tile { x: y }",
        ),
        (
            "body { background: #0c0918; color: #f5edff; margin: 0 }",
            "--page: #0c0918; --ink-1: #f5edff;",
            None,
        ),
        ("html { background-color: #111 }", "--page: #111;", None),
        (
            ":scope { --page: red; padding: 0 }",
            "--page: red;",
            ":scope { --page: red; padding: 0; }",
        ),
        (":root { --page: red; }\n:scope { --mine: 1 }", "--page: red;", ":scope { --mine: 1; }"),
    ],
)
def test_page_tokens_do_not_need_a_root_block(css, page, rest):
    style = split_page_tokens(css)
    assert style.page == f"{PAGE_SELECTOR} {{ {page} }}"
    assert style.dashboard == rest


def test_tokens_inside_narrower_selectors_stay_scoped():
    style = split_page_tokens(".tile[data-tile-id=revenue] { --ink-1: #182006; --page: red; }")
    assert style.page is None
    assert style.dashboard == ".tile[data-tile-id=revenue] { --ink-1: #182006; --page: red; }"


def test_braces_inside_strings_and_compound_page_selectors_do_not_confuse_the_hoist():
    style = split_page_tokens('.a::after { content: "}"; }\n.tile { --page: red }')
    assert style.page is None
    assert style.dashboard == '.a::after { content: "}"; }\n.tile { --page: red }'
    style = split_page_tokens('.a::after { content: "{"; }\n--page: red;')
    assert style.page == PAGE_SELECTOR + " { --page: red; }"
    assert style.dashboard == '.a::after { content: "{"; }'
    style = split_page_tokens(".wrapper body { --page: red }\n.tile { color: blue }")
    assert style.page is None
    assert style.dashboard == ".wrapper body { --page: red }\n.tile { color: blue }"
    style = split_page_tokens("html, body { --page: red; }\nBODY { color: #fff }")
    assert style.page == PAGE_SELECTOR + " { --page: red; --ink-1: #fff; }"
    assert style.dashboard is None


def test_commented_out_root_blocks_and_braces_do_not_count():
    style = split_page_tokens("/* :root { --page: red; } */\n.tile { color: red; }")
    assert style.page is None
    assert style.dashboard == ".tile { color: red; }"
    style = split_page_tokens("@layer x { /* } */ :root { --page: red; } }")
    assert style.page is None
    assert ":root { --page: red; }" in style.dashboard


def test_rules_nested_in_a_plain_page_selector_are_dropped_by_name():
    """#650: the hoist read only the block-less statements of a page block, so a nested
    rule vanished from both outputs and from `dropped`/`ignored` — lint stayed green
    while the author's CSS never reached the page."""
    style = split_page_tokens("body {\n --ink-1: green;\n .foo { color: red }\n}\n.card { x: y }")
    assert style.page == PAGE_SELECTOR + " { --ink-1: green; }"
    assert style.dashboard == ".card { x: y }"
    assert style.dropped == ("'.foo' is a rule nested in body",)
    style = split_page_tokens("body { @media (min-width: 40em) { .x { color: red } } }")
    assert style.page is None
    assert style.dashboard is None
    assert style.dropped == ("'@media (min-width: 40em)' is a rule nested in body",)
    assert style.ignored == ()


def test_rules_nested_in_scope_are_kept_verbatim_in_the_dashboard_scope():
    """`:scope` is the dashboard's own box, so a nested rule needs no rewriting to mean
    what the author wrote. It is re-emitted as-is inside `@scope (main.container)`."""
    style = split_page_tokens(":scope { --page: pink; .foo { color: red } }")
    assert style.page == PAGE_SELECTOR + " { --page: pink; }"
    assert style.dashboard == ":scope { --page: pink; .foo { color: red } }"
    assert style.dropped == ()
    assert style.ignored == ()
    style = split_page_tokens(":scope { padding: 0; @media (min-width: 40em) { .x { c: d } } }")
    assert style.page is None
    assert style.dashboard == ":scope { padding: 0; @media (min-width: 40em) { .x { c: d } } }"
    assert style.dropped == ()
    style = split_page_tokens(':scope { .a::after { content: "}" } body { color: red } }')
    assert style.dashboard == ':scope { .a::after { content: "}" } body { color: red } }'
    assert style.ignored == ("body inside :scope",)


def test_scope_keeps_declarations_and_nested_blocks_in_source_order():
    """#656: declarations were emitted before nested rules, so a bare `&` or a nested
    `@media` written first beat a later declaration of the same specificity."""
    style = split_page_tokens(":scope { & { padding-top: 4px } padding-top: 2px }")
    assert style.dashboard == ":scope { & { padding-top: 4px } padding-top: 2px; }"
    style = split_page_tokens(
        ":scope { a: 1; @media (min-width: 1px) { a: 2 } b: 3; .x { c: 4 } d: 5 }"
    )
    assert style.dashboard == (
        ":scope { a: 1; @media (min-width: 1px) { a: 2 } b: 3; .x { c: 4 } d: 5; }"
    )


def test_a_token_hoisted_out_of_scope_also_stays_where_it_was_written():
    """Hoisting alone moved `--accent` to `:root`, where an earlier `& { --accent }` on
    the dashboard itself outranked it, the reverse of what the author's order says."""
    style = split_page_tokens(":scope { & { --accent: blue } --accent: red; --page: url(x) }")
    assert style.page == PAGE_SELECTOR + " { --accent: red; }"
    assert style.dashboard == ":scope { & { --accent: blue } --accent: red; }"
    assert style.dropped == ("--page must be a plain colour or gradient",)


def test_root_blocks_nested_in_other_at_rules_stay_inside_the_scope():
    style = split_page_tokens("@layer x { :root { --page: red; } }\n.tile { x: y }")
    assert style.page is None
    assert style.dropped == ()
    assert ":root { --page: red; }" in style.dashboard
    assert style.ignored == (":root inside @layer x",)
    style = split_page_tokens('@container (min-width: 1px) { :root[data-theme="dark"] { --x: 1 } }')
    assert style.ignored == (':root[data-theme="dark"] inside @container (min-width: 1px)',)


def test_theme_specific_root_blocks_hoist_per_theme():
    """`:root[data-theme]` and friends were not page selectors, so the whole block stayed
    inside `@scope (main.container)` where `:root` never matches and every token in it,
    allowlisted ones included, was lost without a lint finding."""
    style = split_page_tokens(
        ":root, :root[data-theme] { --accent: #f00; }\n"
        ':root[data-theme="light"] { --accent: #0f0; background: #fff; }\n'
        "HTML[data-theme='Dark'] { --page: #000; }\n"
        "body[data-theme=dark i] { --ink-1: #eee; }"
    )
    assert style.page == (
        f"{PAGE_SELECTOR} {{ --accent: #f00; }}\n"
        ':root[data-theme="dark"] { --page: #000; --ink-1: #eee; }\n'
        ':root[data-theme="light"] { --accent: #0f0; --page: #fff; }'
    )
    assert style.dashboard is None
    assert style.dropped == ()
    style = split_page_tokens(':root[data-theme="dark"] { --page: #000; margin: 0; .x { a: b } }')
    assert style.dropped == (
        "'margin: 0' is not a --token",
        """'.x' is a rule nested in :root[data-theme="dark"]""",
    )


def test_custom_tokens_in_page_blocks_are_kept_on_the_dashboard_scope():
    """A custom `--crawl` in `:root` was dropped as not a page token, so a title using
    `-webkit-text-stroke: 2.5px var(--crawl)` computed a 0px stroke. It is re-emitted on
    `:scope` where it was written, never on the page."""
    style = split_page_tokens(
        ".a { b: c }\n"
        ":root, :root[data-theme] { --crawl: #ffe81f; --page: #02030a; --holo: #4bd5ee; }\n"
        ':root[data-theme="light"] { --crawl: #7a6500; }\n'
        "--speed: 60s;\n"
        "h1 { -webkit-text-stroke: 2px var(--crawl); }"
    )
    assert style.page == f"{PAGE_SELECTOR} {{ --page: #02030a; }}"
    assert style.dashboard == (
        ".a { b: c }\n"
        ":scope { --crawl: #ffe81f; --holo: #4bd5ee; }\n"
        ':root[data-theme="light"] :scope { --crawl: #7a6500; }\n'
        ":scope { --speed: 60s; }\n"
        "h1 { -webkit-text-stroke: 2px var(--crawl); }"
    )
    assert style.dropped == ()


@pytest.mark.parametrize(
    "value",
    ["url(https://evil.test/x)", "red\n--page: blue", "a</style><script>x()</script>"],
)
def test_custom_tokens_never_reach_the_page(value):
    style = split_page_tokens(f":root {{ --topbar: red; --mine: {value}; --page: #111; }}")
    assert style.page == f"{PAGE_SELECTOR} {{ --page: #111; }}"
    assert style.dashboard == ":scope { --topbar: red; }"
    assert "--mine must be a plain value" in style.dropped


def test_page_tokens_that_read_a_custom_token_get_its_value():
    """`--accent: var(--crawl)` on `:root` would read a variable that only exists on the
    dashboard scope, so the value is substituted, per theme when the variable is."""
    style = split_page_tokens(
        ":root { --crawl: #ffe81f; --accent: var(--crawl); --ink-1: var(--font-ink, red); }\n"
        ':root[data-theme="light"] { --crawl: #7a6500; }\n'
        ":root { --glow: url(x); --page: var(--glow); }"
    )
    assert style.page == (
        f"{PAGE_SELECTOR} {{ --accent: #ffe81f; --ink-1: var(--font-ink, red); "
        "--page: var(--glow); }\n"
        ':root[data-theme="light"] { --accent: #7a6500; }'
    )
    style = split_page_tokens(":root { --a: ur; --page: var(--a)l(x); }")
    assert style.page == f"{PAGE_SELECTOR} {{ --page: var(--a)l(x); }}"


def test_media_blocks_holding_page_selectors_split_under_the_same_wrapper():
    style = split_page_tokens(
        "@media (prefers-color-scheme: dark) {\n"
        "  :root { --page: #000; --glow: red; }\n"
        "  .tile { a: b }\n"
        "}\n"
        "@media (min-width: 1px) { .tile { x: y } }"
    )
    assert style.page == (
        f"@media (prefers-color-scheme: dark) {{ {PAGE_SELECTOR} {{ --page: #000; }} }}"
    )
    assert style.dashboard == (
        "@media (prefers-color-scheme: dark) { :scope { --glow: red; } .tile { a: b } }\n"
        "@media (min-width: 1px) { .tile { x: y } }"
    )
    assert style.ignored == ()
    style = split_page_tokens("@media (x) { :root { --page: red; } }")
    assert style.page == f"@media (x) {{ {PAGE_SELECTOR} {{ --page: red; }} }}"
    assert style.dashboard is None


@pytest.mark.parametrize(
    ("css", "rest"),
    [
        (":root[data-theme] .tile { a: b }", ":root[data-theme] :scope .tile { a: b }"),
        (
            ':root[data-theme="dark"]  .bn-value > i { a: b }',
            ':root[data-theme="dark"] :scope  .bn-value > i { a: b }',
        ),
        ("html body .t{a:b}", "html body :scope .t {a:b}"),
        (
            "HTML[data-theme='Light' i] .t { a: b }",
            "HTML[data-theme='Light' i] :scope .t { a: b }",
        ),
        (
            ".a, :root[data-theme] .b:is(.c, .d), .e { a: b }",
            ".a, :root[data-theme] :scope .b:is(.c, .d), .e { a: b }",
        ),
        (
            "@media (min-width: 1px) { :root[data-theme=light] .t { a: b } .u { c: d } }",
            "@media (min-width: 1px) { :root[data-theme=light] :scope .t { a: b } .u { c: d } }",
        ),
    ],
)
def test_scoped_rules_under_a_page_prefix_get_scope_put_back(css, rest):
    """Inside `@scope`, `:root[data-theme] .tile` reads as `:scope :root[data-theme] .tile`
    and matches nothing, so the prefix gets `:scope` after it and the rest is untouched."""
    style = split_page_tokens(css)
    assert style.page is None
    assert style.dashboard == rest
    assert style.unmatched == ()


def test_page_prefixes_that_cannot_reach_the_dashboard_are_named():
    css = (
        ':root[data-theme="dark"] > .t, .x { a: b }\n'
        "html + .t { a: b }\n"
        "body, .tile { color: red }\n"
        ":root[data-theme] :scope .t, body::before, .a:is(:root, .b) .c, & .d { a: b }"
    )
    style = split_page_tokens(css)
    assert style.dashboard == css
    assert style.unmatched == (':root[data-theme="dark"] > .t', "html + .t", "body")


def test_metric_page_groups_used_by_per_dashboard(client):
    page = client.get("/m/revenue").text
    start = page.index('<ul class="used-by">')
    block = page[start : page.index("</ul>", start)]
    assert block.count("<li>") == 1
    assert 'href="/d/demo"' in block
    tiles = block[block.index('<span class="used-by-tiles">') + 28 : block.index("</span>")]
    assert len(tiles.split(", ")) >= 2, tiles


def _info_block(html: str) -> str:
    start = html.index("dd-menu dash-info")
    start = html.rfind("<", 0, start)
    return html[start : html.index('id="dash-desc"', start)]


def test_dashboard_info_is_an_icon_next_to_the_title(client):
    page = client.get("/d/demo").text
    assert 'id="dash-info-dd"' in page
    assert 'aria-label="Dashboard info"' in page
    assert 'class="dd-label">Details</span>' not in page
    assert "<details" not in page
    assert "<summary>" not in page
    title_at = page.index('id="dash-title"')
    info_at = page.index('id="dash-info-dd"')
    assert title_at < info_at
    header = page[page.index("<header") : title_at]
    assert 'id="dash-info-dd"' not in header


def test_dashboard_info_lists_metrics(client):
    block = _info_block(client.get("/d/demo").text)
    assert 'href="/m/revenue"' in block
    assert "shares a relation with" in block
    assert 'href="/m/order_count"' in block


def test_dashboard_info_reads_git_author(tmp_path):
    import subprocess

    create_demo(tmp_path)
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "ada@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Ada Lovelace"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        page = c.get("/d/demo").text
    assert "Ada Lovelace" in page


def test_dashboard_info_shows_updated_after_a_second_commit(tmp_path):
    import os
    import subprocess

    create_demo(tmp_path)
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": "2026-08-18T12:00:00",
        "GIT_COMMITTER_DATE": "2026-08-18T12:00:00",
    }
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "ada@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Ada Lovelace"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, env=env)
    dash = tmp_path / ".sqldash" / "demo.yaml"
    dash.write_text(dash.read_text() + "\n# touch\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "touch"], cwd=tmp_path, check=True, env=env)
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        page = c.get("/d/demo").text
    assert "updated" in page


def test_dashboard_info_has_no_author_outside_git(tmp_path):
    create_demo(tmp_path)
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        page = c.get("/d/demo").text
    assert "<dt>Author</dt>" not in page


def test_dashboard_page_survives_broken_metrics_yaml(tmp_path):
    """all_metrics() raises on a bad metrics.yaml. The dashboard page used to
    422 JSON instead of rendering. #195."""
    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "metrics.yaml").write_text("metrics: {}\n")
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        res = c.get("/d/demo")
    assert res.status_code == 200, res.text
    assert "text/html" in res.headers.get("content-type", "")
    assert "dash-info" in res.text
    assert "<details" not in res.text
    block = _info_block(res.text)
    assert 'href="/m/revenue"' not in block
    assert "revenue" in block


def test_dashboard_info_names_sibling_dashboards(tmp_path):
    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "other.yaml").write_text(
        "title: Other\n"
        "source: {type: duckdb, attach_files: true}\n"
        "tiles: [{title: Rev, metric: revenue}]\n"
    )
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        page = c.get("/d/demo").text
    assert "also on" in page
    assert 'href="/d/other"' in page
    assert "Other" in page


def test_select_default_all_appears_in_the_dropdown(tmp_path):
    """#216: omitting `all` from options made the browser pick the first real option."""
    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "f.yaml").write_text(
        "title: F\n"
        "source: {type: duckdb, attach_files: true}\n"
        "filters:\n"
        "  - {name: region, type: select, default: all, options: [us, eu, apac]}\n"
        "tiles: []\n"
    )
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        page = c.get("/d/f").text
    assert 'option value="all" selected' in page
    assert 'value="us"' in page


def test_select_without_default_sits_on_all(tmp_path):
    """#236: no default made the browser pick `us` while CLI/API bound nothing."""
    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "f.yaml").write_text(
        "title: F\n"
        "source: {type: duckdb, attach_files: true}\n"
        "filters:\n"
        "  - {name: region, type: select, options: [us, eu, apac]}\n"
        "tiles: []\n"
    )
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        page = c.get("/d/f").text
        payload = c.get("/api/dashboards/f").json()["dashboard"]
    assert 'option value="all" selected' in page
    assert payload["filters"][0]["resolved_default"] == "all"
    assert payload["filters"][0]["options"] == ["us", "eu", "apac"]


def test_settings_panel_does_not_name_a_dot_repo(tmp_path, monkeypatch):
    """sqldash serve . listed a workspace row named '.'. #282."""
    monkeypatch.chdir(tmp_path)
    create_demo(tmp_path)
    app = create_app(tmp_path, allowed_hosts=["testserver"], serve_label=".")
    with TestClient(app) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        page = client.get("/settings/panel").text
    assert f'class="repo-name">{tmp_path.resolve().name}</span>' in page
    assert 'class="repo-name">.</span>' not in page


def test_topbar_names_the_project_directory(client, demo_dir):
    """Single-project mode has no repo; the switcher names the directory."""
    page = client.get("/d/demo").text
    assert f'class="dd-label">{demo_dir.name}</span>' in page
    assert 'class="dd-label">dashboards</span>' not in page


def test_switcher_items_carry_their_title(client):
    """#212: a width-capped menu ellipsizes; title keeps the full name hoverable."""
    page = client.get("/d/demo").text
    assert 'title="Order Analytics"' in page
    assert 'src="/static/js/dropdown.js?v=' in page
    assert '"/static/js/dropdown.js":"/static/js/dropdown.js?v=' in page


def test_js_modules_share_one_mtime_url(client):
    """A src ?v= that the import map does not share loads dropdown.js twice."""
    page = client.get("/d/demo").text
    mapped = json.loads(js_importmap())["imports"]["/static/js/dropdown.js"]
    assert mapped == asset_url("js/dropdown.js")
    assert f'src="{mapped}"' in page
    assert f'"/static/js/dropdown.js":"{mapped}"' in page


def test_pages_link_every_app_stylesheet_in_cascade_order(client):
    """The split app css only reproduces the old cascade if every file loads, sorted."""
    names = [
        "01-base.css",
        "02-chrome.css",
        "03-tiles.css",
        "04-query-editor.css",
        "05-edit-mode.css",
        "06-controls.css",
        "07-refinements.css",
        "08-dropdowns.css",
        "09-alignment.css",
        "10-library.css",
        "11-setup.css",
        "12-file-browser.css",
        "13-states.css",
    ]
    css_dir = Path(sqldash.__file__).parent / "static/css/app"
    on_disk = sorted(p.name for p in css_dir.glob("*.css"))
    assert on_disk == names
    for url in ("/", "/d/demo", "/d/demo/query", "/d/demo/workspace"):
        page = client.get(url).text
        hrefs = re.findall(r'href="(/static/css/app/[^"]+)"', page)
        assert [h.split("?v=")[0].rsplit("/", 1)[1] for h in hrefs] == names
        assert hrefs == app_stylesheets()
        assert all("?v=" in h for h in hrefs)


def test_static_js_asks_the_browser_to_revalidate(client):
    res = client.get("/static/js/dropdown.js")
    assert res.status_code == 200
    assert res.headers["cache-control"] == "no-cache"


def test_missing_dashboard_404(client):
    assert client.get("/api/dashboards/nope").status_code == 404


def test_put_rejects_a_slash_in_the_dashboard_name(client, demo_dir):
    """PUT docs/notes wrote a nested file discover() cannot serve, and skipped If-Match. #334."""
    text = (
        "title: Notes\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - title: T\n"
        "    sql: SELECT 1 AS a\n"
    )
    res = client.put(
        "/api/dashboards/docs/notes",
        json={"text": text},
        headers={"If-Match": "deadbeefdeadbeef"},
    )
    assert res.status_code == 422, res.text
    assert "cannot contain '/'" in res.json()["detail"]
    assert not (demo_dir / ".sqldash" / "docs").exists()


def test_missing_dashboard_page_is_html(client):
    """A mistyped /d/nope used to dump raw JSON. Load failures already had HTML. #288."""
    res = client.get("/d/nope")
    assert res.status_code == 404
    assert "text/html" in res.headers["content-type"]
    assert "can&#39;t be loaded" in res.text
    assert "Back to dashboards" in res.text
    query = client.get("/d/nope/query")
    assert query.status_code == 404
    assert "text/html" in query.headers["content-type"]


def _dashboard_page_paths() -> list[str]:
    return sorted(
        route.path
        for route in routes_pages.router.routes
        if route.path.startswith("/d/{name:dname}") and route.response_class is HTMLResponse
    )


def test_every_dashboard_page_route_renders_the_error_page(client, demo_dir):
    """/d/{name}/workspace answered raw JSON while /d/{name} and /query were friendly. #550."""
    paths = _dashboard_page_paths()
    assert "/d/{name:dname}/workspace" in paths
    assert len(paths) >= 3
    bad = demo_dir / ".sqldash" / "wrecked.yaml"
    bad.write_text("title: Wrecked\nsource: {typ: duckdb}\ntiles: []\n")
    try:
        for path in paths:
            for name, status in (("nope", 404), ("wrecked", 422)):
                url = path.replace("{name:dname}", name)
                res = client.get(url)
                assert res.status_code == status, url
                assert "text/html" in res.headers["content-type"], url
                assert "can&#39;t be loaded" in res.text, url
                assert "Back to dashboards" in res.text, url
    finally:
        bad.unlink()


def test_run_query_with_filter_defaults(client):
    _, ex = run_to_completion(client, {"dashboard": "demo", "query": "revenue_by_category"})
    assert ex["status"] == "done", ex.get("error")
    assert ex["result"]["columns"][0]["name"] == "category"
    assert ex["result"]["row_count"] > 0


def test_run_query_with_param_override(client):
    _, ex = run_to_completion(
        client,
        {
            "dashboard": "demo",
            "query": "revenue_share_by_region",
            "params": {"dates_start": "2000-01-01"},
        },
    )
    assert ex["status"] == "done", ex.get("error")
    regions = {row[0] for row in ex["result"]["rows"]}
    assert regions == {"us", "eu", "apac"}


def test_run_negative_row_limit_is_422(client):
    """A negative cap used to return 0 rows truncated:true. #286."""
    res = client.post(
        "/api/run", json={"dashboard": "demo", "sql": "SELECT 1 AS n", "row_limit": -3}
    )
    assert res.status_code == 422, res.text
    assert "row_limit" in res.json()["detail"]


def test_run_row_limit_zero_returns_no_rows(client):
    """`row_limit or default` treated 0 as unset and returned everything. #286."""
    _, ex = run_to_completion(client, {"dashboard": "demo", "sql": "SELECT 1 AS n", "row_limit": 0})
    assert ex["status"] == "done", ex.get("error")
    assert ex["result"]["rows"] == []
    assert ex["result"]["truncated"] is True


def test_run_adhoc_sql(client):
    _, ex = run_to_completion(client, {"dashboard": "demo", "sql": "SELECT 42 AS answer"})
    assert ex["status"] == "done", ex.get("error")
    assert ex["result"]["rows"] == [[42]]


def _writable_duckdb_project(tmp_path):
    import duckdb

    root = tmp_path / ".sqldash"
    root.mkdir()
    conn = duckdb.connect(str(root / "wh.duckdb"))
    conn.execute(
        "CREATE TABLE orders AS SELECT * FROM (VALUES ('a', 1), ('b', 2), ('c', 3)) t(k, v)"
    )
    conn.close()
    (root / "wh.yaml").write_text(
        "title: WH\n"
        "source: {type: duckdb, database: wh.duckdb}\n"
        "queries:\n"
        "  n: SELECT COUNT(*) AS n FROM orders\n"
        "  copycol: SELECT k AS copy, v FROM orders ORDER BY v\n"
        "tiles:\n"
        "  - {title: N, query: n}\n"
        "  - {title: Copy col, query: copycol}\n"
    )
    return root


def _on_disk_count(root):
    import duckdb

    conn = duckdb.connect(str(root / "wh.duckdb"), read_only=True)
    try:
        return conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    finally:
        conn.close()


def test_adhoc_delete_is_refused_and_the_rows_survive(tmp_path):
    """POST /api/run ran DELETE on the pooled connection and committed it to the
    .duckdb file; the tile then read 0 where it read 3. #355."""
    root = _writable_duckdb_project(tmp_path)
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        res = client.post("/api/run", json={"dashboard": "wh", "sql": "DELETE FROM orders"})
        assert res.status_code == 422, res.text
        assert res.json()["detail"] == "ad-hoc sql is read-only; DELETE statements are refused"
        _, ex = run_to_completion(client, {"dashboard": "wh", "query": "n"})
        assert ex["status"] == "done", ex.get("error")
        assert ex["result"]["rows"] == [[3]]
    assert _on_disk_count(root) == 3


def test_adhoc_copy_to_is_refused_and_writes_nothing(tmp_path):
    """COPY ... TO wrote query results to any path the server user can write. #355."""
    _writable_duckdb_project(tmp_path)
    target = tmp_path / "pwned.csv"
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        res = client.post(
            "/api/run",
            json={"dashboard": "wh", "sql": f"COPY (SELECT 1 AS x) TO '{target}'"},
        )
        assert res.status_code == 422, res.text
        assert "read-only" in res.json()["detail"]
        assert "COPY" in res.json()["detail"]
    assert not target.exists()


def test_adhoc_ddl_is_refused_so_nothing_outlives_the_request(tmp_path):
    """A CREATE VIEW on the long-lived connection changed every later request's
    answer for the same name. #355."""
    _writable_duckdb_project(tmp_path)
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        res = client.post(
            "/api/run",
            json={"dashboard": "wh", "sql": "CREATE OR REPLACE VIEW t AS SELECT 0 AS v"},
        )
        assert res.status_code == 422, res.text
        assert "CREATE" in res.json()["detail"]
        _, ex = run_to_completion(client, {"dashboard": "wh", "sql": "SELECT * FROM t"})
        assert ex["status"] == "error", ex


def test_adhoc_templated_into_is_refused_like_the_plain_form(client):
    """A quote inside `{% if %}` hid INTO from the guard, which scanned the
    template; the rendered text dropped the quote and Postgres ran SELECT INTO."""
    plain = client.post(
        "/api/run", json={"dashboard": "demo", "sql": "SELECT 1 AS x INTO pwned_tbl"}
    )
    templated = client.post(
        "/api/run",
        json={
            "dashboard": "demo",
            "sql": "SELECT 1 AS x {% if region %}'{% endif %} INTO pwned_tbl --'",
            "params": {"region": "all"},
        },
    )
    assert plain.status_code == templated.status_code == 422, templated.text
    assert templated.json()["detail"] == plain.json()["detail"]
    assert plain.json()["detail"] == (
        "ad-hoc sql is read-only; a statement containing INTO is refused"
    )


def test_adhoc_templated_read_still_runs(client):
    sql = (
        "SELECT COUNT(*) AS n FROM orders WHERE 1 = 1 "
        "{% if region %}AND region = {{ region }}{% endif %}"
    )
    counts = {}
    for region in ("all", "us"):
        _, ex = run_to_completion(
            client, {"dashboard": "demo", "sql": sql, "params": {"region": region}}
        )
        assert ex["status"] == "done", ex.get("error")
        counts[region] = ex["result"]["rows"][0][0]
    assert 0 < counts["us"] < counts["all"]


def test_adhoc_enable_logging_is_refused_and_the_server_keeps_answering(tmp_path):
    """A SELECT opener carried enable_logging() past the guard: outside the project
    it aborted the server on the pool's rollback, inside it wrote every later
    statement to csv."""
    _writable_duckdb_project(tmp_path)
    logs = tmp_path / "logs"
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        for path in ("/tmp/sqldash-outside", logs):
            sql = f"SELECT * FROM enable_logging(storage='file', storage_path='{path}')"
            res = client.post("/api/run", json={"dashboard": "wh", "sql": sql})
            assert res.status_code == 422, res.text
            assert res.json()["detail"] == (
                "ad-hoc sql is read-only; enable_logging() is refused because it changes "
                "logging or profiling for every later query on the pooled connection"
            )
        _, ex = run_to_completion(client, {"dashboard": "wh", "query": "n"})
        assert ex["status"] == "done", ex.get("error")
        assert ex["result"]["rows"] == [[3]]
    assert not logs.exists()


def test_adhoc_snowflake_abort_session_is_refused(client):
    """Live on Snowflake this ended a session of the source's user."""
    res = client.post(
        "/api/run", json={"dashboard": "demo", "sql": "SELECT SYSTEM$ABORT_SESSION(1) AS r"}
    )
    assert res.status_code == 422, res.text
    assert res.json()["detail"] == (
        "ad-hoc sql is read-only; system$abort_session() is refused because it ends any "
        "session of the source's user"
    )


def test_adhoc_multi_statement_is_refused(client):
    res = client.post("/api/run", json={"dashboard": "demo", "sql": "SELECT 1; SELECT 2"})
    assert res.status_code == 422, res.text
    assert res.json()["detail"] == "ad-hoc sql accepts exactly one statement"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 42 AS answer",
        "WITH x AS (SELECT 42 AS answer) SELECT answer FROM x",
        "-- a note to self\nSELECT 42 AS answer",
        "SELECT 42 AS answer;",
        "SELECT 42 AS answer WHERE ';' <> 'copy'",
    ],
)
def test_adhoc_reads_still_run(client, sql):
    _, ex = run_to_completion(client, {"dashboard": "demo", "sql": sql})
    assert ex["status"] == "done", ex.get("error")
    assert ex["result"]["rows"] == [[42]]


def test_named_query_sql_is_author_trusted(tmp_path):
    """A `queries:` entry whose text would trip the body-keyword check (a column
    called `copy`) still runs: authored SQL is opener-only, not the ad-hoc
    surface. #355 / #500."""
    _writable_duckdb_project(tmp_path)
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        _, ex = run_to_completion(client, {"dashboard": "wh", "query": "copycol"})
        assert ex["status"] == "done", ex.get("error")
        assert ex["result"]["columns"][0]["name"] == "copy"
        assert ex["result"]["rows"] == [["a", 1], ["b", 2], ["c", 3]]


def test_named_query_delete_is_refused_and_the_rows_survive(tmp_path):
    """A tile DELETE ran on every dashboard load and lint stayed green. #500."""
    root = _writable_duckdb_project(tmp_path)
    (root / "cleanup.yaml").write_text(
        "title: DML check\n"
        "source: {type: duckdb, database: wh.duckdb}\n"
        "tiles:\n"
        "  - title: cleanup\n"
        "    chart: table\n"
        "    sql: |\n"
        "      DELETE FROM orders WHERE k = 'a'\n"
    )
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        res = client.post("/api/run", json={"dashboard": "cleanup", "query": "cleanup"})
        assert res.status_code == 422, res.text
        assert res.json()["detail"] == "tile SQL is a write statement"
        _, ex = run_to_completion(client, {"dashboard": "wh", "query": "n"})
        assert ex["status"] == "done", ex.get("error")
        assert ex["result"]["rows"] == [[3]]
    assert _on_disk_count(root) == 3


def test_sqlite_replace_and_vacuum_tiles_are_refused_and_the_file_is_untouched(tmp_path):
    """A REPLACE INTO tile rewrote a row, and a VACUUM tile shrank the file from 447
    pages to 2, on every page load."""
    root = tmp_path / ".sqldash"
    root.mkdir()
    db = root / "data.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, note TEXT)")
    conn.execute("INSERT INTO t VALUES (1, 'original')")
    conn.execute("CREATE TABLE junk (x TEXT)")
    conn.executemany("INSERT INTO junk VALUES (?)", [("x" * 400,)] * 500)
    conn.commit()
    conn.execute("DELETE FROM junk")
    conn.commit()
    conn.close()
    before = db.read_bytes()
    (root / "probe.yaml").write_text(
        "title: Probe\n"
        "source: {type: sqlite, database: data.db}\n"
        "tiles:\n"
        "  - {title: replace a row, chart: table, sql: \"REPLACE INTO t VALUES (1, 'gone')\"}\n"
        "  - {title: reclaim, chart: table, sql: VACUUM}\n"
        "  - {title: rows, chart: table, sql: SELECT note FROM t}\n"
    )
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        for query in ("replace_a_row", "reclaim"):
            res = client.post("/api/run", json={"dashboard": "probe", "query": query})
            assert res.status_code == 422, res.text
            assert res.json()["detail"] == "tile SQL is a write statement"
        _, ex = run_to_completion(client, {"dashboard": "probe", "query": "rows"})
        assert ex["status"] == "done", ex.get("error")
        assert ex["result"]["rows"] == [["original"]]
    assert db.read_bytes() == before


def test_named_query_explain_analyze_delete_is_refused_and_the_rows_survive(tmp_path):
    """DuckDB EXPLAIN ANALYZE executes the statement; opener-only let it through. #500."""
    root = _writable_duckdb_project(tmp_path)
    (root / "cleanup.yaml").write_text(
        "title: DML check\n"
        "source: {type: duckdb, database: wh.duckdb}\n"
        "tiles:\n"
        "  - title: cleanup\n"
        "    chart: table\n"
        "    sql: EXPLAIN ANALYZE DELETE FROM orders\n"
    )
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        res = client.post("/api/run", json={"dashboard": "cleanup", "query": "cleanup"})
        assert res.status_code == 422, res.text
        assert res.json()["detail"] == "tile SQL is a write statement"
        _, ex = run_to_completion(client, {"dashboard": "wh", "query": "n"})
        assert ex["status"] == "done", ex.get("error")
        assert ex["result"]["rows"] == [[3]]
    assert _on_disk_count(root) == 3


def test_run_unknown_query_404(client):
    res = client.post("/api/run", json={"dashboard": "demo", "query": "nope"})
    assert res.status_code == 404


def test_run_query_rejects_a_metric_window(client):
    """CLI errors on query --start; HTTP used to return the unfiltered result
    as done. Same input, same loud no."""
    res = client.post(
        "/api/run", json={"dashboard": "demo", "sql": "SELECT 1 AS n", "start": "-30d"}
    )
    assert res.status_code == 422, res.text
    assert "start/end/filters apply to metrics" in res.json()["detail"]


def test_run_missing_param_422(client):
    res = client.post("/api/run", json={"dashboard": "demo", "sql": "SELECT {{ undeclared }}"})
    assert res.status_code == 422
    assert "undeclared" in res.json()["detail"]


def test_sql_error_surfaces(client):
    _, ex = run_to_completion(client, {"dashboard": "demo", "sql": "SELECT * FROM missing_table"})
    assert ex["status"] == "error"
    assert "missing_table" in ex["error"]


def test_csv_download(client):
    execution_id, ex = run_to_completion(
        client, {"dashboard": "demo", "query": "revenue_share_by_region"}
    )
    assert ex["status"] == "done"
    res = client.get(f"/api/executions/{execution_id}/csv")
    assert res.status_code == 200
    lines = res.text.strip().splitlines()
    assert lines[0] == "region,revenue"
    assert len(lines) == 4
    assert "partial" not in res.headers["content-disposition"]
    assert not any(line.startswith("#") for line in lines)


EXACT_SQL = (
    "SELECT 12345678901234567890::HUGEINT AS huge, 9007199254740993::BIGINT AS edge, "
    "9007199254740991::BIGINT AS safe, -9007199254740991::BIGINT AS neg_safe, "
    "'-10000000000000000000000000000000000001'::HUGEINT AS neg, "
    "'-1E-10'::DECIMAL(38,10) AS tiny, [9007199254740993::HUGEINT] AS nested, true AS flag"
)


def test_the_browser_gets_integers_it_would_round_as_text(client):
    """JSON.parse reads 9007199254740993 as ...992, so an integer past 2^53 goes
    to the page as its decimal text. Integers a double holds exactly stay numbers."""
    execution_id, ex = run_to_completion(client, {"dashboard": "demo", "sql": EXACT_SQL})
    assert ex["status"] == "done", ex
    assert ex["result"]["rows"] == [
        [
            "12345678901234567890",
            "9007199254740993",
            9007199254740991,
            -9007199254740991,
            "-10000000000000000000000000000000000001",
            "-1E-10",
            ["9007199254740993"],
            True,
        ]
    ]
    assert [c["type"] for c in ex["result"]["columns"]][:6] == [
        "integer",
        "integer",
        "integer",
        "integer",
        "integer",
        "decimal",
    ]
    rows = list(csv.reader(io.StringIO(client.get(f"/api/executions/{execution_id}/csv").text)))
    assert rows[1][:6] == [
        "12345678901234567890",
        "9007199254740993",
        "9007199254740991",
        "-9007199254740991",
        "-10000000000000000000000000000000000001",
        "-1E-10",
    ]


def test_csv_download_writes_json_cells_as_json_and_non_finite_floats(client):
    """A list or struct cell was written as its Python repr (`{'k': 'v'}`), and a
    NaN was an empty cell like NULL."""
    sql = "SELECT [1, NULL] AS l, {'k': 'v'} AS s, 'NaN'::DOUBLE AS n, NULL::DOUBLE AS z"
    execution_id, ex = run_to_completion(client, {"dashboard": "demo", "sql": sql})
    assert ex["status"] == "done", ex
    assert ex["result"]["rows"] == [[[1, None], {"k": "v"}, "NaN", None]]
    rows = list(csv.reader(io.StringIO(client.get(f"/api/executions/{execution_id}/csv").text)))
    assert rows == [["l", "s", "n", "z"], ["[1,null]", '{"k":"v"}', "NaN", ""]]


def test_csv_download_of_a_truncated_result_says_so(client):
    """A row-capped execution exported the capped window as if it were the
    whole result: no marker in the file, none in the name. #362"""
    execution_id, ex = run_to_completion(
        client,
        {
            "dashboard": "demo",
            "sql": "SELECT * FROM generate_series(1, 500) AS g(x)",
            "row_limit": 10,
        },
    )
    assert ex["status"] == "done"
    assert ex["result"]["truncated"] is True
    res = client.get(f"/api/executions/{execution_id}/csv?name=Big table")
    assert res.status_code == 200
    assert 'filename="Big-table-partial.csv"' in res.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(res.text)))
    assert rows[0] == ["x"]
    assert [r[0] for r in rows[1:11]] == [str(i) for i in range(1, 11)]
    assert len(rows) == 12
    assert len(rows[11]) == len(rows[0])
    assert rows[11][0].startswith("# truncated: first 10 rows only")
    assert "--row-limit" in rows[11][0]


def test_truncated_csv_note_is_padded_to_the_header_width(client):
    """A ragged last row is refused wholesale by a strict importer with a fixed
    schema (psql \\copy), so the note carries the header's column count. #362 review."""
    execution_id, _ = run_to_completion(
        client,
        {
            "dashboard": "demo",
            "sql": (
                "SELECT g.x AS a, g.x * 2 AS b, g.x * 3 AS c FROM generate_series(1, 50) AS g(x)"
            ),
            "row_limit": 4,
        },
    )
    res = client.get(f"/api/executions/{execution_id}/csv?name=Wide")
    rows = list(csv.reader(io.StringIO(res.text)))
    assert rows[0] == ["a", "b", "c"]
    assert len(rows) == 6
    assert len(rows[5]) == 3, rows[5]
    assert rows[5][1:] == ["", ""], rows[5]
    assert rows[5][0].startswith("# truncated: first 4 rows only")


def test_csv_download_neutralizes_spreadsheet_formulas(client):
    """`=HYPERLINK(...)` from the warehouse ran when the export was opened in a
    spreadsheet. Numbers, including a decimal that arrives as a string, stay numbers."""
    sql = (
        'SELECT * FROM (VALUES (\'=HYPERLINK("http://example.invalid","x")\', -3.5, '
        "CAST(-2.25 AS DECIMAL(5,2)), '-3.5', '+SUM(1)', '@cmd', chr(9) || '=1', "
        "chr(13) || '=2', '-1+2', 'plain', 42, '1e-3')) "
        'AS t("=head", f, d, s, plus, at_sign, tab, cr, minus_formula, plain, n, sci)'
    )
    execution_id, ex = run_to_completion(client, {"dashboard": "demo", "sql": sql})
    assert ex["status"] == "done", ex
    res = client.get(f"/api/executions/{execution_id}/csv")
    rows = list(csv.reader(io.StringIO(res.text, newline="")))
    assert rows[0][0] == "'=head"
    assert rows[1] == [
        '\'=HYPERLINK("http://example.invalid","x")',
        "-3.5",
        "-2.25",
        "-3.5",
        "'+SUM(1)",
        "'@cmd",
        "'\t=1",
        "'\r=2",
        "'-1+2",
        "plain",
        "42",
        "1e-3",
    ]


SPREADSHEET_CORPUS = json.loads(
    (Path(__file__).parent / "spreadsheet_safe.json").read_text(encoding="utf-8")
)["cases"]


@pytest.mark.parametrize("case", SPREADSHEET_CORPUS, ids=lambda c: repr(c["value"]))
def test_spreadsheet_safe_matches_the_shared_corpus(case):
    """csv-safe.js (test_csv_safe.mjs) and the CLI csv writer (test_cli.py) read the same file."""
    assert spreadsheet_safe(case["value"]) == case["want"]


def test_formula_guard_leaves_the_truncation_note_alone(client):
    execution_id, _ = run_to_completion(
        client,
        {
            "dashboard": "demo",
            "sql": "SELECT '=' || g.x AS a, -g.x AS b FROM generate_series(1, 50) AS g(x)",
            "row_limit": 3,
        },
    )
    rows = list(csv.reader(io.StringIO(client.get(f"/api/executions/{execution_id}/csv").text)))
    assert rows[1:4] == [["'=1", "-1"], ["'=2", "-2"], ["'=3", "-3"]]
    assert rows[4][0].startswith("# truncated: first 3 rows only")
    assert rows[4][1:] == [""]


def test_csv_download_uses_the_tile_name(client):
    """The ⤓ button used to save c96b57041942.csv. #281."""
    execution_id, ex = run_to_completion(
        client, {"dashboard": "demo", "query": "revenue_share_by_region"}
    )
    assert ex["status"] == "done"
    res = client.get(f"/api/executions/{execution_id}/csv?name=Revenue by category")
    assert res.status_code == 200
    assert 'filename="Revenue-by-category.csv"' in res.headers["content-disposition"]


def test_dashboard_crud_roundtrip(client, demo_dir):
    res = client.get("/api/dashboards/demo")
    assert res.status_code == 200
    body = res.json()
    etag = body["etag"]
    # The response carries no raw file text — it would hand back every literal
    # credential `redact_source` has just removed from `source`. A whole-file
    # PUT is driven from the file itself, which its caller already has.
    text = (demo_dir / ".sqldash" / "demo.yaml").read_text()

    res = client.put("/api/dashboards/demo", json={"text": text}, headers={"If-Match": etag})
    assert res.status_code == 200

    res = client.put(
        "/api/dashboards/demo", json={"text": text}, headers={"If-Match": "0000000000000000"}
    )
    assert res.status_code == 409

    res = client.put(
        "/api/dashboards/demo", json={"text": "title: broken\n"}, headers={"If-Match": etag}
    )
    assert res.status_code == 422


def test_source_auth_never_in_api(client):
    body = client.get("/api/dashboards/demo").json()
    assert body["dashboard"]["source"]["type"] == "duckdb"
    assert "password" not in str(body["dashboard"]["source"])


def test_get_then_put_a_hoisted_tile_round_trips(tmp_path):
    """No API response carries raw file text, so GET + PUT is the only way a
    script can edit a tile — and GET hands back the hoisted tile as `query:
    <name>, sql: null`. Sending that straight back with a new title used to
    422 `references unknown query` for a query the file defines (#392)."""
    create_demo(tmp_path)
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        body = c.get("/api/dashboards/demo").json()
        tile = next(t for t in body["dashboard"]["tiles"] if t["id"] == "revenue_by_category")
        assert (tile["query"], tile["sql"]) == ("revenue_by_category", None)
        tile["title"] = "Revenue by Category Renamed"
        res = c.put(
            "/api/dashboards/demo/tiles/revenue_by_category",
            json={"tile": tile},
            headers={"If-Match": body["etag"]},
        )
        assert res.status_code == 200, res.text
    text = (tmp_path / ".sqldash" / "demo.yaml").read_text()
    assert "  - title: Revenue by Category Renamed\n" in text
    assert "queries:" not in text
    assert "    sql: |\n      SELECT category, ROUND(SUM(amount), 2) AS revenue\n" in text


def test_deleting_the_last_named_query_tile_drops_the_queries_key(tmp_path):
    """POST a query-bearing tile then DELETE it must not leave `queries: {}`.
    Demo.yaml has no `queries:` key; the residue used to survive every later
    UI edit. #462."""
    create_demo(tmp_path)
    yaml_path = tmp_path / ".sqldash" / "demo.yaml"
    assert "queries:" not in yaml_path.read_text()
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        etag = c.get("/api/dashboards/demo").json()["etag"]
        res = c.post(
            "/api/dashboards/demo/tiles",
            json={
                "tile": {
                    "title": "Temp tile",
                    "query": "temp_q",
                    "chart": "table",
                    "position": {"x": 0, "y": 12, "w": 6, "h": 4},
                },
                "sql": "SELECT 1",
            },
            headers={"If-Match": etag},
        )
        assert res.status_code == 200, res.text
        assert "temp_q:" in yaml_path.read_text()
        etag = c.get("/api/dashboards/demo").json()["etag"]
        res = c.delete(
            "/api/dashboards/demo/tiles/temp_tile",
            headers={"If-Match": etag},
        )
        assert res.status_code == 200, res.text
    text = yaml_path.read_text()
    assert "queries:" not in text, text
    assert "temp_q" not in text
    assert "Temp tile" not in text


def test_post_creates_a_metric_tile_without_sql(tmp_path):
    """The query-page Metric mode POSTs this shape. The tile must stay a metric
    ref — compiled SQL must not land in queries: or on the tile."""
    from sqldash.project.store import DashboardStore

    create_demo(tmp_path)
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        etag = c.get("/api/dashboards/demo").json()["etag"]
        res = c.post(
            "/api/dashboards/demo/tiles",
            json={
                "tile": {
                    "id": "revenue_from_ui",
                    "type": "chart",
                    "title": "Revenue from UI",
                    "metric": {"name": "revenue"},
                    "chart": {"type": "big_number", "format": "currency"},
                },
                "sql": None,
            },
            headers={"If-Match": etag},
        )
        assert res.status_code == 200, res.text
        ids = [t["id"] for t in res.json()["tiles"]]
        assert "revenue_from_ui" in ids
        dup = c.post(
            "/api/dashboards/demo/tiles",
            json={
                "tile": {
                    "id": "revenue_from_ui",
                    "type": "chart",
                    "title": "Revenue from UI",
                    "metric": {"name": "revenue"},
                    "chart": {"type": "big_number", "format": "currency"},
                },
                "sql": None,
            },
            headers={"If-Match": res.json()["etag"]},
        )
        assert dup.status_code == 409, dup.text
        assert "already exists" in dup.json()["detail"]["message"]
        assert any(t["id"] == "revenue_from_ui" for t in dup.json()["detail"]["tiles"])
        empty = c.post(
            "/api/dashboards/demo/tiles",
            json={"tile": {"type": "text", "title": None, "markdown": "x"}, "sql": None},
            headers={"If-Match": res.json()["etag"]},
        )
        assert empty.status_code == 422, empty.text
        assert "tile id is required" in empty.text
    text = (tmp_path / ".sqldash" / "demo.yaml").read_text()
    tail = text.split("Revenue from UI", 1)[1]
    cut = tail.find("\n  - ")
    block = tail if cut < 0 else tail[:cut]
    assert "metric: revenue" in block, block
    assert "sql:" not in block, block
    dash, _, _ = DashboardStore(tmp_path / ".sqldash").load("demo")
    tile = next(t for t in dash.tiles if t.title == "Revenue from UI")
    assert tile.metric is not None
    assert tile.metric.name == "revenue"
    assert tile.query is None
    assert tile.sql is None


def test_post_tile_rejects_unknown_tile_keys(tmp_path):
    """A typo'd field used to extra=ignore inside `tile`, so `metrik` next to
    `metric` 200'd an orders tile. #461."""
    create_demo(tmp_path)
    yaml_path = tmp_path / ".sqldash" / "demo.yaml"
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        etag = c.get("/api/dashboards/demo").json()["etag"]
        before = yaml_path.read_text()
        res = c.post(
            "/api/dashboards/demo/tiles",
            json={
                "tile": {
                    "title": "Typo test",
                    "metrik": "revenue",
                    "metric": "order_count",
                    "position": {"x": 0, "y": 12, "w": 6, "h": 4},
                }
            },
            headers={"If-Match": etag},
        )
    assert res.status_code == 422, res.text
    assert "metrik" in res.text
    assert "did you mean 'metric'" in res.text
    assert yaml_path.read_text() == before
    assert "Typo test" not in yaml_path.read_text()


def test_post_tile_rejects_tile_level_dimensions(tmp_path):
    """tile-level dimensions: is not a grain-style shorthand; it belongs inside
    metric: {name, dimensions, grain}. #480."""
    create_demo(tmp_path)
    yaml_path = tmp_path / ".sqldash" / "demo.yaml"
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        etag = c.get("/api/dashboards/demo").json()["etag"]
        before = yaml_path.read_text()
        res = c.post(
            "/api/dashboards/demo/tiles",
            json={
                "tile": {
                    "title": "By region",
                    "metric": "revenue",
                    "dimensions": ["region"],
                    "position": {"x": 0, "y": 12, "w": 6, "h": 4},
                }
            },
            headers={"If-Match": etag},
        )
    assert res.status_code == 422, res.text
    assert "metric: {name, dimensions, grain}" in res.text
    assert yaml_path.read_text() == before


def test_post_tile_dimensions_does_not_hide_a_metric_typo(tmp_path):
    """A tile with both `dimensions:` and `metrik` used to report only the
    dimensions hint and swallow the `did you mean 'metric'?` suggestion. #480."""
    create_demo(tmp_path)
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        etag = c.get("/api/dashboards/demo").json()["etag"]
        res = c.post(
            "/api/dashboards/demo/tiles",
            json={
                "tile": {
                    "title": "By region",
                    "metrik": "revenue",
                    "dimensions": ["region"],
                    "position": {"x": 0, "y": 12, "w": 6, "h": 4},
                }
            },
            headers={"If-Match": etag},
        )
    assert res.status_code == 422, res.text
    assert "metric: {name, dimensions, grain}" in res.text
    assert "did you mean 'metric'" in res.text


def test_post_tile_model_error_carries_one_value_error_prefix(tmp_path):
    """The validator re-raised pydantic's own message, which already carries
    "Value error, ", so pydantic prefixed it again. #649."""
    create_demo(tmp_path)
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        etag = c.get("/api/dashboards/demo").json()["etag"]
        res = c.post(
            "/api/dashboards/demo/tiles",
            json={"tile": {"title": "no data here", "size": "6x2"}},
            headers={"If-Match": etag},
        )
    assert res.status_code == 422, res.text
    message = res.json()["detail"][0]["msg"]
    assert message == (
        "Value error, tile 'no data here': chart tiles require exactly one of "
        "'query', 'sql', or 'metric'"
    ), message


def test_post_tile_sql_inside_tile_names_the_sibling(tmp_path):
    """`sql` inside `tile` used to be dropped before parse, so the 422 said
    the tile had none of query/sql/metric while `sql` was in the body. #461."""
    create_demo(tmp_path)
    yaml_path = tmp_path / ".sqldash" / "demo.yaml"
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        etag = c.get("/api/dashboards/demo").json()["etag"]
        before = yaml_path.read_text()
        res = c.post(
            "/api/dashboards/demo/tiles",
            json={
                "tile": {
                    "title": "X",
                    "chart": "table",
                    "sql": "SELECT 1",
                    "position": {"x": 0, "y": 12, "w": 6, "h": 4},
                }
            },
            headers={"If-Match": etag},
        )
    assert res.status_code == 422, res.text
    assert "sibling" in res.text
    assert yaml_path.read_text() == before


def test_put_stale_tile_id_after_title_rename_does_not_duplicate(tmp_path):
    """PUT used to insert-or-replace, so a client holding the pre-rename
    derived id appended a second tile instead of 404ing."""
    from sqldash.project.store import DashboardStore

    create_demo(tmp_path)
    yaml_path = tmp_path / ".sqldash" / "demo.yaml"
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    payload = {
        "tile": {
            "type": "chart",
            "title": "Total revenue (v2)",
            "metric": {"name": "revenue", "compare": "previous_period"},
            "chart": {"type": "big_number"},
        },
        "sql": None,
    }
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        etag = c.get("/api/dashboards/demo").json()["etag"]
        renamed = c.put(
            "/api/dashboards/demo/tiles/total_revenue",
            json=payload,
            headers={"If-Match": etag},
        )
        assert renamed.status_code == 200, renamed.text
        ids = [t["id"] for t in renamed.json()["tiles"]]
        assert "total_revenue_v2" in ids
        assert "total_revenue" not in ids
        stale = c.put(
            "/api/dashboards/demo/tiles/total_revenue",
            json=payload,
            headers={"If-Match": renamed.json()["etag"]},
        )
        assert stale.status_code == 404, stale.text
        detail = stale.json()["detail"]
        assert "unknown tile 'total_revenue'" in detail["message"]
        current_ids = [t["id"] for t in detail["tiles"]]
        assert "total_revenue_v2" in current_ids
        assert "total_revenue" not in current_ids
    dash, _, _ = DashboardStore(tmp_path / ".sqldash").load("demo")
    assert [t.title for t in dash.tiles].count("Total revenue (v2)") == 1
    assert not any(t.id == "total_revenue" for t in dash.tiles)
    assert yaml_path.read_text().count("Total revenue (v2)") == 1


def test_put_clearing_a_tile_level_grain_changes_the_file(tmp_path):
    """#508: the query page's "none" grain on the demo's `grain: day` tile
    answered 200 and left the file byte-identical. GET returns the folded
    grain only inside `metric`, so a round trip clears it the same way."""
    create_demo(tmp_path)
    yaml_path = tmp_path / ".sqldash" / "demo.yaml"
    before = yaml_path.read_text()
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        body = c.get("/api/dashboards/demo").json()
        tile = next(t for t in body["dashboard"]["tiles"] if t["id"] == "daily_revenue")
        assert tile["metric"]["grain"] == "day"
        assert tile["grain"] is None
        tile["metric"]["grain"] = None
        res = c.put(
            "/api/dashboards/demo/tiles/daily_revenue",
            json={"tile": tile, "sql": None},
            headers={"If-Match": body["etag"]},
        )
        assert res.status_code == 200, res.text
        assert yaml_path.read_text() == before.replace("    grain: day\n", "", 1)
        body = c.get("/api/dashboards/demo").json()
        tile = next(t for t in body["dashboard"]["tiles"] if t["id"] == "daily_revenue")
        assert tile["metric"]["grain"] is None


def test_put_a_tile_level_grain_sets_it(tmp_path):
    """The documented shorthand `metric: revenue` + `grain: month` sets the grain
    over the API rather than reading the empty nested metric as a clear."""
    create_demo(tmp_path)
    yaml_path = tmp_path / ".sqldash" / "demo.yaml"
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        body = c.get("/api/dashboards/demo").json()
        tile = next(t for t in body["dashboard"]["tiles"] if t["id"] == "daily_revenue")
        tile.update(metric="revenue", grain="month")
        res = c.put(
            "/api/dashboards/demo/tiles/daily_revenue",
            json={"tile": tile, "sql": None},
            headers={"If-Match": body["etag"]},
        )
        assert res.status_code == 200, res.text
        assert "grain: day" not in yaml_path.read_text()
        body = c.get("/api/dashboards/demo").json()
        tile = next(t for t in body["dashboard"]["tiles"] if t["id"] == "daily_revenue")
        assert tile["metric"]["grain"] == "month"


def test_query_page(client):
    res = client.get("/d/demo/query")
    assert res.status_code == 200
    assert "sql-editor" in res.text
    assert "ace.js" in res.text
    assert "revenue_by_category" in res.text
    assert "new tile" in res.text
    assert "Save tile" not in res.text
    assert 'data-mode="metric"' in res.text
    assert "metric-picker" in res.text


def test_query_page_edit_loads_the_tile(client):
    res = client.get("/d/demo/query", params={"tile": "revenue_by_category"})
    assert res.status_code == 200
    assert "edit tile" in res.text
    assert "Save tile" in res.text
    assert 'value="Revenue by category"' in res.text


def test_query_page_unknown_tile_is_not_a_blank_new_tile(client):
    res = client.get("/d/demo/query", params={"tile": "nope"})
    assert res.status_code == 200
    assert "<title>Tile not found · Order Analytics · sqldash</title>" in res.text
    assert "No tile 'nope' on Order Analytics" in res.text
    assert "tile not found" in res.text
    assert ">new tile<" not in res.text


def test_query_page_unknown_tile_id_is_escaped(client):
    res = client.get("/d/demo/query", params={"tile": "<script>x</script>"})
    assert res.status_code == 200
    assert "<script>x</script>" not in res.text
    assert "No tile '&lt;script&gt;x&lt;/script&gt;'" in res.text


def test_dashboard_page_has_no_tile_drawer(client):
    res = client.get("/d/demo")
    assert res.status_code == 200
    assert "tile-drawer" not in res.text
    assert "/static/vendor/ace/ace.js" not in res.text
    assert "/d/demo/query?tile=revenue_by_category" in res.text


def test_daterange_defaults_resolve(client):
    _, ex = run_to_completion(client, {"dashboard": "demo", "query": "recent_orders"})
    assert ex["status"] == "done", ex.get("error")
    assert ex["result"]["row_count"] > 0


def test_daterange_override(client):
    _, ex = run_to_completion(
        client,
        {
            "dashboard": "demo",
            "query": "recent_orders",
            "params": {"dates_start": "2000-01-01", "dates_end": "2000-01-02"},
        },
    )
    assert ex["status"] == "done", ex.get("error")
    assert ex["result"]["row_count"] == 0


def test_api_metrics_lists_semantic_layer(client):
    body = client.get("/api/metrics").json()
    names = {m["name"] for m in body["metrics"]}
    assert {"revenue", "order_count", "avg_order_value"} <= names
    revenue = next(m for m in body["metrics"] if m["name"] == "revenue")
    assert revenue["origin"] == "project"
    assert {d["name"] for d in revenue["dimensions"]} == {"region", "category"}
    scoped = client.get("/api/metrics", params={"dashboard": "demo"}).json()["metrics"]
    assert {m["name"] for m in scoped} >= {"revenue", "order_count"}


def test_run_by_metric_with_filter_defaults(client):
    _, ex = run_to_completion(
        client, {"dashboard": "demo", "metric": "revenue", "dimensions": ["region"]}
    )
    assert ex["status"] == "done", ex.get("error")
    assert [c["name"] for c in ex["result"]["columns"]] == ["region", "revenue"]
    assert ex["result"]["row_count"] >= 2


def test_run_by_metric_with_the_last_representable_end_day(client):
    """A date-only end compiles as `< next day`; 9999-12-31 has no next day and
    used to raise OverflowError instead of keeping the inclusive bound."""
    _, ex = run_to_completion(
        client,
        {
            "dashboard": "demo",
            "metric": "revenue",
            "params": {"dates_start": "2026-01-01", "dates_end": "9999-12-31"},
        },
    )
    assert ex["status"] == "done", ex.get("error")
    assert ex["result"]["rows"][0][0] > 0


def test_run_by_metric_with_grain(client):
    _, ex = run_to_completion(client, {"dashboard": "demo", "metric": "revenue", "grain": "week"})
    assert ex["status"] == "done", ex.get("error")
    assert ex["result"]["columns"][0]["name"] == "order_date"


def test_run_by_metric_respects_region_filter(client):
    _, ex = run_to_completion(
        client,
        {
            "dashboard": "demo",
            "metric": "revenue",
            "dimensions": ["region"],
            "params": {"region": "us"},
        },
    )
    assert ex["status"] == "done", ex.get("error")
    assert [row[0] for row in ex["result"]["rows"]] == ["us"]


def test_run_metric_unknown_param_name_422(client):
    res = client.post(
        "/api/run",
        json={
            "dashboard": "demo",
            "metric": "revenue",
            "dimensions": ["region"],
            "params": {"regoin": "us"},
        },
    )
    assert res.status_code == 422
    assert res.json()["detail"] == (
        "unknown filter dimension 'regoin' — valid dimensions: region, category"
    )


@pytest.mark.parametrize(
    "value",
    [{"$gt": "a"}, {"op": "in", "value": {"a": 1}}, {"op": "=", "value": {"a": 1}}],
)
def test_run_metric_unsupported_filter_shape_422(client, value):
    """#658: the first shape 422'd as "has no value", the second was accepted, bound
    into `IN (?)` and answered 202 before failing as a warehouse cast error. Both are
    the caller's shape, so both are refused at the boundary with the filter named."""
    res = client.post(
        "/api/run",
        json={
            "dashboard": "demo",
            "metric": "revenue",
            "dimensions": ["region"],
            "filters": {"region": value},
        },
    )
    assert res.status_code == 422
    detail = res.json()["detail"]
    assert "region" in detail
    assert "accepted shapes" in detail
    assert "has no value" not in detail


@pytest.mark.parametrize("op", [">", "!="])
def test_run_metric_explicit_op_with_a_list_422(client, op):
    """#668: this 202'd and ran `region IN (?, ?)`, so `!=` returned the very rows
    it was asked to leave out."""
    res = client.post(
        "/api/run",
        json={
            "dashboard": "demo",
            "metric": "revenue",
            "dimensions": ["region"],
            "filters": {"region": {"op": op, "value": ["eu", "us"]}},
        },
    )
    assert res.status_code == 422
    assert f"op '{op}' with a list value" in res.json()["detail"]


def test_run_metric_explicit_equals_with_a_list_matches_a_bare_list(client):
    """#668: spelling `op: "="` means the same membership as omitting it, so the two
    shapes return the same rows."""
    rows = []
    for value in (["eu", "us"], {"op": "=", "value": ["eu", "us"]}):
        _, ex = run_to_completion(
            client,
            {
                "dashboard": "demo",
                "metric": "revenue",
                "dimensions": ["region"],
                "filters": {"region": value},
            },
        )
        assert ex["status"] == "done", ex.get("error")
        rows.append(ex["result"]["rows"])
    assert rows[0] == rows[1]
    assert sorted(r[0] for r in rows[0]) == ["eu", "us"]


def test_run_query_unknown_param_name_422(client):
    """#626: the query route forwarded params verbatim and `prepare_sql` only
    looked up what the rendered SQL still mentions, so the typo ran unfiltered
    and answered 200 while the same name on the metric route 422'd."""
    filtered = run_to_completion(
        client,
        {"dashboard": "demo", "query": "revenue_by_category", "params": {"region": "eu"}},
    )[1]
    assert filtered["status"] == "done", filtered.get("error")
    res = client.post(
        "/api/run",
        json={"dashboard": "demo", "query": "revenue_by_category", "params": {"regoin": "eu"}},
    )
    assert res.status_code == 422
    assert res.json()["detail"] == (
        "unknown parameter 'regoin' — valid parameters: dates, dates_end, dates_start, region"
    )
    assert (
        filtered["result"]["rows"]
        != run_to_completion(
            client, {"dashboard": "demo", "query": "revenue_by_category", "params": {}}
        )[1]["result"]["rows"]
    )


def test_run_adhoc_sql_unknown_param_name_422(client):
    res = client.post(
        "/api/run",
        json={
            "dashboard": "demo",
            "sql": "SELECT {{ region }} AS r",
            "params": {"regoin": "eu"},
        },
    )
    assert res.status_code == 422
    assert "unknown parameter 'regoin'" in res.json()["detail"]


def test_run_query_param_only_inside_an_untaken_block_still_runs(tmp_path):
    """The rendered SQL is not the vocabulary: `{{ tier }}` inside an untaken
    `{% if region %}` is still a name this query defines. #626."""
    (tmp_path / "cond.yaml").write_text(
        "title: Conditional\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "filters:\n"
        "  - {name: region, type: select, options: [all, eu]}\n"
        "queries:\n"
        "  q: |\n"
        "    SELECT 1 AS n\n"
        "    {% if region %}WHERE {{ region }} = {{ tier }}{% endif %}\n"
        "tiles: [{title: T, query: q}]\n"
    )
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        _, ex = run_to_completion(c, {"dashboard": "cond", "query": "q", "params": {"tier": "g"}})
        assert ex["status"] == "done", ex.get("error")
        assert ex["result"]["rows"] == [[1]]
        res = c.post("/api/run", json={"dashboard": "cond", "query": "q", "params": {"teir": "g"}})
    assert res.status_code == 422
    assert res.json()["detail"] == "unknown parameter 'teir' — valid parameters: region, tier"


def _branching_app(tmp_path):
    (tmp_path / "cond.yaml").write_text(
        "title: Branching\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "filters:\n"
        "  - {name: region, type: select, options: [all, eu]}\n"
        "queries:\n"
        "  q: |\n"
        "    {% if region %}SELECT {{ region }} AS v\n"
        "    {% elif tier %}SELECT {{ tier }} AS v\n"
        "    {% else %}SELECT 'none' AS v{% endif %}\n"
        "tiles: [{title: T, query: q}]\n"
    )
    return create_app(tmp_path, allowed_hosts=["testserver"])


def test_run_query_else_and_elif_branches_run_and_bind(tmp_path):
    """#657: an {% else %} beside a bare-param {% if %} was refused as nesting.
    Each branch's param is still a bind — a hostile value comes back as a value,
    not as SQL the warehouse ran."""
    hostile = "'; DROP TABLE t; --"
    app = _branching_app(tmp_path)
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        for params, expected in (
            ({"region": "eu"}, "eu"),
            ({"tier": "gold"}, "gold"),
            ({}, "none"),
            ({"tier": hostile}, hostile),
        ):
            _, ex = run_to_completion(c, {"dashboard": "cond", "query": "q", "params": params})
            assert ex["status"] == "done", ex.get("error")
            assert ex["result"]["rows"] == [[expected]], params


def test_run_query_elif_name_is_part_of_the_query_vocabulary(tmp_path):
    """An {% elif %} name has to be extracted like an {% if %} name, or the
    branch is unreachable and `refuse_invented_params` calls the name a typo."""
    app = _branching_app(tmp_path)
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        res = c.post("/api/run", json={"dashboard": "cond", "query": "q", "params": {"teir": "g"}})
    assert res.status_code == 422
    assert res.json()["detail"] == "unknown parameter 'teir' — valid parameters: region, tier"


def test_run_metric_applies_a_dimension_the_dashboard_has_no_filter_for(client):
    _, ex = run_to_completion(
        client,
        {
            "dashboard": "demo",
            "metric": "revenue",
            "dimensions": ["region"],
            "params": {"category": "apparel"},
        },
    )
    assert ex["status"] == "done", ex.get("error")
    filtered = {row[0]: row[1] for row in ex["result"]["rows"]}
    _, whole = run_to_completion(
        client, {"dashboard": "demo", "metric": "revenue", "dimensions": ["region"]}
    )
    unfiltered = {row[0]: row[1] for row in whole["result"]["rows"]}
    assert filtered
    assert all(filtered[k] < unfiltered[k] for k in filtered)


def test_run_metric_skips_unrelated_dashboard_filters(tmp_path):
    """The browser posts every current filter value with every tile run, and a
    dashboard filter that is not a metric dimension must keep being skipped."""
    create_demo(tmp_path)
    path = tmp_path / ".sqldash" / "demo.yaml"
    daterange = "  - {name: dates, type: daterange, label: Date range, default: last_60_days}\n"
    assert daterange in path.read_text()
    path.write_text(
        path.read_text().replace(
            daterange,
            daterange
            + "  - {name: nodefault, type: select, label: Unrelated, options: [a, b]}\n"
            + "  - {name: alldflt, type: select, label: Off, default: all, options: [all, a, b]}\n",
        )
    )
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        warm_up(c, "demo")
        _, ex = run_to_completion(
            c,
            {
                "dashboard": "demo",
                "metric": "revenue",
                "dimensions": ["region"],
                "params": {
                    "dates_start": "2020-01-01",
                    "dates_end": "2030-01-01",
                    "region": "us",
                    "nodefault": "",
                    "alldflt": "all",
                },
            },
        )
        assert ex["status"] == "done", ex.get("error")
        assert [row[0] for row in ex["result"]["rows"]] == ["us"]


def test_run_unknown_metric_404(client):
    res = client.post("/api/run", json={"dashboard": "demo", "metric": "nope"})
    assert res.status_code == 404
    assert "revenue" in res.json()["detail"]


def test_run_metric_unknown_dimension_422(client):
    res = client.post(
        "/api/run",
        json={"dashboard": "demo", "metric": "revenue", "dimensions": ["region; DROP TABLE x"]},
    )
    assert res.status_code == 422
    assert "valid dimensions" in res.json()["detail"]


def test_update_meta_endpoint(client):
    etag = client.get("/api/dashboards/demo").json()["etag"]
    res = client.patch(
        "/api/dashboards/demo/meta",
        json={"title": "Order Analytics v2", "description": "Edited from the UI"},
        headers={"If-Match": etag},
    )
    assert res.status_code == 200
    body = client.get("/api/dashboards/demo").json()
    assert body["dashboard"]["title"] == "Order Analytics v2"
    client.patch(
        "/api/dashboards/demo/meta",
        json={
            "title": "Order Analytics",
            "description": "Demo dashboard over a local CSV — swap the source for your warehouse.",
        },
        headers={"If-Match": body["etag"]},
    )


def test_update_filters_endpoint(client):
    body = client.get("/api/dashboards/demo").json()
    original = body["dashboard"]["filters"]
    region0 = next(f for f in original if f["name"] == "region")
    assert not region0.get("options"), region0
    assert region0.get("options_sql")
    new_filters = [
        {
            k: v
            for k, v in f.items()
            if k in ("name", "type", "label", "default", "options", "bind") and v is not None
        }
        for f in original
    ] + [
        {
            "name": "category",
            "type": "select",
            "label": "Category",
            "default": "all",
            "options": ["all", "tools", "toys"],
        }
    ]
    res = client.put(
        "/api/dashboards/demo/filters",
        json={"filters": new_filters},
        headers={"If-Match": body["etag"]},
    )
    assert res.status_code == 200, res.text
    body2 = client.get("/api/dashboards/demo").json()
    assert [f["name"] for f in body2["dashboard"]["filters"]] == ["dates", "region", "category"]
    region = next(f for f in body2["dashboard"]["filters"] if f["name"] == "region")
    assert region["options_sql"], "options_sql must survive a UI filter round-trip"


def test_update_filters_accepts_get_payload(client, demo_dir):
    """GET injects resolved_default; PUT of that list used to 422 extra inputs. #444."""
    body = client.get("/api/dashboards/demo").json()
    filters = body["dashboard"]["filters"]
    dates = next(f for f in filters if f["name"] == "dates")
    assert "resolved_default" in dates
    names = [f["name"] for f in filters]
    yaml_path = demo_dir / ".sqldash" / "demo.yaml"
    before = yaml_path.read_text()
    res = client.put(
        "/api/dashboards/demo/filters",
        json={"filters": filters},
        headers={"If-Match": body["etag"]},
    )
    assert res.status_code == 200, res.text
    assert yaml_path.read_text() == before
    body2 = client.get("/api/dashboards/demo").json()
    assert [f["name"] for f in body2["dashboard"]["filters"]] == names
    typo = [
        {**{k: v for k, v in f.items() if k != "resolved_default"}, "typo_field": 1}
        for f in filters
    ]
    res = client.put(
        "/api/dashboards/demo/filters",
        json={"filters": typo},
        headers={"If-Match": body2["etag"]},
    )
    assert res.status_code == 422, res.text
    assert "typo_field" in res.text


def test_filter_yaml_still_forbids_resolved_default():
    from sqldash.project.store import InvalidDashboardError, parse_dashboard

    with pytest.raises(InvalidDashboardError, match="resolved_default"):
        parse_dashboard(
            "title: T\n"
            "source: {type: duckdb, database: ':memory:'}\n"
            "filters:\n"
            "  - {name: dates, type: daterange, resolved_default: x}\n"
            "tiles: []\n"
        )


def test_multi_source_run(client, demo_dir):
    import textwrap

    (demo_dir / ".sqldash" / "multi.yaml").write_text(
        textwrap.dedent("""
        title: Multi Source
        source: {type: duckdb, database: ':memory:', attach_files: true}
        sources:
          scratch: {type: duckdb, database: ':memory:'}
        tiles:
          - {title: From scratch, source: scratch, sql: 'SELECT 7 AS n'}
    """)
    )
    try:
        _, ex = run_to_completion(
            client, {"dashboard": "multi", "sql": "SELECT 7 AS n", "source": "scratch"}
        )
        assert ex["status"] == "done", ex.get("error")
        assert ex["result"]["rows"] == [[7]]
        res = client.post(
            "/api/run", json={"dashboard": "multi", "sql": "SELECT 1", "source": "nope"}
        )
        assert res.status_code == 404
        body = client.get("/api/dashboards/multi").json()
        assert body["dashboard"]["sources"]["scratch"]["type"] == "duckdb"
        assert "password" not in str(body["dashboard"]["sources"])
    finally:
        (demo_dir / ".sqldash" / "multi.yaml").unlink()


def test_run_named_query_uses_the_owning_tiles_source(demo_dir, tmp_path):
    """A query addressed by name answers from the tile that owns it, the way the
    CLI's owner-walk and the browser's tile.source do. #458."""
    import duckdb

    for name, amount in (("a.db", 300.0), ("b.db", 500.0)):
        conn = duckdb.connect(str(tmp_path / name))
        conn.execute("CREATE TABLE orders (amount DOUBLE)")
        conn.execute(f"INSERT INTO orders VALUES ({amount})")
        conn.close()
    (demo_dir / ".sqldash" / "owned.yaml").write_text(
        "title: Owned\n"
        f"source: {{type: duckdb, database: '{tmp_path / 'a.db'}'}}\n"
        f"sources:\n  src_b: {{type: duckdb, database: '{tmp_path / 'b.db'}'}}\n"
        'queries:\n  total: "SELECT SUM(amount) AS total FROM orders"\n'
        '  shared: "SELECT SUM(amount) AS total FROM orders"\n'
        '  unused: "SELECT SUM(amount) AS total FROM orders"\n'
        "tiles:\n"
        "  - {title: On B, chart: big_number, query: total, source: src_b}\n"
        "  - {title: Shared default, chart: big_number, query: shared}\n"
        "  - {title: Shared B, chart: big_number, query: shared, source: src_b}\n"
    )
    app = create_app(demo_dir, allowed_hosts=["testserver"])
    try:
        with TestClient(app) as c:
            c.headers["X-Sqldash-Token"] = app.state.api_token
            _, ex = run_to_completion(c, {"dashboard": "owned", "query": "total"})
            assert ex["status"] == "done", ex.get("error")
            assert ex["result"]["rows"] == [[500.0]]

            _, ex = run_to_completion(c, {"dashboard": "owned", "query": "total", "source": ""})
            assert ex["result"]["rows"] == [[300.0]]

            res = c.post("/api/run", json={"dashboard": "owned", "query": "shared"})
            assert res.status_code == 422, res.text
            assert "different sources" in res.json()["detail"]
            assert "src_b" in res.json()["detail"]
            assert "'' is the dashboard default" in res.json()["detail"]

            _, ex = run_to_completion(
                c, {"dashboard": "owned", "query": "shared", "source": "src_b"}
            )
            assert ex["result"]["rows"] == [[500.0]]

            _, ex = run_to_completion(c, {"dashboard": "owned", "query": "unused"})
            assert ex["status"] == "done", ex.get("error")
            assert ex["result"]["rows"] == [[300.0]]
    finally:
        (demo_dir / ".sqldash" / "owned.yaml").unlink()


def test_tile_unknown_source_rejected():
    from sqldash.project.store import InvalidDashboardError, parse_dashboard

    with pytest.raises(InvalidDashboardError, match="unknown source 'ghost'"):
        parse_dashboard(
            "title: T\nsource: {type: duckdb}\n"
            "tiles: [{title: W, source: ghost, sql: 'SELECT 1'}]\n"
        )


def test_schema_endpoint_for_autocomplete(client):
    body = client.get("/api/dashboards/demo/schema").json()
    orders = next(t for t in body["tables"] if t["name"] == "orders")
    cols = {c["name"] for c in orders["columns"]}
    assert {"order_date", "region", "category", "amount"} <= cols


def test_schema_endpoint_unknown_source_404(client):
    res = client.get("/api/dashboards/demo/schema", params={"source": "nope"})
    assert res.status_code == 404


def test_schema_endpoint_unreachable_source_is_502(tmp_path):
    (tmp_path / "d.yaml").write_text(
        "title: Down\n"
        "source: {type: sqlite, database: /no/such/dir/x.db}\n"
        "tiles: [{title: W, sql: 'SELECT 1'}]\n"
    )
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        res = c.get("/api/dashboards/d/schema")
    assert res.status_code == 502, res.text
    assert "detail" in res.json()


BAD_URL_PASSWORD = "HUNTER2_URL_SECRET"


@pytest.mark.parametrize("endpoint", ["schema", "databases", "roles"])
def test_unparseable_source_url_is_a_502_without_the_password(tmp_path, caplog, endpoint):
    (tmp_path / "d.yaml").write_text(
        "title: Bad\n"
        "source: {type: duckdb}\n"
        f"sources: {{bad: {{url: 'bad url//admin:{BAD_URL_PASSWORD}@host/db'}}}}\n"
        "tiles: [{title: W, sql: 'SELECT 1'}]\n"
    )
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    caplog.set_level("DEBUG")
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        res = c.get(f"/api/dashboards/d/{endpoint}", params={"source": "bad"})
        run = c.post("/api/run", json={"dashboard": "d", "sql": "SELECT 1", "source": "bad"})
    assert res.status_code == 502, res.text
    assert res.json()["detail"].startswith("cannot resolve dialect for source:")
    assert res.json() == run.json()
    assert BAD_URL_PASSWORD not in res.text
    assert BAD_URL_PASSWORD not in caplog.text


def test_schema_endpoint_does_not_echo_connect_args_secrets(tmp_path):
    (tmp_path / "d.yaml").write_text(
        "title: Leak\n"
        "source:\n"
        "  type: duckdb\n"
        "  connect_args:\n"
        "    private_key: DASH_PLAINTEXT_PK\n"
        "    session_token: DASH_PLAINTEXT_SESSION\n"
        "tiles: [{title: W, sql: 'SELECT 1'}]\n"
    )
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        res = c.get("/api/dashboards/d/schema")
    assert res.status_code == 502, res.text
    assert "DASH_PLAINTEXT_PK" not in res.text
    assert "DASH_PLAINTEXT_SESSION" not in res.text
    assert "error" in res.text.lower() or "detail" in res.text.lower()


def test_run_does_not_echo_connect_args_secrets(tmp_path):
    """execute()'s engine.connect() TypeError used to skip _clean and leak
    through POST /api/run as TypeError: ... private_key='...'."""
    (tmp_path / "d.yaml").write_text(
        "title: Leak\n"
        "source:\n"
        "  type: duckdb\n"
        "  connect_args:\n"
        "    private_key: DASH_PLAINTEXT_PK\n"
        "    session_token: DASH_PLAINTEXT_SESSION\n"
        "tiles: [{title: W, sql: 'SELECT 1'}]\n"
    )
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        _, ex = run_to_completion(c, {"dashboard": "d", "sql": "SELECT 1"})
    assert ex["status"] == "error", ex
    blob = json.dumps(ex)
    assert "DASH_PLAINTEXT_PK" not in blob
    assert "DASH_PLAINTEXT_SESSION" not in blob
    assert "TypeError" not in blob


def test_query_page_always_has_a_source_picker(client):
    """The demo's metrics.yaml is the dashboard's own connection, so the picker
    lists it once (test_sources covers a distinct project source)."""
    res = client.get("/d/demo/query")
    assert 'id="source-picker"' in res.text
    assert "metrics.yaml ·" not in res.text
    assert "default ·" in res.text


def test_available_sources_lists_project_sources_without_secrets(client):
    """metrics.yaml repeats the demo dashboard's connection, so it is not listed
    again; it still resolves (test_schema_and_run_accept_a_project_source)."""
    body = client.get("/api/dashboards/demo/available-sources").json()
    keys = {s["key"] for s in body["sources"]}
    assert keys == {""}
    blob = json.dumps(body)
    assert "password" not in blob
    assert "token" not in blob


def test_available_sources_does_not_echo_a_url_password(tmp_path):
    (tmp_path / "demo.yaml").write_text(
        "title: Demo\n"
        "source: 'postgresql://u:hunter2@db.example.com:5432/app'\n"
        "tiles: [{title: T, sql: 'SELECT 1 AS n'}]\n"
    )
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        body = c.get("/api/dashboards/demo/available-sources").json()
    blob = json.dumps(body)
    assert "hunter2" not in blob
    assert "postgresql://u:" not in blob
    labels = [s["label"] for s in body["sources"]]
    assert any(label.startswith("default · ") for label in labels)
    assert all("hunter2" not in label for label in labels)


def test_schema_and_run_accept_a_project_source(client):
    res = client.get("/api/dashboards/demo/schema", params={"source": "metrics.yaml"})
    assert res.status_code == 200
    names = {t["name"] for t in res.json()["tables"]}
    assert "orders" in names
    _, ex = run_to_completion(
        client, {"dashboard": "demo", "sql": "SELECT 1 AS n", "source": "metrics.yaml"}
    )
    assert ex["status"] == "done", ex.get("error")
    _, metric_ex = run_to_completion(
        client, {"dashboard": "demo", "metric": "revenue", "source": "metrics.yaml"}
    )
    assert metric_ex["status"] == "done", metric_ex.get("error")


def test_saving_a_tile_copies_a_project_source_into_the_file(tmp_path):
    create_demo(tmp_path)
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        etag = c.get("/api/dashboards/demo").json()["etag"]
        res = c.post(
            "/api/dashboards/demo/tiles",
            json={
                "tile": {
                    "id": "from_metrics",
                    "title": "From metrics source",
                    "query": "from_metrics",
                    "source": "metrics.yaml",
                    "chart": {"type": "table"},
                },
                "sql": "SELECT 1 AS n",
            },
            headers={"If-Match": etag},
        )
        assert res.status_code == 200, res.text
        etag = res.json()["etag"]
        again = c.put(
            "/api/dashboards/demo/tiles/from_metrics",
            json={
                "tile": {
                    "title": "From metrics source",
                    "query": "from_metrics",
                    "source": "metrics.yaml",
                    "chart": {"type": "table"},
                },
                "sql": "SELECT 1 AS n",
            },
            headers={"If-Match": etag},
        )
        assert again.status_code == 200, again.text
    text = (tmp_path / ".sqldash" / "demo.yaml").read_text()
    assert "From metrics source" in text
    assert "source: metrics" in text.split("tiles:")[1]
    named = text.split("\nsource:")[1].split("\ntiles:")[0]
    assert "attach_files: true" in named.split("metrics:")[1]
    assert "base_dir" not in named


def test_copying_a_source_does_not_write_plaintext_secrets(tmp_path):
    (tmp_path / "other.yaml").write_text(
        "title: Other\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "sources:\n"
        "  prod: {type: postgres, url: 'postgresql://u:hunter2@db.example.com:5432/app'}\n"
        "tiles: [{title: T, sql: 'SELECT 1 AS n'}]\n"
    )
    (tmp_path / "demo.yaml").write_text(
        "title: Demo\nsource: {type: duckdb, database: ':memory:'}\ntiles: []\n"
    )
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        etag = c.get("/api/dashboards/demo").json()["etag"]
        for title, tile_id in (("Copied", "copied"), ("Copied again", "copied_again")):
            res = c.post(
                "/api/dashboards/demo/tiles",
                json={
                    "tile": {
                        "id": tile_id,
                        "title": title,
                        "query": tile_id,
                        "source": "other.sources.prod",
                        "chart": {"type": "table"},
                    },
                    "sql": "SELECT 1 AS n",
                },
                headers={"If-Match": etag},
            )
            assert res.status_code == 200, res.text
            etag = res.json()["etag"]
    text = (tmp_path / "demo.yaml").read_text()
    assert "hunter2" not in text
    assert "other_prod_2" not in text
    assert "other_prod:" in text
    assert "postgresql://u@db.example.com:5432/app" in text


def test_copying_a_source_keeps_env_host_in_the_url(tmp_path):
    (tmp_path / "other.yaml").write_text(
        "title: Other\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "sources:\n"
        "  prod: {type: postgres, url: 'postgresql://u:hunter2@${env:PGHOST}:5432/app'}\n"
        "tiles: [{title: T, sql: 'SELECT 1 AS n'}]\n"
    )
    (tmp_path / "demo.yaml").write_text(
        "title: Demo\nsource: {type: duckdb, database: ':memory:'}\ntiles: []\n"
    )
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        etag = c.get("/api/dashboards/demo").json()["etag"]
        res = c.post(
            "/api/dashboards/demo/tiles",
            json={
                "tile": {
                    "id": "copied",
                    "title": "Copied",
                    "query": "copied",
                    "source": "other.sources.prod",
                    "chart": {"type": "table"},
                },
                "sql": "SELECT 1 AS n",
            },
            headers={"If-Match": etag},
        )
        assert res.status_code == 200, res.text
    text = (tmp_path / "demo.yaml").read_text()
    assert "hunter2" not in text
    assert "${env:PGHOST}" in text
    assert "postgresql://u@${env:PGHOST}:5432/app" in text


def test_metric_edit_put_keeps_per_tile_source(tmp_path):
    create_demo(tmp_path)
    yaml_path = tmp_path / ".sqldash" / "demo.yaml"
    text = yaml_path.read_text()
    text = text.replace(
        "source: {type: duckdb, attach_files: true}",
        "source: {type: duckdb, attach_files: true}\n"
        "sources:\n  alt: {type: duckdb, database: ':memory:'}",
        1,
    )
    text = text.replace(
        "    metric: revenue\n    size: 6x2",
        "    metric: revenue\n    source: alt\n    size: 6x2",
        1,
    )
    yaml_path.write_text(text)
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        etag = c.get("/api/dashboards/demo").json()["etag"]
        res = c.put(
            "/api/dashboards/demo/tiles/total_revenue",
            json={
                "tile": {
                    "type": "chart",
                    "title": "Total revenue",
                    "metric": {"name": "revenue", "compare": "previous_period"},
                    "source": "alt",
                    "chart": {"type": "big_number"},
                },
                "sql": None,
            },
            headers={"If-Match": etag},
        )
        assert res.status_code == 200, res.text
    assert "source: alt" in yaml_path.read_text().split("Total revenue", 1)[1][:400]


def test_add_text_tile_via_api(client):
    body = client.get("/api/dashboards/demo").json()
    res = client.post(
        "/api/dashboards/demo/tiles",
        json={
            "tile": {
                "id": "note_from_ui",
                "type": "text",
                "title": None,
                "markdown": "**hello** from the UI",
            },
            "sql": None,
        },
        headers={"If-Match": body["etag"]},
    )
    assert res.status_code == 200, res.text
    body2 = client.get("/api/dashboards/demo").json()
    note = next(w for w in body2["dashboard"]["tiles"] if w["id"] == "note_from_ui")
    assert note["type"] == "text"
    assert note["markdown"] == "**hello** from the UI"
    client.delete("/api/dashboards/demo/tiles/note_from_ui", headers={"If-Match": body2["etag"]})


def test_conditional_block_dropped_when_select_is_all(client):
    _, ex = run_to_completion(client, {"dashboard": "demo", "query": "revenue_by_category"})
    assert ex["status"] == "done", ex.get("error")
    assert ex["result"]["row_count"] == 5


def test_conditional_block_applied_with_region(client):
    _, ex = run_to_completion(
        client,
        {"dashboard": "demo", "query": "revenue_by_category", "params": {"region": "apac"}},
    )
    assert ex["status"] == "done", ex.get("error")
    assert 0 < ex["result"]["row_count"] <= 5


def test_metric_formats_in_payload(client):
    body = client.get("/api/dashboards/demo").json()
    formats = body["dashboard"]["metric_formats"]
    assert formats["revenue"] == "currency"
    assert formats["order_count"] == "compact"
    has_time = body["dashboard"]["metric_has_time"]
    assert has_time["revenue"] is True
    assert has_time["order_count"] is True


def test_query_against_missing_attach_base_dir_names_the_directory(tmp_path):
    create_demo(tmp_path)
    demo = tmp_path / ".sqldash" / "demo.yaml"
    text = demo.read_text()
    text = text.replace(
        "source: {type: duckdb, attach_files: true}",
        "source: {type: duckdb, attach_files: true}\n"
        "sources:\n  alt:\n    type: duckdb\n    attach_files: true\n"
        "    base_dir: nonexistent\n",
        1,
    )
    demo.write_text(text)
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        _, ex = run_to_completion(
            client, {"dashboard": "demo", "sql": "SELECT 1 AS n", "source": "alt"}
        )
        assert ex["status"] == "error"
        assert "nonexistent" in ex["error"]
        assert "base_dir" in ex["error"]


def test_query_against_missing_default_source_base_dir_names_the_directory(tmp_path):
    """The default source used to skip the query-time guard: callers pass the
    dashboard directory, which exists, so attach_dir_missing returned None. #339."""
    create_demo(tmp_path)
    demo = tmp_path / ".sqldash" / "demo.yaml"
    text = demo.read_text()
    text = text.replace(
        "source: {type: duckdb, attach_files: true}",
        "source: {type: duckdb, attach_files: true, base_dir: nonesuch}",
        1,
    )
    demo.write_text(text)
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        _, ex = run_to_completion(client, {"dashboard": "demo", "sql": "SELECT 1 AS n"})
        assert ex["status"] == "error", ex
        assert "nonesuch" in ex["error"]
        assert "base_dir" in ex["error"]


def test_query_against_empty_attach_dir_names_the_directory(tmp_path):
    """An existing dir with no csv/parquet used to fail as a Catalog Error. #478."""
    (tmp_path / "probe.yaml").write_text(
        "title: No files probe\n"
        "source: {type: duckdb, attach_files: true}\n"
        "tiles:\n"
        "  - title: n\n"
        "    chart: big_number\n"
        "    sql: SELECT COUNT(*) AS n FROM orders\n"
    )
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        _, ex = run_to_completion(
            client, {"dashboard": "probe", "sql": "SELECT COUNT(*) AS n FROM orders"}
        )
        assert ex["status"] == "error", ex
        err = ex["error"]
        assert "attached 0 files" in err
        assert "csv" in err or "parquet" in err
        assert str(tmp_path) in err or str(tmp_path.resolve()) in err
        assert "Catalog Error" not in err


def test_query_relative_database_uses_source_base_dir_not_dashboard_dir(tmp_path):
    """`base_dir: data/csv` + `database: app.duckdb` used to open a decoy
    `app.duckdb` at the dashboard root. #339."""
    import duckdb

    create_demo(tmp_path)
    root = tmp_path / ".sqldash"
    nested = root / "data" / "csv"
    nested.mkdir(parents=True)
    for path, value in ((nested / "app.duckdb", 42), (root / "app.duckdb", 999)):
        conn = duckdb.connect(str(path))
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute(f"INSERT INTO t VALUES ({value})")
        conn.close()
    demo = root / "demo.yaml"
    demo.write_text(
        demo.read_text().replace(
            "source: {type: duckdb, attach_files: true}",
            "source: {type: duckdb, attach_files: true}\n"
            "sources:\n  alt:\n    type: duckdb\n    attach_files: true\n"
            "    base_dir: data/csv\n    database: app.duckdb\n",
            1,
        )
    )
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        _, ex = run_to_completion(
            client, {"dashboard": "demo", "sql": "SELECT x FROM t", "source": "alt"}
        )
        assert ex["status"] == "done", ex
        assert ex["result"]["rows"] == [[42]]


def test_query_named_source_nested_relative_base_dir_does_not_double_join(tmp_path):
    """Joining data/csv onto an already-derived …/data/csv invented a missing dir. #339."""
    create_demo(tmp_path)
    nested = tmp_path / ".sqldash" / "data" / "csv"
    nested.mkdir(parents=True)
    (nested / "orders.csv").write_text("n\n7\n")
    demo = tmp_path / ".sqldash" / "demo.yaml"
    text = demo.read_text()
    text = text.replace(
        "source: {type: duckdb, attach_files: true}",
        "source: {type: duckdb, attach_files: true}\n"
        "sources:\n  alt:\n    type: duckdb\n    attach_files: true\n"
        "    base_dir: data/csv\n",
        1,
    )
    demo.write_text(text)
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        _, ex = run_to_completion(
            client, {"dashboard": "demo", "sql": "SELECT n FROM orders", "source": "alt"}
        )
        assert ex["status"] == "done", ex
        assert ex["result"]["rows"] == [[7]]


def test_default_source_base_dir_wins_over_a_same_named_csv_in_the_dashboard_dir(tmp_path):
    """The HTTP path handed the engine the dashboard directory and never applied
    the default source's `base_dir:`, so a stray `data/orders.csv` beside the
    dashboard answered for the `b/orders.csv` the file pointed at, while the
    CLI said the right number. #356, fixed by #354; pinned here so the two
    surfaces cannot drift apart again on the very layout the report used."""
    root = tmp_path / ".sqldash"
    for folder, body in (("b", "id\n1\n2\n"), ("a", "id\n10\n20\n"), ("data", "id\n100\n")):
        (root / folder).mkdir(parents=True)
        (root / folder / "orders.csv").write_text(body)
    (root / "main.yaml").write_text(
        "title: Main\n"
        "source: {type: duckdb, attach_files: true, base_dir: b}\n"
        "sources:\n"
        "  north: {type: duckdb, attach_files: true, base_dir: a}\n"
        "tiles:\n"
        "  - {title: Orders default, sql: SELECT COUNT(*) AS n FROM orders}\n"
        "  - {title: Orders north, source: north, sql: SELECT SUM(id) AS s FROM orders}\n"
    )
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        _, ex = run_to_completion(client, {"dashboard": "main", "query": "orders_default"})
        assert ex["status"] == "done", ex.get("error")
        assert ex["result"]["rows"] == [[2]]
        _, ex = run_to_completion(
            client, {"dashboard": "main", "query": "orders_north", "source": "north"}
        )
        assert ex["status"] == "done", ex.get("error")
        assert ex["result"]["rows"] == [[30]]
        _, ex = run_to_completion(
            client, {"dashboard": "main", "sql": "SELECT COUNT(*) AS n FROM orders"}
        )
        assert ex["status"] == "done", ex.get("error")
        assert ex["result"]["rows"] == [[2]]


def test_metric_without_time_dimension_is_flagged_in_payload(tmp_path):
    """Browser used to draw 0.0% vs previous period when lint already errors. #280."""
    (tmp_path / ".sqldash").mkdir()
    (tmp_path / ".sqldash" / "metrics.yaml").write_text(
        "source: {type: duckdb, database: ':memory:'}\n"
        "relations:\n  t: {sql: 'SELECT 1 AS n'}\n"
        "metrics:\n  n: {relation: t, expr: COUNT(*)}\n"
    )
    (tmp_path / ".sqldash" / "d.yaml").write_text(
        "title: D\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n  - {title: N, metric: n, compare: previous_period}\n"
    )
    app = create_app(tmp_path / ".sqldash", allowed_hosts=["testserver"])
    with TestClient(app) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        body = client.get("/api/dashboards/d").json()
        assert body["dashboard"]["metric_has_time"]["n"] is False


def test_dashboard_currency_and_locale_fields():
    from sqldash.project.store import parse_dashboard

    d = parse_dashboard(
        "title: T\ncurrency: EUR\nlocale: de-DE\nsource: {type: duckdb}\n"
        "tiles: [{title: W, sql: 'SELECT 1 AS n', format: EUR}]\n"
    )
    assert d.currency == "EUR"
    assert d.tiles[0].chart.format == "EUR"


def test_invalid_currency_rejected():
    from sqldash.project.store import InvalidDashboardError, parse_dashboard

    with pytest.raises(InvalidDashboardError, match="ISO 4217"):
        parse_dashboard("title: T\ncurrency: euros\nsource: {type: duckdb}\n")


def test_invalid_format_rejected():
    from sqldash.project.store import InvalidDashboardError, parse_dashboard

    with pytest.raises(InvalidDashboardError, match="ISO 4217"):
        parse_dashboard(
            "title: T\nsource: {type: duckdb}\n"
            "tiles: [{title: W, sql: 'SELECT 1', format: dollars}]\n"
        )


def test_disallowed_host_rejected(demo_dir):
    app = create_app(demo_dir, allowed_hosts=["testserver"])
    with TestClient(app, base_url="http://evil.example.com") as c:
        res = c.get("/d/demo")
        assert res.status_code == 403


def test_mutating_call_without_token_rejected(demo_dir):
    app = create_app(demo_dir, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        res = c.post("/api/run", json={"dashboard": "demo", "sql": "SELECT 1"})
        assert res.status_code == 403
        assert "X-Sqldash-Token" in res.json()["detail"]
        assert c.get("/d/demo").status_code == 200


def test_put_without_if_match_rejected(client):
    res = client.put("/api/dashboards/demo", json={"text": "title: X\n"})
    assert res.status_code == 422


@pytest.mark.parametrize(
    "form",
    ['"{etag}"', 'W/"{etag}"', 'w/"{etag}"', "{etag}", "*", '"0000000000000000", "{etag}"'],
    ids=["quoted", "weak", "weak-lowercase", "bare", "star", "list"],
)
def test_if_match_accepts_every_rfc_7232_form(tmp_path, form):
    """RFC 7232 spells an entity-tag quoted, so a conforming client sends
    `If-Match: "abc"` and could never write while the header was compared raw;
    only our own pages, which send the bare hash, ever matched. #651."""
    create_demo(tmp_path)
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        etag = c.get("/api/dashboards/demo").json()["etag"]
        res = c.patch(
            "/api/dashboards/demo/meta",
            json={"title": "Renamed", "description": None},
            headers={"If-Match": form.format(etag=etag)},
        )
        assert res.status_code == 200, res.text
    assert "title: Renamed" in (tmp_path / ".sqldash" / "demo.yaml").read_text()


def test_quoted_if_match_works_on_every_mutating_route(tmp_path):
    """One rule, not one call site: every route that takes If-Match, plus the
    whole-file PUT and the dashboard DELETE. #651."""
    create_demo(tmp_path)
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token

        def quoted(name="demo"):
            return {"If-Match": '"{}"'.format(c.get(f"/api/dashboards/{name}").json()["etag"])}

        body = c.get("/api/dashboards/demo").json()["dashboard"]
        tile_id = body["tiles"][0]["id"]
        res = c.patch(
            "/api/dashboards/demo/positions",
            json={"positions": {tile_id: {"x": 0, "y": 0, "w": 6, "h": 4}}},
            headers=quoted(),
        )
        assert res.status_code == 200, res.text
        res = c.patch(
            "/api/dashboards/demo/meta",
            json={"title": "Quoted", "description": None},
            headers=quoted(),
        )
        assert res.status_code == 200, res.text
        res = c.put(
            "/api/dashboards/demo/filters",
            json={"filters": body["filters"]},
            headers=quoted(),
        )
        assert res.status_code == 200, res.text
        tile = {
            "id": "quoted_tile",
            "type": "chart",
            "title": "Quoted tile",
            "metric": {"name": "revenue"},
            "chart": {"type": "big_number", "format": "currency"},
        }
        res = c.post(
            "/api/dashboards/demo/tiles", json={"tile": tile, "sql": None}, headers=quoted()
        )
        assert res.status_code == 200, res.text
        res = c.put(
            "/api/dashboards/demo/tiles/quoted_tile",
            json={
                "tile": {**tile, "chart": {"type": "big_number", "format": "percent"}},
                "sql": None,
            },
            headers=quoted(),
        )
        assert res.status_code == 200, res.text
        res = c.delete("/api/dashboards/demo/tiles/quoted_tile", headers=quoted())
        assert res.status_code == 200, res.text

        text = (tmp_path / ".sqldash" / "demo.yaml").read_text()
        res = c.put("/api/dashboards/demo", json={"text": text}, headers=quoted())
        assert res.status_code == 200, res.text

        (tmp_path / ".sqldash" / "doomed.yaml").write_text(
            "title: Doomed\nsource: {type: duckdb, database: ':memory:'}\ntiles: []\n"
        )
        res = c.delete("/api/dashboards/doomed", headers=quoted("doomed"))
        assert res.status_code == 204, res.text
    assert not (tmp_path / ".sqldash" / "doomed.yaml").exists()


def test_a_genuinely_stale_if_match_still_conflicts(tmp_path):
    """The 409 has to survive the fix, and its two values have to be tellable
    apart — `etag abc, expected "abc"` claimed a concurrent edit that never
    happened, which is what made this cost an hour to debug. #651."""
    create_demo(tmp_path)
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        current = c.get("/api/dashboards/demo").json()["etag"]
        res = c.patch(
            "/api/dashboards/demo/meta",
            json={"title": "Nope", "description": None},
            headers={"If-Match": '"0000000000000000"'},
        )
        assert res.status_code == 409, res.text
        detail = res.json()["detail"]
        assert f"on disk {current!r}" in detail, detail
        assert "If-Match '\"0000000000000000\"'" in detail, detail
        res = c.put(
            "/api/dashboards/demo",
            json={"text": "title: X\nsource: {type: duckdb}\ntiles: []\n"},
            headers={"If-Match": 'W/"0000000000000000"'},
        )
        assert res.status_code == 409, res.text
        assert "If-Match 'W/\"0000000000000000\"'" in res.json()["detail"]
    assert "title: Nope" not in (tmp_path / ".sqldash" / "demo.yaml").read_text()


def test_metric_run_treats_unlisted_all_as_off(tmp_path):
    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "allbug.yaml").write_text(
        "title: All-sentinel\n"
        "source: {type: duckdb, attach_files: true}\n"
        "filters:\n"
        "  - {name: region, type: select, default: all, options: [us, eu, apac]}\n"
        "queries:\n"
        "  q: |\n"
        "    SELECT COUNT(*) AS n FROM orders\n"
        "    {% if region %}WHERE region = {{ region }}{% endif %}\n"
        "tiles:\n"
        "  - {title: Sql, query: q}\n"
        "  - {title: Metric, metric: order_count}\n"
    )
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        _, sql = run_to_completion(c, {"dashboard": "allbug", "query": "q"})
        _, metric = run_to_completion(c, {"dashboard": "allbug", "metric": "order_count"})
        _, us = run_to_completion(
            c, {"dashboard": "allbug", "metric": "order_count", "params": {"region": "us"}}
        )
    assert sql["status"] == "done", sql.get("error")
    assert metric["status"] == "done", metric.get("error")
    assert metric["result"]["rows"] == sql["result"]["rows"]
    assert metric["result"]["rows"][0][0] > 0
    assert us["result"]["rows"][0][0] < metric["result"]["rows"][0][0]


def test_metric_run_without_dashboard(client):
    _, ex = run_to_completion(client, {"metric": "revenue", "dimensions": ["region"]})
    assert ex["status"] == "done"
    assert {row[0] for row in ex["result"]["rows"]} == {"us", "eu", "apac"}


def test_standalone_metric_run_rejects_source(client):
    res = client.post("/api/run", json={"metric": "revenue", "source": "alt"})
    assert res.status_code == 422
    assert "dashboard" in res.json()["detail"]


def test_dashboard_metric_run_honors_start(client):
    """dashboard+metric used to drop start= and answer from the filter-bar
    default (last_60_days on the demo) — same request on the CLI applied
    -30d. An explicit window has to win, or the two surfaces disagree."""
    _, defaulted = run_to_completion(client, {"dashboard": "demo", "metric": "revenue"})
    _, windowed = run_to_completion(
        client, {"dashboard": "demo", "metric": "revenue", "start": "-30d"}
    )
    assert defaulted["status"] == "done", defaulted
    assert windowed["status"] == "done", windowed
    assert windowed["result"]["rows"][0][0] < defaulted["result"]["rows"][0][0]


def test_standalone_metric_run_accepts_a_relative_start(client):
    """Standalone /api/run used to ignore start= (extras-ignored on RunRequest)
    and still return done over the full series. The demo data spans months, so
    a real -30d window is a smaller total — that is the only assertion that
    fails without the wiring."""
    _, full = run_to_completion(client, {"metric": "revenue"})
    _, windowed = run_to_completion(client, {"metric": "revenue", "start": "-30d"})
    assert full["status"] == "done", full
    assert windowed["status"] == "done", windowed
    assert windowed["result"]["rows"][0][0] < full["result"]["rows"][0][0]


def test_run_without_dashboard_or_metric_rejected(client):
    res = client.post("/api/run", json={"sql": "SELECT 1"})
    assert res.status_code == 422


def test_run_rejects_unknown_body_fields(client):
    """Unknown keys used to extra=ignore, so compare/yoy 202'd the current
    window and the caller could not tell. #419."""
    res = client.post(
        "/api/run",
        json={
            "dashboard": "demo",
            "metric": "revenue",
            "start": "2026-08-01",
            "end": "2026-09-01",
            "compare": "yoy",
        },
    )
    assert res.status_code == 422, res.text
    assert "compare" in res.text


def test_run_rejects_metric_and_query_together(client):
    res = client.post(
        "/api/run",
        json={
            "dashboard": "demo",
            "metric": "order_count",
            "query": "revenue_share_by_region",
        },
    )
    assert res.status_code == 422, res.text
    assert "metric" in res.text
    assert "query" in res.text


def test_run_rejects_query_and_sql_together(client):
    """The named query used to run and the sql — including a write — never
    reached the read-only guard. #419."""
    res = client.post(
        "/api/run",
        json={
            "dashboard": "demo",
            "query": "revenue_share_by_region",
            "sql": "DELETE FROM orders",
        },
    )
    assert res.status_code == 422, res.text
    assert "sql" in res.text
    assert "query" in res.text


@pytest.mark.parametrize("start", [20260101, 20260101.0, True])
def test_run_rejects_a_non_string_date_param_by_name(client, start):
    """An int override was accepted and failed with a raw DuckDB binder error. #584."""
    res = client.post(
        "/api/run",
        json={
            "dashboard": "demo",
            "metric": "revenue",
            "params": {"dates_start": start, "dates_end": "2026-02-01"},
        },
    )
    assert res.status_code == 422, res.text
    assert "unrecognized date" in res.json()["detail"]


def test_metric_page(client):
    res = client.get("/m/revenue")
    assert res.status_code == 200
    assert "SUM(amount)" in res.text
    assert "metric-previews" in res.text


def test_metric_page_unknown(client):
    """A mistyped /m/nope used to dump raw JSON, same defect #288 fixed for /d/. #340."""
    res = client.get("/m/nope")
    assert res.status_code == 404
    assert "text/html" in res.headers["content-type"]
    assert "can&#39;t be loaded" in res.text
    assert "Back to dashboards" in res.text
    assert "no metric named 'nope'" in res.text or "no metric named &#39;nope&#39;" in res.text


_BROKEN_METRICS = {
    "stray_key": ("    expr: SUM(amount)\n", "    expr: SUM(amount)\n    bogus_key: 1\n"),
    "yaml_syntax": ("    title: Revenue\n", "    title: Revenue\n   expr: [oops\n"),
}


@pytest.mark.parametrize("breakage", sorted(_BROKEN_METRICS))
def test_broken_metrics_yaml_renders_pages_not_json(tmp_path, breakage):
    """A broken metrics.yaml answered /m/... with raw JSON and emptied the index. #593."""
    create_demo(tmp_path)
    metrics = tmp_path / ".sqldash" / "metrics.yaml"
    old, new = _BROKEN_METRICS[breakage]
    metrics.write_text(metrics.read_text().replace(old, new, 1))
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        for url in ("/m/revenue", "/m/nope"):
            res = c.get(url)
            assert res.status_code == 422, url
            assert "text/html" in res.headers["content-type"], url
            assert "<title>" in res.text, url
            assert "Back to dashboards" in res.text, url
            assert '<code class="error-file">metrics.yaml</code>' in res.text, url
            assert "metrics.yaml: " in res.text, url
        index = c.get("/")
        assert index.status_code == 200
        assert "<h1>Metrics</h1>" in index.text
        assert '<span class="row-file">metrics.yaml</span>' in index.text
        assert "metrics.yaml: " in index.text


def test_index_links_metrics(client):
    res = client.get("/")
    assert 'href="/m/revenue"' in res.text


def test_create_dashboard(client, demo_dir):
    res = client.post("/api/dashboards", json={"title": "Ops Overview"})
    assert res.status_code == 201, res.text
    assert res.json()["name"] == "ops_overview"
    path = demo_dir / ".sqldash" / "ops_overview.yaml"
    assert path.exists()
    text = path.read_text()
    assert "title: Ops Overview" in text
    assert "duckdb" in text
    assert client.get("/d/ops_overview").status_code == 200
    res = client.post("/api/dashboards", json={"title": "Ops overview!"})
    assert res.status_code == 409
    path.unlink()


def test_create_dashboard_blank_title(client):
    assert client.post("/api/dashboards", json={"title": "!!!"}).status_code == 422


def test_create_dashboard_long_title(client, demo_dir):
    """A 300-char title used to slugify into a >NAME_MAX file name and blow up
    inside `atomic_write` as a bare 500 (#391). The slug is bounded now, the
    file keeps the whole title, and a second title that trims to the same stem
    still gets the named 409 rather than silently overwriting the first."""
    title = "Quarterly Revenue " * 20
    res = client.post("/api/dashboards", json={"title": title})
    assert res.status_code == 201, res.text
    name = res.json()["name"]
    assert len(f"{name}.yaml.tmp".encode()) <= 255
    path = demo_dir / ".sqldash" / f"{name}.yaml"
    assert path.exists()
    assert f"title: {title.strip()}" in path.read_text()
    assert client.get(f"/d/{name}").status_code == 200
    before = path.read_text()
    assert client.post("/api/dashboards", json={"title": title + " again"}).status_code == 409
    assert path.read_text() == before, "the 409 overwrote the dashboard it collided with"
    path.unlink()


def test_create_dashboard_long_multibyte_title(client, demo_dir):
    """NAME_MAX is bytes, so a title truncated by characters can still be too
    long. A CJK title used to have no stem at all (#579); it now keeps its
    characters and is bounded by bytes like a mixed one, and both are served."""
    res = client.post("/api/dashboards", json={"title": "収益ダッシュボード" * 40})
    assert res.status_code == 201, res.text
    name = res.json()["name"]
    assert name.startswith("収益ダッシュボード")
    assert len(f"{name}.yaml.tmp".encode()) <= 255
    assert (demo_dir / ".sqldash" / f"{name}.yaml").exists()
    assert client.get(f"/d/{name}").status_code == 200
    (demo_dir / ".sqldash" / f"{name}.yaml").unlink()

    res = client.post("/api/dashboards", json={"title": "収益a" * 200})
    assert res.status_code == 201, res.text
    name = res.json()["name"]
    assert len(f"{name}.yaml.tmp".encode()) <= 255
    path = demo_dir / ".sqldash" / f"{name}.yaml"
    assert path.exists()
    assert client.get(f"/d/{name}").status_code == 200
    path.unlink()


@pytest.mark.parametrize(
    ("title", "name"),
    [
        ("Übersicht", "übersicht"),
        ("Straße", "straße"),
        ("売上分析", "売上分析"),
        ("日本語ダッシュボード", "日本語ダッシュボード"),
        ("Ω", "ω"),
        ("हिन्दी रिपोर्ट", "हिन्दी_रिपोर्ट"),
        ("Отчёт по продажам", "отчёт_по_продажам"),
    ],
)
def test_create_dashboard_keeps_a_title_in_any_script(client, demo_dir, title, name):
    """#579: a title with no ASCII letter was refused as having no letters, and
    one with a single ASCII letter lost the rest (Übersicht became bersicht).
    The name keeps its characters, vowel marks included, and is served."""
    res = client.post("/api/dashboards", json={"title": title})
    assert res.status_code == 201, res.text
    assert res.json()["name"] == name
    path = demo_dir / ".sqldash" / f"{name}.yaml"
    assert f"title: {title}" in path.read_text(encoding="utf-8")
    assert client.get(f"/d/{name}").status_code == 200
    assert client.get(f"/api/dashboards/{name}").status_code == 200
    path.unlink()


@pytest.mark.parametrize(
    "title",
    [
        pytest.param("———", id="punctuation"),
        pytest.param("\u0301", id="lone-combining-mark"),
        pytest.param("\u0301\u0302", id="marks-only"),
        pytest.param("——\u0301——", id="mark-between-dashes"),
    ],
)
def test_create_dashboard_title_with_no_letters_still_names_the_rule(client, title):
    """The 422 is kept for a title that really has no letter or digit, where
    its message is now true. A combining mark is not a letter: kept on its
    own it made a dashboard whose name renders as nothing."""
    before = set(client.app.state.store.discover())
    res = client.post("/api/dashboards", json={"title": title})
    assert set(client.app.state.store.discover()) == before
    assert res.status_code == 422
    assert "at least one letter or number" in res.json()["detail"]


@pytest.mark.parametrize(
    ("existing", "title"),
    [
        pytest.param(unicodedata.normalize("NFD", "übersicht"), "Übersicht", id="existing-nfd"),
        pytest.param("\u01f0", "J\u030c", id="title-decomposes-when-lowered"),
        pytest.param("\u01f0", "\u01f0", id="identical"),
    ],
)
def test_create_dashboard_refuses_a_name_taken_in_another_normal_form(
    client, monkeypatch, existing, title
):
    """A name can reach the index in either normal form and look identical in it:
    a file written by another tool may be decomposed, and lowercasing an
    uppercase letter plus a mark (`J̌`) yields `j` + mark where the file on disk
    holds the precomposed `ǰ`. macOS resolves both forms to one file, so the
    create already failed there; a normalization-sensitive filesystem created a
    second, identical-looking dashboard. Discovery is stubbed so this holds on
    every platform, not only where the filesystem happens to catch it."""
    store = client.app.state.store
    real = store.discover
    monkeypatch.setattr(
        store, "discover", lambda: {**real(), existing: store.root / f"{existing}.yaml"}
    )
    before = {p.name for p in store.root.iterdir()}
    res = client.post("/api/dashboards", json={"title": title})
    assert res.status_code == 409, res.text
    assert {p.name for p in store.root.iterdir()} == before


def test_save_text_names_a_too_long_file_name(client):
    """The whole-file PUT takes the name from the URL, so a bounded slug does
    not cover it — and a filesystem with a tighter NAME_MAX than ours would
    still 500 on a create. ENAMETOOLONG becomes the 422 that names the fix."""
    res = client.put(
        f"/api/dashboards/{'y' * 300}",
        json={"text": "title: Y\nsource: {type: duckdb, database: ':memory:'}\n"},
        headers={"If-Match": "nope"},
    )
    assert res.status_code == 422, res.text
    assert "too long for this filesystem" in res.json()["detail"]


def _switcher(html: str) -> str:
    start = html.index('id="dash-switcher-dd"')
    return html[start : html.index('id="topbar-title"', start)]


def test_index_ignores_foreign_yaml(tmp_path):
    """Compose/Taskfile/pre-commit next to dashboards are not broken dashboards."""
    create_demo(tmp_path)
    root = tmp_path / ".sqldash"
    (root / "docker-compose.yaml").write_text("services:\n  db: {}\n")
    (root / "Taskfile.yaml").write_text("version: '3'\ntasks: {}\n")
    (root / "pre-commit-config.yaml").write_text("repos: []\n")
    (root / "params.yaml").write_text("experiments: {}\n")
    (root / "_quarto.yml").write_text("title: My Analysis\nformat: html\n")
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        page = c.get("/").text
        dash = c.get("/d/demo").text
    switcher = _switcher(dash)
    assert "drow-bad" not in page
    assert "_quarto.yml" not in page
    assert 'href="/d/_quarto"' not in switcher
    for name in ("docker-compose", "Taskfile", "pre-commit-config", "params"):
        assert f"{name}.yaml" not in page, page
        assert f'href="/d/{name}"' not in switcher, switcher
    assert "Order Analytics" in switcher


def test_index_keeps_unreadable_yaml_off_the_switcher(tmp_path):
    """A half-saved dashboard stays a broken index row; the switcher skips it."""
    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "half.yaml").write_text("title: [unterminated\n")
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        page = c.get("/").text
        dash = c.get("/d/demo").text
    assert "half.yaml" in page
    assert "drow-bad" in page
    assert 'href="/d/half"' not in _switcher(dash)


def test_index_shows_broken_dashboard(client, demo_dir):
    bad = demo_dir / ".sqldash" / "broken.yaml"
    bad.write_text("title: Broken\nsource: {typ: duckdb}\ntiles: []\n")
    try:
        res = client.get("/")
        assert res.status_code == 200
        assert "drow-bad" in res.text
        assert "broken.yaml" in res.text
    finally:
        bad.unlink()


def test_index_card_delete_flow(client, demo_dir):
    bad = demo_dir / ".sqldash" / "doomed.yaml"
    bad.write_text("title: Doomed\nsource: {typ: duckdb}\ntiles: []\n")
    try:
        res = client.get("/")
        assert 'data-name="doomed"' in res.text
        etag = res.text.split('data-name="doomed" data-title="doomed" data-etag="')[1].split('"')[0]
        assert etag
        res = client.delete("/api/dashboards/doomed", headers={"If-Match": "wrong"})
        assert res.status_code == 409
        res = client.delete("/api/dashboards/doomed", headers={"If-Match": etag})
        assert res.status_code == 204
        assert not bad.exists()
    finally:
        bad.unlink(missing_ok=True)


def test_index_inline_metric_badge(client, demo_dir):
    inline = demo_dir / ".sqldash" / "inliner.yaml"
    inline.write_text(
        "title: Inliner\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "metrics:\n"
        "  scratch_total: {sql: SELECT 1 AS v, expr: SUM(v)}\n"
        "tiles: []\n"
    )
    try:
        res = client.get("/")
        assert "inline · inliner" in res.text
    finally:
        inline.unlink()


def test_repos_api_in_single_mode(client, tmp_path, monkeypatch):
    import sqldash.workspace as workspace

    monkeypatch.setattr(workspace, "registry_path", lambda: tmp_path / "repos.yaml")
    body = client.get("/api/repos").json()
    assert body["workspace"] is False
    assert body["repos"] == []
    assert "Workspace repos" in client.get("/settings/panel").text

    repo_dir = tmp_path / "extra"
    repo_dir.mkdir()
    res = client.post("/api/repos", json={"target": str(repo_dir)})
    assert res.status_code == 201
    assert res.json() == {"name": "extra", "served": False}
    rows = client.get("/api/repos").json()["repos"]
    assert rows[0]["served"] is False
    assert "registered" in client.get("/settings/panel").text
    assert client.delete("/api/repos/extra").status_code == 204
    assert client.get("/api/repos").json()["repos"] == []


def test_profile_health(client, demo_dir, monkeypatch):
    import sqldash.api.routes_config as rc

    profiled = demo_dir / ".sqldash" / "profiled.yaml"
    profiled.write_text(
        "title: Profiled\n"
        "source: {type: snowflake, account: acme-x1, profile: acme-prod}\n"
        "tiles: []\n"
    )
    try:
        monkeypatch.setattr(rc, "load_profiles", lambda: {"unrelated": {}})
        body = client.get("/api/profiles/health").json()
        row = next(p for p in body["profiles"] if p["profile"] == "acme-prod")
        assert row["defined"] is False
        assert "profiled.source" in row["referenced_by"]
        page = client.get("/settings/panel").text
        assert "Credential profiles" in page
        assert "not defined on this machine" in page

        monkeypatch.setattr(rc, "load_profiles", lambda: {"acme-prod": {}})
        row = next(
            p
            for p in client.get("/api/profiles/health").json()["profiles"]
            if p["profile"] == "acme-prod"
        )
        assert row["defined"] is True
    finally:
        profiled.unlink()


def test_broken_dashboard_page_renders_html_error(client, demo_dir):
    bad = demo_dir / ".sqldash" / "hosed.yaml"
    bad.write_text("title: Hosed\nsource: {typ: duckdb}\ntiles: []\n")
    try:
        res = client.get("/d/hosed")
        assert res.status_code == 422
        assert "text/html" in res.headers["content-type"]
        assert "can&#39;t be loaded" in res.text or "can't be loaded" in res.text
        assert "sqldash lint" in res.text
        api = client.get("/api/dashboards/hosed")
        assert api.status_code == 422
        assert api.headers["content-type"].startswith("application/json")
    finally:
        bad.unlink()


def test_options_sql_default_is_selected_before_the_query_returns(tmp_path):
    """Tiles used to render unfiltered while CLI/API applied the default. #300."""
    create_demo(tmp_path)
    demo = tmp_path / ".sqldash" / "demo.yaml"
    text = demo.read_text()
    text = text.replace(
        'options_sql: "SELECT DISTINCT region FROM orders ORDER BY region"\n',
        'options_sql: "SELECT DISTINCT region FROM orders ORDER BY region"\n    default: us\n',
        1,
    )
    demo.write_text(text)
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        page = client.get("/d/demo")
        assert page.status_code == 200
        assert 'data-default="us"' in page.text
        assert 'value="us"' in page.text
        assert "selected" in page.text
        body = client.get("/api/dashboards/demo").json()
        region = next(f for f in body["dashboard"]["filters"] if f["name"] == "region")
        assert region["resolved_default"] == "us"


def test_options_sql_default_is_html_escaped_in_data_default(tmp_path):
    """Jinja autoescapes the attribute; a quote in default must not break out. #300."""
    create_demo(tmp_path)
    demo = tmp_path / ".sqldash" / "demo.yaml"
    text = demo.read_text()
    text = text.replace(
        'options_sql: "SELECT DISTINCT region FROM orders ORDER BY region"\n',
        'options_sql: "SELECT DISTINCT region FROM orders ORDER BY region"\n'
        "    default: '\">xss'\n",
        1,
    )
    demo.write_text(text)
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        page = client.get("/d/demo")
        assert page.status_code == 200
        assert 'data-default="">xss"' not in page.text
        assert 'data-default="&#34;&gt;xss"' in page.text


def test_options_sql_without_default_omits_data_default(client):
    page = client.get("/d/demo")
    assert page.status_code == 200
    assert "data-options-sql" in page.text
    assert "data-default=" not in page.text


def test_filter_options_sql_run(client, demo_dir):
    optioned = demo_dir / ".sqldash" / "optioned.yaml"
    optioned.write_text(
        "title: Optioned\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "filters:\n"
        "  - name: region\n"
        "    type: select\n"
        "    options_sql: \"SELECT r FROM (VALUES ('us'), ('eu'), ('us')) t(r)\"\n"
        "queries:\n"
        "  q: \"SELECT 1 AS v {% if region %}WHERE 'x' = {{ region }}{% endif %}\"\n"
        "tiles:\n"
        "  - {title: X, query: q}\n"
    )
    try:
        _, ex = run_to_completion(client, {"dashboard": "optioned", "filter_options": "region"})
        assert ex["status"] == "done"
        assert [row[0] for row in ex["result"]["rows"]] == ["us", "eu", "us"]

        res = client.post("/api/run", json={"dashboard": "optioned", "filter_options": "nope"})
        assert res.status_code == 404

        _, ex = run_to_completion(
            client, {"dashboard": "optioned", "query": "q", "params": {"region": "all"}}
        )
        assert ex["status"] == "done"
        assert ex["result"]["rows"] == [[1]]
    finally:
        optioned.unlink()


def test_filter_options_sql_rejects_params(client, demo_dir):
    bad = demo_dir / ".sqldash" / "badopt.yaml"
    bad.write_text(
        "title: BadOpt\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "filters:\n"
        '  - {name: r, type: select, options_sql: "SELECT {{ other }}"}\n'
        "tiles: []\n"
    )
    try:
        res = client.post("/api/run", json={"dashboard": "badopt", "filter_options": "r"})
        assert res.status_code == 422
    finally:
        bad.unlink()


def test_tile_hue_rendered_server_side(client):
    page = client.get("/d/demo").text
    assert page.count("--wcolor: var(--series-") >= 5


def test_workspace_profile_health_covers_metrics_yaml(tmp_path):
    from sqldash.api.routes_config import profile_health
    from sqldash.scaffold import create_demo
    from sqldash.server import create_app

    for repo in ("acme", "beta"):
        create_demo(tmp_path / repo)
    metrics = tmp_path / "acme" / ".sqldash" / "metrics.yaml"
    text = metrics.read_text().replace(
        "source:\n  type: duckdb\n  attach_files: true",
        "source:\n  type: snowflake\n  account: acme-x1\n  profile: acme-prod",
    )
    assert "acme-prod" in text
    metrics.write_text(text)
    app = create_app(workspace=[(repo, tmp_path / repo / ".sqldash") for repo in ("acme", "beta")])
    rows = profile_health(app.state.store, app.state.layer)
    row = next(p for p in rows if p["profile"] == "acme-prod")
    assert "acme/metrics.yaml" in row["referenced_by"]


def test_workspace_enumeration_survives_broken_metrics_yaml(tmp_path):
    from sqldash.api.routes_config import profile_health
    from sqldash.project.sources import labeled_sources
    from sqldash.scaffold import create_demo
    from sqldash.server import create_app

    for repo in ("acme", "beta"):
        create_demo(tmp_path / repo)
    (tmp_path / "acme" / ".sqldash" / "metrics.yaml").write_text("metrics: {}\n")
    app = create_app(workspace=[(repo, tmp_path / repo / ".sqldash") for repo in ("acme", "beta")])
    entries = labeled_sources(app.state.store, app.state.layer)
    labels = [e.label for e in entries]
    assert "beta/metrics.yaml" in labels
    assert "acme/metrics.yaml" not in labels
    assert any(label.startswith("acme/demo") for label in labels)
    profile_health(app.state.store, app.state.layer)


def test_an_empty_authored_end_default_fails_loudly(tmp_path):
    """An empty `end` in a daterange default is author error, and the warehouse
    says so. Normalizing it to None instead binds NULL into the author's SQL,
    which is not an error at all — `x BETWEEN a AND NULL` is simply never true,
    so the tile renders a confident 0 rows. A wrong number that looks fine is
    worse here than a loud failure, so this pins the failure.
    """
    (tmp_path / "d.yaml").write_text(
        "title: Empty end\n"
        "source: {type: duckdb, attach_files: true}\n"
        "filters:\n"
        "  - {name: dates, type: daterange, label: When, "
        'default: {start: "2026-01-01", end: ""}}\n'
        "tiles:\n"
        "  - {id: t, chart: table, sql: 'SELECT count(*) AS n FROM orders "
        "WHERE order_date BETWEEN {{ dates_start }} AND {{ dates_end }}'}\n"
    )
    (tmp_path / "orders.csv").write_text("order_date,amount\n2026-01-05,10\n2026-02-05,20\n")
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        _, ex = run_to_completion(c, {"dashboard": "d", "query": "t", "params": {}})
    assert ex["status"] == "error", ex
    assert "invalid date" in (ex["error"] or "").lower(), ex


def test_an_authored_end_token_means_the_end_of_its_window(tmp_path):
    """`-30d` names a window, so in the end position it means today — the same
    thing it means passed as an override or as `metric query --end`. Resolving
    the authored default without its role made one token mean two different
    dates depending on where it was written, and the narrower range is silent.
    """
    from sqldash.models.dashboard import Dashboard
    from sqldash.params import param_values

    dashboard = Dashboard(
        title="T",
        source={"type": "duckdb"},
        filters=[
            {
                "name": "dates",
                "type": "daterange",
                "label": "W",
                "default": {"start": "2026-01-01", "end": "-30d"},
            }
        ],
        queries={"q": "SELECT {{ dates_start }}, {{ dates_end }}"},
        tiles=[{"id": "t", "query": "q"}],
    )
    names = ["dates_start", "dates_end"]
    authored, _ = param_values(dashboard, names, {})
    passed_in, _ = param_values(
        dashboard, names, {"dates_start": "2026-01-01", "dates_end": "-30d"}
    )
    assert authored["dates_end"] == date.today().isoformat()
    assert authored == passed_in


def test_index_ambiguous_row_is_inert_and_findable(client, demo_dir):
    """The flag exists so a collision is discoverable, but `data-search` was
    built from title/name/description only — so typing "ambiguous" in the
    browser filter hid the very rows the flag exists to surface.
    """
    root = demo_dir / ".sqldash"
    made = []
    try:
        for n in ("amb_a", "amb_b"):
            path = root / f"{n}.yaml"
            path.write_text(
                f"title: Dash {n}\n"
                "source: {type: duckdb, database: ':memory:'}\n"
                "metrics:\n  clash_total: {sql: SELECT 1 AS v, expr: SUM(v)}\n"
                "tiles: []\n"
            )
            made.append(path)
        text = client.get("/").text
        row = next(line for line in text.splitlines() if "clash_total" in line and "drow" in line)
        assert "ambiguous amb_a amb_b" in row, row
        assert 'href="/m/clash_total"' not in row, row
        assert "drow-inert" in row, row
        assert 'title="defined inline by amb_a, amb_b"' in text
    finally:
        for path in made:
            path.unlink()


def test_the_dashboard_endpoint_ships_no_literal_credential(tmp_path):
    """`client_payload` redacts `source` through `redact_source`, and the same
    response used to carry the raw file text beside it — handing back every
    literal secret the redaction had just removed. Asserted on the whole
    serialized response, so any future field that reintroduces the file fails
    this too, not only a key named `text`.
    """
    secret = "super-secret-pass-123"
    (tmp_path / "d.yaml").write_text(
        "title: Leak\n"
        "source:\n"
        "  type: postgres\n"
        "  host: db.example.com\n"
        "  database: analytics\n"
        "  username: svc_reports\n"
        f"  password: {secret}\n"
        'queries: {q: "SELECT 1 AS v"}\n'
        "tiles:\n"
        "  - {title: A, query: q}\n"
    )
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        res = c.get("/api/dashboards/d")
        assert res.status_code == 200
        assert secret not in res.text, res.text
        body = res.json()
        assert body["dashboard"]["source"]["password"] == "•••", body["dashboard"]["source"]
        assert body["etag"]


def test_create_dashboard_strips_plaintext_secrets(tmp_path):
    (tmp_path / "demo.yaml").write_text(
        "title: Main\n"
        "source:\n"
        "  type: postgres\n"
        "  host: db.internal\n"
        "  database: analytics\n"
        "  username: svc_dash\n"
        "  password: hunter2plain\n"
        "  options: {sslmode: require, token: abc123}\n"
        "tiles: []\n"
    )
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        res = client.post("/api/dashboards", json={"title": "Copy of main"})
        assert res.status_code == 201, res.text
    text = (tmp_path / "copy_of_main.yaml").read_text()
    assert "hunter2plain" not in text
    assert "abc123" not in text
    assert "sslmode: require" in text
    assert "db.internal" in text


def test_markdown_tiles_escape_raw_html(tmp_path):
    (tmp_path / "dash.yaml").write_text(
        "title: dmg\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - title: md\n"
        "    size: 6x2\n"
        "    markdown: |\n"
        "      **bold stays**\n"
        "      <script>window.__pwned=1</script>\n"
        '      <img src=x onerror="window.__pwned=2">\n'
    )
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        html = client.get("/d/dash").text
    body = html.split('class="tile-body">', 1)[1].split("</div>", 1)[0]
    assert "<strong>bold stays</strong>" in body
    assert "<script>" not in body
    assert "<img" not in body
    assert "&lt;script&gt;window.__pwned=1&lt;/script&gt;" in body
    assert "&lt;img src=x onerror=&quot;window.__pwned=2&quot;&gt;" in body


def test_run_metric_rejects_params_without_dashboard(client):
    res = client.post("/api/run", json={"metric": "revenue", "params": {"region": "us"}})
    assert res.status_code == 422
    assert "filters" in res.json()["detail"]


def test_run_metric_filters_without_dashboard_still_work(client):
    _, result = run_to_completion(client, {"metric": "revenue", "filters": {"region": "us"}})
    assert result["status"] == "done", result


def test_invalid_utf8_yaml_does_not_500_the_pages(tmp_path):
    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "oops.yaml").write_bytes(b"\xff\xfe invalid")
    app = create_app(tmp_path / ".sqldash", allowed_hosts=["testserver"])
    with TestClient(app) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        assert client.get("/").status_code == 200
        assert client.get("/d/demo").status_code == 200
        bad = client.get("/d/oops")
        assert bad.status_code == 422  # the styled broken-file page, not a 500
        assert "text/html" in bad.headers["content-type"]
        assert "oops" in bad.text
        assert "Internal Server Error" not in bad.text
        listing = client.get("/api/dashboards")
        assert listing.status_code == 200
        entry = next(d for d in listing.json()["dashboards"] if d["name"] == "oops")
        assert entry["valid"] is False
        assert "UTF-8" in entry["error"]


def test_deleting_a_non_utf8_broken_dashboard_succeeds(tmp_path):
    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "oops.yaml").write_bytes(b"\xff\xfe invalid")
    app = create_app(tmp_path / ".sqldash", allowed_hosts=["testserver"])
    with TestClient(app) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        res = client.request("DELETE", "/api/dashboards/oops", headers={"If-Match": ""})
        assert res.status_code == 204, res.text
    assert not (tmp_path / ".sqldash" / "oops.yaml").exists()


def test_put_repairs_a_non_utf8_dashboard(tmp_path):
    """PUT used to 500 on path.read_text() of the broken file. #333."""
    create_demo(tmp_path)
    oops = tmp_path / ".sqldash" / "oops.yaml"
    oops.write_bytes(
        b"title: Broken\nsource: {type: duckdb, database: ':memory:'}\ntiles: []\n\xff"
    )
    app = create_app(tmp_path / ".sqldash", allowed_hosts=["testserver"])
    with TestClient(app) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        listing = client.get("/api/dashboards").json()
        entry = next(d for d in listing["dashboards"] if d["name"] == "oops")
        assert entry["valid"] is False
        text = (
            "title: Fixed\n"
            "source: {type: duckdb, database: ':memory:'}\n"
            "tiles:\n"
            "  - title: T\n"
            "    sql: SELECT 1 AS a\n"
        )
        res = client.put("/api/dashboards/oops", json={"text": text}, headers={"If-Match": ""})
        assert res.status_code == 200, res.text
        page = client.get("/d/oops")
        assert page.status_code == 200
        assert "Fixed" in page.text


def test_patch_on_a_non_utf8_dashboard_is_422_not_500(tmp_path):
    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "oops.yaml").write_bytes(b"\xff\xfe invalid")
    app = create_app(tmp_path / ".sqldash", allowed_hosts=["testserver"])
    with TestClient(app) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        res = client.patch(
            "/api/dashboards/oops/positions",
            json={"positions": {"t": {"x": 0, "y": 0, "w": 6, "h": 4}}},
            headers={"If-Match": ""},
        )
        assert res.status_code == 422, res.text
        assert "UTF-8" in res.json()["detail"]


def test_run_refuses_an_inverted_window_on_every_shape(client):
    """#361: every shape reached the warehouse with (later, earlier) and came
    back `done` with `[[None]]`, which the tile drew as a dash. The browser
    renders a 422 detail as the tile's error state, so this is the tile too."""
    shapes = (
        {"metric": "revenue", "start": "2026-09-01", "end": "2026-01-01"},
        {"dashboard": "demo", "metric": "revenue", "start": "2026-09-01", "end": "2026-01-01"},
        {
            "dashboard": "demo",
            "metric": "revenue",
            "params": {"dates_start": "2026-09-01", "dates_end": "2026-01-01"},
        },
        {
            "dashboard": "demo",
            "query": "revenue_by_category",
            "params": {"dates_start": "2026-09-01", "dates_end": "2026-01-01"},
        },
    )
    for body in shapes:
        res = client.post("/api/run", json=body)
        assert res.status_code == 422, (body, res.text)
        assert "date range is inverted" in res.json()["detail"], body
        assert "'2026-09-01' is after" in res.json()["detail"], body
    _, one_day = run_to_completion(
        client, {"metric": "revenue", "start": "2026-01-01", "end": "2026-01-01"}
    )
    assert one_day["status"] == "done", one_day


def test_pages_lock_down_exfil_channels_and_keep_the_token_out_of_the_dom(client):
    page = client.get("/d/demo")
    assert page.status_code == 200
    policy = page.headers["Content-Security-Policy"]
    nonce = re.search(r"'nonce-([^']+)'", policy).group(1)
    assert policy == content_security_policy(nonce)
    for directive in ("img-src 'self' data: blob:", "font-src 'self'", "connect-src 'self'"):
        assert directive in policy
    assert "frame-ancestors 'self'" in policy
    assert page.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert "sqldash-token" not in page.text
    token = client.app.state.api_token
    assert token not in page.text
    cookie = page.headers["set-cookie"]
    assert cookie.startswith(f"sqldash-token-80={token};")
    assert "SameSite=strict" in cookie
    assert "HttpOnly" not in cookie
    api = client.get("/api/dashboards")
    assert api.status_code == 200
    assert "set-cookie" not in api.headers
    assert "'nonce-" in api.headers["Content-Security-Policy"]
    assert '<style id="dash-css">@scope (main.container) {' not in client.get("/").text


def test_script_src_trusts_only_this_responses_nonce(client):
    page = client.get("/d/demo")
    policy = page.headers["Content-Security-Policy"]
    script_src = next(d for d in policy.split("; ") if d.startswith("script-src"))
    assert "'unsafe-inline'" not in script_src
    nonce = re.search(r"'nonce-([^']+)'", script_src).group(1)
    inline = re.findall(r"<script(?![^>]*\bsrc=)([^>]*)>", page.text)
    executable = [attrs for attrs in inline if "application/json" not in attrs]
    assert executable, "base.html bootstraps the theme and importmap inline"
    assert all(f'nonce="{nonce}"' in attrs for attrs in executable), executable
    assert not re.search(r"\son[a-z]+=", page.text)
    again = client.get("/d/demo").headers["Content-Security-Policy"]
    assert f"'nonce-{nonce}'" not in again


HOSTILE = 'Probe <!--<script> & </script> "end"'


def _json_payloads(html: str) -> list[str]:
    return re.findall(r'type="application/json">(.*?)</script>', html, flags=re.S)


def test_json_payloads_are_inert_in_script_data(tmp_path):
    create_demo(tmp_path)
    dash = tmp_path / ".sqldash" / "demo.yaml"
    dash.write_text(dash.read_text().replace("title: Order Analytics", f"title: '{HOSTILE}'", 1))
    metrics = tmp_path / ".sqldash" / "metrics.yaml"
    metrics.write_text(
        metrics.read_text().replace(
            "description: Number of orders placed", f"description: '{HOSTILE}'", 1
        )
    )
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        pages = {
            "/d/demo": lambda data: data["dashboard"]["title"],
            "/d/demo/query": lambda data: data["dashboard"]["title"],
            "/m/order_count": lambda data: data["description"],
        }
        for path, pick in pages.items():
            res = c.get(path)
            assert res.status_code == 200, (path, res.text)
            (payload,) = _json_payloads(res.text)
            assert not set(payload) & set("<>&"), path
            assert pick(json.loads(payload)) == HOSTILE, path
            assert '<script type="module"' in res.text.split(payload, 1)[1], path


def test_script_json_escapes_only_markup_characters():
    data = {"t": "a</script><!--<script>&b", "n": [1, 2.5, None, True], "u": "é\u2028"}
    out = routes_pages.script_json(data)
    assert not set(out) & set("<>&")
    assert json.loads(out) == data


def test_token_cookie_name_follows_the_page_scheme_on_default_ports(demo_dir):
    app = create_app(demo_dir, allowed_hosts=["testserver"])
    with TestClient(app, base_url="https://testserver") as secure:
        assert secure.get("/").headers["set-cookie"].startswith("sqldash-token-443=")
    with TestClient(app, base_url="http://testserver:8400") as explicit:
        assert explicit.get("/").headers["set-cookie"].startswith("sqldash-token-8400=")


def test_a_running_total_is_listed_as_one(client):
    """`cumulative_revenue` has revenue's exact expr, so without the flag the
    listing showed the two as the same metric (#605)."""
    metrics = {m["name"]: m for m in client.get("/api/metrics").json()["metrics"]}
    assert metrics["cumulative_revenue"]["cumulative"] is True
    assert "cumulative" not in metrics["revenue"]
    assert metrics["trailing_28d_revenue"]["window"] == "28 days"
    page = client.get("/m/cumulative_revenue").text
    assert "running total" in page
    assert "running total" not in client.get("/m/revenue").text


def test_metric_page_marks_a_trunc_macro_as_not_runnable_sql(tmp_path):
    """The metric page is where a human copies an expression into the query
    workspace, so an expr sqldash resolves rather than the warehouse has to say
    so beside it. A metric without one keeps the plain definition list."""
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: duckdb, database: ':memory:'}\n"
        "metrics:\n"
        "  active_weeks:\n"
        "    table: orders\n"
        "    expr: \"COUNT(DISTINCT SQLDASH_TRUNC('week', ordered_at))\"\n"
        "  plain_revenue:\n"
        "    table: orders\n"
        "    expr: SUM(amount)\n"
    )
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        macro = c.get("/m/active_weeks")
        plain = c.get("/m/plain_revenue")
    assert macro.status_code == 200, macro.text
    assert "SQLDASH_TRUNC" in macro.text
    assert "not a warehouse function" in macro.text
    assert plain.status_code == 200, plain.text
    assert "def-note" not in plain.text


def _confined_project(tmp_path, secret):
    """A project shaped like `sqldash init --demo`: duckdb over its own csv."""
    root = tmp_path / "proj" / ".sqldash"
    (root / "data").mkdir(parents=True)
    (root / "data" / "orders.csv").write_text("k,v\na,1\nb,2\nc,3\n")
    (root / "leak.yaml").write_text(
        "title: Leak\n"
        "source: {type: duckdb, attach_files: true}\n"
        "queries:\n"
        f"  steal: SELECT content FROM read_text('{secret}')\n"
        "  n: SELECT COUNT(*) AS n FROM orders\n"
        "tiles:\n"
        "  - {title: N, query: n}\n"
    )
    return tmp_path / "proj"


def test_adhoc_sql_cannot_read_the_owners_credential_store(tmp_path):
    """Any viewer of a served dashboard could POST /api/run with
    `read_text('~/.config/sqldash/profiles.yaml')` and get the owner's warehouse
    passwords back in the results grid — the served page's token is the only
    credential the route wants. #622."""
    secret = tmp_path / "config" / "sqldash" / "profiles.yaml"
    secret.parent.mkdir(parents=True)
    secret.write_text("acme:\n  password: test-fixture-not-a-real-secret\n")
    project = _confined_project(tmp_path, secret)
    app = create_app(project, allowed_hosts=["testserver"])
    with TestClient(app) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        for payload in (
            {"dashboard": "leak", "sql": f"SELECT content FROM read_text('{secret}')"},
            {"dashboard": "leak", "sql": f"SELECT file FROM glob('{secret.parent}/*')"},
            {"dashboard": "leak", "query": "steal"},
        ):
            _, ex = run_to_completion(client, payload)
            assert ex["status"] == "error", ex
            assert "test-fixture-not-a-real-secret" not in json.dumps(ex)
            assert "external_access: true" in ex["error"]
        _, ok = run_to_completion(client, {"dashboard": "leak", "query": "n"})
        assert ok["result"]["rows"] == [[3]], ok


_CONFINED_ONLY = (
    "title: W\nsource: {type: duckdb, database: wh.duckdb}\n"
    "tiles:\n  - {title: c, sql: SELECT v FROM t}\n"
)
_OPTED_OUT_ONLY = (
    "title: W\nsource: {type: duckdb, database: wh.duckdb, external_access: true}\n"
    "tiles:\n  - {title: c, sql: SELECT v FROM t}\n"
)
_CONFINED_AND_OPTED_OUT = (
    "title: W\nsource: {type: duckdb, database: wh.duckdb}\n"
    "sources:\n  open: {type: duckdb, database: wh.duckdb, external_access: true}\n"
    "tiles:\n  - {title: c, sql: SELECT v FROM t}\n"
    "  - {title: o, source: open, sql: SELECT 1 AS v}\n"
)


def _shared_file_project(tmp_path, text):
    """A dashboard over one duckdb file, plus a secret outside the project."""
    root = tmp_path / "proj" / ".sqldash"
    root.mkdir(parents=True)
    conn = duckdb.connect(str(root / "wh.duckdb"))
    conn.execute("CREATE TABLE t AS SELECT 7 AS v")
    conn.close()
    (root / "wh.yaml").write_text(text)
    secret = tmp_path / "outside" / "secret.txt"
    secret.parent.mkdir(parents=True)
    secret.write_text("test-fixture-not-a-real-secret\n")
    return tmp_path / "proj", f"SELECT content FROM read_text('{secret}')"


def test_editing_the_only_duckdb_source_to_external_access_serves_without_restart(tmp_path):
    """`external_access: true` is the documented way to let a source out, and
    editing a running dashboard to say so was refused until restart, blamed on a
    second source that did not exist (#642). Once no dashboard declares the
    confined config any more, the change is an edit, not a second dashboard."""
    project, read = _shared_file_project(tmp_path, _CONFINED_ONLY)
    app = create_app(project, allowed_hosts=["testserver"])
    with TestClient(app) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        _, ok = run_to_completion(client, {"dashboard": "wh", "sql": "SELECT v FROM t"})
        assert ok["result"]["rows"] == [[7]], ok
        (project / ".sqldash" / "wh.yaml").write_text(_OPTED_OUT_ONLY)
        _, ex = run_to_completion(client, {"dashboard": "wh", "sql": read})
        assert ex["status"] == "done", ex
        assert "test-fixture-not-a-real-secret" in ex["result"]["rows"][0][0]


def test_a_file_that_raced_a_confined_and_external_mix_recovers_on_edit(tmp_path):
    """The #642 repro: one dashboard declaring a confined and an external source
    on one file, edited down to the external one alone. The refused external
    connector was cached, so the edit was a cache hit and nothing ever retired
    the confined engine holding the file."""
    project, read = _shared_file_project(tmp_path, _CONFINED_AND_OPTED_OUT)
    app = create_app(project, allowed_hosts=["testserver"])
    with TestClient(app) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        for _ in range(3):
            _, confined = run_to_completion(client, {"dashboard": "wh", "sql": "SELECT v FROM t"})
            assert confined["result"]["rows"] == [[7]], confined
            _, opened = run_to_completion(
                client, {"dashboard": "wh", "source": "open", "sql": "SELECT 1 AS v"}
            )
            assert opened["status"] == "error", opened
            assert "restart the server" in opened["error"]
        (project / ".sqldash" / "wh.yaml").write_text(_OPTED_OUT_ONLY)
        _, ex = run_to_completion(client, {"dashboard": "wh", "sql": read})
        assert ex["status"] == "done", ex
        assert "test-fixture-not-a-real-secret" in ex["result"]["rows"][0][0]


def test_a_dashboard_still_declaring_the_confined_source_keeps_the_file_confined(tmp_path):
    """The #641 half: a second dashboard asking for external access on a file a
    still-declared confined source holds is refused, and the confined source
    stays confined. Only an edit that leaves nothing declaring the old reach may
    widen it."""
    project, read = _shared_file_project(tmp_path, _CONFINED_ONLY)
    (project / ".sqldash" / "open.yaml").write_text(_OPTED_OUT_ONLY)
    app = create_app(project, allowed_hosts=["testserver"])
    with TestClient(app) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        _, ok = run_to_completion(client, {"dashboard": "wh", "sql": "SELECT v FROM t"})
        assert ok["result"]["rows"] == [[7]], ok
        for _ in range(2):
            _, refused = run_to_completion(client, {"dashboard": "open", "sql": read})
            assert refused["status"] == "error", refused
            assert "test-fixture-not-a-real-secret" not in json.dumps(refused)
            _, confined = run_to_completion(client, {"dashboard": "wh", "sql": read})
            assert confined["status"] == "error", confined
            assert "test-fixture-not-a-real-secret" not in json.dumps(confined)


def test_the_payload_carries_the_day_its_defaults_were_resolved_against(client):
    """The filter bar resolves presets against this, not the client clock, so the
    browser and every headless surface run one window for one preset (#673)."""
    body = client.get("/api/dashboards/demo").json()
    assert body["today"] == date.today().isoformat()
    dates = next(f for f in body["dashboard"]["filters"] if f["type"] == "daterange")
    assert dates["resolved_default"]["preset"] == "last_60_days"
    assert dates["resolved_default"]["end"] == body["today"]
    page = client.get("/d/demo").text
    stamped = json.loads(
        page.split('id="dashboard-data" type="application/json">')[1].split("</script>")[0]
    )
    assert stamped["today"] == body["today"]


@pytest.fixture
def strict_client(tmp_path):
    create_demo(tmp_path)
    app = create_app(tmp_path / ".sqldash", allowed_hosts=["testserver"])
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        yield c, tmp_path / ".sqldash"


NEW_DASHBOARD = "title: New\nsource: {type: duckdb, database: ':memory:'}\ntiles: []\n"


@pytest.fixture
def fresh_client(tmp_path):
    create_demo(tmp_path)
    app = create_app(tmp_path / ".sqldash", allowed_hosts=["testserver"])
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        yield c, tmp_path / ".sqldash"


def _detail(res) -> str:
    return " ".join(item["msg"] for item in res.json()["detail"])


def test_meta_patch_refuses_a_misspelled_field_by_name(strict_client):
    client, root = strict_client
    before = (root / "demo.yaml").read_text()
    etag = client.get("/api/dashboards/demo").json()["etag"]
    res = client.patch(
        "/api/dashboards/demo/meta", json={"titel": "Typo"}, headers={"If-Match": etag}
    )
    assert res.status_code == 422, res.text
    detail = _detail(res)
    assert "unknown field 'titel'" in detail
    assert "did you mean 'title'" in detail
    assert "valid fields: title, description" in detail
    assert (root / "demo.yaml").read_text() == before


def test_create_refuses_an_unknown_field_instead_of_dropping_it(strict_client):
    client, root = strict_client
    res = client.post("/api/dashboards", json={"title": "Probe", "nmae": "probe"})
    assert res.status_code == 422, res.text
    assert "valid fields: title, repo" in _detail(res)
    assert not (root / "probe.yaml").exists()


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("put", "/api/dashboards/demo", {"text": None, "txt": 1}),
        ("patch", "/api/dashboards/demo/positions", {"positions": {}, "position": {}}),
        ("put", "/api/dashboards/demo/filters", {"filters": [], "filterz": []}),
    ],
)
def test_dashboard_write_bodies_refuse_extra_fields(strict_client, method, path, body):
    client, root = strict_client
    etag = client.get("/api/dashboards/demo").json()["etag"]
    if "text" in body:
        body = {**body, "text": (root / "demo.yaml").read_text() + "\n# edited\n"}
    res = client.request(method.upper(), path, json=body, headers={"If-Match": etag})
    assert res.status_code == 422, res.text
    assert "unknown field" in _detail(res)
    assert client.get("/api/dashboards/demo").json()["etag"] == etag


def test_put_with_a_stale_etag_does_not_resurrect_a_deleted_dashboard(fresh_client):
    client, root = fresh_client
    etag = client.get("/api/dashboards/demo").json()["etag"]
    assert client.delete("/api/dashboards/demo", headers={"If-Match": etag}).status_code == 204
    res = client.put(
        "/api/dashboards/demo", json={"text": NEW_DASHBOARD}, headers={"If-Match": etag}
    )
    assert res.status_code == 404, res.text
    assert "If-Match: *" in res.json()["detail"]
    assert not (root / "demo.yaml").exists()


def test_put_creates_a_dashboard_only_with_if_match_star(fresh_client):
    client, root = fresh_client
    res = client.put(
        "/api/dashboards/fresh", json={"text": NEW_DASHBOARD}, headers={"If-Match": "anything"}
    )
    assert res.status_code == 404, res.text
    assert not (root / "fresh.yaml").exists()
    res = client.put(
        "/api/dashboards/fresh", json={"text": NEW_DASHBOARD}, headers={"If-Match": "*"}
    )
    assert res.status_code == 200, res.text
    assert (root / "fresh.yaml").read_text() == NEW_DASHBOARD


@pytest.mark.parametrize("name", ["we ird?q#h%", "Upper", "two__underscores", "_edge"])
def test_put_create_applies_the_post_name_rules(fresh_client, name):
    client, root = fresh_client
    res = client.put(
        f"/api/dashboards/{quote(name, safe='')}",
        json={"text": NEW_DASHBOARD},
        headers={"If-Match": "*"},
    )
    assert res.status_code == 422, res.text
    assert "invalid dashboard name" in res.json()["detail"]
    assert sorted(p.name for p in root.glob("*.yaml")) == [
        "agents.yaml",
        "demo.yaml",
        "metrics.yaml",
    ]


def test_dashboard_links_are_url_encoded(fresh_client):
    client, root = fresh_client
    (root / "we ird?q#h%.yaml").write_text(NEW_DASHBOARD)
    index = client.get("/")
    assert 'href="/d/we%20ird%3Fq%23h%25"' in index.text
    page = client.get("/d/we%20ird%3Fq%23h%25")
    assert page.status_code == 200, page.text
    assert 'href="/d/we%20ird%3Fq%23h%25/workspace"' in page.text


@pytest.mark.parametrize(
    "payload",
    [
        {"metric": "revenue", "start": "-99999999d"},
        {"dashboard": "demo", "metric": "revenue", "params": {"dates_start": "-99999999d"}},
        {
            "dashboard": "demo",
            "metric": "revenue",
            "params": {"dates_end": "last_99999999999_days"},
        },
    ],
)
def test_run_huge_relative_date_is_422_not_500(client, payload):
    res = client.post("/api/run", json=payload)
    assert res.status_code == 422, res.text
    assert res.json()["detail"].startswith("unrecognized date ")


def test_repo_add_refuses_a_misspelled_name_instead_of_registering_under_the_dir(
    strict_client, tmp_path, monkeypatch
):
    client, _ = strict_client
    registry = tmp_path / "repos.yaml"
    monkeypatch.setattr(workspace, "registry_path", lambda: registry)
    (tmp_path / "extra").mkdir()
    res = client.post("/api/repos", json={"target": str(tmp_path / "extra"), "nmae": "typo"})
    assert res.status_code == 422, res.text
    detail = _detail(res)
    assert "unknown field 'nmae' (did you mean 'name'?)" in detail
    assert "valid fields: target, name, branch" in detail
    assert not registry.exists()


@pytest.mark.parametrize(
    ("method", "path", "body", "hint"),
    [
        ("post", "/api/run", {"dashboard": "demo", "sql": "SELECT 1", "rowlimit": 5}, "row_limit"),
        ("post", "/api/dashboards/demo/tiles", {"tile": {"title": "t"}, "sqll": "x"}, "sql"),
        ("post", "/api/dashboards/demo/library", {"title": "q", "sql": "x", "titel": "y"}, "title"),
        ("patch", "/api/dashboards/demo/library/abc", {"titel": "y"}, "title"),
        ("post", "/api/dashboards/demo/roles", {"rol": "ADMIN"}, "role"),
        ("post", "/api/dashboards/demo/databases", {"databse": "DB"}, "database"),
        ("post", "/api/studio/entrypoints/check", {"entrypont": "x"}, "entrypoint"),
        ("post", "/api/studio/sessions/s1/finish", {"revison": "r"}, "revision"),
        ("post", "/api/studio/sessions/s1/permission-mode", {"auto_aprove": True}, "auto_approve"),
    ],
)
def test_every_request_body_names_a_misspelled_field(strict_client, method, path, body, hint):
    client, _ = strict_client
    res = client.request(method.upper(), path, json=body, headers={"If-Match": "*"})
    assert res.status_code == 422, res.text
    assert f"(did you mean '{hint}'?)" in _detail(res)


def test_every_json_request_body_model_is_strict():
    models = {}
    for module in pkgutil.iter_modules(sqldash.api.__path__):
        router = getattr(importlib.import_module(f"sqldash.api.{module.name}"), "router", None)
        for route in getattr(router, "routes", []):
            if isinstance(route, APIRoute):
                for param in route.dependant.body_params:
                    models[param.field_info.annotation.__name__] = param.field_info.annotation
    assert {"RepoRequest", "RunRequest", "SessionRequest", "AgentEntrypoint"} <= set(models)
    assert [name for name, model in models.items() if not issubclass(model, StrictBody)] == []


def test_page_style_block_cannot_close_its_style_tag(client, monkeypatch):
    """The page block is rendered raw into <style>. Its value check keeps `</` out
    today; the render escapes it anyway, as it already does for the dashboard block."""
    hostile = ":root { --page: red; }</style><b id=injected>x</b><style>"
    monkeypatch.setattr(
        Dashboard, "page_style", lambda self: PageStyle(page=hostile, dashboard=None, dropped=())
    )
    page = client.get("/d/demo").text
    block = page.split('<style id="dash-page">', 1)[1].split("</style>", 1)[0]
    assert block == hostile.replace("</", "<\\/")
