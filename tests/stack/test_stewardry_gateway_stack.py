"""S14c design check: a Stewardry-only gateway instance (ADR 0009, option 2).

A second gateway from the broker's pinned image, configured from Stewardry's
repo: only the five reviewed Atlassian tools, its own audience scope
(`stewardry-gateway`), and its own token-exchange client. Stack tier only;
no connected use in Milestone 1. The findings feed the Milestone 2 go/no-go
(docs/plan/s14c-stewardry-gateway.md).
"""

import datetime as dt
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import Engine

from stewardry.platform import hub_bootstrap as hb
from stewardry.platform.access import apply_access_mapping
from stewardry.platform.artifacts import ArtifactStore
from stewardry.platform.atlassian.client import ConsentRequested, SignInRequired, ToolSuccess
from stewardry.platform.atlassian.mcp_client import McpGatewayClient
from stewardry.platform.audit import AuditLog
from stewardry.platform.credentials import CredentialStore, GatewayCredential
from stewardry.platform.db import migrate
from stewardry.platform.db.engine import make_engine
from stewardry.web.app import Services, create_app
from stewardry.web.hub import HubClient
from stewardry.web.sessions import SessionStore
from stewardry.web.settings import WebSettings
from tests.stack.browser import Browser
from tests.stack.conftest import BROKER, GATEWAY_URL, HUB_ISSUER, MOCK_MCP, StandIn, _compose
from tests.stack.test_gateway_stack import APP, _mapping, _sign_in

pytestmark = pytest.mark.stack

STEWARDRY_GATEWAY = "http://localhost:8510/mcp"
REVIEWED = {
    "getJiraIssue",
    "searchConfluence",
    "getConfluenceContent",
    "atlassianUserInfo",
    "getAccessibleAtlassianResources",
}
SNAPSHOT = BROKER / "src/mcp_gateway/snapshots/atlassian.json"


def _token(stand_in: StandIn, engine: Engine, tmp: Path, username: str, scope: str) -> str:
    """Sign a persona in to Stewardry, asking for `scope`; returns the access token."""
    store = CredentialStore()
    settings = WebSettings(APP, HUB_ISSUER, "stewardry", stand_in.entries[hb.CLIENT_ENTRY], scope)
    with httpx2.Client(timeout=30) as http:
        hub = HubClient(
            HUB_ISSUER, "stewardry", settings.hub_client_secret, settings.redirect_uri, http, scope
        )
        services = Services(
            settings, engine, hub, AuditLog(), SessionStore(), store, ArtifactStore(tmp / "a")
        )
        app = TestClient(create_app(services), base_url=APP, follow_redirects=False)
        _sign_in(app, username, stand_in.passwords[username])
    credential = store.get(stand_in.subjects[username], dt.datetime.now(dt.UTC))
    assert credential is not None
    return credential.access_token


@pytest.fixture(scope="module")
def gateway(
    stand_in: StandIn, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[dict[str, Any]]:
    secret = stand_in.entries[hb.GATEWAY_CLIENT_ENTRY]
    up = _compose(
        stand_in.stack.admin_password,
        "up",
        "-d",
        "--build",
        "--wait",
        "stewardry-gateway",
        profiles=("gateway", "stewardry"),
        STEWARDRY_GATEWAY_CLIENT_SECRET=secret,
    )
    if up.returncode != 0:
        pytest.fail("the Stewardry-only gateway did not start:\n" + up.stderr[-2000:])
    tmp = tmp_path_factory.mktemp("s14c")
    engine = make_engine(tmp / "db.sqlite")
    migrate.upgrade(engine)
    apply_access_mapping(
        engine,
        AuditLog(),
        stand_in.subjects["platform"],
        _mapping(stand_in.subjects),
        bootstrap=True,
    )
    tokens = {
        "own": _token(stand_in, engine, tmp, "auditor", "openid stewardry-gateway"),
        "shared": _token(stand_in, engine, tmp, "auditor", "openid mcp-gateway"),
    }
    yield {"tokens": tokens, "stand_in": stand_in}
    engine.dispose()


def credential(token: str) -> GatewayCredential:
    return GatewayCredential(token, dt.datetime.now(dt.UTC) + dt.timedelta(hours=1))


def list_tools(url: str, token: str) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {token}"}
        async with (
            httpx2.AsyncClient(headers=headers, timeout=30) as http,
            streamable_http_client(url, http_client=http) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            listed = await session.list_tools()
        return {t.name: t for t in listed.tools}

    return anyio.run(run)


def drive_consent(stand_in: StandIn, username: str) -> Any:
    def driver(url: str) -> str:
        browser = Browser()
        try:
            page = browser.request("GET", url)
            done = browser.login(page, username, stand_in.passwords[username])
            final = browser.request("GET", str(done.url.join(done.headers["location"])))
            assert final.status_code == 200
        finally:
            browser.close()
        return "accept"

    return driver


def test_only_the_five_reviewed_tools_are_listed(gateway: dict[str, Any]) -> None:
    own = list_tools(STEWARDRY_GATEWAY, gateway["tokens"]["own"])
    names = {n.removeprefix("atlassian_") for n in own if n.startswith("atlassian_")}
    assert names == REVIEWED
    assert set(own) - {f"atlassian_{n}" for n in REVIEWED} == {
        "connect_atlassian",
        "disconnect_atlassian",
    }
    shared = list_tools(GATEWAY_URL, gateway["tokens"]["shared"])
    assert "atlassian_executeRead" in shared and "atlassian_discover" in shared  # the contrast


def test_tool_schemas_are_the_bundled_snapshots(gateway: dict[str, Any]) -> None:
    snapshot = {t["name"]: t for t in json.loads(SNAPSHOT.read_text())["tools"]}
    own = list_tools(STEWARDRY_GATEWAY, gateway["tokens"]["own"])
    for name in REVIEWED:
        listed = own[f"atlassian_{name}"].model_dump(by_alias=True, exclude_none=True)
        assert listed["inputSchema"] == snapshot[name]["inputSchema"], name


def test_token_exchange_works_for_the_new_audience(gateway: dict[str, Any]) -> None:
    token = credential(gateway["tokens"]["own"])
    client = McpGatewayClient(
        STEWARDRY_GATEWAY, consent_driver=drive_consent(gateway["stand_in"], "auditor")
    )
    connected = client.call_tool(token, "connect_atlassian", {}, interactive=True)
    assert isinstance(connected, ToolSuccess), connected
    outcome = McpGatewayClient(STEWARDRY_GATEWAY).call_tool(
        token, "atlassian_atlassianUserInfo", {}, interactive=False
    )
    assert isinstance(outcome, ToolSuccess), outcome


def test_execute_read_and_discover_cannot_be_called(gateway: dict[str, Any]) -> None:
    httpx2.post(f"{MOCK_MCP}/_test/reset")
    token = credential(gateway["tokens"]["own"])
    for tool in (
        "atlassian_executeRead",
        "atlassian_discover",
        "atlassian_searchJiraIssuesUsingJql",
    ):
        raw = McpGatewayClient(STEWARDRY_GATEWAY).raw_call(token, tool, {}, interactive=False)
        refused = raw.error is not None or (raw.result is not None and raw.result.is_error)
        assert refused, tool
    state = httpx2.get(f"{MOCK_MCP}/_test/state").json()
    calls = state.get("calls", state if isinstance(state, list) else [])
    assert not [c for c in calls if c.get("tool") in ("executeRead", "discover")]


def test_tokens_for_one_gateway_do_not_work_on_the_other(gateway: dict[str, Any]) -> None:
    own, shared = (credential(gateway["tokens"][k]) for k in ("own", "shared"))
    on_shared = McpGatewayClient(GATEWAY_URL).call_tool(
        own, "atlassian_atlassianUserInfo", {}, interactive=False
    )
    on_own = McpGatewayClient(STEWARDRY_GATEWAY).call_tool(
        shared, "atlassian_atlassianUserInfo", {}, interactive=False
    )
    assert on_shared == SignInRequired() and on_own == SignInRequired()
    assert not isinstance(on_shared, ConsentRequested)
