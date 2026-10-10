"""Fail if a tracked file contains something shaped like a secret.

Prints file and line number only, never the matched text.
Usage: python tools/check_secrets.py [root]
"""

import re
import subprocess
import sys
from pathlib import Path

PATTERNS = {
    "Anthropic API key": re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "JWT": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    "bearer header": re.compile(r"(?i)authorization:\s*bearer\s+[A-Za-z0-9._~+/-]{20,}"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
}


def tracked_files(root: Path) -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [root / line for line in out.splitlines() if (root / line).is_file()]


def findings(paths: list[Path], root: Path) -> list[str]:
    found = []
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(lines, start=1):
            for name, pattern in PATTERNS.items():
                if pattern.search(line):
                    found.append(f"{path.relative_to(root)}:{number}: looks like a {name}")
    return found


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    found = findings(tracked_files(root), root)
    for line in found:
        print(line)
    print(f"secrets: {len(found)} finding(s)")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
