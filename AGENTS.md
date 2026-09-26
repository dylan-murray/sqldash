# AGENTS.md

sqldash: single-file SQL dashboards — pure Python, no cloud, no build step. Dashboards
are YAML files; a FastAPI server renders and edits them; a semantic layer serves
governed metrics to humans (tiles) and agents (MCP).

## Local instructions

If an `AGENTS.local.md` file exists at the repo root, read it before working and follow it.

## Shipping work

**Size the PR to the review, not to the issue.** A small issue is one PR onto
`main`. Do not bundle several small issues into one PR, however cleanly the
commits are separated inside it, and do not chain independent fixes onto each
other either: a stack couples PRs that have nothing to do with each other, so
a rebase in one forces a restack of all of them and a merge has to wait for
its neighbours. Two small fixes that happen to touch the same file are not a
stack; rebase the second after the first merges.

The reason is merge granularity, not tidiness. A security fix should not wait on
an unrelated UI fix, and a bundled PR forces one all-or-nothing decision across
changes with different risk profiles — and one all-or-nothing revert later.
Name branches after the change (`fix/redact-secrets`). PR bodies do not link or
close issues. Everything else — a refactor, a doc
change, a follow-up that isn't an issue — follows the same instinct: one
reviewable idea per PR, onto `main`.

**A large issue becomes a PR stack.** When one issue or feature needs more code
than a reviewer can hold in one PR, break it into dependent pieces: the first
PR onto `main`, the next onto that branch, and so on, ordered by what should
land soonest, each piece reviewable on its own. That is the case the rest of
this section is for. GitHub retargets the next PR to main only if the base
branch is deleted after merge — stacking onto a leftover `feat/foo` leaves the
work off main. Open a PR
from that branch to main in the same pass, or retarget. Merging a stacked PR
into its leftover base is a dead end.

**The stack has to exist on GitHub, not just in your local branches.** Push the
branches and open every PR in the same pass — a branch with no PR is invisible,
and half a stack is worse than none because the parts that are up look like they
stand alone. Concretely:

- Open a PR for **every** branch in the stack, right away, not just the bottom
  one.
- Set each PR's **base to the branch below it**, so GitHub's own diff shows only
  that PR's changes and retargets to `main` automatically as the stack merges.
- Put a **stack map in every PR body** — all the PRs in order, linked, with the
  current one marked. Without it a reviewer landing in the middle of a stack
  cannot see what it sits on or what is waiting behind it.
- When you restack after a rebase, force-push **all** the branches — with
  `--force-with-lease`, never a bare `--force`, so a push that would discard
  commits you have not seen fails instead of succeeding quietly. Then check the
  bases are still what you intended: GitHub silently retargets a PR when its
  base branch is deleted.
- **After any restack, confirm every PR is still mergeable and that its checks
  actually started.** GitHub does not run PR CI on a PR whose base has moved
  under it, and an unmergeable PR reports *no checks at all* — which reads
  exactly like "the checks have not started yet", not like a problem. Rebase
  each branch onto the one below, in order from the bottom, until
  `gh pr view <n> --json mergeable` says `MERGEABLE` for all of them.

**What "the stack exists on GitHub" can and cannot mean for an agent.** GitHub
now has a native stacked-PR object, and it offers to build one ("This pull
request can be stacked… Preview stack") as soon as the bases chain. That object
is **read-only over the API**: `PullRequest.stack` and `stackEntry` can be
queried, `Mutation` exposes nothing to create one, and `gh` has no stack
command (checked against gh 2.87, 2026-08-12). So an agent's job is everything
that makes the offer appear — chained bases, a PR per branch, the stack map,
all of them mergeable — and then to **say the banner is there**, because
clicking it is the one part only a human can do. Do not report a stack as
"created in GitHub" on the strength of base branches alone.

## Working in this repo

- Python ≥3.11, managed with `uv`. Run things with `uv run ...`.
- Tests: `uv run pytest -q` (or `make test`). Lint: `uv run ruff check sqldash tests`
  (or `make lint`). Both must pass before you're done.
- Before a PR merges, run `scripts/ci_local.sh <PR>`: it reproduces the GitHub CI jobs
  locally against the PR merged into main.
- Frontend is no-build by design: plain ES modules in `sqldash/static/js/`, vendored
  third-party assets in `sqldash/static/vendor/` (pinned in `VERSIONS.md`,
  refreshed via `make vendor-update`). Never introduce npm/node/bundlers.
- JS sanity check: `node --check sqldash/static/js/<file>.js`.
- Verify changes by exercising the real thing: `uv run sqldash init --demo /tmp/demo &&
  uv run sqldash serve /tmp/demo`, drive the flow, and for file-writing features
  inspect the actual YAML diff in a git-tracked scratch project.
- The package lives at the repo top level (`sqldash/`), not under `src/`.
- Style: ruff, line length 100, pydantic v2 models with `ConfigDict(extra="forbid")`,
  no code comments unless a constraint can't be expressed in code.
- **Imports go at the top of the file.** `PLC0415` enforces this. Two exceptions,
  both deliberate: an optional extra (`snowflake`, `lkml`, `playwright`) must be
  imported inside the function that needs it, with a `# noqa: PLC0415` naming the
  extra; and `cli.py` is exempt wholesale because importing the server stack at
  module scope makes every `sqldash --help` pay for it (156ms → 502ms). Don't
  copy the `cli.py` pattern into other modules, and don't "tidy" its lazy imports
  to the top — measure first.
