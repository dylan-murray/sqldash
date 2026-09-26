"""Generate a workspace-scale UX fixture: four themed repos, ~50 dashboards,
100 metrics, every tile backed by self-contained DuckDB SQL so the whole thing
renders without any external data.

  uv run python scripts/seed_scale.py [root]      # default /tmp/sqldash-scale
  uv run sqldash repo add <root>/<repo> ...       # or let --register do it
"""

import sys
import textwrap
from pathlib import Path

EVENTS_SQL = (
    "SELECT DATE '2026-02-01' + CAST(i % 180 AS INT) AS event_date, "
    "['us','eu','apac','latam'][(i % 4) + 1] AS region, "
    "['web','mobile','partner'][(i % 3) + 1] AS channel, "
    "round(50 + (i % 97) * 3.7, 2) AS amount, "
    "1 + i % 5 AS qty FROM range(2000) t(i)"
)

EXPRS = ["SUM(amount)", "COUNT(*)", "AVG(amount)", "SUM(qty)", "AVG(qty)"]

MONEY_WORDS = (
    "revenue",
    "spend",
    "cost",
    "cac",
    "ltv",
    "aov",
    "margin",
    "fees",
    "burn",
    "payroll",
    "opex",
    "capex",
    "profit",
    "ebitda",
    "receivable",
    "payable",
    "debt",
)

REPOS = {
    "commerce-analytics": {
        "metrics": [
            "gross_revenue",
            "net_revenue",
            "orders",
            "aov",
            "refunds",
            "refund_rate",
            "units_sold",
            "repeat_purchase_rate",
            "cart_abandonment",
            "checkout_conversion",
            "discount_spend",
            "shipping_revenue",
            "shipping_cost",
            "gross_margin",
            "basket_size",
            "first_time_buyers",
            "returning_buyers",
            "sms_attributed_revenue",
            "email_attributed_revenue",
            "organic_revenue",
            "paid_revenue",
            "wholesale_revenue",
            "marketplace_fees",
            "chargebacks",
            "promo_redemptions",
        ],
        "dashboards": [
            "Revenue Overview",
            "Orders Deep Dive",
            "Refunds & Chargebacks",
            "Regional Performance",
            "Channel Mix",
            "Fulfillment Health",
            "Weekly Business Review",
            "Holiday Readiness",
            "Pricing Experiments",
            "Customer Cohorts",
            "Marketplace Watch",
            "Wholesale Tracker",
            "Executive Summary",
        ],
    },
    "growth-marketing": {
        "metrics": [
            "signups",
            "activations",
            "activation_rate",
            "trials_started",
            "trial_conversion",
            "mql",
            "sql_leads",
            "cac",
            "ltv",
            "ltv_cac_ratio",
            "campaign_spend",
            "campaign_clicks",
            "campaign_impressions",
            "ctr",
            "cpc",
            "cpm",
            "referral_signups",
            "viral_coefficient",
            "waitlist_joins",
            "demo_requests",
            "newsletter_subscribers",
            "unsubscribes",
            "churned_users",
            "reactivations",
            "nps_responses",
        ],
        "dashboards": [
            "Funnel Overview",
            "Campaign Performance",
            "Paid Acquisition",
            "Organic Growth",
            "Email Engagement",
            "Referral Program",
            "Activation Journey",
            "Churn Watch",
            "Launch Retrospective",
            "Landing Page Tests",
            "Attribution Explorer",
            "Growth Weekly",
            "Board Metrics",
        ],
    },
    "platform-observability": {
        "metrics": [
            "p50_latency",
            "p95_latency",
            "p99_latency",
            "error_rate",
            "request_count",
            "uptime_pct",
            "apdex",
            "saturation",
            "cpu_utilization",
            "memory_utilization",
            "disk_io",
            "network_egress",
            "queue_depth",
            "consumer_lag",
            "cache_hit_rate",
            "cold_starts",
            "deploy_count",
            "rollback_count",
            "incident_count",
            "mttr",
            "mtbf",
            "alert_volume",
            "pager_pages",
            "slow_queries",
            "timeout_count",
        ],
        "dashboards": [
            "Service Health",
            "API Latency",
            "Error Budget",
            "Incident Review",
            "Deploy Velocity",
            "Queue Backpressure",
            "Cache Efficiency",
            "Database Load",
            "Edge Performance",
            "On-call Load",
            "Capacity Planning",
            "SLO Scorecard",
        ],
    },
    "finance-ops": {
        "metrics": [
            "invoices_issued",
            "invoices_paid",
            "days_sales_outstanding",
            "accounts_receivable",
            "accounts_payable",
            "cash_burn",
            "runway_months",
            "opex",
            "capex",
            "payroll_cost",
            "cloud_spend",
            "saas_spend",
            "vendor_count",
            "budget_variance",
            "gross_profit",
            "operating_margin",
            "ebitda",
            "deferred_revenue",
            "collections_rate",
            "bad_debt",
            "fx_impact",
            "tax_accruals",
            "expense_reports",
            "reimbursements",
            "headcount_cost",
        ],
        "dashboards": [
            "Cash Position",
            "AR Aging",
            "AP Pipeline",
            "Vendor Spend",
            "Cloud Cost Watch",
            "Budget vs Actuals",
            "Payroll Summary",
            "Quarterly Close",
            "Runway Model",
            "Collections Tracker",
            "Spend Anomalies",
            "CFO Dashboard",
        ],
    },
}


def humanize(name: str) -> str:
    return name.replace("_", " ").capitalize()


def metric_yaml(names: list[str]) -> str:
    lines = [
        'source: {type: duckdb, database: ":memory:"}',
        "relations:",
        f'  events: {{sql: "{EVENTS_SQL}"}}',
        "metrics:",
    ]
    for i, name in enumerate(names):
        fmt = (
            "currency"
            if any(w in name for w in MONEY_WORDS)
            else ("percent" if name.endswith(("_rate", "_pct", "conversion")) else "number")
        )
        lines += [
            f"  {name}:",
            f"    title: {humanize(name)}",
            f"    description: {humanize(name)} across all {'regions' if i % 2 else 'channels'}.",
            "    relation: events",
            f"    expr: {EXPRS[i % len(EXPRS)]}",
            f"    format: {fmt}",
            "    time_dimension: {name: event_date, grain: day}",
            "    dimensions: [{name: region}, {name: channel}]",
        ]
    return "\n".join(lines) + "\n"


def series_sql(seed: int) -> str:
    return (
        f"SELECT DATE '2026-05-01' + CAST(i AS INT) AS day, "
        f"round({500 + seed * 40} + {150 + seed * 9} * sin(i / {4 + seed % 5}.0) "
        f"+ (i * {17 + seed} % 90), 2) AS value FROM range(60) t(i)"
    )


def bar_sql(seed: int) -> str:
    rows = ", ".join(
        f"('{label}', {round(900 + ((seed * 7 + k * 131) % 800) * 3.1, 2)})"
        for k, label in enumerate(["north", "south", "east", "west", "central"])
    )
    return f"SELECT c AS segment, v AS total FROM (VALUES {rows}) t(c, v)"


def dashboard_yaml(title: str, index: int, metrics: list[str]) -> str:
    m1 = metrics[index % len(metrics)]
    m2 = metrics[(index * 3 + 1) % len(metrics)]
    tiles = [
        f"  - title: {humanize(m1)}\n    metric: {m1}\n    size: 3x2",
        f"  - title: {humanize(m2)} by day\n    metric: {m2}\n    grain: day\n"
        "    chart: area\n    size: 9x2",
        f'  - title: Trend\n    chart: line\n    size: 6x3\n    sql: "{series_sql(index)}"',
        f'  - title: By segment\n    chart: bar\n    size: 6x3\n    sql: "{bar_sql(index)}"',
    ]
    if index % 3 == 0:
        tiles.append(
            f"  - title: Breakdown\n    chart: table\n    size: 12x2\n"
            f'    sql: "{bar_sql(index + 5)}"'
        )
    if index % 4 == 0:
        tiles.append(
            "  - markdown: |\n"
            f"      **{title}** — generated fixture for scale testing. "
            "Every number here is synthetic.\n    size: 12x1"
        )
    body = textwrap.dedent(
        f"""\
        title: {title}
        description: {humanize(m1)} and {humanize(m2).lower()} for the {title.lower()} working group.

        source: {{type: duckdb, database: ":memory:"}}

        tiles:
        """
    )
    return body + "\n".join(tiles) + "\n"


def main() -> None:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/sqldash-scale")
    total_dashboards = 0
    total_metrics = 0
    for repo, config in REPOS.items():
        folder = root / repo / ".sqldash"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "metrics.yaml").write_text(metric_yaml(config["metrics"]))
        total_metrics += len(config["metrics"])
        for i, title in enumerate(config["dashboards"]):
            slug = title.lower().replace(" & ", " ").replace(" ", "_").replace("-", "_")
            (folder / f"{slug}.yaml").write_text(
                dashboard_yaml(title, i + len(repo), config["metrics"])
            )
            total_dashboards += 1
    broken = root / "commerce-analytics" / ".sqldash" / "legacy_import.yaml"
    broken.write_text("title: Legacy Import\nsource: {typ: duckdb}\ntiles: []\n")
    print(
        f"seeded {total_dashboards} dashboards + {total_metrics} metrics "
        f"(+1 intentionally broken) across {len(REPOS)} repos under {root}"
    )
    for repo in REPOS:
        print(f"  uv run sqldash repo add {root / repo}")
    print("then: uv run sqldash serve   (from a directory with no dashboards)")


if __name__ == "__main__":
    main()
