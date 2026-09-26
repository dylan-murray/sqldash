# Custom dashboard themes

A dashboard's top-level `css:` block controls its canvas, cards, typography, and
filters. It travels with the YAML, so a theme is a normal git diff. You can write
it yourself or ask [AI Studio](studio.md) to make the changes.

![Six themes written by an agent from one line each: Neon observatory, Rose quartz, Electric citrus, Morning broadsheet, Ember, Amber terminal](../assets/demo-themes.webp)

## Try the examples

From a checkout of this repo:

```bash
sqldash serve examples/studio
```

Open **Neon observatory**, **Electric citrus**, **Ember** or **Amber terminal** in
the dashboard picker with the app in dark appearance, and **Rose quartz** or
**Morning broadsheet** in light. All six use the same revenue, order count, and
average order value metrics, with generated DuckDB data and working date and
region filters.

[Example files and prompts](../examples/studio/README.md)

## Make it yours

Open AI Studio on **Revenue overview**, pin the revenue tile, and describe the
change:

> Make revenue the focal point. Give it violet glass and a brighter number.

Then add instructions for the whole dashboard:

> Apply a neon observatory theme: a dark grid background, violet cards, cyan
> accents, and generous spacing. Put the styles in the dashboard's CSS block.
> Keep the metric definitions, queries, filters, and tile IDs unchanged.

Send the requests to your coding agent and keep iterating as the dashboard
refreshes. **Undo last edit** restores the latest turn's tracked YAML/CSS changes;
it is not an unlimited history. Review the files with `git diff` before committing.
See the [Studio guide](studio.md) for entrypoints, permissions, and undo boundaries.

## Write it directly

A theme is one `css:` block at the top level of the dashboard YAML. Design tokens
written at the top of it set the whole page, edge to edge, in both light and dark mode.
Every other rule styles the dashboard itself: tiles, headings, charts.

```yaml
css: |
  --page: #100d1c;
  --page-glow: radial-gradient(ellipse at top left, #39215d, transparent 60%);
  --glass: #100d1cd9;
  --surface: #20172e;
  --ink-1: #f5edff;
  --ink-2: #c6b6e7;
  --ink-muted: #ad9ccb;
  --accent: #dcbbff;
  .tile {
    border: 1px solid #9670c8;
    border-radius: 20px;
  }
  .tile[data-tile-id="revenue"] .value {
    color: #dcbbff;
  }
```

The last rule targets a tile with `id: revenue`. Explicit tile IDs make targeted
styles easier to maintain when titles change.

Page tokens are `--page`, `--page-glow`, `--glass` (the top bar), `--surface`,
`--surface-raised`, the inks, borders, `--accent` and friends, and `--series-1` to
`--series-8` for chart colours. They count as page level when written bare at the top,
inside `:root`, `html`, `body` or `:scope`, or as a plain `background` or `color` on
`body`. Values are colours or gradients; anything else at page level is dropped, and
`sqldash lint` names it. A token set inside a narrower selector stays scoped to it,
which is how the Citrus example darkens the ink of one lime tile.

To set a token for one appearance mode only, add the theme to the selector:
`:root[data-theme="dark"]` or `:root[data-theme="light"]` (`html` and `body` work the
same way, and `:root[data-theme]` means both). A selector list such as
`:root, :root[data-theme]` counts as one page block. A `@media` or `@supports` block
around a page block keeps its condition.

Your own variables are welcome in the same blocks. A custom `--crawl` or `--holo` in
`:root`, `html` or `body` is kept for the dashboard: it is set on `:scope`, per theme when
the block named one, so `var(--crawl)` works in every rule below it. It never reaches the
top bar or the Studio panel. When a page token reads one, as in `--accent: var(--crawl)`,
the page gets the variable's value.

```yaml
css: |
  :root, :root[data-theme] {
    --crawl: #ffe81f;
    --page: #02030a;
    --accent: var(--crawl);
  }
  :root[data-theme="light"] {
    --crawl: #7a6500;
  }
  .dash-title-row h1 {
    color: transparent;
    -webkit-text-stroke: 2px var(--crawl);
  }
```

The rest of `css:` is wrapped in `@scope (main.container)`, so it reaches the dashboard
area and nothing else: use `:scope` for the dashboard's own box, and leave the page
background, the navigation and the Studio panel to the page tokens.

A rule can still depend on the theme. `:root[data-theme="dark"] .tile`, `html body .tile`
or `:root[data-theme] .tile` style tiles as written: sqldash puts `:scope` after the page
prefix, so the rule reaches the dashboard and nothing outside it. The prefix has to be
followed by a space. `:root > .tile`, `html + .x`, or a bare `body` in a selector list
like `body, .tile` cannot match anything in the dashboard, and `sqldash lint` names them.

Nested rules and nested `@media` blocks work at the top level of `css:` and inside
`:scope`, where they are kept exactly as written and in the order written, so a later
declaration beats an earlier nested `&` rule just as it does in plain CSS nesting. A page
token inside `:scope` paints the page and stays on `:scope` too. Nesting one inside `:root`, `html` or
`body` does not work, because those selectors sit outside the scope and the rule could
never apply. It is dropped and `sqldash lint` names it; move it to the top level or
into `:scope`:

```yaml
css: |
  :scope {
    --page: #0c0918;
    @media (min-width: 60em) {
      .tile { border-radius: 20px; }
    }
  }
```

Charts are rendered on canvas. CSS targeting cards or text does not directly
style chart series, and chart palette tokens currently come from the document
root rather than dashboard-scoped variables. The examples use a cosmetic `filter`
on `.chart-mount` to tint charts; it affects the whole rendered chart, including
its labels and tooltip. Check contrast in the appearance mode you intend to use.

Use local CSS gradients and available fonts. The dashboard's content security
policy blocks external font and image URLs; a theme cannot rely on Google Fonts
or a remote background image. Keep text readable, preserve focus indicators,
and respect `prefers-reduced-motion` when adding animation.
