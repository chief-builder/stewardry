"""Check that vendored upstream files match their pinned commit (needs network).

Offline tests already check the vendored files against MANIFEST.json. This
closes the loop: each manifest hash must equal the SHA-256 of the same path at
the pinned commit, fetched through the GitHub API. Uses GITHUB_TOKEN when set.
Usage: python tools/check_vendored.py [third_party/<name> ...]
"""

import base64
import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path

DEFAULT = [Path("third_party/vendor-token-broker")]


def upstream_sha256(repository: str, commit: str, path: str, token: str | None) -> str:
    url = f"https://api.github.com/repos/{repository}/contents/{path}?ref={commit}"
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        body = json.load(response)
    return hashlib.sha256(base64.b64decode(body["content"])).hexdigest()


def main() -> int:
    roots = [Path(a) for a in sys.argv[1:]] or DEFAULT
    token = os.environ.get("GITHUB_TOKEN")
    problems = 0
    for root in roots:
        manifest = json.loads((root / "MANIFEST.json").read_text())
        for path, expected in sorted(manifest["files"].items()):
            local = hashlib.sha256((root / path).read_bytes()).hexdigest()
            upstream = upstream_sha256(manifest["repository"], manifest["commit"], path, token)
            if not local == expected == upstream:
                problems += 1
                print(f"mismatch: {root / path} (local, manifest, and upstream must agree)")
        print(f"{root}: {len(manifest['files'])} file(s) checked at {manifest['commit'][:12]}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
