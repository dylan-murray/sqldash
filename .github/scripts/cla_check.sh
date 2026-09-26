#!/usr/bin/env bash
# Passes when everyone who authored the PR has a row in CLA.md's signature
# table and the PR changed nothing else in CLA.md.
#
# "Everyone" is the PR opener plus the GitHub login behind every commit
# currently on the PR, so a maintainer reopening a contributor's branch does
# not carry unsigned commits through. Only rows after the table header count: a matching line in the
# legal text or a code block is not a signature. The diff against the base may
# add rows and nothing else, so the terms cannot be edited in the same PR that
# signs them. That rule applies only when a non-exempt author is involved: the
# maintainer's own PRs are how the terms (and this file) change at all.
#
# The diff runs against the merge commit's first parent when there is one, so
# a base that advanced after the event does not show up as the PR's change;
# BASE_SHA is the fallback for a plain checkout.
#
# Env: AUTHOR (PR opener), BASE_SHA (the PR's base commit), and either AUTHORS
# (space-separated logins, for local runs) or PR_NUMBER + GH_REPO + GH_TOKEN
# to look the commit authors up. Run from the repo root on the PR's merge
# commit.
set -euo pipefail

exempt() {
  case "$1" in
    dylan-murray|dependabot\[bot\]|github-actions\[bot\]) return 0 ;;
    *) return 1 ;;
  esac
}

if [ -z "${AUTHORS:-}" ]; then
  AUTHORS="$AUTHOR"
  if [ -n "${PR_NUMBER:-}" ]; then
    commits=$(gh api "repos/$GH_REPO/pulls/$PR_NUMBER/commits" --paginate \
      --jq '.[] | "\(.author.login // "-")\t\(.sha[:7])\t\(.commit.author.email)"')
    while IFS=$'\t' read -r login sha email; do
      [ -z "$sha" ] && continue
      if [ "$login" = "-" ]; then
        echo "::error::commit $sha is by $email, which is not linked to a GitHub account, so its author cannot be checked against CLA.md. Link the email or reauthor the commit."
        exit 1
      fi
      AUTHORS="$AUTHORS $login"
    done <<<"$commits"
  fi
fi

table=$(awk '/^\| *GitHub username *\| *Name *\| *Date *\|/{found=1; next} found' CLA.md)
checked=""
for login in $(tr ' ' '\n' <<<"$AUTHORS" | sort -u); do
  exempt "$login" && continue
  esc=$(printf '%s' "$login" | sed 's/[][\.*^$/]/\\&/g')
  if ! grep -qiE '^\| *@'"$esc"' *\|[^|]*\|[^|]*\|[[:space:]]*$' <<<"$table"; then
    echo "::error file=CLA.md::@$login has not signed the CLA. Add one line to the signature table at the bottom of CLA.md in this pull request."
    exit 1
  fi
  checked="$checked @$login"
done

if [ -z "$checked" ]; then
  echo "every author is the maintainer or a bot; no signature needed and CLA.md may change"
  exit 0
fi

if git rev-parse -q --verify HEAD^2 >/dev/null 2>&1; then
  diff_base=HEAD^1
else
  diff_base=$BASE_SHA
fi
anyrow='^\| *@[A-Za-z0-9._-]+ *\|[^|]*\|[^|]*\|[[:space:]]*$'
bad=$(git diff -U0 "$diff_base" HEAD -- CLA.md | grep -E '^[+-]' | grep -vE '^(\+\+\+|---)' \
  | grep -vE '^\+'"${anyrow#^}" || true)
if [ -n "$bad" ]; then
  echo "::error file=CLA.md::CLA.md changed beyond adding a signature row. Only add your line to the table; the terms are edited separately."
  echo "$bad"
  exit 1
fi

echo "signed:$checked; the PR changes nothing else in CLA.md"
