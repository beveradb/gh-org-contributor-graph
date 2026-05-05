# gh-org-contributor-graph

Visualize contributor activity across all repos in a GitHub org over time.

For each repo in an org, the fetcher maintains a bare clone, runs
`git log --numstat`, and aggregates per-(repo, author) weekly buckets of
commits / additions / deletions into a single JSON file. A static HTML viewer
(Phase 2) renders it with interactive filters.

Generic — works on any org. Auth is via the GitHub CLI's existing token.

## Status

- [x] **Phase 1** — `fetch.py` data fetcher (local-clone based)
- [x] **Phase 2** — `viewer.html` interactive viewer
- [x] **Phase 3** — event-level activity scatter, response cache, layout polish
- [ ] **Phase 4** — drill-down panels, CSV export, repo grouping by topic/language

## Quickstart

```bash
gh auth login                            # if not already signed in
cp aliases.example.json aliases.json     # optional: edit to merge identities
python3 fetch.py --org <ORG_LOGIN>       # writes contributors.json
python3 -m http.server 8765              # serve next to contributors.json
open http://localhost:8765/viewer.html   # auto-loads contributors.json
```

`viewer.html` also accepts a manual file pick when opened via `file://`.

### Sharing as a single file

To send a coworker a self-contained version (data baked in, no
fetcher / server / repo access required on their end):

```bash
python3 bundle.py                  # writes dist/<org>-contributors.html
python3 bundle.py --inline-plotly  # +Plotly.js inline, fully offline
```

The output is one HTML file (~1 MB for an org our size; ~5 MB with
Plotly inlined) that opens directly via `file://`. Email / AirDrop /
Slack it; recipient double-clicks.

**Privacy reminder:** the bundle includes everything in
`contributors.json` — author emails, private repo names, etc. Only
share with people authorized to see all of it.

## Phase 1: `fetch.py`

```
python3 fetch.py --org <ORG_LOGIN> [options]
```

| Flag | Default | Behavior |
|------|---------|----------|
| `--org <name>` | required | GitHub org login |
| `--output <path>` | `contributors.json` | output JSON |
| `--cache-dir <path>` | `repos` | bare-clone cache dir |
| `--aliases <path>` | `aliases.json` | identity-merge map (skipped if file missing) |
| `--include-forks` | off | include repos forked from outside the org |
| `--exclude-archived` | off | skip archived repos |
| `--no-merges` | off | exclude merge commits from counts |
| `--only <name>` | (all) | only process named repos (repeatable) |
| `--top-extensions <n>` | 15 | file extensions tracked literally per repo (rest in `_other`) |
| `--no-prs-issues` | off | skip PR + issue API calls |
| `--max-age <sec>` | 3600 | cache TTL for the org repos list and per-repo PR/issue API |
| `--refresh` | off | ignore cache and re-fetch all API data |
| `--no-git-fetch` | off | reuse existing bare clones without `git fetch` |

### Why local clones?

GitHub's `/stats/contributors` endpoint computes asynchronously and is
unreliable for large active repos — `202 Accepted` responses can persist
for >5 minutes per repo. Local clones are deterministic, give exact line
counts, and a typical org with ~10 repos finishes in seconds. Trade-off:
disk space for the bare-clone cache (a few hundred MB for a busy org).

### Caching

API responses (org repo list, per-repo PRs, per-repo issues) are cached
to `cache/<org>/...` for `--max-age` seconds (1 hour by default). On a
warm cache the whole pipeline runs in seconds rather than ~minute, which
matters when iterating on the viewer. Invalidate explicitly with
`--refresh` (or set `--max-age 0`). Bare clones in `repos/` are also
reused; pass `--no-git-fetch` to skip the `git fetch` step entirely.

### Identity merging (`aliases.json`)

Same person, multiple emails / accounts. The fetcher already extracts
GitHub logins from `<id>+<login>@users.noreply.github.com` automatically.
For everything else, drop a JSON file mapping any email / login / name
(case-insensitive) to a canonical id:

```json
{
  "alice@personal.com": "alice-gh",
  "alice@work.com": "alice-gh",
  "ci@example.com": "ci-bot"
}
```

To find an unknown identity's GitHub login:

```bash
git -C repos/<repo>.git log --author=<email> --pretty=format:'%H' --max-count=1 \
  | xargs -I{} gh api repos/<owner>/<repo>/commits/{} --jq .author.login
```

`aliases.json` is gitignored — it's per-user data. See
`aliases.example.json` for the schema.

### Output JSON

```json
{
  "org": "...",
  "fetched_at": "ISO-8601",
  "method": "local-clone",
  "filters": {"include_forks": false, "include_archived": true,
              "no_merges": false, "only": null},
  "repos": [{"name", "full_name", "created_at", "pushed_at", "archived",
             "fork", "private", "default_branch", "language", "topics",
             "html_url"}],
  "stats": [{
    "repo": "<repo-name>",
    "author": {"name", "email", "login", "aliased",
               "names": [...], "emails": [...]},
    "total": <int commits>,
    "weeks": [{"w": <unix sunday>, "c": commits, "a": additions, "d": deletions}]
  }]
}
```

Weeks are Sunday-anchored UTC (matching GitHub's `/stats/contributors`
convention). `aliased: true` indicates the row was merged from multiple
git identities via `aliases.json`.

## Auth

Uses `gh auth token`. Install [GitHub CLI](https://cli.github.com/) and run
`gh auth login`. The token needs `repo` scope to read private org repos.

## Phase 2: `viewer.html`

Single static file. No build step. Plotly via CDN. Auto-loads
`contributors.json` if served over http(s); offers a file picker on
`file://`.

Layout:

- Combined chart (selected repos summed) at the top.
- One smaller chart per repo below, ordered by activity in the window.

Controls:

- **Date range** — date pickers + quick buttons (All / 3y / 1y / 6m / 3m / 1m).
- **Contributors** — multi-select ranked by total commits, with
  `Exclude bots` toggle (regex covers `*[bot]`, `dependabot`, `renovate`,
  `coderabbit`, `github-actions`, `copilot`, `aquafix`, etc.).
- **Metrics** — multi-toggle: commits, additions, deletions, net lines,
  PRs opened, PRs merged, issues opened. Each enabled metric renders as
  its own subplot row (independent y-axis), so commit count and line
  count don't fight for the same scale.
- **File extensions** — multi-select; applies only to line metrics
  (`additions` / `deletions` / `net lines`). Selecting only `.md`, for
  example, surfaces docs activity vs code activity.
- **Granularity** — weekly / monthly / quarterly.
- **Mode** — stacked area / line / cumulative line.
- **Repos** — multi-select with `Active only` shortcut (excludes
  archived + zero-activity).

Hover gives author + metric + date. Each contributor gets a stable
color, shared across the combined chart and every per-repo chart.

### Activity timeline (event scatter)

Below the combined chart is an event-level scatter rendering every
commit / PR open / PR merge / issue open as a single marker, colored
by repo. Y axis switches between contributor and repo via the toggle
above the chart; event types can be hidden individually. Marker size
on commits scales with lines changed. Visualizes activity clusters
over time at a glance.

Per-repo charts are tucked into a collapsible `<details>` below the
scatter, so the page leads with the two big-picture views and the
small-multiples are one click away.

## License

MIT
