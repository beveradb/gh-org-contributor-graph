# gh-org-contributor-graph

Visualize contributor activity across all repos in a GitHub org over time.

For each repo in an org, the fetcher maintains a bare clone, runs
`git log --numstat`, hits the GitHub API for PRs and issues, and writes
a single `contributors.json`. The static HTML viewer renders it with
interactive filters: combined chart, event-level activity scatter, and
per-repo small-multiples.

Generic — works on any org. Auth is via the GitHub CLI's existing token.

Requirements: Python 3.10+, `git`, [GitHub CLI](https://cli.github.com/).
No Python dependencies (stdlib only).

## Status

- [x] **Phase 1** — `fetch.py` data fetcher (local-clone based)
- [x] **Phase 2** — `viewer.html` interactive viewer
- [x] **Phase 3** — event-level activity scatter, response cache, layout polish
- [ ] **Phase 4** — drill-down panels, CSV export, repo grouping by topic/language

## Quickstart

```bash
gh auth login                            # if not already signed in
cp aliases.example.json aliases.json     # optional: edit to merge identities
```

Three workflows from there:

**Local view** (iterating, dev loop):
```bash
python3 fetch.py --org <ORG_LOGIN>       # writes contributors.json
python3 -m http.server 8765              # serve next to contributors.json
open http://localhost:8765/viewer.html   # auto-loads contributors.json
```

**One-shot share** (single self-contained HTML for a coworker):
```bash
python3 bundle.py --org <ORG_LOGIN>      # fetches + bundles in one step
                                          # → dist/<ORG>-contributors.html
```

**Already-fetched share** (use existing `contributors.json`):
```bash
python3 bundle.py                        # bundles whatever is in CWD
python3 bundle.py --inline-plotly        # also embed Plotly.js (offline)
```

The bundle is one HTML file (~1 MB for an org our size; ~5 MB with
Plotly inlined) that opens via `file://`. AirDrop / email / Slack it
to a coworker; they double-click to open.

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
| `--cache-dir-api <path>` | `cache` | API response cache dir |
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

```jsonc
{
  "org": "...",
  "fetched_at": "ISO-8601",
  "method": "local-clone",
  "filters": {
    "include_forks": false, "include_archived": true,
    "no_merges": false, "only": null,
    "include_prs_issues": true, "top_extensions": 15
  },
  "repos": [{"name", "full_name", "created_at", "pushed_at", "archived",
             "fork", "private", "default_branch", "language", "topics",
             "html_url"}],
  "repo_top_extensions": {"<repo>": [".ts", ".md", ...]},
  "stats": [{
    "repo": "<repo-name>",
    "author": {"name", "email", "login", "aliased", "is_bot",
               "names": [...], "emails": [...]},
    "total": <commits>,
    "weeks": [{
      "w":  <unix sunday>,
      "c":  <commits>,  "a": <additions>, "d": <deletions>,
      "ext": {".md": [<adds>, <dels>], ..., "_other": [<adds>, <dels>]},
      "po": <PRs opened>, "pm": <PRs merged>, "io": <issues opened>
    }]
  }],
  "events": [{
    "t":  "c" | "po" | "pm" | "io",
    "r":  "<repo>", "u": "<canonical author id>",
    "ts": <unix timestamp>,
    "a":  <additions, commits only>,
    "d":  <deletions, commits only>,
    "sha": "<7-char sha, commits only>"
  }]
}
```

Weeks are Sunday-anchored UTC (matching GitHub's `/stats/contributors`
convention). The `events` array is sorted by `ts` and powers daily
granularity + the activity scatter. `aliased: true` on an author
indicates the row was merged from multiple git identities via
`aliases.json`.

## Auth

Uses `gh auth token`. Install [GitHub CLI](https://cli.github.com/) and run
`gh auth login`. The token needs `repo` scope to read private org repos.

## Phase 2: `viewer.html`

Single static file. No build step. Plotly via CDN. Auto-loads
`contributors.json` if served over http(s); offers a file picker on
`file://`.

Layout (top-down):

1. **Combined chart** — selected repos summed; one subplot row per
   selected metric.
2. **Activity timeline** — event-level scatter, every commit / PR /
   issue as a marker. Y axis switches between contributor and repo;
   color is per repo; commit-marker size is `√(lines changed)`.
3. **Per-repo charts** — collapsible `<details>` section, one chart
   per repo with activity in the window, ordered by activity.

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
- **Granularity** — daily / weekly / monthly / quarterly. Daily uses
  the per-event timestamps from `events[]`; weekly+ use the
  precomputed week buckets.
- **Mode** — stacked area / line / cumulative line.
- **Repos** — multi-select with `Active only` shortcut (excludes
  archived + zero-activity).

Hover gives author + metric + date. Each contributor gets a stable
color, shared across the combined chart and every per-repo chart.

## bundle.py

```
python3 bundle.py [--org <ORG_LOGIN>] [options]
```

| Flag | Default | Behavior |
|------|---------|----------|
| `--org <name>` | (none) | run `fetch.py --org <name>` first (cache-aware) |
| `--data <path>` | `contributors.json` | source data file |
| `--viewer <path>` | `viewer.html` | viewer template |
| `--output <path>` | `dist/<org>-contributors.html` | output bundle |
| `--inline-plotly` | off | embed Plotly.js into the bundle (~+3 MB; offline-safe) |
| `--refresh` | off | with `--org`, force re-fetch instead of using cache |

The bundle injects `contributors.json` ahead of the viewer's main
script as `window.__BUNDLED_DATA__`, then escapes `</` so a stray
`</script>` in any data field can't close the enclosing tag. The
viewer's autoload prefers bundled data, then falls back to fetching
`contributors.json`, then to a file picker.

## Repo layout

```
fetch.py            data fetcher (local clones + cached API hits)
viewer.html         single-file static viewer (Plotly via CDN)
bundle.py           one-shot self-contained HTML packager
aliases.example.json  identity-merge schema example
LICENSE             MIT
```

Generated / gitignored:
```
contributors.json   fetcher output
aliases.json        per-user identity-merge map
repos/              bare clones cache
cache/              API response cache (per-org)
dist/               bundled HTML files
```

## License

MIT
