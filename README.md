# gh-org-contributor-graph

Visualize contributor activity across all repos in a GitHub org over time.

For each repo in an org, the fetcher maintains a bare clone, runs
`git log --numstat`, and aggregates per-(repo, author) weekly buckets of
commits / additions / deletions into a single JSON file. A static HTML viewer
(Phase 2) renders it with interactive filters.

Generic — works on any org. Auth is via the GitHub CLI's existing token.

## Status

- [x] **Phase 1** — `fetch.py` data fetcher (local-clone based)
- [ ] **Phase 2** — `viewer.html` interactive viewer
- [ ] **Phase 3** — polish (drill-down, exports, grouping)

## Quickstart

```bash
gh auth login                            # if not already signed in
cp aliases.example.json aliases.json     # optional: edit to merge identities
python3 fetch.py --org <ORG_LOGIN>       # writes contributors.json
# (Phase 2) open viewer.html in a browser
```

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

### Why local clones?

GitHub's `/stats/contributors` endpoint computes asynchronously and is
unreliable for large active repos — `202 Accepted` responses can persist
for >5 minutes per repo. Local clones are deterministic, give exact line
counts, and a typical org with ~10 repos finishes in seconds. Trade-off:
disk space for the bare-clone cache (a few hundred MB for a busy org).

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

## Phase 2 (planned)

Single-file `viewer.html` that loads `contributors.json` and renders
Plotly charts with:

- Date range slider, repo multi-select, contributor multi-select / "top N"
- Metric: commits / additions / deletions / net lines
- Granularity: weekly / monthly / quarterly
- Mode: stacked area / line / cumulative / heatmap
- Bot exclusion toggle
- Hover detail and click-to-filter

## License

MIT
