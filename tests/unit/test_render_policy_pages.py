"""The Confluence page kit (S15): what you paste is what Stewardry parses."""

import re
from pathlib import Path

from stewardry.platform.evidence import parse_page
from tools.render_policy_pages import render


def test_every_rendered_page_is_eligible_evidence(tmp_path: Path) -> None:
    paths = render(tmp_path)
    pages = paths[1:]
    assert len(pages) == 21
    for page in pages:
        body = re.sub(
            r"^<!doctype html><meta charset='utf-8'><title>.*?</title>", "", page.read_text()
        )
        parsed = parse_page(body)
        assert parsed.problems == [], page.name
        assert parsed.metadata is not None and parsed.metadata.policy_id == page.stem


def test_the_index_marks_the_confidential_pages(tmp_path: Path) -> None:
    text = render(tmp_path)[0].read_text()
    rows = text.split("<tr>")
    marked = [re.findall(r"SEC-\d\d", r)[0] for r in rows if "restrict view to GRC" in r]
    assert marked == ["SEC-04", "SEC-09", "SEC-17"]
