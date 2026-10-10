"""Tests for the repository checks in tools/ (docs links and secret patterns)."""

import subprocess
from pathlib import Path

from tools.check_docs import slug
from tools.check_pinned_links import pinned_links
from tools.check_secrets import findings


def test_slug_matches_github_style_anchors() -> None:
    assert slug("Gaps the broker cannot meet as-is") == "gaps-the-broker-cannot-meet-as-is"
    assert slug("ADR 0003: Agent runtime") == "adr-0003-agent-runtime"


def test_secret_shapes_are_reported_without_their_value(tmp_path: Path) -> None:
    # Built at runtime so this test file never contains a secret-shaped string.
    fake_key = "sk-" + "ant-" + "x" * 24
    planted = tmp_path / "config.txt"
    planted.write_text(f"first line\nkey = {fake_key}\n", encoding="utf-8")

    found = findings([planted], tmp_path)

    assert found == ["config.txt:2: looks like a anthropic-api-key value"]
    assert fake_key not in found[0]


def test_clean_file_has_no_findings(tmp_path: Path) -> None:
    clean = tmp_path / "notes.md"
    clean.write_text("Set ANTHROPIC_API_KEY in your .env file.\n", encoding="utf-8")
    assert findings([clean], tmp_path) == []


def test_pinned_github_links_are_found(tmp_path: Path) -> None:
    sha = "a" * 40
    (tmp_path / "doc.md").write_text(
        f"See [api](https://github.com/acme/broker/blob/{sha}/docs/api.md) and "
        f"<https://github.com/acme/broker/blob/{sha}/src/x.json>. "
        "Not pinned: https://github.com/acme/broker/blob/main/README.md\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)  # noqa: S607
    assert pinned_links(tmp_path) == {
        ("acme", "broker", sha, "docs/api.md"),
        ("acme", "broker", sha, "src/x.json"),
    }
