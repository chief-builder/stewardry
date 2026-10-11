"""The real gateway client against an in-process MCP server (S14b, tier: offline).

The same client is exercised against the broker's stand-in stack in
tests/stack/test_gateway_stack.py."""

import datetime as dt
from contextlib import asynccontextmanager
from typing import Any

import httpx2
import pytest
from mcp.client._memory import InMemoryTransport
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel

from stewardry.platform.atlassian.client import (
    ConsentRequested,
    GatewayUnreachable,
    SignInRequired,
    ToolFailure,
    ToolSuccess,
)
from stewardry.platform.atlassian.gateway_errors import classify
from stewardry.platform.atlassian.mcp_client import McpGatewayClient
from stewardry.platform.credentials import GatewayCredential

CONSENT = "http://localhost:8600/v1/authorize/atlassian?txn=opaque-123"
CREDENTIAL = GatewayCredential("token-alice", dt.datetime.now(dt.UTC) + dt.timedelta(hours=1))
SEEN: dict[str, Any] = {}


def gateway() -> MCPServer:
    server = MCPServer("fake-gateway")

    @server.tool()
    def atlassian_atlassianUserInfo() -> dict[str, str]:
        return {"accountId": "557058:alice"}

    @server.tool()
    def atlassian_getJiraIssue(cloudId: str, issueIdOrKey: str) -> str:
        raise ToolError("Issue does not exist or you do not have permission to see it.")

    @server.tool()
    async def connect_atlassian(ctx: Context) -> str:
        answer = await ctx.elicit_url("Connect your Atlassian account to continue.", CONSENT, "e1")
        SEEN["answer"] = answer.action
        if answer.action != "accept":
            raise ToolError("Atlassian was not connected; the request was cancelled")
        return "Atlassian is connected."

    class Reason(BaseModel):
        reason: str

    @server.tool()
    async def asks_for_a_form(ctx: Context) -> str:
        answer = await ctx.elicit("Tell me something", Reason)
        SEEN["form"] = answer.action
        return "done"

    return server


def client(**kwargs: Any) -> McpGatewayClient:
    server = gateway()
    return McpGatewayClient(
        "http://localhost:8500/mcp", connect=lambda http: InMemoryTransport(server), **kwargs
    )


def test_a_tool_result_is_a_success_with_structured_content() -> None:
    outcome = client().call_tool(CREDENTIAL, "atlassian_atlassianUserInfo", {}, interactive=False)
    assert isinstance(outcome, ToolSuccess)
    assert outcome.structured == {"accountId": "557058:alice"}
    assert classify(outcome, CONSENT.split("txn=")[0]).cls == "ok"


def test_a_tool_error_is_a_failure_with_its_text() -> None:
    outcome = client().call_tool(
        CREDENTIAL,
        "atlassian_getJiraIssue",
        {"cloudId": "c", "issueIdOrKey": "SQR-1"},
        interactive=False,
    )
    assert isinstance(outcome, ToolFailure) and "do not have permission" in outcome.text


def test_consent_arrives_as_a_structured_url_elicitation_and_is_declined() -> None:
    SEEN.clear()
    outcome = client().call_tool(CREDENTIAL, "connect_atlassian", {}, interactive=True)
    assert outcome == ConsentRequested(CONSENT)
    assert SEEN["answer"] == "decline"  # the gateway never waits on a browser


def test_a_background_call_never_accepts_even_with_a_driver() -> None:
    SEEN.clear()
    driver_calls: list[str] = []
    outcome = client(consent_driver=lambda url: driver_calls.append(url) or "accept").call_tool(
        CREDENTIAL, "connect_atlassian", {}, interactive=False
    )
    assert outcome == ConsentRequested(CONSENT)
    assert driver_calls == [] and SEEN["answer"] == "decline"


def test_an_interactive_call_with_a_driver_accepts_and_returns_the_result() -> None:
    SEEN.clear()
    outcome = client(consent_driver=lambda url: "accept").call_tool(
        CREDENTIAL, "connect_atlassian", {}, interactive=True
    )
    assert isinstance(outcome, ToolSuccess) and outcome.text == "Atlassian is connected."
    assert SEEN["answer"] == "accept"


def test_form_elicitations_are_always_declined() -> None:
    SEEN.clear()
    client(consent_driver=lambda url: "accept").call_tool(
        CREDENTIAL, "asks_for_a_form", {}, interactive=True
    )
    assert SEEN["form"] == "decline"


def _failing(exc: Exception) -> Any:
    @asynccontextmanager
    async def connect(http: httpx2.AsyncClient) -> Any:
        raise exc
        yield  # pragma: no cover

    return connect


def test_http_401_means_sign_in_again() -> None:
    request = httpx2.Request("POST", "http://localhost:8500/mcp")
    response = httpx2.Response(401, request=request)
    gateway_401 = httpx2.HTTPStatusError("401", request=request, response=response)
    outcome = McpGatewayClient("u", connect=_failing(gateway_401)).call_tool(
        CREDENTIAL, "atlassian_atlassianUserInfo", {}, interactive=False
    )
    assert outcome == SignInRequired()


@pytest.mark.parametrize(
    "exc",
    [
        httpx2.ConnectError("refused"),
        httpx2.ReadTimeout("slow"),
        ExceptionGroup("tg", [httpx2.ConnectError("refused")]),
        TimeoutError(),
    ],
)
def test_an_unreachable_gateway_is_never_consent(exc: Exception) -> None:
    outcome = McpGatewayClient("u", connect=_failing(exc)).call_tool(
        CREDENTIAL, "atlassian_atlassianUserInfo", {}, interactive=False
    )
    assert isinstance(outcome, GatewayUnreachable)
    assert classify(outcome, CONSENT.split("txn=")[0]).cls == "unavailable"


def test_other_http_errors_are_unreachable_not_consent() -> None:
    request = httpx2.Request("POST", "http://localhost:8500/mcp")
    error = httpx2.HTTPStatusError("503", request=request, response=httpx2.Response(503))
    outcome = McpGatewayClient("u", connect=_failing(error)).call_tool(
        CREDENTIAL, "atlassian_atlassianUserInfo", {}, interactive=False
    )
    assert outcome == GatewayUnreachable("gateway answered HTTP 503")


def test_unexpected_errors_are_not_swallowed() -> None:
    with pytest.raises(ExceptionGroup):
        McpGatewayClient("u", connect=_failing(ExceptionGroup("x", [KeyError("bug")]))).call_tool(
            CREDENTIAL, "atlassian_atlassianUserInfo", {}, interactive=False
        )


def test_the_bearer_token_is_the_acting_users_own() -> None:
    seen: list[str] = []

    @asynccontextmanager
    async def connect(http: httpx2.AsyncClient) -> Any:
        seen.append(http.headers["authorization"])
        raise httpx2.ConnectError("stop")
        yield  # pragma: no cover

    McpGatewayClient("u", connect=connect).call_tool(
        CREDENTIAL, "atlassian_atlassianUserInfo", {}, interactive=False
    )
    assert seen == ["Bearer token-alice"]
