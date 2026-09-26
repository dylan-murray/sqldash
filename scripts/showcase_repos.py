"""Sibling dashboard repos for the README recordings.

The walkthrough is one real project (`examples/studio`); these are the other
teams' folders next to it in the library, so the first frame shows a workspace
rather than a single directory. Self-contained DuckDB `VALUES` dashboards, no
data files, never committed.
"""

from pathlib import Path

REPOS = {
    "growth_marketing": [
        ("channel_mix", "Channel Mix", "Spend and CAC by channel."),
        ("lifecycle_email", "Lifecycle Email", "Sends, opens, and downstream revenue."),
        ("paid_social", "Paid Social", "Campaign performance across networks."),
    ],
    "platform_observability": [
        ("service_latency", "Service Latency", "p50 and p95 across services."),
        ("error_budget", "Error Budget", "Burn rate against SLO."),
    ],
    "finance_ops": [
        ("margin_by_product", "Margin by Product", "Gross margin and COGS."),
        ("cash_forecast", "Cash Forecast", "13-week rolling forecast."),
    ],
}


CHANNEL_MIX = """title: Channel Mix
description: Spend, acquisition cost, and conversion across paid and organic.

source: {type: duckdb, database: ":memory:"}

queries:
  channels: |
    SELECT * FROM (VALUES
      ('organic', 0, 41200, 0.041), ('paid search', 48000, 28400, 0.028),
      ('paid social', 31500, 16900, 0.023), ('email', 4200, 22100, 0.067),
      ('referral', 9800, 12750, 0.052), ('affiliate', 15300, 9400, 0.019)
    ) AS t(channel, spend, signups, conversion)

tiles:
  - title: Spend
    chart: big_number
    format: currency
    size: 3x2
    sql: "SELECT 108800 AS spend"
  - title: Signups
    chart: big_number
    format: compact
    size: 3x2
    sql: "SELECT 130750 AS signups"
  - title: Blended CAC
    chart: big_number
    format: currency
    size: 3x2
    sql: "SELECT 8.32 AS cac"
  - title: Conversion
    chart: big_number
    format: percent
    size: 3x2
    sql: "SELECT 0.038 AS conversion"
  - title: Signups by channel
    chart: bar
    size: 7x5
    format: compact
    sql: |
      SELECT channel, signups FROM (VALUES
        ('organic', 41200), ('paid search', 28400), ('paid social', 16900),
        ('email', 22100), ('referral', 12750), ('affiliate', 9400)
      ) AS t(channel, signups)
      ORDER BY signups DESC
  - title: Channel detail
    chart: table
    size: 5x5
    query: channels
    format: {spend: currency, signups: compact, conversion: percent}
"""


def repo_dashboard(title: str, description: str, seed: int) -> str:
    trend = (
        f"SELECT DATE '2026-06-01' + CAST(i AS INT) AS day, "
        f"60 + (i * {seed}) % 40 AS value FROM range(45) t(i)"
    )
    return (
        f"title: {title}\n"
        f"description: {description}\n\n"
        'source: {type: duckdb, database: ":memory:"}\n\n'
        "tiles:\n"
        "  - title: Trend\n"
        "    chart: area\n"
        "    size: 8x4\n"
        f'    sql: "{trend}"\n'
        "  - title: Total\n"
        "    chart: big_number\n"
        "    size: 4x2\n"
        f'    sql: "SELECT {seed * 1187} AS total"\n'
    )


def write_repos(work: Path) -> list[tuple[str, Path]]:
    workspace = []
    for index, (repo, dashboards) in enumerate(REPOS.items()):
        root = work / repo
        root.mkdir(parents=True)
        for offset, (slug, title, description) in enumerate(dashboards):
            seed = 3 + index * 5 + offset
            body = (
                CHANNEL_MIX if slug == "channel_mix" else repo_dashboard(title, description, seed)
            )
            (root / f"{slug}.yaml").write_text(body)
        workspace.append((repo, root))
    return workspace
