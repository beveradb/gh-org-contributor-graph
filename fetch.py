#!/usr/bin/env python3
"""Fetch contributor stats for all repos in a GitHub org via local clones.

For each repo in the org we maintain a bare mirror under --cache-dir, fetch
the latest history, and run `git log --numstat` to extract per-commit
(author, timestamp, additions, deletions). We aggregate into weekly buckets
per (repo, author identity) and write a single JSON file.

Auth: uses `gh` for repo enumeration and cloning (so private repos work
out of the box if `gh auth login` is set up).

Why local clones rather than GitHub's /stats/contributors endpoint?
That endpoint is asynchronous and unreliable on large active repos
(202 retries can run for >5 minutes). Local clones are deterministic
and give exact line counts.
"""

import argparse
import json
import re
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


GITHUB_API = "https://api.github.com"
USER_AGENT = "gh-org-contributor-graph"

# noreply email patterns:
#   12345+login@users.noreply.github.com   (post-2017)
#   login@users.noreply.github.com         (pre-2017)
NOREPLY_RE = re.compile(
    r"^(?:(?P<id>\d+)\+)?(?P<login>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}[A-Za-z0-9])?)"
    r"@users\.noreply\.github\.com$",
    re.IGNORECASE,
)


# ---------- GitHub API (just for repo enumeration) ----------


def get_token() -> str:
    try:
        r = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, check=True
        )
        return r.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        sys.exit(
            "could not read gh auth token — install GitHub CLI and run "
            f"`gh auth login`: {e}"
        )


def gh_request(path_or_url: str, token: str):
    url = (
        path_or_url
        if path_or_url.startswith("http")
        else f"{GITHUB_API}/{path_or_url.lstrip('/')}"
    )
    req = Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urlopen(req) as resp:
            body = resp.read().decode()
            data = json.loads(body) if body else None
            return resp.status, data, dict(resp.headers)
    except HTTPError as e:
        body = e.read().decode() if e.fp else ""
        try:
            data = json.loads(body) if body else None
        except json.JSONDecodeError:
            data = None
        return e.code, data, dict(e.headers)


def parse_next(link_header: str):
    if not link_header:
        return None
    for part in link_header.split(","):
        section = part.strip().split(";")
        if len(section) < 2:
            continue
        url = section[0].strip().lstrip("<").rstrip(">")
        for attr in section[1:]:
            if attr.strip() == 'rel="next"':
                return url
    return None


def gh_paginate(path: str, token: str) -> list:
    results = []
    url = path
    while url:
        status, data, headers = gh_request(url, token)
        if status != 200:
            raise RuntimeError(f"GET {url} -> {status}: {data}")
        if not isinstance(data, list):
            raise RuntimeError(
                f"expected array from {url}, got {type(data).__name__}"
            )
        results.extend(data)
        url = parse_next(headers.get("Link", ""))
    return results


def list_org_repos(org: str, token: str) -> list[dict]:
    return gh_paginate(f"orgs/{org}/repos?per_page=100&type=all", token)


# ---------- Local clone management ----------


def run_git(args: list[str], cwd: Path | None = None, check: bool = True):
    cmd = ["git"] + args
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} (cwd={cwd}) -> {r.returncode}\n"
            f"stderr: {r.stderr.strip()}"
        )
    return r


def ensure_clone(owner: str, repo: str, cache_dir: Path, token: str) -> Path:
    """Bare-clone the repo into cache_dir, or fetch if it already exists.

    Returns the path to the bare git dir.
    """
    target = cache_dir / f"{repo}.git"
    if target.exists():
        # Update existing mirror
        try:
            run_git(["fetch", "--all", "--prune", "--quiet"], cwd=target)
            return target
        except RuntimeError as e:
            print(f"  ! fetch failed for {repo}, re-cloning: {e}", file=sys.stderr)
            # Fall through to fresh clone
            import shutil

            shutil.rmtree(target)

    # Fresh bare clone via authenticated HTTPS URL
    url = f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"
    cache_dir.mkdir(parents=True, exist_ok=True)
    run_git(["clone", "--bare", "--quiet", url, str(target)])
    return target


# ---------- Git log parsing ----------


def iter_commits(git_dir: Path, no_merges: bool = False):
    """Yield dicts: {sha, name, email, iso_date, additions, deletions}.

    Uses `git log --all --numstat` and a custom commit-header format.
    Line counts are sums across files in each commit (binary files contribute
    0 instead of the API's `-`).
    """
    sep = "\x1eCOMMIT\x1e"
    fmt = sep + "%H%x09%an%x09%ae%x09%aI"
    args = ["log", "--all", "--numstat", f"--pretty=format:{fmt}"]
    if no_merges:
        args.insert(1, "--no-merges")
    proc = subprocess.Popen(
        ["git"] + args,
        cwd=git_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    cur = None
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.rstrip("\n")
        if line.startswith("\x1eCOMMIT\x1e"):
            if cur is not None:
                yield cur
            payload = line[len("\x1eCOMMIT\x1e"):]
            parts = payload.split("\t", 3)
            if len(parts) != 4:
                cur = None
                continue
            sha, name, email, iso = parts
            cur = {
                "sha": sha,
                "name": name,
                "email": email,
                "iso_date": iso,
                "additions": 0,
                "deletions": 0,
            }
        elif line.strip() == "":
            continue
        else:
            if cur is None:
                continue
            cols = line.split("\t")
            if len(cols) < 2:
                continue
            a, d = cols[0], cols[1]
            if a != "-":
                try:
                    cur["additions"] += int(a)
                except ValueError:
                    pass
            if d != "-":
                try:
                    cur["deletions"] += int(d)
                except ValueError:
                    pass
    if cur is not None:
        yield cur
    proc.wait()
    if proc.returncode not in (0, None):
        err = proc.stderr.read() if proc.stderr else ""
        raise RuntimeError(f"git log failed in {git_dir}: {err}")


# ---------- Aggregation ----------


def sunday_unix(iso_date: str) -> int:
    """Unix timestamp for Sunday 00:00 UTC of the commit's week.

    Matches GitHub's /stats/contributors week anchor.
    """
    dt = datetime.fromisoformat(iso_date).astimezone(timezone.utc)
    # weekday(): Mon=0..Sun=6. Days back to Sunday: Sun=0, Mon=1, ..., Sat=6.
    days_since_sunday = (dt.weekday() + 1) % 7
    sunday = (dt - timedelta(days=days_since_sunday)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return int(sunday.timestamp())


def extract_login(email: str) -> str | None:
    m = NOREPLY_RE.match(email or "")
    return m.group("login") if m else None


def author_key(name: str, email: str, aliases: dict | None = None) -> str:
    """Stable identity key.

    Resolution order:
      1. aliases.json hit (by email, by extracted login, or by name)
      2. github noreply login extracted from email
      3. lowercased email
      4. lowercased name (last resort)
    """
    aliases = aliases or {}
    e = (email or "").lower()
    n = (name or "").lower()
    login = extract_login(email)
    if e and e in aliases:
        return f"canonical:{aliases[e]}"
    if login and login.lower() in aliases:
        return f"canonical:{aliases[login.lower()]}"
    if n and n in aliases:
        return f"canonical:{aliases[n]}"
    if login:
        return f"login:{login.lower()}"
    if e:
        return f"email:{e}"
    return f"name:{n}"


def aggregate(commits, repo_name: str, aliases: dict | None = None):
    """Aggregate commits into per-(author, week) buckets.

    Returns list of {repo, author, total, weeks: [...]}.
    """
    aliases = aliases or {}
    buckets: dict = {}
    for c in commits:
        key = author_key(c["name"], c["email"], aliases)
        b = buckets.get(key)
        if b is None:
            login = extract_login(c["email"])
            canonical = key.split(":", 1)[1] if key.startswith("canonical:") else None
            b = {
                "meta": {
                    "name": c["name"],
                    "email": c["email"],
                    "login": canonical or login,
                    "aliased": canonical is not None,
                    "emails": set(),
                    "names": set(),
                },
                "weeks": defaultdict(lambda: [0, 0, 0]),
                "total": 0,
            }
            buckets[key] = b
        if c["name"]:
            b["meta"]["names"].add(c["name"])
            b["meta"]["name"] = c["name"]
        if c["email"]:
            b["meta"]["emails"].add(c["email"])
            if not b["meta"]["email"]:
                b["meta"]["email"] = c["email"]
        try:
            w = sunday_unix(c["iso_date"])
        except Exception:
            continue
        wb = b["weeks"][w]
        wb[0] += 1
        wb[1] += c["additions"]
        wb[2] += c["deletions"]
        b["total"] += 1
    rows = []
    for b in buckets.values():
        weeks = [
            {"w": w, "c": v[0], "a": v[1], "d": v[2]}
            for w, v in sorted(b["weeks"].items())
        ]
        meta = b["meta"]
        meta["emails"] = sorted(meta["emails"])
        meta["names"] = sorted(meta["names"])
        rows.append(
            {
                "repo": repo_name,
                "author": meta,
                "total": b["total"],
                "weeks": weeks,
            }
        )
    return rows


# ---------- Repo metadata ----------


def repo_meta(repo: dict) -> dict:
    return {
        "name": repo["name"],
        "full_name": repo["full_name"],
        "created_at": repo["created_at"],
        "pushed_at": repo.get("pushed_at"),
        "archived": repo.get("archived", False),
        "fork": repo.get("fork", False),
        "private": repo.get("private", False),
        "default_branch": repo.get("default_branch"),
        "language": repo.get("language"),
        "topics": repo.get("topics", []),
        "html_url": repo["html_url"],
    }


# ---------- Main ----------


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--org", required=True, help="GitHub org login")
    p.add_argument("--output", default="contributors.json", help="output JSON path")
    p.add_argument(
        "--cache-dir",
        default="repos",
        help="directory for bare clones (default: ./repos)",
    )
    p.add_argument(
        "--include-forks",
        action="store_true",
        help="include repos forked from outside the org (default: skip)",
    )
    p.add_argument(
        "--exclude-archived",
        action="store_true",
        help="skip archived repos (default: include them)",
    )
    p.add_argument(
        "--no-merges",
        action="store_true",
        help="exclude merge commits from counts",
    )
    p.add_argument(
        "--only",
        action="append",
        default=None,
        help="only process repos with this name (repeatable)",
    )
    p.add_argument(
        "--aliases",
        default="aliases.json",
        help="JSON file mapping email/login/name (lowercased) -> canonical id "
        "(default: aliases.json if present, else no merging)",
    )
    args = p.parse_args()

    aliases: dict[str, str] = {}
    aliases_path = Path(args.aliases)
    if aliases_path.exists():
        try:
            raw = json.loads(aliases_path.read_text())
            aliases = {k.lower(): v for k, v in raw.items() if not k.startswith("_")}
            print(f"loaded {len(aliases)} aliases from {aliases_path}")
        except json.JSONDecodeError as e:
            sys.exit(f"could not parse {aliases_path}: {e}")
    elif args.aliases != "aliases.json":
        sys.exit(f"aliases file not found: {aliases_path}")

    token = get_token()
    cache_dir = Path(args.cache_dir).resolve()
    print(f"fetching repos for org={args.org}...")
    repos = list_org_repos(args.org, token)
    print(f"  {len(repos)} repos total")

    selected = []
    for r in repos:
        if not args.include_forks and r.get("fork"):
            continue
        if args.exclude_archived and r.get("archived"):
            continue
        if args.only and r["name"] not in args.only:
            continue
        selected.append(r)
    print(
        f"  {len(selected)} after filters "
        f"(forks={'yes' if args.include_forks else 'no'}, "
        f"archived={'no' if args.exclude_archived else 'yes'}"
        + (f", only={args.only}" if args.only else "")
        + ")"
    )

    print(f"  cache dir: {cache_dir}")
    cache_dir.mkdir(parents=True, exist_ok=True)

    all_stats: list[dict] = []
    n_commits_total = 0
    for i, r in enumerate(selected, 1):
        name = r["name"]
        print(f"[{i}/{len(selected)}] {r['full_name']}", flush=True)
        t0 = time.time()
        try:
            git_dir = ensure_clone(args.org, name, cache_dir, token)
        except RuntimeError as e:
            print(f"  ! clone/fetch failed: {e}", file=sys.stderr)
            continue
        t_clone = time.time() - t0

        t0 = time.time()
        try:
            commits = list(iter_commits(git_dir, no_merges=args.no_merges))
        except RuntimeError as e:
            print(f"  ! git log failed: {e}", file=sys.stderr)
            continue
        t_log = time.time() - t0

        rows = aggregate(commits, name, aliases=aliases)
        all_stats.extend(rows)
        n_commits_total += len(commits)
        print(
            f"  clone {t_clone:.1f}s, log {t_log:.1f}s, "
            f"{len(commits)} commits, {len(rows)} authors"
        )

    output = {
        "org": args.org,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "method": "local-clone",
        "filters": {
            "include_forks": args.include_forks,
            "include_archived": not args.exclude_archived,
            "no_merges": args.no_merges,
            "only": args.only,
        },
        "repos": [repo_meta(r) for r in selected],
        "stats": all_stats,
    }

    out_path = Path(args.output)
    out_path.write_text(json.dumps(output, indent=2))
    n_authors = len({s["author"].get("login") or s["author"].get("email") or s["author"]["name"] for s in all_stats})
    print(
        f"\nwrote {out_path}: {len(selected)} repos, "
        f"{n_authors} unique authors, {n_commits_total} commits, "
        f"{len(all_stats)} repo*author rows"
    )


if __name__ == "__main__":
    main()
