# Security policy

## Reporting a vulnerability

Please don't open a public issue for a security problem. Report it privately
through GitHub instead: open the repository's **Security** tab and click
**Report a vulnerability**
([direct link](https://github.com/dylan-murray/sqldash/security/advisories/new)).
Only the maintainer sees the report, and the fix and advisory get worked out in
that same private thread.

A useful report says what you did, what happened, and what you expected: the
command or request, the dashboard or semantic-layer YAML involved (with
credentials removed), and the sqldash version (`sqldash --version`).

sqldash is maintained by one person, so responses are best effort. You should
hear back within a week. Please give a reasonable window for a fix before
disclosing publicly; we'll agree on a date together in the advisory.

## Supported versions

sqldash is pre-1.0. Security fixes land on `main` and ship in the next release;
only the latest release is supported.

## What counts

Things we especially want to hear about:

- SQL reaching a database through a path that should only accept metric,
  dimension or filter names and values (the semantic layer, the HTTP API, MCP).
- Credentials or secrets leaking into files, logs, API responses or the browser.
- `sqldash serve` exposing more than intended, for example cross-origin access
  to a local server, or file reads or writes outside the project.
- Anything in the GitHub Actions workflows that lets a pull request, issue or
  comment from outside the project reach secrets or write access.

Running `sqldash serve --host 0.0.0.0` on an untrusted network, or connecting
it to a database you don't control, is outside what sqldash defends against.
