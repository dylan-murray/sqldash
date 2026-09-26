# Studio theme showcase

Four dashboards, one set of governed metrics. Everything runs on generated
DuckDB data: no credentials, downloads, or warehouse required.

From the repository root:

```bash
sqldash lint examples/studio
sqldash serve examples/studio
```

Studio needs macOS or Linux and a separately installed coding agent. You can
browse the themes without an agent or Studio using `sqldash serve examples/studio`.

| Dashboard | Design | App appearance |
| --- | --- | --- |
| `revenue.yaml` | Starting point for your own Studio edits | Dark or light |
| `neon.yaml` | Violet glass, a glowing grid, cyan accents | Dark |
| `citrus.yaml` | Acid lime, angular cards, offset shadows | Dark |
| `quartz.yaml` | Rose and lavender, rounded cards, serif heading | Light |
| `broadsheet.yaml` | Cream newsprint, black ink, serif headlines, a double rule | Light |
| `ember.yaml` | Sunset coral and orange on deep plum, revenue lit like coals | Dark |
| `terminal.yaml` | Black and amber, monospace numbers, a prompt before the title | Dark |

Switch dashboards with the picker in the top bar. Each theme sets its own page,
top bar and surface colours with page tokens at the top of `css:`, so the app chrome
follows the theme. Neon, Citrus, Ember and Terminal are designed for dark
appearance, Quartz and Broadsheet for light; the other mode still works. The three
newer themes also set `--series-N`, so their charts take the theme's palette; Neon,
Citrus and Quartz tint charts with a CSS filter instead. These are editable examples, not built-in
presets that require a theme installer.

![Theme gallery](../../assets/demo-themes.webp)

`metrics.yaml` generates 120 days of sample orders relative to today, with region,
channel, category, product, and customer columns. Revenue, order count, customers,
and average order value share the same date and dimensions, so every dashboard
here filters by period, region, and channel and breaks revenue down by category,
channel, and product.
Each dashboard uses those definitions and the same eight tiles; only its title,
description, and CSS change. The date-relative data keeps the demo useful later.

## Try Studio

Start with **Revenue overview**, open **AI Studio**, and pin the revenue tile:

> Make revenue the focal point. Violet glass, brighter number.

Add a message before sending:

> Apply the Neon observatory theme from neon.yaml. Keep the metrics and filters.

Choose your agent, send the requests, and watch the file refresh. Keep chatting to
refine it, or use **Undo last edit** to restore the latest edit. For another look,
try “Apply the Electric citrus theme from citrus.yaml.”

The README recording uses a labeled scripted demo agent for reproducibility;
your installed agent will generate its own response and changes.

[Studio guide](../../docs/studio.md) · [Custom themes guide](../../docs/themes.md)
