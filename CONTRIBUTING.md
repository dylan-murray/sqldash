# Contributing

Thanks for helping build sqldash. The bar here is simple: small, verified changes
that keep the project's promises — pure Python, no cloud, no build step.

## Setup

```bash
uv sync --all-extras
make test      # pytest (includes browser smoke tests if chromium is installed)
make lint      # ruff check
make fmt       # ruff format
```

All three must pass before a PR. CI additionally runs the suite on Python
3.11–3.13 with a live MySQL and headless chromium.

## Ground rules

- **No JS toolchain, ever.** The frontend is plain ES modules in
  `sqldash/static/js/` with vendored third-party assets in
  `sqldash/static/vendor/` (pinned in `VERSIONS.md`, refreshed via
  `make vendor-update`). Do not introduce npm, node, or bundlers.
  Sanity-check edited JS with `node --check`.
- **Secrets never in files.** Credentials come from `${env:VAR}` references or
  named profiles in the per-user config dir — never from tracked files. The
  semantic-layer compiler is the SQL-injection boundary: callers pass metric and
  dimension *names* and filter *values*, never SQL.
- **No code comments** unless a constraint genuinely cannot be expressed in
  code. Docstrings cover the *why* and the invariants; keep them short.
- **Verify the real thing.** `sqldash init --demo /tmp/demo && sqldash serve /tmp/demo`
  and drive the flow you changed. For file-writing features, inspect the actual
  YAML diff in a git-tracked scratch project. A UI bug fix should add a case to
  `tests/test_browser_smoke.py`.
- Style: ruff (lint + format), line length 100, pydantic v2 models with
  `ConfigDict(extra="forbid")`.

## For AI agents

`AGENTS.md` is the orientation for AI agents working in this repo.

## Contributor License Agreement

sqldash is Apache-2.0. The project also keeps the right to offer it under other
terms, which only works if one party holds the rights to the whole codebase. So before a first pull
request can merge, add yourself to the signature list at the bottom of
[CLA.md](CLA.md) in that same PR. That is the whole process: no external service,
no email. The `CLA` check on the PR looks for the GitHub username of everyone who
authored a commit currently on it in the signature table, and fails if the PR changes
anything else in CLA.md.

## Reporting issues

Include the dashboard YAML (redacted), the command you ran, and the full error.
`sqldash lint` output is usually the fastest path to a diagnosis.

Security problems go through GitHub's private vulnerability reporting instead
of a public issue; see [SECURITY.md](SECURITY.md).
