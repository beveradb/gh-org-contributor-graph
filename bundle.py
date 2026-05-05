#!/usr/bin/env python3
"""Bundle viewer.html + contributors.json into a single self-contained HTML file.

The data is injected as `window.__BUNDLED_DATA__ = {...}` ahead of the
viewer's main script. Plotly is still loaded from a CDN (the bundle stays
human-readable and a few MB smaller); pass --inline-plotly to embed it
too for fully-offline use.

Usage:
    python3 bundle.py                       # writes dist/<org>-contributors.html
    python3 bundle.py --inline-plotly       # also embeds Plotly.js
    python3 bundle.py --output share.html   # custom path

Recipient: double-click the produced .html — it works from file://
without a server, fetcher, or any other setup.
"""

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def encode_json_for_script(data) -> str:
    """JSON-encode for safe embedding in a <script> tag."""
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    # Escape sequences that can break out of <script> or HTML comments
    return (
        raw.replace("</", "<\\/")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


def fetch_plotly(url: str) -> str:
    print(f"fetching plotly from {url}...", file=sys.stderr)
    with urllib.request.urlopen(url) as r:
        return r.read().decode("utf-8")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", default="contributors.json")
    p.add_argument("--viewer", default="viewer.html")
    p.add_argument("--output", default=None)
    p.add_argument(
        "--inline-plotly",
        action="store_true",
        help="embed Plotly.js inline (fully offline; bigger file)",
    )
    args = p.parse_args()

    viewer_path = Path(args.viewer)
    data_path = Path(args.data)
    if not viewer_path.exists():
        sys.exit(f"viewer not found: {viewer_path}")
    if not data_path.exists():
        sys.exit(f"data not found: {data_path} — run `python3 fetch.py --org ...` first")

    viewer = read_text(viewer_path)
    data = json.loads(read_text(data_path))

    # Choose default output path from org name
    if args.output is None:
        org = data.get("org", "org")
        out_dir = Path("dist")
        out_dir.mkdir(exist_ok=True)
        output_path = out_dir / f"{org}-contributors.html"
    else:
        output_path = Path(args.output)

    # ----- inject the JSON ahead of the viewer's first inline <script> -----
    encoded = encode_json_for_script(data)
    inject = (
        '<script id="bundled-data">\n'
        f"window.__BUNDLED_DATA__ = {encoded};\n"
        "</script>\n"
    )
    # Anchor: the line just before the inline JS that defines STATE
    anchor = "// State + data"
    idx = viewer.find(anchor)
    if idx == -1:
        sys.exit("could not find injection anchor in viewer.html")
    script_open = viewer.rfind("<script>", 0, idx)
    if script_open == -1:
        sys.exit("could not find opening <script> tag for the inline JS")
    bundled = viewer[:script_open] + inject + viewer[script_open:]

    # ----- optionally inline Plotly -----
    if args.inline_plotly:
        m = re.search(
            r'<script\s+src="(https://cdn\.plot\.ly/[^"]+)"[^>]*></script>',
            bundled,
        )
        if not m:
            sys.exit("could not find Plotly CDN <script> tag")
        plotly_src = fetch_plotly(m.group(1))
        replacement = f"<script>\n{plotly_src}\n</script>"
        bundled = bundled[: m.start()] + replacement + bundled[m.end() :]

    output_path.write_text(bundled, encoding="utf-8")
    size_mb = output_path.stat().st_size / (1024 * 1024)
    n_repos = len(data.get("repos", []))
    n_events = len(data.get("events", []))
    print(
        f"wrote {output_path} ({size_mb:.1f} MB) — "
        f"{data.get('org')}: {n_repos} repos, {n_events:,} events"
        + (" [plotly inlined]" if args.inline_plotly else " [plotly via CDN]")
    )


if __name__ == "__main__":
    main()
