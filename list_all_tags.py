#!/usr/bin/env python3
"""List all Gamma API tags (slug + label), paginated.

Usage:
    uv run python list_all_tags.py
    uv run python list_all_tags.py --search geo
    uv run python list_all_tags.py --json
"""

import argparse
import json
import urllib.request
from typing import Any

GAMMA_HOST = "https://gamma-api.polymarket.com"
PAGE_SIZE = 100  # Gamma caps a page at 100


def fetch_tags(host: str, page_size: int = PAGE_SIZE) -> list[dict[str, Any]]:
    """Fetch all tags from Gamma API, handling pagination via offset."""
    all_tags: list[dict[str, Any]] = []
    offset = 0

    while True:
        url = f"{host}/tags?limit={page_size}&offset={offset}"
        req = urllib.request.Request(url, headers={"User-Agent": "poly-maker"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            batch = json.loads(resp.read().decode())

        if not batch:
            break

        all_tags.extend(batch)
        print(f"  fetched {len(all_tags)} tags...", end="\r")

        if len(batch) < page_size:
            break  # last page
        offset += page_size

    print()  # newline after progress
    return all_tags


def main() -> None:
    parser = argparse.ArgumentParser(description="List all Gamma API tags")
    parser.add_argument("--search", "-s", type=str, default="",
                        help="filter by keyword (slug or label)")
    parser.add_argument("--json", action="store_true",
                        help="output as JSON")
    parser.add_argument("--host", default=GAMMA_HOST,
                        help=f"Gamma API host (default: {GAMMA_HOST})")
    args = parser.parse_args()

    print(f"Fetching all tags from {args.host} ...")
    tags = fetch_tags(args.host)
    print(f"Total tags: {len(tags)}\n")

    # filter
    kw = args.search.lower()
    if kw:
        tags = [
            t for t in tags
            if kw in t.get("slug", "").lower()
            or kw in t.get("label", "").lower()
        ]
        print(f"Filtered by '{args.search}': {len(tags)} tags\n")

    if args.json:
        print(json.dumps(tags, indent=2, ensure_ascii=False))
        return

    # table output
    print(f"{'SLUG':<40s} {'LABEL':<30s} {'ID':>6s}")
    print("-" * 80)
    for t in tags:
        slug = t.get("slug", "")
        label = t.get("label", "")
        tid = t.get("id", "")
        print(f"{slug:<40s} {label:<30s} {str(tid):>6s}")


if __name__ == "__main__":
    main()
#（注：内容由AI生成）
