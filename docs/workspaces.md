# Where dashboards live

Dashboards are plain files, so they can live anywhere git reaches.

- **Inside any repo.** `sqldash init` creates a `.sqldash/` folder. Commit it next to
  the service or pipeline it measures, and its dashboards ride along with the code.
- **A central analytics repo.** One repo, many dashboards, if that's how your team
  works.
- **A single file.** `sqldash serve revenue.yaml` is a complete deployment.
- **All of them at once.** Register the repos you care about and serve the whole
  workspace, grouped by repo.

## The workspace registry

```bash
sqldash repo add git@github.com:acme/dashboards.git
sqldash repo add ~/work/data-platform        # local checkouts work too
sqldash repo list
sqldash repo remove data-platform
sqldash serve                                # serves every registered repo
sqldash serve ~/work/data-platform           # or still just one
```

The registry lives next to your credential profiles (per-user, never committed). In a
workspace, dashboards are namespaced as `repo/dashboard`, so the same dashboard name
can exist in two repos.

## Serving from a remote

```bash
# clone it yourself, serve the checkout
git clone git@github.com:acme/payments-service.git && cd payments-service && sqldash serve

# or let sqldash manage the clone; it pulls on every start
sqldash serve git@github.com:acme/payments-service.git
```

The second form clones into a local cache (the path is printed on startup) and
fast-forward pulls each time you serve. Edits made in the UI land in that clone.
Commit and push from there to share them. Everyone connects with their own
credentials, and the repo never contains secrets.

## Wallboards and snapshots

`sqldash snapshot` renders every dashboard to a static PNG plus a self-contained
`index.html` gallery. It is the path for stakeholders and TVs with no warehouse
credentials, without inventing app auth:

```bash
pip install 'sqldash[snapshot]' && playwright install chromium
sqldash snapshot . -o snapshots            # or a git URL, or --all for the workspace
```

Commit the output, drop it in a bucket, or cron it. The renders are just files.
Whoever runs the snapshot supplies the credentials; viewers need none.

A dashboard that fails to load is never dropped silently. The rest still render,
the gallery shows a "failed to load" card in its place, the reason goes to
stderr, and the command exits 1 so a cron job or CI notices. If no dashboard
loads, nothing is written (a previous gallery in the output directory is left
alone) and the command exits 1.
