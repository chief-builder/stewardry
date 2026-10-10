"""Check that relative Markdown links and #anchors resolve.

External links are checked in CI by lychee; this check needs no network.
Usage: python tools/check_docs.py [root]
"""

import re
import subprocess
import sys
from pathlib import Path

LINK = re.compile(r"\]\(([^)#\s]*)(#[^)\s]+)?\)")
HEADING = re.compile(r"^#+ (.+)$", re.MULTILINE)
FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)


def slug(heading: str) -> str:
    text = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return text.replace(" ", "-")


def anchors(path: Path) -> set[str]:
    text = FENCE.sub("", path.read_text(encoding="utf-8"))
    return {slug(h) for h in HEADING.findall(text)}


def markdown_files(root: Path) -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.md"],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [root / line for line in out.splitlines() if (root / line).is_file()]


def problems(root: Path) -> list[str]:
    found = []
    for md in markdown_files(root):
        text = FENCE.sub("", md.read_text(encoding="utf-8"))
        for match in LINK.finditer(text):
            target, anchor = match.group(1), match.group(2)
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            path = (md.parent / target) if target else md
            where = md.relative_to(root)
            if not path.exists():
                found.append(f"{where}: missing target {target}")
            elif (
                anchor
                and path.is_file()
                and path.suffix == ".md"
                and anchor[1:] not in anchors(path)
            ):
                found.append(f"{where}: missing anchor {target}{anchor}")
    return found


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    found = problems(root)
    for line in found:
        print(line)
    print(f"docs: {len(found)} problem(s)")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
