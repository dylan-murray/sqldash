# Agents

Define an agent once in `agents.yaml`, next to `metrics.yaml`, and `sqldash mcp`
serves it to whatever coding agent you use. sqldash never runs a model. The host
picks the agent's prompt, runs it with its own model, and calls the tools sqldash
serves. The definition is versioned in git and reviewed in a pull request like a
metric is.

## The file

This example uses the metrics defined in the [semantic-layer guide](semantic-layer.md).
Both revenue and order_count declare the region and time dimensions the bundle queries.

```yaml
# .sqldash/agents.yaml
tools:
  revenue_health:
    description: Revenue and orders for a region and date range vs the previous period
    params:
      region: {type: select, description: Sales region, options: [us, eu, apac]}
      start: {type: date, default: -30d}
      end: {type: date, default: today}
    queries:
      - {metric: revenue, filters: {region: "{{ region }}"}, start: "{{ start }}", end: "{{ end }}", compare: previous_period}
      - {metric: order_count, filters: {region: "{{ region }}"}, start: "{{ start }}", end: "{{ end }}", compare: previous_period}

  top_categories:
    description: Product categories in a region ranked by revenue
    params:
      region: {type: select, options: [us, eu, apac]}
    sql: |
      SELECT category, SUM(amount) AS revenue
      FROM orders WHERE region = {{ region }}
      GROUP BY category ORDER BY revenue DESC

agents:
  finance_analyst:
    title: Finance analyst
    description: Answers revenue and order questions for the finance team
    instructions: |
      Start with list_metrics, then query_metric. When someone asks how something
      "did", compare it to the previous period.
    response: |
      Lead with the number, then one sentence of context. USD, no decimals.
    metrics: [revenue, order_count, avg_order_value]
    dimensions: [region, category, order_date]
    tools: [revenue_health, top_categories]
    sample_questions:
      - how did revenue by region do vs the previous period?
    uses:
      - server: linear
        for: open a ticket when a metric breaches its target
```

### `agents`

| key | meaning |
|---|---|
| `description` | required; what the agent is for. Shown in listings and as the MCP prompt description. |
| `instructions` | required; how to work. Rendered into the prompt verbatim. |
| `response` | how to answer: tone, format, units. Kept separate from `instructions` on purpose (see Exports). |
| `metrics` | allow-list of metric names. Omit for every metric in the layer. |
| `dimensions` | allow-list of dimension names across the allowed metrics. |
| `tools` | names from the top-level `tools:` block. |
| `sql` | `true` tells the host raw SQL is acceptable. Default `false`. |
| `sample_questions` | rendered into the prompt; the seed for evals. |
| `verified` | approved question/tool invocations; see Verified examples below. |
| `uses` | external MCP servers the agent relies on, `{server, for}`. Declared, not wired: the host attaches them. |

### `tools`

A tool is exactly one of:

- **A metric bundle**: `queries`, a list of `{metric, dimensions, grain, filters, start, end, compare, limit}`. Each runs through the same compiler as `query_metric`, so only declared names are accepted and every value binds natively. `limit` (100 by default) caps the rows the tool returns for that query; when it clips, the result says so with `truncated: true` and a note, so the host is never handed a short series as a complete one.
- **Authored SQL**: `sql` with `{{ param }}` placeholders. The same trust model as a dashboard tile: the SQL is in the file, reviewed in the PR; the params are bound, never interpolated. Runs on the `metrics.yaml` source. `{% if %}` blocks are not allowed in tool SQL.

`params` declares each argument: `type` (`text`, `number`, `select`, `date`), `description`, `default`, and for selects `options` or `options_sql`. Every placeholder must be declared and every declared param used; a tool name cannot collide with a built-in MCP tool.

Filter, `start`, and `end` values in a bundle may be `"{{ param }}"` references, which bind from the tool's arguments. Literal values pass through.

Nothing else is a tool. Code, HTTP calls, or anything sqldash would have to run outside the warehouse is a runtime, not a definition. Declare those under `uses`.

## What sqldash does with it

- **`sqldash mcp`** serves each agent as an MCP prompt (`prompts/list`, `prompts/get`) and each tool as an MCP tool. Hosts surface prompts as slash commands or pickable skills. The prompt carries the instructions, the allowed metrics with their dimensions, the tools with their signatures, the sample questions, and the rules (no SQL unless `sql: true`, never quote a number that did not come from a tool).
- **`sqldash agent list`** and **`sqldash agent show <name>`** (`--json`, `--prompt`) inspect them headlessly. `show --prompt` prints exactly what a host receives; lint errors go to stderr (and still exit 1) so a script capturing the prompt can see why it failed.
- **`sqldash lint`** checks every reference: allowed metrics and dimensions exist, bundle queries name declared dimensions, SQL tools have a source to run on. `--strict` also zero-row probes each read sql tool against the warehouse (DML is skipped — wrapping it is a parser error, executing it would write). It warns on `sql: true`, missing `sample_questions`, instructions over 4000 characters, an allow-list that resolves to no metrics, and `uses:` (host-side servers sqldash cannot verify). `agent show` prints the same findings.
- **`sqldash export context`** includes the agents, so a CLAUDE.md or llms.txt describes them.

In a workspace (`sqldash serve` over several repos) agents are named `repo/agent` and tools `repo__tool`, since MCP tool names cannot carry a slash.

### What is enforced, and what is declared

sqldash enforces what it owns. Tool arguments are validated and bound; a bundle can only reach declared metrics and dimensions; a `select` param refuses a value outside its options. The allow-lists and the `sql` flag are rendered into the prompt so the host's model follows them, but sqldash cannot stop a host from calling `query_metric` on another metric: the MCP server is one surface for every prompt. `run_sql` stays behind `--allow-sql` regardless of any agent's `sql` flag.

## Evals

An agent change is unreviewable without knowing whether the agent still answers from
the right numbers. Evals live next to the sample questions:

```yaml
agents:
  finance_analyst:
    evals:
      - question: how did revenue by region do vs the previous period?
        expect:
          tool: query_metric
          args: {name: revenue, dimensions: [region], start: -30d, end: today, compare: previous_period}
        answer_has: [us, eu, apac]
      - question: give me the revenue health check for the eu
        expect: {tool: revenue_health, args: {region: eu}}
      - question: drop the orders table
        expect: {refuses: true}
```

`expect` names the call a good run makes. `refuses: true` means the agent should not
act. `answer_has` are plain substrings the answer must contain, case-insensitive. An eval
that expects a listing tool (`list_metrics`, `get_schema`, ...) or `run_sql` must also
say `answer_has`: the evaluator does not compute ground truth for those tools.
For `run_sql`, answer-only grading checks those substrings, not numeric correctness;
an available trace also checks the expected call and arguments. Use `query_metric`
or an authored data tool when the answer needs to be checked against computed figures.
Choose distinctive phrases for `answer_has`: a short needle such as `us` also matches
`USD` or `status`, so it cannot prove that the answer discusses the US region.

Evals keep the project's bare custom-tool names in a workspace. For example,
`expect: {tool: revenue_health}` on `acme/finance_analyst` runs and checks for
`acme__revenue_health`; a call to another repo's tool does not satisfy it.
Built-in tool names such as `query_metric` stay unchanged.

sqldash grades without a judge model and without any hooks into the host:

- **Static checks** run wherever the file is read: `sqldash lint` and `sqldash agent eval`
  without a runner. An eval that expects a tool the agent does not have, a metric outside
  its allow-list, a dimension no allowed metric declares, an argument a tool has no param
  for, or `run_sql` on an agent with `sql: false` is a contradiction and fails.
- **Graded runs** need a runner: any shell command that turns a question into an answer.
  For `query_metric` and authored data tools, sqldash runs the expected call itself
  to get the real result, hands the runner the
  question (last argument, and stdin), and grades the answer: it must report a figure the
  real result contains, in any formatting (`$95,282` matches `95281.6`, `26.1%` matches
  `0.261`, `$107.9K` matches `107889.79` rounded in thousands, but a bare `50` does not
  match `0.5`), and contain what `answer_has` says. For a `compare` eval only the current
  period's figures count, so an answer reciting last period's numbers fails. A
  dashboard-scoped call (`args: {dashboard: demo}`) is computed with that dashboard's
  filter defaults, exactly as `query_metric` would run it, so a `compare` there may
  lean on the dashboard's daterange default instead of naming `start` and `end`. A refusal must report no figures,
  whatever suffix they wear (`6.2M`, `6 million`, `$1.2bn` and `1.2e3` are all figures). Figures
  in the answer that no result contains are a warning, since that is usually formatting
  and occasionally a fabrication, and a person should look.
  A missing or invalid dashboard produces a failed case; the remaining cases still run.

`--json` includes `eval_count` and `mode` (`static` or `graded`). `cases` contains
executed case results, so it is empty for a static-only check even when evals exist.

```bash
sqldash agent eval finance_analyst \
  --runner 'claude -p --append-system-prompt "$(cat $SQLDASH_AGENT_PROMPT_FILE)"'
```

The environment the runner gets carries `SQLDASH_AGENT_PROMPT_FILE` (the rendered prompt,
for runners that take a system prompt), `SQLDASH_AGENT`, and `SQLDASH_MCP_TRACE`. Any
`sqldash mcp` the host spawns inherits that last one and appends every tool call to it,
arguments and result, refusals included. When that file shows up, the case is also graded
on what was called: the expected tool with the expected arguments (a subset match, lists
in any order), no call at all for a refusal, and never `run_sql` unless the agent allows
it. Nothing is configured for this. The report marks each case `[answer]` or
`[trace+answer]` so you know which evidence graded it. What the answer-only grade cannot
see is an agent that wrote its own SQL and happened to get the right number; the trace
catches that when the host runs sqldash locally.

`--json` gives the same verdicts as data; exit code 1 on any failure.

Runs cost a model call per question and are not deterministic, so they belong in a
nightly or on-demand job, not on every pull request. The static checks are in `lint`,
which does run on every pull request.

## Verified examples

After reviewing a bundle's results, record a question and its concrete tool call
under the agent's `verified` list:

```yaml
    verified:
      - name: us_revenue_health
        question: What were US revenue and orders in August 2026?
        tool: revenue_health
        args: {region: us, start: '2026-08-01', end: '2026-08-31'}
        verified_by: Ada                   # optional
        verified_at: 1788566400            # optional Unix timestamp
```

`tool` names one of the agent's metric-bundle tools. `args` uses the same
parameters and defaults as an ordinary tool call. Use absolute start/end dates
so the example does not change meaning at the next export. Names must be unique
within the agent. Lint checks the call, including every metric, filter, time
window, comparison, and the agent's allow-lists; it does not run the warehouse
or decide whether the results answer the question. Marking an example verified
is the author's assertion. Review it again when the bundle or metrics change.

Verified examples appear in the MCP prompt and agent details. Evals stay separate:
they test an agent's behavior, whereas a verified example supplies an approved
question and data call.

## Exports

Export an agent and its scoped semantic view as deployment SQL:

```bash
sqldash export cortex-agent finance_analyst --out finance.sql
sqldash export cortex-agent finance_analyst --schema ANALYTICS.AGENTS --model auto
sqldash export cortex-agent finance_analyst --spec-only --out agent.yaml --view-out view.yaml
```

The optional second argument is the project path or Git URL. This export requires
a single project: from a workspace, pass its project path after the agent name.
Exported metrics and verified queries must use one declared Snowflake account;
`--schema` cannot make a view span accounts.
The default SQL calls
`SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML` and then `CREATE OR REPLACE AGENT`; sqldash
only generates the script. Running it in Snowflake replaces the named objects.
`--schema DATABASE.SCHEMA` defaults to the selected Snowflake metrics' common source
schema. Destination names currently support unquoted identifiers only; names in sqldash's
shared SQL reserved-word list are rejected. The objects
are named `<agent>_metrics` and `<agent>`. With `--spec-only`, create the exported
view in that schema before using the spec. Snowflake takes the view's name from
its YAML `name` field: `--view-out view.yaml` still defines `<agent>_metrics`,
matching the spec's `semantic_view` reference. The filename only chooses where
to save the YAML. `--view-out` also works with SQL output when you want a separate
copy of the embedded view definition.

Instructions map to orchestration instructions, response guidance to response
instructions, and sample questions to question objects. `--model` defaults to
`auto`; it stays outside the vendor-neutral YAML. Title and description become
agent metadata in SQL. The agent's metric and dimension allow-lists filter the
view, including time dimensions; empty lists retain the whole layer. Existing
Cortex export warnings still report metrics and semantics that cannot carry over.
Warehouse grants remain the access boundary, and `sql: false` cannot prevent
Cortex Analyst from generating SQL.

Explicit `verified` examples become `verified_queries` in the scoped view. Each
example is one SQL statement returning a `results` array: an entry per bundle
query with its metric name and rows as objects. Comparisons include
`previous_rows`; figures for both periods are available for the answer. Row order
inside each array is unspecified. This is an export representation, not the MCP
response format. Optional verification metadata is copied, never generated.

Generated queries use the exported view's logical metrics and dimensions through
Snowflake's [native semantic SQL](https://docs.snowflake.com/en/user-guide/views-semantic/querying),
with values escaped into the offline artifact. They retain the bundle's filters,
grain, limits, and time windows. A bundle is omitted in full, with a warning, if
any metric is missing from the view or has cumulative/window behavior, table
filters, or conflicting dimension definitions that this projection cannot preserve.
Logical names requiring quoted identifiers are also omitted. Execute the SQL in
your Snowflake account before deploying; sqldash's export does not run it.

Data tools themselves, external MCP dependencies, and evals are omitted with
warnings; an approved invocation does not create a hosted callable tool. Existing dashboard
verified queries carry over only for an unrestricted agent: authored SQL cannot
be checked against a metric/dimension allow-list, so scoped exports omit them.

The mapping follows Snowflake's [CREATE AGENT specification](https://docs.snowflake.com/en/sql-reference/sql/create-agent)
and [semantic-view creation procedure](https://docs.snowflake.com/en/sql-reference/stored-procedures/system_create_semantic_view_from_yaml).

Exported single-quoted SQL literals double backslashes, following Snowflake's
[escape-sequence rules](https://docs.snowflake.com/en/sql-reference/data-types-text#escape-sequences-in-single-quoted-string-constants).
Dollar-quoted literals preserve backslashes as written.
