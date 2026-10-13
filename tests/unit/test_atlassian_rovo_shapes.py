"""Stewardry against the real response shapes of Atlassian's Rovo MCP server
(recorded in the S15 connected run, identifiers replaced; gaps G7, G8, G11)."""

import datetime as dt
import json
from typing import Any

import pytest

from stewardry.platform.atlassian.adapter import AtlassianReads, AtlassianSettings
from stewardry.platform.atlassian.client import GatewayOutcome, ToolFailure, ToolSuccess
from stewardry.platform.atlassian.gateway_errors import classify
from stewardry.platform.credentials import GatewayCredential
from stewardry.platform.evidence import parse_page
from stewardry.platform.execution import OperationRefused
from tests.unit.conftest import ROOT

RECORDED = json.loads((ROOT / "tests/fixtures/atlassian-rovo-2026-10-12.json").read_text())
SITE = "11111111-2222-4333-8444-555555555555"
PREFIX = "http://localhost:8600/v1/authorize/atlassian?txn="
CREDENTIAL = GatewayCredential("t", dt.datetime.now(dt.UTC) + dt.timedelta(hours=1))


class Recorded:
    def __init__(self, name: str) -> None:
        self.entry: dict[str, Any] = RECORDED["responses"][name]
        self.sent: list[dict[str, Any]] = []

    def call_tool(
        self, credential: GatewayCredential, name: str, arguments: Any, *, interactive: bool
    ) -> GatewayOutcome:
        self.sent.append(dict(arguments))
        if self.entry["type"] == "success":
            return ToolSuccess(None, self.entry["text"])  # text JSON; no structured content
        return ToolFailure(self.entry["text"])


def reads(name: str) -> AtlassianReads:
    return AtlassianReads(Recorded(name), AtlassianSettings(SITE, PREFIX), sleep=lambda _: None)


def test_every_result_is_text_json_in_a_data_envelope() -> None:
    for name, entry in RECORDED["responses"].items():
        if entry["type"] == "success":
            assert set(json.loads(entry["text"])) == {"data"}, name


def test_account_id_and_site() -> None:
    assert (
        reads("user_info").account_id(CREDENTIAL) == "712020:00000000-0000-4000-8000-000000000001"
    )
    assert reads("accessible_resources").accessible_site_ids(CREDENTIAL) == {SITE}


def test_a_full_page_is_eligible_evidence_with_its_version() -> None:
    page = reads("page").confluence_read_page(CREDENTIAL, "622760")
    assert page["version"] == 1 and page["title"] == "SEC-05 Encryption and Key Management"
    parsed = parse_page(page["body_html"])
    assert parsed.problems == []  # Confluence's cell <p>s and data-local-id attributes are fine
    assert parsed.metadata is not None and parsed.metadata.policy_id == "SEC-05"
    assert [p.ref for p in parsed.passages] == ["5.1", "5.2", "5.3", "5.4", "5.5", "5.6"]


def test_a_restricted_page_is_a_vendor_refusal_not_consent_or_outage() -> None:
    entry = RECORDED["responses"]["restricted_page"]
    assert classify(ToolFailure(entry["text"]), PREFIX).cls == "upstream_tool_error"
    with pytest.raises(OperationRefused):
        reads("restricted_page").confluence_read_page(CREDENTIAL, "1638401")


def test_a_paused_broker_is_unavailable() -> None:
    entry = RECORDED["responses"]["broker_outage"]
    assert classify(ToolFailure(entry["text"]), PREFIX).cls == "unavailable"


def test_jira_read_requests_only_standard_or_mapped_fields() -> None:
    gateway = Recorded("jira_issue")
    adapter = AtlassianReads(gateway, AtlassianSettings(SITE, PREFIX), sleep=lambda _: None)
    result = adapter.jira_get_request(CREDENTIAL, "SQR-1", ["summary", "reporter", "customer"])
    assert gateway.sent[0]["fields"] == ["reporter", "summary"]  # no unmapped `customer`
    assert (
        result["fields"]["summary"] == "Security questionnaire for Larkspur Analytics (synthetic)"
    )
    assert result["fields"]["customer"] is None
