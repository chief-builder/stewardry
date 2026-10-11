"""A fake of the broker's MCP gateway, as an MCP client that always advertises
URL elicitation sees it. Error texts are the pinned gateway's own templates."""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from stewardry.platform.atlassian.client import (
    ConsentRequested,
    GatewayOutcome,
    ToolFailure,
    ToolSuccess,
)
from stewardry.platform.credentials import GatewayCredential

CLOUD_ID = "1b2c3d4e-5f60-4718-9a2b-3c4d5e6f7081"
BROKER = "http://localhost:8600"
CONSENT_PREFIX = f"{BROKER}/v1/authorize/atlassian?txn="


def gateway_text(template: str, *values: str) -> str:
    """Fill a pinned gateway template ("{}" holes) the way the gateway would."""
    return template.replace("{}", "%s") % values


@dataclass
class Call:
    name: str
    arguments: dict[str, Any]
    token: str
    interactive: bool


@dataclass
class FakeGateway:
    connected: set[str] = field(default_factory=set)  # tokens whose user connected Atlassian
    forced: list[GatewayOutcome] = field(default_factory=list)
    restricted_pages: set[str] = field(default_factory=set)
    calls: list[Call] = field(default_factory=list)

    def call_tool(
        self,
        credential: GatewayCredential,
        name: str,
        arguments: Mapping[str, Any],
        *,
        interactive: bool,
    ) -> GatewayOutcome:
        self.calls.append(Call(name, dict(arguments), credential.access_token, interactive))
        if self.forced:
            return self.forced.pop(0)
        if credential.access_token not in self.connected:
            return ConsentRequested(f"{CONSENT_PREFIX}opaque-{len(self.calls)}")
        return self._tool(name, arguments)

    def _tool(self, name: str, arguments: Mapping[str, Any]) -> GatewayOutcome:
        if name == "connect_atlassian":
            return ToolSuccess(None, "Atlassian is connected.")
        if name == "atlassian_getJiraIssue":
            return ToolSuccess(
                None,
                json.dumps(
                    {
                        "key": arguments["issueIdOrKey"],
                        "fields": {
                            "summary": "Security questionnaire for Larkspur",
                            "reporter": "Alice",
                            "customfield_10042": "larkspur-synthetic",
                            "description": "internal notes that must not leak",
                            "attachment": [{"filename": "secret.pdf"}],
                        },
                    }
                ),
            )
        if name == "atlassian_searchConfluence":
            return ToolSuccess(
                {
                    "results": [
                        {
                            "content": {
                                "id": "101",
                                "title": "SEC-1 Access control",
                                "version": {"number": 4},
                            }
                        },
                        {
                            "content": {
                                "id": "102",
                                "title": "SEC-2 Encryption",
                                "version": {"number": 2},
                            }
                        },
                        {
                            "content": {
                                "id": "999",
                                "title": "HR salary bands",
                                "version": {"number": 9},
                            }
                        },
                    ]
                },
                "",
            )
        if name == "atlassian_getConfluenceContent":
            page = arguments["content_id"]
            if page in self.restricted_pages:
                return ToolFailure("Content not found or you do not have permission to view it")
            return ToolSuccess(
                None,
                json.dumps(
                    {
                        "id": page,
                        "title": f"Policy {page}",
                        "version": {"number": 3},
                        "body": {
                            "value": f"<h1>Policy {page}</h1><p>Data is encrypted at rest.</p>"
                        },
                    }
                ),
            )
        if name == "atlassian_getAccessibleAtlassianResources":
            return ToolSuccess(
                [
                    {
                        "id": CLOUD_ID,
                        "url": "https://example-site.atlassian.net",
                        "name": "example-site",
                    }
                ],
                "",
            )
        if name == "atlassian_atlassianUserInfo":
            return ToolSuccess({"accountId": "557058:fake-account"}, "")
        return ToolFailure(f"unknown tool {name}")
