"""Connected compatibility verification (S15, tier: connected).

Run manually, with the real stack up and the setup checklist done:

    make test-connected

It starts Stewardry's web app on http://localhost:8700 against the real hub
and gateway. Personas sign in in their own private browser windows and click
Connect Atlassian. The checks then run under each persona's own identity,
and a dated report labeled `connected` is written to docs/connected/<date>/,
together with the response shapes that close gaps G7 and G8.

Never recorded: tokens, credentials, consent links (their transaction
values are removed), and passwords. Stewardry never writes to Atlassian.
The expert's page edit is made by a person, in Confluence's UI.
"""

import datetime as dt
import hashlib
import json
import os
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import uvicorn
import yaml

from stewardry.platform import registry
from stewardry.platform.access import apply_access_mapping, load_access_mapping
from stewardry.platform.artifacts import ArtifactStore
from stewardry.platform.atlassian.adapter import (
    AtlassianReads,
    AtlassianSettings,
    ConnectRefused,
)
from stewardry.platform.atlassian.client import (
    ConsentRequested,
    GatewayOutcome,
    ToolFailure,
    ToolSuccess,
)
from stewardry.platform.atlassian.gateway_errors import classify
from stewardry.platform.atlassian.mcp_client import McpGatewayClient
from stewardry.platform.audit import AuditLog, verify
from stewardry.platform.catalog import Catalogs
from stewardry.platform.credentials import CredentialStore, GatewayCredential
from stewardry.platform.db import migrate
from stewardry.platform.db.engine import make_engine
from stewardry.platform.evidence import parse_page
from stewardry.platform.execution import (
    Executor,
    HandlerStop,
    OperationRefused,
    RunStopped,
    ToolCall,
)
from stewardry.platform.grants import authorize_stage, submit_task
from stewardry.platform.policy import load_agent_policy, load_baseline
from stewardry.web.app import Services, create_app
from stewardry.web.hub import HubClient, HubError
from stewardry.web.sessions import SessionStore
from stewardry.web.settings import WebSettings

pytestmark = [
    pytest.mark.connected,
    pytest.mark.skipif(
        os.environ.get("STEWARDRY_CONNECTED") != "1",
        reason="connected tier: run manually with `make test-connected`",
    ),
]

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "deploy/connected/connected.yaml"
MAPPING = ROOT / "deploy/hub/proposed-access-mapping.yaml"
BROKER = Path(os.environ.get("STEWARDRY_BROKER_CHECKOUT", ROOT.parent / "vendor-token-broker"))
APP = "http://localhost:8700"
WAIT_FOR_PEOPLE = dt.timedelta(minutes=30)


def _say(text: str) -> None:
    print(f"\n>>> {text}", flush=True)


def _redact_link(text: str) -> str:
    """Consent links carry a one-time transaction value; keep only the path."""
    head, _, _ = text.partition("txn=")
    return head + "txn=[removed]" if "txn=" in text else text


def _outcome(outcome: GatewayOutcome) -> dict[str, Any]:
    if isinstance(outcome, ToolSuccess):
        return {"type": "success", "structured": outcome.structured, "text": outcome.text}
    if isinstance(outcome, ToolFailure):
        return {"type": "tool_error", "text": outcome.text}
    if isinstance(outcome, ConsentRequested):
        return {"type": "consent", "url": _redact_link(outcome.url)}
    return {"type": type(outcome).__name__}


@dataclass
class Recorder:
    """Wraps the real client; keeps each call's tool, arguments, and response."""

    inner: McpGatewayClient
    calls: list[dict[str, Any]] = field(default_factory=list)
    persona: str = ""

    def call_tool(
        self,
        credential: GatewayCredential,
        name: str,
        arguments: Mapping[str, Any],
        *,
        interactive: bool,
    ) -> GatewayOutcome:
        outcome = self.inner.call_tool(credential, name, arguments, interactive=interactive)
        self.calls.append(
            {
                "persona": self.persona,
                "tool": name,
                "arguments": dict(arguments),
                "response": _outcome(outcome),
            }
        )
        return outcome


@dataclass
class Session:
    settings: WebSettings
    config: dict[str, Any]
    credentials: CredentialStore
    subjects: dict[str, str]  # persona role -> hub sub
    recorder: Recorder
    reads: AtlassianReads
    report: dict[str, Any]
    out: Path
    engine: Any
    log: AuditLog
    catalogs: Catalogs

    def credential(self, role: str) -> GatewayCredential:
        found = self.credentials.get(self.subjects[role], dt.datetime.now(dt.UTC))
        assert found is not None, f"{role} is not signed in"
        return found

    def as_(self, role: str) -> AtlassianReads:
        self.recorder.persona = role
        return self.reads


@pytest.fixture(scope="module")
def session() -> Iterator[Session]:
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    settings = WebSettings.from_environment()
    mapping = load_access_mapping(MAPPING)
    by_name = {u.username: u for u in mapping.users}
    subjects = {role: by_name[name].sub for role, name in config["personas"].items()}
    platform = next(u.sub for u in mapping.users if "platform_engineer" in u.roles)
    owner = next(u.sub for u in mapping.users if "engagement_owner" in u.roles)

    tmp = Path(tempfile.mkdtemp(prefix="stewardry-connected-"))
    engine = make_engine(tmp / "db.sqlite")
    migrate.upgrade(engine)
    log = AuditLog()
    apply_access_mapping(engine, log, platform, mapping, bootstrap=True)
    registry.apply_policy(
        engine,
        log,
        platform,
        load_agent_policy(ROOT / "policies/agents/questionnaire-agent.yaml"),
        load_baseline(ROOT / "policies/platform-baseline.yaml"),
    )
    registry.transition_status(engine, log, owner, "questionnaire-agent", "build", "docs/brief.md")

    field_keys = (
        {"customer": config["jira"]["customer_field"]}
        if config["jira"].get("customer_field")
        else {}
    )
    recorder = Recorder(McpGatewayClient(settings.gateway_url))
    reads = AtlassianReads(
        recorder, AtlassianSettings(config["cloud_id"], settings.consent_url_prefix, field_keys)
    )
    credentials = CredentialStore()
    import httpx2

    http = httpx2.Client(timeout=30)
    hub = HubClient(
        settings.hub_issuer,
        settings.hub_client_id,
        settings.hub_client_secret,
        settings.redirect_uri,
        http,
        settings.hub_scope,
    )
    services = Services(
        settings,
        engine,
        hub,
        log,
        SessionStore(),
        credentials,
        ArtifactStore(tmp / "artifacts"),
        reads,
    )
    stop = threading.Event()

    def keep_fresh() -> None:
        """Renew each persona's credential before it lapses, as the web app does on
        each request. People sign in one at a time, so a browser that has been
        closed would otherwise let its 5-minute hub token expire mid-run."""
        while not stop.wait(30):
            now = dt.datetime.now(dt.UTC)
            for sub in subjects.values():
                for sid in credentials.sessions_of(sub):
                    token = credentials.needs_refresh(sid, now, dt.timedelta(minutes=2))
                    if token is None:
                        continue
                    try:
                        renewed = hub.refresh(token)
                    except HubError:
                        continue  # the hub session ended; that person signs in again
                    credentials.put(
                        sid,
                        sub,
                        GatewayCredential(renewed.access_token, renewed.expires_at),
                        renewed.refresh_token or token,
                    )

    threading.Thread(target=keep_fresh, daemon=True).start()
    server = uvicorn.Server(
        uvicorn.Config(create_app(services), host="127.0.0.1", port=8700, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    today = dt.date.today().isoformat()
    out = ROOT / "docs" / "connected" / today
    out.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "provenance": "connected",
        "date": today,
        "broker_revision": subprocess.run(  # noqa: S603
            ["git", "-C", str(BROKER), "rev-parse", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip(),
        "participants": "1 person operating every role",
        "checks": {},
    }
    catalogs = Catalogs(ROOT, {"policies/catalogs/secpol.yaml": ROOT / config["catalog_file"]})
    yield Session(
        settings, config, credentials, subjects, recorder, reads, report, out, engine, log, catalogs
    )

    with engine.connect() as conn:
        result = verify(conn)
    report["audit_chain"] = {"ok": result.ok, "events": result.events, "head_seq": result.head_seq}
    (out / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True, default=str))
    (out / "responses.json").write_text(
        json.dumps(recorder.calls, indent=2, sort_keys=True, default=str)
    )
    lines = [
        f"# Connected verification, {today}: **connected**",
        "",
        "One person operates every role (participant count: 1).",
        "",
        "| Check | Result |",
        "|---|---|",
    ]
    lines += [f"| {k} | {v.get('result', v)} |" for k, v in report["checks"].items()]
    (out / "report.md").write_text("\n".join(lines) + "\n")
    stop.set()
    server.should_exit = True
    thread.join(timeout=10)
    http.close()
    engine.dispose()


def _wait_until_connected(s: Session, roles: list[str]) -> None:
    names = ", ".join(f"{s.config['personas'][r]} ({r})" for r in roles)
    _say(
        f"Sign in at {APP} as each of: {names}. Use a separate private browser window for "
        "each person. In each window, click 'Connect Atlassian' and approve with that "
        "person's own Atlassian account. Waiting..."
    )
    deadline = dt.datetime.now(dt.UTC) + WAIT_FOR_PEOPLE
    pending = set(roles)
    while pending and dt.datetime.now(dt.UTC) < deadline:
        for role in sorted(pending):
            cred = s.credentials.get(s.subjects[role], dt.datetime.now(dt.UTC))
            if cred is None:
                continue
            try:
                connected = s.as_(role).account_id(cred) is not None
            except HandlerStop, OperationRefused:
                connected = False  # not connected yet (or Atlassian refused): keep waiting
            if connected:
                _say(f"{role} is signed in and connected.")
                pending.discard(role)
        time.sleep(15)
    assert not pending, f"not connected in time: {sorted(pending)}"


def _ask(question: str) -> str:
    _say(question)
    return input("> ").strip()


# ------------------------------------------------------------------ checks


def test_people_sign_in_and_connect(session: Session) -> None:
    _wait_until_connected(session, ["sales", "grc", "legal"])


def test_sites_and_distinct_accounts(session: Session) -> None:
    accounts, sites = {}, {}
    for role in ("sales", "grc", "legal"):
        cred = session.credential(role)
        accounts[role] = session.as_(role).account_id(cred)
        sites[role] = sorted(session.as_(role).accessible_site_ids(cred))
    distinct = len(set(accounts.values())) == len(accounts)
    on_site = all(session.config["cloud_id"] in v for v in sites.values())
    session.report["checks"]["site and account mapping"] = {
        "result": "pass" if distinct and on_site else "FAIL",
        "account_ids": accounts,
        "cloud_ids": sites,
    }
    assert distinct and on_site


def test_sales_reads_the_jira_request(session: Session) -> None:
    policy = load_agent_policy(ROOT / "policies/agents/questionnaire-agent.yaml").policy
    fields = policy.operations["jira.get_request"].fields
    key = session.config["jira"]["request_key"]
    try:
        result = session.as_("sales").jira_get_request(session.credential("sales"), key, fields)
        session.report["checks"]["Jira field read as Sales"] = {"result": "pass", "read": result}
    except Exception as exc:  # recorded for G7; the raw response is in responses.json
        session.report["checks"]["Jira field read as Sales"] = {"result": f"FAIL: {exc}"}
        raise


def test_grc_reads_a_full_page_with_version_and_hash(session: Session) -> None:
    page_id = session.config["pages"]["readable"]
    page = session.as_("grc").confluence_read_page(session.credential("grc"), page_id)
    body = page.get("body_html") or ""
    parsed = parse_page(body)
    session.report["checks"]["full page read as GRC"] = {
        "result": "pass" if not parsed.problems else f"ineligible: {parsed.problems}",
        "version": page.get("version"),
        "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "passages": len(parsed.passages),
    }
    (session.out / "page-body.html").write_text(body)  # synthetic content (G7 fixture)
    assert page.get("body_html")


def test_a_restricted_page_is_denied_to_sales_twice(session: Session) -> None:
    page_id = session.config["pages"]["restricted"]
    vendor = "not denied"
    try:
        session.as_("sales").confluence_read_page(session.credential("sales"), page_id)
    except OperationRefused as exc:
        vendor = f"denied by Atlassian ({exc})"
    # Stewardry's own denial: the Sales intake stage may not read policy pages at all.
    store = CredentialStore()
    later = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
    store.put(
        "s-sales",
        session.subjects["sales"],
        GatewayCredential(session.credential("sales").access_token, later),
    )
    catalogs = session.catalogs
    executor = Executor(
        session.engine,
        session.log,
        store,
        load_baseline(ROOT / "policies/platform-baseline.yaml"),
        session.reads.tool_specs(catalogs),
        catalogs,
    )
    task = submit_task(
        session.engine,
        session.log,
        session.subjects["sales"],
        "questionnaire-agent",
        "larkspur-synthetic",
        "connected-1",
        "SQR-1",
    )
    run = executor.start_run(task, "intake", "connected")
    result = executor.call(run, ToolCall("confluence.read_page", {"page_id": page_id}))
    stewardry = "denied by Stewardry" if not result.ok else "not denied"
    session.report["checks"]["restricted page denied to Sales"] = {
        "result": "pass" if vendor.startswith("denied") and not result.ok else "FAIL",
        "atlassian": vendor,
        "stewardry": f"{stewardry} ({result.error})",
    }
    assert vendor.startswith("denied") and not result.ok


def test_an_expert_edit_changes_version_and_hash(session: Session) -> None:
    page_id = session.config["pages"]["edited"]
    grc = session.as_("grc")
    before = grc.confluence_read_page(session.credential("grc"), page_id)
    _ask(
        "As the expert's Atlassian account (never connected to Stewardry), edit the page "
        f"{page_id} in Confluence's UI: change one passage's wording and publish. "
        "Press Enter when it is saved."
    )
    after = grc.confluence_read_page(session.credential("grc"), page_id)
    digest = [
        hashlib.sha256((p.get("body_html") or "").encode()).hexdigest() for p in (before, after)
    ]
    changed = digest[0] != digest[1]
    session.report["checks"]["expert edit detected"] = {
        "result": "pass" if changed else "FAIL",
        "version": [before.get("version"), after.get("version")],
        "content_sha256": digest,
    }
    assert changed


def test_outages_are_not_consent(session: Session) -> None:
    answer = _ask(
        "Pause the broker for a few seconds to check outage handling? "
        "(docker pause keeps its memory, so connections survive) [y/N]"
    )
    if answer.lower() != "y":
        session.report["checks"]["outage handling"] = {"result": "skipped by the operator"}
        pytest.skip("operator skipped the outage check")
    subprocess.run(["docker", "pause", "vtb-broker-kc"], check=True)  # noqa: S607
    try:
        outcome = session.recorder.call_tool(
            session.credential("grc"), "atlassian_atlassianUserInfo", {}, interactive=False
        )
    finally:
        subprocess.run(["docker", "unpause", "vtb-broker-kc"], check=True)  # noqa: S607
    cls = classify(outcome, session.settings.consent_url_prefix).cls
    session.report["checks"]["outage handling"] = {
        "result": "pass" if cls == "unavailable" else f"FAIL ({cls})"
    }
    assert cls == "unavailable"


def test_disconnect_then_consent_again(session: Session) -> None:
    answer = _ask(
        "Disconnect the Legal persona's Atlassian connection to check that consent is "
        "needed again? (Legal reconnects later through Connect Atlassian) [y/N]"
    )
    if answer.lower() != "y":
        session.report["checks"]["disconnect then consent"] = {"result": "skipped by the operator"}
        pytest.skip("operator skipped the disconnect check")
    session.recorder.call_tool(
        session.credential("legal"), "disconnect_atlassian", {}, interactive=False
    )
    try:
        link = session.as_("legal").connect(session.credential("legal"))
    except ConnectRefused as exc:
        link = None
        session.report["checks"]["disconnect then consent"] = {"result": f"FAIL ({exc})"}
        raise
    session.report["checks"]["disconnect then consent"] = {
        "result": "pass" if link is not None else "FAIL (still connected)"
    }
    assert link is not None


def test_a_background_run_never_falls_back_to_another_identity(session: Session) -> None:
    """GRC runs a drafting read; with GRC's credential removed, the run pauses
    instead of using anyone else's."""
    store = CredentialStore()
    catalogs = session.catalogs
    executor = Executor(
        session.engine,
        session.log,
        store,
        load_baseline(ROOT / "policies/platform-baseline.yaml"),
        session.reads.tool_specs(catalogs),
        catalogs,
    )
    task = submit_task(
        session.engine,
        session.log,
        session.subjects["sales"],
        "questionnaire-agent",
        "larkspur-synthetic",
        "connected-2",
        "SQR-1",
    )
    authorize_stage(session.engine, session.log, session.subjects["grc"], task)
    later = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
    store.put(
        "s-grc",
        session.subjects["grc"],
        GatewayCredential(session.credential("grc").access_token, later),
    )
    run = executor.start_run(task, "drafting", "connected")
    store.remove_session("s-grc")
    with pytest.raises(RunStopped) as stopped:
        executor.call(
            run, ToolCall("confluence.read_page", {"page_id": session.config["pages"]["readable"]})
        )
    session.report["checks"]["no fallback identity"] = {
        "result": "pass" if stopped.value.kind == "pause" else "FAIL"
    }
    assert stopped.value.kind == "pause"
