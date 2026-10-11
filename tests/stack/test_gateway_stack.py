"""Gateway contract tests against the broker's stand-in stack (S14b, tier: stack).

Starts the broker's gateway-profile stack from the pinned checkout (read-only
use), bootstraps Stewardry's client and personas into its hub (S14), signs
personas in to Stewardry, and runs Stewardry's real MCP client and adapter
against the gateway. The Atlassian upstream is the broker's stand-in
(mock-mcp); its responses are canned, so these tests check the wire,
identity, consent, and error contract, not Atlassian's data shapes (S15).

Personas are bootstrapped users with generated passwords; the broker's
alice and bob are not used (their passwords live in the broker's test code,
which Stewardry does not copy, ADR 0008).
"""

import base64
import datetime as dt
import json
import logging
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from stewardry.platform import hub_bootstrap as hb
from stewardry.platform import registry
from stewardry.platform.access import AccessMapping, apply_access_mapping
from stewardry.platform.artifacts import ArtifactStore
from stewardry.platform.atlassian.adapter import AtlassianReads, AtlassianSettings
from stewardry.platform.atlassian.client import (
    ConsentRequested,
    GatewayUnreachable,
    SignInRequired,
    ToolFailure,
    ToolSuccess,
)
from stewardry.platform.atlassian.gateway_errors import classify, classify_text
from stewardry.platform.atlassian.mcp_client import McpGatewayClient
from stewardry.platform.audit import AuditLog
from stewardry.platform.catalog import Catalogs
from stewardry.platform.credentials import CredentialStore, GatewayCredential
from stewardry.platform.db import migrate
from stewardry.platform.db.engine import make_engine
from stewardry.platform.db.schema import audit_events
from stewardry.platform.execution import Executor, RunStopped, ToolCall
from stewardry.platform.grants import authorize_stage, submit_task
from stewardry.platform.policy import load_agent_policy, load_baseline
from stewardry.web.app import Services, create_app
from stewardry.web.hub import HubClient
from stewardry.web.sessions import SessionStore
from stewardry.web.settings import WebSettings
from tests.stack.browser import Browser
from tests.stack.conftest import (
    BROKER_URL,
    GATEWAY_URL,
    HUB_ISSUER,
    MOCK_MCP,
    ROOT,
    BrokerStack,
    _docker,
)

pytestmark = pytest.mark.stack

APP = "http://localhost:8700"
CONSENT_PREFIX = f"{BROKER_URL}/v1/authorize/atlassian?txn="
STAND_IN_CLOUD_ID = "00000000-0000-4000-8000-00000000a71a"  # mock-mcp's fixed site
USER_INFO = "atlassian_atlassianUserInfo"
ADDITIONS = json.loads((ROOT / "deploy/hub/stewardry-realm-additions.json").read_text())
LOG_RECORDS: list[str] = []


class _Capture(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        LOG_RECORDS.append(self.format(record))


@dataclass
class Stack:
    engine: Engine
    log: AuditLog
    subjects: dict[str, str]
    passwords: dict[str, str]
    credentials: dict[str, GatewayCredential]

    def drive_consent(self, username: str) -> Any:
        """A browser-side consent for this persona, like the broker's own tests."""

        def driver(url: str) -> str:
            browser = Browser()
            try:
                page = browser.request("GET", url)  # broker -> hub login page
                done = browser.login(page, username, self.passwords[username])
                assert done.status_code == 302
                final = browser.request("GET", str(done.url.join(done.headers["location"])))
                assert final.status_code == 200, final.text[:200]
            finally:
                browser.close()
            return "accept"

        return driver


def _mapping(subjects: dict[str, str]) -> AccessMapping:
    """The proposed mapping, with roles chosen for these tests (Stewardry's own)."""
    data = hb.proposed_mapping(ROOT / "deploy/access/access-mapping.example.yaml", subjects)
    roles = {
        "legal": (["grc"], ["larkspur-synthetic"]),
        "owner": (["grc", "engagement_owner"], ["larkspur-synthetic"]),
        "sme": (["sales"], ["larkspur-synthetic"]),
    }
    for user in data["users"]:
        if user["username"] in roles:
            user["roles"], user["customers"] = roles[user["username"]]
    return AccessMapping.model_validate(data)


def _sign_in(app: TestClient, username: str, password: str) -> None:
    start = app.get("/login")
    browser = Browser()
    try:
        page = browser.request("GET", start.headers["location"])
        done = browser.login(page, username, password)
    finally:
        browser.close()
    assert done.status_code == 302, "the hub refused the sign-in"
    location = httpx2.URL(done.headers["location"])
    assert app.get("/auth/callback", params=dict(location.params)).status_code == 303


@pytest.fixture(scope="module")
def stack(broker_stack: BrokerStack, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    handler = _Capture()
    logging.getLogger().addHandler(handler)
    previous = logging.getLogger().level
    logging.getLogger().setLevel(logging.DEBUG)
    tmp = tmp_path_factory.mktemp("gateway")
    with httpx2.Client(timeout=30) as http:
        admin = hb.KeycloakAdmin("http://localhost:8180", "mcp", http)
        admin.login(HUB_ISSUER, "admin", broker_stack.admin_password)
        result = hb.bootstrap(admin, HUB_ISSUER, ADDITIONS, tmp / ".secrets.env")
    entries = hb.read_secrets(tmp / ".secrets.env")
    passwords = {u: entries[hb.persona_entry(u)] for u in result.created_users}

    engine = make_engine(tmp / "db.sqlite")
    migrate.upgrade(engine)
    log = AuditLog()
    subjects = result.subjects
    apply_access_mapping(engine, log, subjects["platform"], _mapping(subjects), bootstrap=True)
    registry.apply_policy(
        engine,
        log,
        subjects["platform"],
        load_agent_policy(ROOT / "policies/agents/questionnaire-agent.yaml"),
        load_baseline(ROOT / "policies/platform-baseline.yaml"),
    )
    registry.transition_status(
        engine, log, subjects["owner"], "questionnaire-agent", "build", "docs/brief.md"
    )

    store = CredentialStore()
    settings = WebSettings(APP, HUB_ISSUER, "stewardry", entries[hb.CLIENT_ENTRY])
    with httpx2.Client(timeout=30) as http:
        hub = HubClient(
            HUB_ISSUER, "stewardry", settings.hub_client_secret, settings.redirect_uri, http
        )
        services = Services(
            settings, engine, hub, log, SessionStore(), store, ArtifactStore(tmp / "artifacts")
        )
        for username, password in passwords.items():
            app = TestClient(create_app(services), base_url=APP, follow_redirects=False)
            _sign_in(app, username, password)
    now = dt.datetime.now(dt.UTC)
    credentials = {u: store.get(subjects[u], now) for u in passwords}
    assert all(credentials.values())
    yield Stack(engine, log, subjects, passwords, credentials)  # type: ignore[arg-type]
    logging.getLogger().removeHandler(handler)
    logging.getLogger().setLevel(previous)
    engine.dispose()


def client(**kwargs: Any) -> McpGatewayClient:
    return McpGatewayClient(GATEWAY_URL, **kwargs)


def reads(gateway: McpGatewayClient | None = None) -> AtlassianReads:
    settings = AtlassianSettings(STAND_IN_CLOUD_ID, CONSENT_PREFIX)
    return AtlassianReads(gateway or client(), settings, sleep=lambda _: None)


def cls(outcome: Any) -> str:
    return classify(outcome, CONSENT_PREFIX).cls


@pytest.fixture
def paused() -> Iterator[Any]:
    names: list[str] = []

    def pause(name: str) -> None:
        _docker("pause", name)
        names.append(name)

    yield pause
    for name in names:
        _docker("unpause", name, check=False)
    time.sleep(2)


# ------------------------------------------------------------- sign-in


def test_personas_sign_in_and_hold_gateway_tokens(stack: Stack) -> None:
    for username, credential in stack.credentials.items():
        payload = credential.access_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        assert claims["sub"] == stack.subjects[username]
        assert "http://localhost:8500/mcp" in claims["aud"] and claims["azp"] == "stewardry"


def test_a_token_the_gateway_rejects_means_sign_in_again(stack: Stack) -> None:
    forged = GatewayCredential("not-a-token", dt.datetime.now(dt.UTC) + dt.timedelta(hours=1))
    outcome = client().call_tool(forged, USER_INFO, {}, interactive=False)
    assert outcome == SignInRequired() and cls(outcome) == "needs_signin"


# ------------------------------------------------------------- consent


def test_an_unconnected_user_gets_structured_consent(stack: Stack) -> None:
    for username in ("legal", "platform"):
        outcome = client().call_tool(stack.credentials[username], USER_INFO, {}, interactive=False)
        assert isinstance(outcome, ConsentRequested)
        assert outcome.url.startswith(CONSENT_PREFIX)
        assert cls(outcome) == "needs_consent"


def test_the_gateways_decline_text_is_in_the_catalog(stack: Stack) -> None:
    raw = client().raw_call(stack.credentials["legal"], USER_INFO, {}, interactive=False)
    assert raw.elicited_urls and raw.result is not None and raw.result.is_error
    text = "".join(getattr(b, "text", "") for b in raw.result.content)
    assert classify_text(text).cls == "consent_declined"


def test_connect_shows_the_link_and_the_users_browser_finishes_consent(stack: Stack) -> None:
    link = reads().connect(stack.credentials["legal"])
    assert link is not None and link.url.startswith(CONSENT_PREFIX)
    stack.drive_consent("legal")(link.url)  # the user's own browser, after the decline
    outcome = client().call_tool(stack.credentials["legal"], USER_INFO, {}, interactive=False)
    assert isinstance(outcome, ToolSuccess), outcome
    assert reads().connect(stack.credentials["legal"]) is None  # already connected
    assert STAND_IN_CLOUD_ID in reads().accessible_site_ids(stack.credentials["legal"])


def test_an_accepted_elicitation_completes_in_the_same_call(stack: Stack) -> None:
    gateway = client(consent_driver=stack.drive_consent("sme"))
    outcome = gateway.call_tool(stack.credentials["sme"], "connect_atlassian", {}, interactive=True)
    assert isinstance(outcome, ToolSuccess), outcome


def test_accepting_without_finishing_times_out(stack: Stack) -> None:
    gateway = client(consent_driver=lambda url: "accept")
    outcome = gateway.call_tool(stack.credentials["auditor"], USER_INFO, {}, interactive=True)
    assert isinstance(outcome, ToolFailure) and cls(outcome) == "consent_timeout"


# ---------------------------------------------------- runs and identity


def test_a_background_run_pauses_without_the_link_and_users_never_mix(stack: Stack) -> None:
    engine, log = stack.engine, stack.log
    store = CredentialStore()
    later = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
    for username in ("legal", "owner"):
        store.put(
            f"s-{username}",
            stack.subjects[username],
            GatewayCredential(stack.credentials[username].access_token, later),
        )
    catalogs = Catalogs(ROOT)
    executor = Executor(
        engine,
        log,
        store,
        load_baseline(ROOT / "policies/platform-baseline.yaml"),
        reads().tool_specs(catalogs),
        catalogs,
    )
    httpx2.post(f"{MOCK_MCP}/_test/reset")

    def run_as(authorizer: str, key: str) -> Any:
        task = submit_task(
            engine, log, stack.subjects["sme"], "questionnaire-agent", "larkspur-synthetic", key
        )
        authorize_stage(engine, log, stack.subjects[authorizer], task)
        return executor.start_run(task, "drafting", "connected")

    connected = run_as("legal", "stack-legal")
    result = executor.call(connected, ToolCall("confluence.read_page", {"page_id": "105"}))
    assert result.ok or result.error  # reached the stand-in as legal (canned data)
    calls = httpx2.get(f"{MOCK_MCP}/_test/state").json()
    fingerprints = {
        c.get("token_fp") for c in calls.get("calls", calls if isinstance(calls, list) else [])
    }

    unconnected = run_as("owner", "stack-owner")
    with pytest.raises(RunStopped) as stopped:
        executor.call(unconnected, ToolCall("confluence.read_page", {"page_id": "105"}))
    assert stopped.value.kind == "pause"
    after = httpx2.get(f"{MOCK_MCP}/_test/state").json()
    after_fps = {
        c.get("token_fp") for c in after.get("calls", after if isinstance(after, list) else [])
    }
    assert after_fps == fingerprints  # owner's run never reached the upstream as anyone

    with engine.connect() as conn:
        audit = " ".join(conn.execute(select(audit_events.c.details_json)).scalars())
    assert "/v1/authorize" not in audit and "txn=" not in audit  # links never kept


# ------------------------------------------------------------ outages


@pytest.mark.parametrize(
    ("container", "expected"),
    [("vtb-broker-kc", "unavailable"), ("vtb-mock-mcp", "unavailable")],
)
def test_an_outage_is_never_reported_as_consent(
    stack: Stack, paused: Any, container: str, expected: str
) -> None:
    paused(container)
    outcome = client().call_tool(stack.credentials["sme"], USER_INFO, {}, interactive=False)
    assert cls(outcome) == expected, outcome


def test_a_hub_outage_is_never_reported_as_consent(stack: Stack, paused: Any) -> None:
    paused("vtb-keycloak")
    outcome = client().call_tool(stack.credentials["sme"], USER_INFO, {}, interactive=False)
    assert cls(outcome) in ("unavailable", "config_error"), outcome
    assert not isinstance(outcome, ConsentRequested)


def test_an_unreachable_gateway_is_unavailable(stack: Stack, paused: Any) -> None:
    paused("vtb-mcp-gateway")
    gateway = client(timeout_s=3, call_timeout_s=5)
    outcome = gateway.call_tool(stack.credentials["sme"], USER_INFO, {}, interactive=False)
    assert isinstance(outcome, GatewayUnreachable) and cls(outcome) == "unavailable"


# ------------------------------------------------------- disconnecting


def test_after_a_disconnect_consent_is_needed_again(stack: Stack) -> None:
    raw = client().raw_call(
        stack.credentials["legal"], "disconnect_atlassian", {}, interactive=False
    )
    assert raw.result is not None
    outcome = client().call_tool(stack.credentials["legal"], USER_INFO, {}, interactive=False)
    assert isinstance(outcome, ConsentRequested) and cls(outcome) == "needs_consent"


def test_a_pending_revoke_is_not_consent(stack: Stack, paused: Any) -> None:
    paused("vtb-mock-vendor")
    client().raw_call(stack.credentials["sme"], "disconnect_atlassian", {}, interactive=False)
    outcome = client().call_tool(stack.credentials["sme"], USER_INFO, {}, interactive=False)
    assert cls(outcome) in ("disconnect_pending", "unavailable"), outcome
    assert not isinstance(outcome, ConsentRequested)


# ------------------------------------------------------------ secrets


def test_no_token_material_in_stewardry_logs_audit_or_stack_logs(stack: Stack) -> None:
    tokens = [c.access_token for c in stack.credentials.values()]
    logs = "\n".join(LOG_RECORDS)
    with stack.engine.connect() as conn:
        audit = " ".join(conn.execute(select(audit_events.c.details_json)).scalars())
    for token in tokens:
        assert token not in logs and token not in audit
    names = _docker("ps", "--filter", "name=^vtb-", "--format", "{{.Names}}").stdout.split()
    for name in names:
        out = subprocess.run(  # noqa: S603 - fixed docker arguments
            ["docker", "logs", "--since", "30m", name],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        for token in tokens:
            assert token not in out.stdout + out.stderr, f"a Stewardry token is in {name}'s log"
