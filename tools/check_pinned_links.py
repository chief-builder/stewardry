"""Check GitHub file links pinned to a commit SHA through the GitHub API.

github.com answers CI requests for `/blob/<sha>/` pages with 503 (seen on
four runs, 2026-10-10), so lychee skips them. A file at a fixed commit
cannot change, so "the file exists at that commit" is a complete check.

Needs network. Uses GITHUB_TOKEN when set (higher rate limit).
Usage: python tools/check_pinned_links.py [root]
"""

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

PINNED = re.compile(
    r"https://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)"
    r"/blob/(?P<sha>[0-9a-f]{40})/(?P<path>[^\s)>#\"']+)"
)


def pinned_links(root: Path) -> set[tuple[str, str, str, str]]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.md"],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    links = set()
    for name in out.splitlines():
        text = (root / name).read_text(encoding="utf-8")
        for m in PINNED.finditer(text):
            links.add((m["owner"], m["repo"], m["sha"], m["path"]))
    return links


def exists(owner: str, repo: str, sha: str, path: str, token: str | None) -> bool:
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={sha}"
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise
    return isinstance(body, dict) and body.get("type") == "file"


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    token = os.environ.get("GITHUB_TOKEN")
    links = sorted(pinned_links(root))
    missing = [link for link in links if not exists(*link, token)]
    for owner, repo, sha, path in missing:
        print(f"missing: {owner}/{repo}@{sha[:12]}:{path}")
    print(f"pinned links: {len(links)} checked, {len(missing)} missing")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
