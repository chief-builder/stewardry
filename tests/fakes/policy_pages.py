"""The synthetic policy fixtures rendered in the supported page layout
(see `stewardry.platform.evidence`), as the adapter's `confluence_read_page`
would return them."""

import html
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from stewardry.platform.atlassian.client import GatewayOutcome, ToolFailure, ToolSuccess
from tests.fakes.gateway import FakeGateway

POLICIES = Path(__file__).resolve().parents[2] / "fixtures" / "policies"
LABELS = {
    "policy_id": "Policy ID",
    "version": "Version",
    "status": "Status",
    "classification": "Classification",
    "owner": "Owner",
    "effective_date": "Effective date",
    "review_date": "Review date",
    "superseded_by": "Superseded by",
}


def policy_html(policy_id: str) -> tuple[dict[str, Any], str]:
    (path,) = POLICIES.glob(f"{policy_id}-*.md")
    _, front, body = path.read_text(encoding="utf-8").split("---\n", 2)
    meta: dict[str, Any] = yaml.safe_load(front)
    rows = "".join(
        f"<tr><th>{label}</th><td>{html.escape(str(meta[key]))}</td></tr>"
        for key, label in LABELS.items()
        if meta.get(key)
    )
    parts = [f"<table><tbody>{rows}</tbody></table>"]
    for block in body.strip().split("\n\n"):
        text = html.escape(block.strip())
        if block.startswith("## "):
            parts.append(f"<h2>{text[3:]}</h2>")
        elif block.startswith("# "):
            parts.insert(0, f"<h1>{text[2:]}</h1>")
        else:
            parts.append(f"<p>{text}</p>")
    return meta, "".join(parts)


def page(policy_id: str, version: int | None = 3, body: str | None = None) -> dict[str, Any]:
    meta, rendered = policy_html(policy_id)
    return {
        "page_id": str(meta["confluence_page_id"]),
        "title": f"{policy_id} {meta['title']}",
        "version": version,
        "body_html": rendered if body is None else body,
    }


def all_pages() -> dict[str, str]:
    """page ID -> policy ID, for every fixture policy."""
    pages = {}
    for path in sorted(POLICIES.glob("SEC-*.md")):
        meta = yaml.safe_load(path.read_text(encoding="utf-8").split("---\n", 2)[1])
        pages[str(meta["confluence_page_id"])] = meta["policy_id"]
    return pages


@dataclass
class PolicyGateway(FakeGateway):
    """The fake gateway, serving the fixture policies as Confluence pages, plus
    one page outside the catalog that must never get through."""

    bodies: dict[str, str] = field(default_factory=dict)  # page ID -> body override

    def _tool(self, name: str, arguments: Mapping[str, Any]) -> GatewayOutcome:
        pages = all_pages()
        if name == "atlassian_searchConfluence":
            results = [
                {"content": {"id": page_id, "title": policy_id, "version": {"number": 3}}}
                for page_id, policy_id in pages.items()
            ]
            results.append({"content": {"id": "999", "title": "HR salary bands"}})
            return ToolSuccess({"results": results}, "")
        if name == "atlassian_getConfluenceContent":
            page_id = arguments["content_id"]
            if page_id in self.restricted_pages or page_id not in pages:
                return ToolFailure("Content not found or you do not have permission to view it")
            content = page(pages[page_id])
            body = self.bodies.get(page_id, content["body_html"])
            return ToolSuccess(
                None,
                json.dumps(
                    {
                        "id": page_id,
                        "title": content["title"],
                        "version": {"number": 3},
                        "body": {"value": body},
                    }
                ),
            )
        return super()._tool(name, arguments)
