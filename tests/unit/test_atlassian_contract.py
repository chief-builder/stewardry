"""Contract tests against the pinned vendor-token-broker (offline, vendored copies).

- The vendored files are byte-identical to the manifest (and, in CI, the
  manifest to the pinned commit: tools/check_vendored.py).
- The error catalog equals the set of ToolError templates in the pinned gateway.
- Every tool and argument the adapter sends exists in the pinned Atlassian
  tool snapshot and the gateway's allow-list.
- The adapter source never names executeRead, discover, or Jira search.
"""

import ast
import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any

from stewardry.platform.atlassian import adapter as adapter_module
from stewardry.platform.atlassian.adapter import TOOLS_USED, AtlassianReads, AtlassianSettings
from stewardry.platform.atlassian.gateway_errors import CATALOG
from stewardry.platform.credentials import GatewayCredential
from tests.fakes.gateway import CLOUD_ID, CONSENT_PREFIX, FakeGateway

ROOT = Path(__file__).resolve().parents[2]
VENDORED = ROOT / "third_party" / "vendor-token-broker"
MANIFEST = json.loads((VENDORED / "MANIFEST.json").read_text())
SNAPSHOT = json.loads((VENDORED / "src/mcp_gateway/snapshots/atlassian.json").read_text())
UPSTREAMS = json.loads((VENDORED / "src/mcp_gateway/upstreams.json").read_text())
SCHEMAS = {tool["name"]: tool["inputSchema"] for tool in SNAPSHOT["tools"]}


def test_vendored_files_match_the_manifest() -> None:
    assert MANIFEST["commit"] == "20d1426622a0c05478c4331b82ad8952411f9a4a"
    for path, digest in MANIFEST["files"].items():
        assert hashlib.sha256((VENDORED / path).read_bytes()).hexdigest() == digest, path


def _template(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant):
                parts.append(str(value.value))
            else:
                parts.append("{}")
        return "".join(parts)
    return None


def gateway_templates() -> set[str]:
    tree = ast.parse((VENDORED / "src/mcp_gateway/server.py").read_text())
    found = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "ToolError"
            and node.args
        ):
            template = _template(node.args[0])
            assert template is not None, f"ToolError at line {node.lineno} is not a string"
            found.add(template)
    return found


def test_error_catalog_matches_the_pinned_gateway_exactly() -> None:
    pinned = gateway_templates()
    assert len(pinned) == 10
    assert set(CATALOG) == pinned, {
        "missing from catalog": sorted(pinned - set(CATALOG)),
        "stale in catalog": sorted(set(CATALOG) - pinned),
    }


def _sent_arguments() -> dict[str, list[dict[str, Any]]]:
    gateway = FakeGateway(connected={"token"})
    reads = AtlassianReads(
        gateway,
        AtlassianSettings(CLOUD_ID, CONSENT_PREFIX, {"customer": "customfield_10042"}),
        sleep=lambda s: None,
    )
    credential = GatewayCredential("token", dt.datetime.max.replace(tzinfo=dt.UTC))
    reads.jira_get_request(credential, "SQR-1", ["summary", "customer"])
    reads.confluence_list_pages(credential, "SECPOL", frozenset({"101"}))
    reads.confluence_read_page(credential, "101")
    reads.accessible_site_ids(credential)
    reads.account_id(credential)
    sent: dict[str, list[dict[str, Any]]] = {}
    for call in gateway.calls:
        sent.setdefault(call.name, []).append(call.arguments)
    return sent


def test_every_tool_used_is_in_the_snapshot_and_allow_list() -> None:
    allowed = next(u for u in UPSTREAMS["upstreams"] if u["name"] == "atlassian")["tools"]
    for tool in TOOLS_USED:
        upstream_name = tool.removeprefix("atlassian_")
        assert upstream_name in allowed, tool
        assert upstream_name in SCHEMAS, tool
    assert set(_sent_arguments()) == set(TOOLS_USED)


def test_every_argument_sent_is_declared_with_a_valid_value() -> None:
    for tool, calls in _sent_arguments().items():
        schema = SCHEMAS[tool.removeprefix("atlassian_")]
        properties = schema.get("properties", {})
        for arguments in calls:
            assert set(arguments) <= set(properties), (tool, set(arguments) - set(properties))
            for required in schema.get("required", []):
                assert required in arguments, (tool, required)
            for name, value in arguments.items():
                allowed = properties[name].get("enum")
                if allowed is not None:
                    assert value in allowed, (tool, name, value)


def _string_literals() -> set[str]:
    found: set[str] = set()
    for module in (ROOT / "src" / "stewardry").rglob("*.py"):
        for node in ast.walk(ast.parse(module.read_text())):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                found.add(node.value)
    return found


def test_the_code_names_no_atlassian_tool_beyond_the_narrow_set() -> None:
    literals = _string_literals()
    named = {s for s in literals if s.startswith("atlassian_")}
    assert named == set(TOOLS_USED)
    for forbidden in (
        "executeRead",
        "discover",
        "searchJiraIssuesUsingJql",
        "executeWrite",
        "executeDestructive",
    ):
        assert not any(forbidden == s or s.endswith(f"_{forbidden}") for s in literals), forbidden
    assert adapter_module.CONNECT == "connect_atlassian"


def test_the_snapshot_still_offers_the_tools_we_refuse() -> None:
    """If these disappear upstream, the threat model's G3 entry should be revisited."""
    assert {"executeRead", "discover"} <= set(SCHEMAS)
