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
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from stewardry.platform.atlassian.adapter import (
    AtlassianReads,
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
from stewardry.platform.evidence import parse_page
from stewardry.platform.execution import (
    Executor,
    OperationRefused,
    RunStopped,
    ToolCall,
)
from stewardry.platform.grants import authorize_stage, submit_task
from stewardry.platform.policy import load_agent_policy, load_baseline
from stewardry.web.settings import WebSettings
from tests.connected.live_app import ROOT, LiveApp, ask, broker_revision, live_app, say

pytestmark = [
    pytest.mark.connected,
    pytest.mark.skipif(
        os.environ.get("STEWARDRY_CONNECTED") != "1",
        reason="connected tier: run manually with `make test-connected`",
    ),
]

BROKER = Path(os.environ.get("STEWARDRY_BROKER_CHECKOUT", ROOT.parent / "vendor-token-broker"))


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
    app: LiveApp

    def credential(self, role: str) -> GatewayCredential:
        found = self.credentials.get(self.subjects[role], dt.datetime.now(dt.UTC))
        assert found is not None, f"{role} is not signed in"
        return found

    def as_(self, role: str) -> AtlassianReads:
        self.recorder.persona = role
        return self.reads


@pytest.fixture(scope="module")
def session() -> Iterator[Session]:
    recorder = Recorder(McpGatewayClient(""))  # the real client is set by live_app

    def wrap(client: McpGatewayClient) -> Recorder:
        recorder.inner = client
        return recorder

    today = dt.date.today().isoformat()
    out = Path(os.environ.get("STEWARDRY_CONNECTED_OUT", ROOT / "docs" / "connected" / today))
    out.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "provenance": "connected",
        "date": today,
        "broker_revision": broker_revision(BROKER),
        "participants": "1 person operating every role",
        "checks": {},
    }
    with live_app(wrap) as app:
        yield Session(
            app.settings,
            app.config,
            app.credentials,
            app.subjects,
            recorder,
            app.reads,
            report,
            out,
            app.engine,
            app.log,
            app.catalogs,
            app,
        )
        with app.engine.connect() as conn:
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


# ------------------------------------------------------------------ checks


def test_people_sign_in_and_connect(session: Session) -> None:
    roles = os.environ.get("STEWARDRY_CONNECTED_ROLES", "sales,grc,legal").split(",")
    session.app.wait_until_connected(roles)


def test_sites_and_distinct_accounts(session: Session) -> None:
    accounts, sites = {}, {}
    for role in ("sales", "grc", "legal"):
        cred = session.credential(role)
        accounts[role] = session.as_(role).account_id(cred)
        sites[role] = sorted(session.as_(role).accessible_site_ids(cred))
    for role, account in accounts.items():
        if account is None:  # show the real response shape (G11) to fix the parser
            raw = [c for c in session.recorder.calls if c["persona"] == role][-2]["response"]
            say(
                f"{role}: no account ID found in atlassianUserInfo's response: "
                f"{json.dumps(raw, default=str)[:600]}"
            )
    distinct = None not in accounts.values() and len(set(accounts.values())) == len(accounts)
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
        missing = sorted(name for name, value in result["fields"].items() if value is None)
        session.report["checks"]["Jira field read as Sales"] = {
            "result": f"partial (not returned: {', '.join(missing)})" if missing else "pass",
            "read": result,
        }
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
    ask(
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
    answer = ask(
        "Pause the broker for a few seconds to check outage handling? "
        "(docker pause keeps its memory, so connections survive) [y/N]"
    )
    if answer.lower() != "y":
        session.report["checks"]["outage handling"] = {"result": "skipped by the operator"}
        pytest.skip("operator skipped the outage check")
    subprocess.run(["docker", "pause", "vtb-broker-kc"], check=True)  # noqa: S607
    session.recorder.persona = "grc"
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
    answer = ask(
        "Disconnect the Legal persona's Atlassian connection to check that consent is "
        "needed again? (Legal reconnects later through Connect Atlassian) [y/N]"
    )
    if answer.lower() != "y":
        session.report["checks"]["disconnect then consent"] = {"result": "skipped by the operator"}
        pytest.skip("operator skipped the disconnect check")
    session.recorder.persona = "legal"
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
