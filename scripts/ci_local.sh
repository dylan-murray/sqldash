#!/usr/bin/env bash
# Runs the jobs in .github/workflows/tests.yml on this machine, so a PR can be checked
# without GitHub Actions minutes. It tests what CI tests: the PR head merged into
# the current origin/main, in a throwaway worktree, never the checkout you are in.
#
#   scripts/ci_local.sh                 # the current branch, merged into origin/main
#   scripts/ci_local.sh 742             # PR #742
#   scripts/ci_local.sh 742 --quick     # lint, Python 3.12 (the job that runs browsers), no extras
#   scripts/ci_local.sh 742 --report    # also post a "local-ci" commit status on the PR
#   scripts/ci_local.sh 742 --trust-fork  # run a PR from a fork, after reading its diff
#
# Jobs run one at a time to keep memory down. Venvs are cached in
# ~/.cache/sqldash-ci-local so later runs only sync what changed. Needs uv, node, docker (for the MySQL
# service CI starts) and Postgres binaries (initdb/pg_ctl) on PATH or in Homebrew.
#
# A PR's code runs here unsandboxed, as you, next to your credentials. So a PR
# from a fork (anyone on the internet) is refused unless --trust-fork is given.
set -uo pipefail

repo="$(git rev-parse --show-toplevel)"
target=""
quick=0
report=0
trust_fork=0
for arg in "$@"; do
  case "$arg" in
    --quick) quick=1 ;;
    --report) report=1 ;;
    --trust-fork) trust_fork=1 ;;
    -h|--help) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) target="$arg" ;;
  esac
done

if [[ "$target" =~ ^[0-9]+$ ]] && [ "$trust_fork" = 0 ]; then
  cross="$(gh pr view "$target" --json isCrossRepository --jq .isCrossRepository 2>/dev/null || true)"
  if [ "$cross" != false ]; then
    echo "PR #$target is from a fork, or its origin could not be checked (got '${cross:-nothing}')."
    echo "Its code would run on this machine with your credentials. Read the diff, then rerun with --trust-fork."
    exit 3
  fi
fi

pythons=("3.11" "3.12" "3.13" "3.14")
[ "$quick" = 1 ] && pythons=("3.12")

work="$(mktemp -d "${TMPDIR:-/tmp}/sqldash-ci-local.XXXXXX")"
tree="$work/tree"
logs="$work/logs"
mkdir -p "$logs"
mysql_name="sqldash-ci-mysql-$$"
venvs="${XDG_CACHE_HOME:-$HOME/.cache}/sqldash-ci-local"
mkdir -p "$venvs"

lock="$venvs/.lock"
if ! mkdir "$lock" 2>/dev/null; then
  echo "another ci_local.sh run holds $lock (pid $(cat "$lock/pid" 2>/dev/null || echo unknown));"
  echo "runs share the cached venvs, so they must not overlap. Remove $lock if that run is gone."
  exit 2
fi
echo $$ >"$lock/pid"

cleanup() {
  docker rm -f "$mysql_name" >/dev/null 2>&1 || true
  git -C "$repo" worktree remove --force "$tree" >/dev/null 2>&1 || true
  if [ "${keep_logs:-0}" = 1 ]; then rm -rf "$work/dist-"*; else rm -rf "$work"; fi
  rm -rf "$lock"
}
trap cleanup EXIT

git -C "$repo" fetch -q origin main
if [ -z "$target" ]; then
  head_ref="$(git -C "$repo" rev-parse HEAD)"
  label="$(git -C "$repo" branch --show-current || echo HEAD)"
elif [[ "$target" =~ ^[0-9]+$ ]]; then
  git -C "$repo" fetch -q origin "pull/$target/head"
  head_ref="$(git -C "$repo" rev-parse FETCH_HEAD)"
  label="PR #$target"
else
  git -C "$repo" fetch -q origin "$target" 2>/dev/null || true
  head_ref="$(git -C "$repo" rev-parse "origin/$target" 2>/dev/null || git -C "$repo" rev-parse "$target")"
  label="$target"
fi

git -C "$repo" worktree add -q --detach "$tree" origin/main
if ! git -C "$tree" -c user.name=ci-local -c user.email=ci-local@localhost \
    merge -q --no-edit "$head_ref" >"$logs/merge.log" 2>&1; then
  echo "FAIL: $label does not merge cleanly into origin/main"
  cat "$logs/merge.log"
  exit 1
fi
echo "testing $label ($(git -C "$repo" rev-parse --short "$head_ref")) merged into origin/main ($(git -C "$repo" rev-parse --short origin/main))"
echo "logs: $logs"

pg_bin="$(ls -d /opt/homebrew/opt/postgresql@*/bin /usr/local/opt/postgresql@*/bin /usr/lib/postgresql/*/bin 2>/dev/null | tail -1)"
[ -n "$pg_bin" ] && export PATH="$pg_bin:$PATH"
command -v initdb >/dev/null || echo "warn: no initdb on PATH, postgres tests will skip"

mysql_port=""
if docker info >/dev/null 2>&1; then
  if docker run -d --name "$mysql_name" -e MYSQL_ROOT_PASSWORD=root -e MYSQL_DATABASE=sqldash_test \
      -p 127.0.0.1::3306 mysql:8 >/dev/null 2>&1; then
    mysql_port="$(docker port "$mysql_name" 3306/tcp | head -1 | sed 's/.*://')"
    for _ in $(seq 1 60); do
      docker exec "$mysql_name" mysqladmin ping -proot --silent >/dev/null 2>&1 && break
      sleep 2
    done
  fi
fi
[ -z "$mysql_port" ] && echo "warn: no MySQL service (docker unavailable), mysql tests will skip"

results=()
failed=0
run() {
  local name="$1"; shift
  local log="$logs/$(echo "$name" | tr ' /(),' '_____').log"
  local started=$SECONDS
  if (cd "$tree" && "$@") >"$log" 2>&1; then
    local flaky
    flaky="$(sed -n 's/^FLAKY: //p' "$log" | tr '\n' ' ')"
    results+=("pass  $name ($((SECONDS - started))s)${flaky:+  flaky, passed on rerun: $flaky}")
  else
    results+=("FAIL  $name ($((SECONDS - started))s)  -> $log")
    failed=1
  fi
  echo "${results[${#results[@]}-1]}"
}

# A failed pytest run is retried once on only the failed tests. Tests that pass on
# the retry are printed as FLAKY so the summary names them; a second failure fails.
pytest_with_rerun() {
  local mysql_env=()
  [ "${1:-}" = mysql ] && [ -n "$mysql_port" ] && mysql_env=(SQLDASH_TEST_MYSQL=1 MYSQL_HOST=127.0.0.1 "MYSQL_PORT=$mysql_port" MYSQL_PASSWORD=root)
  rm -rf .pytest_cache
  if env ${mysql_env[@]+"${mysql_env[@]}"} uv run pytest -q -rs; then
    return 0
  fi
  local failed
  failed="$(uv run python -c 'import json; print(" ".join(json.load(open(".pytest_cache/v/cache/lastfailed"))))' 2>/dev/null)"
  [ -n "$failed" ] || return 1
  echo "retrying failed tests once: $failed"
  env ${mysql_env[@]+"${mysql_env[@]}"} uv run pytest -q -rs --lf || return 1
  echo "FLAKY: $failed"
}

lint() {
  export UV_PROJECT_ENVIRONMENT="$venvs/noextras"
  unset VIRTUAL_ENV
  uv sync -q &&
    uv run ruff check sqldash tests &&
    uv run ruff format --check sqldash tests
}

tests_locked() {
  local py="$1" env="$venvs/py$1"
  export UV_PROJECT_ENVIRONMENT="$env"
  unset VIRTUAL_ENV
  uv sync -q --all-extras --python "$py" &&
    { [ "$py" != 3.12 ] || uv run playwright install chromium >/dev/null; } &&
    node --test tests/*.mjs &&
    pytest_with_rerun mysql &&
    uv run sqldash lint examples --strict &&
    uv build -q -o "$work/dist-py$py"
}

tests_no_extras() {
  export UV_PROJECT_ENVIRONMENT="$venvs/noextras"
  unset VIRTUAL_ENV
  uv sync -q && pytest_with_rerun
}

install_check() {
  unset VIRTUAL_ENV UV_PROJECT_ENVIRONMENT
  ./scripts/check_install.sh
}

# Every test job needs lint, as in tests.yml: a lint failure skips them.
run "lint" lint
if [ "$failed" = 1 ]; then
  results+=("skip  every test job, because lint failed")
  echo "${results[${#results[@]}-1]}"
else
  for py in "${pythons[@]}"; do
    run "test ($py)" tests_locked "$py"
  done
  run "test (no extras)" tests_no_extras
  [ "$quick" = 1 ] || run "test (pip install, fresh deps)" install_check
fi

echo
printf '%s\n' "${results[@]}"
if [ "$failed" = 0 ]; then state=success; summary="all ${#results[@]} jobs passed"; else state=failure; summary="a job failed"; fi
[ "$quick" = 1 ] && summary="$summary (quick: lint, 3.12, no extras)"
echo "$label: $summary"
[ "$failed" = 1 ] && keep_logs=1 && echo "logs kept for the failure: $logs"

if [ "$report" = 1 ]; then
  if [[ "$target" =~ ^[0-9]+$ ]]; then
    gh api -X POST "repos/{owner}/{repo}/statuses/$head_ref" \
      -f state="$state" -f context="local-ci" -f description="$summary" >/dev/null &&
      echo "posted local-ci=$state on $(git -C "$repo" rev-parse --short "$head_ref")"
  else
    echo "--report needs a PR number"
  fi
fi
exit "$failed"
