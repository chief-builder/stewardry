"""Fail if a tracked file contains something shaped like a secret.

Prints file and line number only, never the matched text.
Usage: python tools/check_secrets.py [root]
"""

import subprocess
import sys
from pathlib import Path

from stewardry.platform.redaction import PATTERNS


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
                    found.append(f"{path.relative_to(root)}:{number}: looks like a {name} value")
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
