"""Render the synthetic policies as Confluence pages, ready to paste.

Each policy becomes a page in the layout Stewardry's evidence parser accepts
(`platform/evidence.py`): a two-column metadata table, then section headings
and numbered passages. Stewardry never writes to Atlassian; you create the
pages yourself:

1. `make confluence-pages` writes build/confluence-pages/ (git-ignored).
2. Open build/confluence-pages/index.html in a browser. For each policy, open
   its page, select all, copy, and paste into a new page in the SECPOL space
   with the title shown. Do not add macros, panels, or other formatting.
3. For the pages marked confidential, add a view restriction to the GRC user.
4. Record each page's ID (from its URL) in the setup checklist; the catalog
   (policies/catalogs/secpol.yaml) is updated from it afterwards.

Usage: python -m tools.render_policy_pages [output_dir]
"""

import html
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
POLICIES = ROOT / "fixtures" / "policies"
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
    """The front matter and the page body for one policy."""
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


def page_title(meta: dict[str, Any]) -> str:
    return f"{meta['policy_id']} {meta['title']}"


def render(output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written, rows = [], []
    for path in sorted(POLICIES.glob("SEC-*.md")):
        policy_id = path.name[:6]
        meta, body = policy_html(policy_id)
        title = html.escape(page_title(meta))
        out = output_dir / f"{policy_id}.html"
        out.write_text(
            f"<!doctype html><meta charset='utf-8'><title>{title}</title>{body}\n",
            encoding="utf-8",
        )
        written.append(out)
        restrict = "restrict view to GRC" if meta["classification"] == "confidential" else ""
        rows.append(
            f"<tr><td><a href='{out.name}'>{policy_id}</a></td><td>{title}</td>"
            f"<td>{html.escape(meta['classification'])}</td><td>{restrict}</td></tr>"
        )
    index = output_dir / "index.html"
    index.write_text(
        "<!doctype html><meta charset='utf-8'><title>SECPOL pages to create</title>"
        "<h1>SECPOL pages to create (synthetic)</h1>"
        "<p>Paste each page's content into a new Confluence page in space SECPOL, "
        "titled as shown. Plain content only: no macros.</p>"
        "<table border='1'><tr><th>Policy</th><th>Page title</th>"
        "<th>Classification</th><th>Restriction</th></tr>" + "".join(rows) + "</table>\n",
        encoding="utf-8",
    )
    return [index, *written]


def main() -> int:
    output_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "build" / "confluence-pages"
    paths = render(output_dir)
    print(f"wrote {len(paths) - 1} pages and an index to {output_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
