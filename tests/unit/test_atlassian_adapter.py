"""The Atlassian adapter: classification, arguments, filtering, and the executor."""

import datetime as dt
import json
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, select

from stewardry.platform.atlassian.adapter import (
    AtlassianReads,
    AtlassianSettings,
    ConnectRefused,
)
from stewardry.platform.atlassian.client import (
    ConsentRequested,
    GatewayUnreachable,
    SignInRequired,
    ToolFailure,
    ToolSuccess,
)
from stewardry.platform.atlassian.gateway_errors import CATALOG, classify, classify_text
from stewardry.platform.audit import AuditLog
from stewardry.platform.credentials import CredentialStore, GatewayCredential
from stewardry.platform.db.schema import audit_events, runs, tasks
from stewardry.platform.execution import (
    Executor,
    HandlerStop,
    OperationRefused,
    RunContext,
    RunStopped,
    ToolCall,
)
from stewardry.platform.grants import authorize_stage, submit_task
from stewardry.platform.policy import load_baseline
from tests.fakes.gateway import CLOUD_ID, CONSENT_PREFIX, FakeGateway, gateway_text
from tests.unit.conftest import AGENT, ALICE, BOB, LARKSPUR, ROOT

LATER = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
BOB_TOKEN = f"token-{BOB}"
SETTINGS = AtlassianSettings(CLOUD_ID, CONSENT_PREFIX, {"customer": "customfield_10042"})
CATALOG_REF = "policies/catalogs/secpol.yaml"


def credential(token: str = BOB_TOKEN) -> GatewayCredential:
    return GatewayCredential(token, LATER)


def reads(gateway: FakeGateway) -> AtlassianReads:
    sleeps: list[float] = []
    adapter = AtlassianReads(gateway, SETTINGS, sleep=sleeps.append)
    adapter.sleeps = sleeps  # type: ignore[attr-defined]
    return adapter


def catalogs(ref: str) -> frozenset[str]:
    return frozenset({"101", "102"}) if ref == CATALOG_REF else frozenset()


# ------------------------------------------------------------ classification


@pytest.mark.parametrize(("template", "cls"), list(CATALOG.items()))
def test_every_pinned_gateway_message_is_classified(template: str, cls: str) -> None:
    text = gateway_text(template, *["Atlassian"] * template.count("{}"))
    if template.startswith("{} is not usable right now"):
        assert classify_text(gateway_text(template, "Atlassian", "revoke-pending")).cls == (
            "disconnect_pending"
        )
        assert classify_text(gateway_text(template, "Atlassian", "unknown-vendor")).cls == (
            "vendor_unusable"
        )
    else:
        assert classify_text(text).cls == cls


def test_consent_comes_only_from_the_structured_elicitation() -> None:
    link = f"{CONSENT_PREFIX}abc"
    assert classify(ConsentRequested(link), CONSENT_PREFIX).cls == "needs_consent"
    # The same link inside the gateway's manual text is never consent.
    text = gateway_text("Connect {} first: open {} in your browser, then retry.", "Atlassian", link)
    assert classify(ToolFailure(text), CONSENT_PREFIX).cls == "unexpected"
    # A consent request pointing anywhere else is not consent either.
    foreign = ConsentRequested("https://evil.test/v1/authorize/atlassian?txn=abc")
    assert classify(foreign, CONSENT_PREFIX).cls == "unexpected"


@pytest.mark.parametrize(
    ("outcome", "cls"),
    [
        (SignInRequired(), "needs_signin"),
        (GatewayUnreachable("ConnectError"), "unavailable"),
        (ToolFailure("Content not found"), "upstream_tool_error"),
        (ToolSuccess(None, "{}"), "ok"),
    ],
)
def test_other_outcomes(outcome: Any, cls: str) -> None:
    assert classify(outcome, CONSENT_PREFIX).cls == cls


def test_settings_are_validated() -> None:
    with pytest.raises(ValueError, match="UUID"):
        AtlassianSettings("not-a-uuid", CONSENT_PREFIX)
    with pytest.raises(ValueError, match="authorize path"):
        AtlassianSettings(CLOUD_ID, "https://evil.test/?txn=")


# ----------------------------------------------------- arguments and results


def test_jira_reads_only_allow_listed_fields() -> None:
    gateway = FakeGateway(connected={BOB_TOKEN})
    result = reads(gateway).jira_get_request(credential(), "SQR-1", ["summary", "customer"])
    assert result == {
        "issue_key": "SQR-1",
        "fields": {
            "summary": "Security questionnaire for Larkspur",
            "customer": "larkspur-synthetic",
        },
    }
    sent = gateway.calls[0].arguments
    assert sent == {
        "cloudId": CLOUD_ID,
        "issueIdOrKey": "SQR-1",
        "fields": ["customfield_10042", "summary"],
        "fieldsByKeys": True,
        "responseContentFormat": "markdown",
    }
    assert "internal notes" not in json.dumps(result) and "secret.pdf" not in json.dumps(result)


def test_page_listing_uses_a_fixed_query_and_drops_pages_outside_the_catalog() -> None:
    gateway = FakeGateway(connected={BOB_TOKEN})
    result = reads(gateway).confluence_list_pages(credential(), "SECPOL", frozenset({"101", "102"}))
    assert [p["page_id"] for p in result["pages"]] == ["101", "102"]
    assert result["pages"][0]["version"] == 4 and not result["truncated"]
    assert gateway.calls[0].arguments["cql"] == 'type = page AND space = "SECPOL"'
    assert "HR salary" not in json.dumps(result)
    with pytest.raises(OperationRefused, match="invalid space key"):
        reads(gateway).confluence_list_pages(credential(), 'SECPOL" OR space = "HR', frozenset())


def test_page_reads_ask_for_the_full_body() -> None:
    gateway = FakeGateway(connected={BOB_TOKEN})
    page = reads(gateway).confluence_read_page(credential(), "101")
    assert page["version"] == 3 and "encrypted at rest" in page["body_html"]
    assert gateway.calls[0].arguments == {
        "cloudId": CLOUD_ID,
        "content_id": "101",
        "detail": "full",
        "content_format": "html",
    }


def test_site_and_account_checks() -> None:
    gateway = FakeGateway(connected={BOB_TOKEN})
    assert reads(gateway).accessible_site_ids(credential()) == {CLOUD_ID}
    assert reads(gateway).account_id(credential()) == "557058:fake-account"


@pytest.mark.parametrize(
    ("payload", "operation"),
    [
        ({"no": "fields"}, lambda r: r.jira_get_request(credential(), "SQR-1", ["summary"])),
        ({"no": "results"}, lambda r: r.confluence_list_pages(credential(), "SECPOL", frozenset())),
        (["not", "a", "page"], lambda r: r.confluence_read_page(credential(), "101")),
    ],
)
def test_unrecognized_shapes_fail_instead_of_guessing(payload: Any, operation: Any) -> None:
    gateway = FakeGateway(connected={BOB_TOKEN}, forced=[ToolSuccess(payload, "")])
    with pytest.raises(HandlerStop, match="response shape") as stopped:
        operation(reads(gateway))
    assert stopped.value.kind == "fail"


# ----------------------------------------------------------- run actions


def test_needing_consent_pauses_without_keeping_the_link() -> None:
    gateway = FakeGateway()  # Bob has not connected Atlassian
    with pytest.raises(HandlerStop, match="must connect") as stopped:
        reads(gateway).confluence_read_page(credential(), "101")
    assert stopped.value.kind == "pause" and CONSENT_PREFIX not in stopped.value.reason
    assert gateway.calls[0].interactive is False


def test_outages_retry_then_pause_and_never_ask_for_consent() -> None:
    outage = ToolFailure(
        gateway_text(
            "a dependency is unavailable ({}); retry shortly", "broker unreachable (ConnectError)"
        )
    )
    gateway = FakeGateway(connected={BOB_TOKEN}, forced=[outage, outage, outage])
    adapter = reads(gateway)
    with pytest.raises(HandlerStop, match="unavailable") as stopped:
        adapter.confluence_read_page(credential(), "101")
    assert stopped.value.kind == "pause" and "connect" not in stopped.value.reason
    assert len(gateway.calls) == 3 and adapter.sleeps == [2.0, 4.0]  # type: ignore[attr-defined]


def test_an_outage_followed_by_success_recovers() -> None:
    gateway = FakeGateway(connected={BOB_TOKEN}, forced=[GatewayUnreachable("timeout")])
    assert reads(gateway).confluence_read_page(credential(), "101")["page_id"] == "101"


def test_configuration_errors_fail_the_run() -> None:
    error = ToolFailure(next(t for t in CATALOG if t.startswith("the gateway could not")))
    with pytest.raises(HandlerStop, match="configuration") as stopped:
        reads(FakeGateway(forced=[error])).confluence_read_page(credential(), "101")
    assert stopped.value.kind == "fail"


def test_vendor_refusals_refuse_the_operation_only() -> None:
    gateway = FakeGateway(connected={BOB_TOKEN}, restricted_pages={"102"})
    with pytest.raises(OperationRefused, match="Atlassian refused"):
        reads(gateway).confluence_read_page(credential(), "102")


def test_non_json_success_is_unexpected_and_retried() -> None:
    gateway = FakeGateway(connected={BOB_TOKEN}, forced=[ToolSuccess(None, "<html>")] * 3)
    with pytest.raises(HandlerStop, match="unavailable"):
        reads(gateway).confluence_read_page(credential(), "101")


def test_missing_credential_pauses() -> None:
    with pytest.raises(HandlerStop, match="credential"):
        reads(FakeGateway()).confluence_read_page(None, "101")


# ------------------------------------------------------------- connecting


def test_connect_returns_the_link_only_for_the_interactive_action() -> None:
    gateway = FakeGateway()
    link = reads(gateway).connect(credential())
    assert link is not None and link.url.startswith(CONSENT_PREFIX)
    assert link.url not in repr(link) and gateway.calls[0].interactive is True
    gateway.connected.add(BOB_TOKEN)
    assert reads(gateway).connect(credential()) is None


def test_connect_refuses_foreign_links_and_outages() -> None:
    foreign = FakeGateway(forced=[ConsentRequested("https://evil.test/?txn=x")])
    with pytest.raises(ConnectRefused):
        reads(foreign).connect(credential())
    with pytest.raises(ConnectRefused, match="unavailable"):
        reads(FakeGateway(forced=[GatewayUnreachable("down")])).connect(credential())


# ------------------------------------------------------- through the executor


@pytest.fixture
def executor(world: Engine, log: AuditLog) -> Iterator[tuple[Executor, FakeGateway]]:
    gateway = FakeGateway(connected={BOB_TOKEN}, restricted_pages={"102"})
    store = CredentialStore()
    store.put("s-bob", BOB, credential())
    tools = reads(gateway).tool_specs(catalogs)
    yield (
        Executor(
            world,
            log,
            store,
            load_baseline(ROOT / "policies/platform-baseline.yaml"),
            tools,
            catalogs,
        ),
        gateway,
    )


def drafting_run(world: Engine, log: AuditLog, executor: Executor) -> RunContext:
    task_id = submit_task(world, log, ALICE, AGENT, LARKSPUR, "req-1", "SQR-1")
    authorize_stage(world, log, BOB, task_id)
    return executor.start_run(task_id, "drafting", "offline-replay")


def test_operations_run_as_the_acting_user_through_the_executor(
    world: Engine, log: AuditLog, executor: tuple[Executor, FakeGateway]
) -> None:
    ex, gateway = executor
    run = drafting_run(world, log, ex)
    listing = ex.call(run, ToolCall("confluence.list_pages", {"space": "SECPOL"}))
    assert [p["page_id"] for p in listing.content["pages"]] == ["101", "102"]
    assert ex.call(run, ToolCall("confluence.read_page", {"page_id": "101"})).ok
    assert {c.token for c in gateway.calls} == {BOB_TOKEN}
    with world.connect() as conn:
        rows = conn.execute(select(audit_events)).all()
    stored = json.dumps([list(r) for r in rows], default=str)
    assert BOB_TOKEN not in stored


def test_vendor_denial_is_audited_as_denied_and_the_run_continues(
    world: Engine, log: AuditLog, executor: tuple[Executor, FakeGateway]
) -> None:
    ex, _ = executor
    run = drafting_run(world, log, ex)
    result = ex.call(run, ToolCall("confluence.read_page", {"page_id": "102"}))
    assert not result.ok and result.error == "Atlassian refused the request"
    with world.connect() as conn:
        outcome, details = conn.execute(
            select(audit_events.c.outcome, audit_events.c.details_json).where(
                audit_events.c.event_type == "tool.call"
            )
        ).one()
        status = conn.execute(select(runs.c.status)).scalar_one()
    assert outcome == "denied" and json.loads(details)["refused_by"] == "vendor"
    assert status == "running"


def test_consent_needed_mid_run_pauses_the_run_and_task(
    world: Engine, log: AuditLog, executor: tuple[Executor, FakeGateway]
) -> None:
    ex, gateway = executor
    run = drafting_run(world, log, ex)
    gateway.connected.clear()  # Bob's Atlassian connection was revoked
    with pytest.raises(RunStopped, match="must connect") as stopped:
        ex.call(run, ToolCall("confluence.read_page", {"page_id": "101"}))
    assert stopped.value.kind == "pause"
    with world.connect() as conn:
        assert conn.execute(select(runs.c.status)).scalar_one() == "paused"
        assert conn.execute(select(tasks.c.status)).scalar_one() == "paused"


def test_wrapped_structured_results_are_unwrapped() -> None:
    """Seen on the stand-in stack (S14b): a list result arrives as {"result": [...]}."""
    from stewardry.platform.atlassian.adapter import _parse

    assert _parse(ToolSuccess({"result": [{"id": "site"}]}, "")) == [{"id": "site"}]
    assert _parse(ToolSuccess({"result": 1, "other": 2}, "")) == {"result": 1, "other": 2}


@pytest.mark.parametrize("payload", [{"accountId": "a1"}, {"account_id": "a1"}])
def test_the_account_id_is_read_in_either_spelling(payload: dict[str, str]) -> None:
    gateway = FakeGateway(connected={BOB_TOKEN}, forced=[ToolSuccess(payload, "")])
    assert reads(gateway).account_id(credential(BOB_TOKEN)) == "a1"
