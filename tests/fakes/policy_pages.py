"""The synthetic policy fixtures rendered in the supported page layout
(see `stewardry.platform.evidence`), as the adapter's `confluence_read_page`
would return them."""

import html
from pathlib import Path
from typing import Any

import yaml

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
