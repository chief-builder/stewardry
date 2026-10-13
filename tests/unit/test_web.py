"""Web sign-in, sessions, CSRF, and identity-swap attempts (S4b), fully offline."""

import datetime as dt
import json
import re
from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from stewardry.platform.artifacts import ArtifactStore
from stewardry.platform.audit import AuditLog
from stewardry.platform.credentials import CredentialStore
from stewardry.platform.db.schema import audit_events, task_grants, tasks
from stewardry.platform.grants import AwaitingAuthorization, acquire_execution
from stewardry.web.app import Services, create_app
from stewardry.web.hub import HubClient
from stewardry.web.sessions import SessionStore
from stewardry.web.settings import WebSettings
from tests.fakes.hub import CLIENT_ID, CLIENT_SECRET, ISSUER, FakeHub, ec_key, other_rsa_key
from tests.unit.conftest import ALICE, BOB, LARKSPUR

BASE = "http://localhost:8700"
SETTINGS = WebSettings(BASE, ISSUER, CLIENT_ID, CLIENT_SECRET)


@pytest.fixture
def hub() -> FakeHub:
    return FakeHub()


@pytest.fixture
def services(world: Engine, log: AuditLog, hub: FakeHub, tmp_path: Path) -> Iterator[Services]:
    http = httpx2.Client(transport=hub.transport())
    client = HubClient(ISSUER, CLIENT_ID, CLIENT_SECRET, SETTINGS.redirect_uri, http)
    yield Services(
        SETTINGS,
        world,
        client,
        log,
        SessionStore(),
        CredentialStore(),
        ArtifactStore(tmp_path / "a"),
    )
    http.close()


def browser(services: Services) -> TestClient:
    return TestClient(create_app(services), base_url=BASE, follow_redirects=False)


def sign_in(
    client: TestClient, hub: FakeHub, sub: str, username: str, groups: list[str] | None = None
) -> httpx2.Response:
    start = client.get("/login")
    assert start.status_code == 303
    code, state = hub.approve(start.headers["location"], sub, username, groups)
    return client.get("/auth/callback", params={"code": code, "state": state})


def csrf(client: TestClient) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/").text)
    assert match, "no CSRF token on the page"
    return match.group(1)


def audit(engine: Engine, event_type: str) -> list[tuple[str, dict[str, object]]]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(audit_events.c.outcome, audit_events.c.details_json)
            .where(audit_events.c.event_type == event_type)
            .order_by(audit_events.c.seq)
        ).all()
    return [(r.outcome, json.loads(r.details_json)) for r in rows]


def submit(client: TestClient, key: str = "k1", **extra: str) -> httpx2.Response:
    form = {
        "csrf_token": csrf(client),
        "customer_id": LARKSPUR,
        "submission_key": key,
        "jira_issue_key": "SQR-1",
    } | extra
    return client.post("/tasks", data=form)


# ------------------------------------------------------------------ basics


def test_signed_out_page_and_security_headers(services: Services) -> None:
    response = browser(services).get("/")
    assert response.status_code == 200 and 'href="/login"' in response.text
    assert response.headers["referrer-policy"] == "same-origin"
    assert response.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert browser(services).get("/healthz").json() == {"ok": True}
    assert browser(services).get("/docs").status_code == 404  # no API explorer


def test_sign_in_creates_a_session_and_holds_the_credential(
    services: Services, hub: FakeHub
) -> None:
    client = browser(services)
    response = sign_in(client, hub, ALICE, "alice")
    assert response.status_code == 303 and response.headers["location"] == "/"
    cookie = response.headers["set-cookie"]
    assert "stewardry_session=" in cookie and "httponly" in cookie.lower()
    assert "samesite=lax" in cookie.lower()
    page = client.get("/").text
    assert "alice" in page and "Roles: sales." in page
    credential = services.credentials.get(ALICE, dt.datetime.now(dt.UTC))
    assert credential is not None and credential.access_token.startswith("gateway-access-")
    assert credential.access_token not in page
    assert audit(services.engine, "auth.sign_in")[-1][0] == "succeeded"


# ----------------------------------------------------- callback protections


def test_replayed_callback_is_refused(services: Services, hub: FakeHub) -> None:
    client = browser(services)
    start = client.get("/login")
    code, state = hub.approve(start.headers["location"], ALICE, "alice")
    assert client.get("/auth/callback", params={"code": code, "state": state}).status_code == 303
    replay = client.get("/auth/callback", params={"code": code, "state": state})
    assert replay.status_code == 400 and "already used" in replay.text
    assert audit(services.engine, "auth.sign_in")[-1][0] == "denied"


def test_state_mismatch_is_refused(services: Services, hub: FakeHub) -> None:
    client = browser(services)
    code, _ = hub.approve(client.get("/login").headers["location"], ALICE, "alice")
    response = client.get("/auth/callback", params={"code": code, "state": "forged"})
    assert response.status_code == 400 and "could not be verified" in response.text


def test_callback_in_another_browser_is_refused(services: Services, hub: FakeHub) -> None:
    code, state = hub.approve(browser(services).get("/login").headers["location"], ALICE, "alice")
    response = browser(services).get("/auth/callback", params={"code": code, "state": state})
    assert response.status_code == 400 and "expired or was already used" in response.text


def test_hub_errors_on_the_callback_are_refused(services: Services) -> None:
    client = browser(services)
    state = re.search(r"state=([^&]+)", client.get("/login").headers["location"])
    assert state is not None
    response = client.get(
        "/auth/callback", params={"error": "access_denied", "state": state.group(1)}
    )
    assert response.status_code == 400


@pytest.mark.parametrize(
    ("fault", "value"),
    [
        ("sign_with", "other-rsa"),  # forged signature
        ("algorithm", "RS256"),  # algorithm the broker never accepts
        ("aud", "another-client"),
        ("iss", "http://evil.test/realms/mcp"),
        ("nonce", "replayed-nonce"),
        ("azp", "another-client"),
        ("exp", "expired"),
        ("preferred_username", None),
    ],
)
def test_bad_id_tokens_are_refused(
    services: Services, hub: FakeHub, fault: str, value: object
) -> None:
    if fault == "sign_with":
        hub.sign_with = other_rsa_key()
    elif fault == "algorithm":
        hub.algorithm = str(value)
    elif fault == "exp":
        hub.claim_overrides = {"exp": 1_000_000_000, "iat": 999_999_000}
    else:
        hub.claim_overrides = {fault: value}
    response = sign_in(browser(services), hub, ALICE, "alice")
    assert response.status_code == 400
    assert "stewardry_session" not in response.headers.get("set-cookie", "")
    assert services.credentials.get(ALICE, dt.datetime.now(dt.UTC)) is None


def test_es256_tokens_are_accepted_when_the_key_matches(services: Services, hub: FakeHub) -> None:
    hub.key = ec_key()  # type: ignore[assignment]
    hub.algorithm = "ES256"
    assert sign_in(browser(services), hub, ALICE, "alice").status_code == 303


def test_hub_outage_is_not_reported_as_a_refusal(services: Services, hub: FakeHub) -> None:
    client = browser(services)
    hub.down = True
    response = client.get("/login")
    assert response.status_code == 503 and "unavailable" in response.text
    hub.down = False
    start = client.get("/login")
    code, state = hub.approve(start.headers["location"], ALICE, "alice")
    hub.down = True
    response = client.get("/auth/callback", params={"code": code, "state": state})
    assert response.status_code == 503 and "refused" not in response.text.lower()


@pytest.mark.parametrize(
    ("sub", "username", "message"),
    [("sub-stranger", "stranger", "not provisioned"), ("sub-imposter", "alice", "re-map")],
)
def test_unprovisioned_or_remapped_users_are_refused(
    services: Services, hub: FakeHub, sub: str, username: str, message: str
) -> None:
    response = sign_in(browser(services), hub, sub, username)
    assert response.status_code == 403 and message in response.text


# --------------------------------------------------- CSRF and identity swap


def test_forms_need_a_session_and_its_csrf_token(services: Services, hub: FakeHub) -> None:
    anonymous = browser(services)
    assert anonymous.post("/tasks", data={"csrf_token": "x"}).status_code == 401

    alice, bob = browser(services), browser(services)
    sign_in(alice, hub, ALICE, "alice")
    sign_in(bob, hub, BOB, "bob")
    form = {"customer_id": LARKSPUR, "submission_key": "k1"}
    assert alice.post("/tasks", data=form).status_code == 403  # no token
    assert alice.post("/tasks", data=form | {"csrf_token": csrf(bob)}).status_code == 403
    cross_site = alice.post(
        "/tasks", data=form | {"csrf_token": csrf(alice)}, headers={"origin": "https://evil.test"}
    )
    assert cross_site.status_code == 403 and "another site" in cross_site.text
    # A browser posting from Stewardry's own page sends its origin (because the
    # referrer policy is same-origin; with no-referrer it would send "null").
    opaque = alice.post(
        "/tasks", data=form | {"csrf_token": csrf(alice)}, headers={"origin": "null"}
    )
    assert opaque.status_code == 403
    same_site = alice.post(
        "/tasks",
        data=form | {"csrf_token": csrf(alice), "jira_issue_key": "SQR-1"},
        headers={"origin": BASE},
    )
    assert same_site.status_code == 303
    assert submit(alice).status_code == 303
    with services.engine.connect() as conn:
        assert len(conn.execute(select(tasks)).all()) == 1


def test_form_fields_cannot_choose_the_user(services: Services, hub: FakeHub) -> None:
    alice = browser(services)
    sign_in(alice, hub, ALICE, "alice")
    assert submit(alice, requester_sub=BOB, actor_sub=BOB, sub=BOB).status_code == 303
    with services.engine.connect() as conn:
        task = conn.execute(select(tasks)).one()
    assert task.requester_sub == ALICE


def test_replayed_submission_creates_one_task(services: Services, hub: FakeHub) -> None:
    alice = browser(services)
    sign_in(alice, hub, ALICE, "alice")
    assert submit(alice, key="same").status_code == 303
    assert submit(alice, key="same").status_code == 303
    with services.engine.connect() as conn:
        assert len(conn.execute(select(tasks)).all()) == 1


def test_group_claims_grant_nothing(services: Services, hub: FakeHub) -> None:
    alice = browser(services)
    sign_in(alice, hub, ALICE, "alice", groups=["grc", "platform_engineer", "vtb-admins"])
    assert "Roles: sales." in alice.get("/").text
    submit(alice)
    with services.engine.connect() as conn:
        task_id = conn.execute(select(tasks.c.task_id)).scalar_one()
    response = alice.post(f"/tasks/{task_id}/authorize", data={"csrf_token": csrf(alice)})
    assert response.status_code == 403 and "no drafting role" in response.text


# ----------------------------------------------------------- GRC workflow


def test_grc_authorizes_under_their_own_identity_and_can_revoke(
    services: Services, hub: FakeHub
) -> None:
    alice, bob = browser(services), browser(services)
    sign_in(alice, hub, ALICE, "alice")
    sign_in(bob, hub, BOB, "bob")
    submit(alice)
    with services.engine.connect() as conn:
        task_id = conn.execute(select(tasks.c.task_id)).scalar_one()
    assert "Authorize drafting under my identity" in bob.get("/").text
    assert (
        bob.post(f"/tasks/{task_id}/authorize", data={"csrf_token": csrf(bob)}).status_code == 303
    )

    execution = acquire_execution(
        services.engine, services.log, services.credentials, task_id, "drafting"
    )
    assert execution.acting_sub == BOB
    bob_token = services.credentials.get(BOB, dt.datetime.now(dt.UTC))
    assert bob_token is not None and execution.credential == bob_token

    with services.engine.connect() as conn:
        grant_id = conn.execute(select(task_grants.c.grant_id)).scalar_one()
    assert (
        bob.post(
            f"/grants/{grant_id}/revoke", data={"csrf_token": csrf(bob), "reason": ""}
        ).status_code
        == 403
    )
    assert (
        bob.post(
            f"/grants/{grant_id}/revoke", data={"csrf_token": csrf(bob), "reason": "done"}
        ).status_code
        == 303
    )
    with services.engine.connect() as conn:
        assert conn.execute(select(tasks.c.status)).scalar_one() == "paused"


def test_sign_out_drops_the_credential_and_work_pauses(services: Services, hub: FakeHub) -> None:
    alice, bob = browser(services), browser(services)
    sign_in(alice, hub, ALICE, "alice")
    sign_in(bob, hub, BOB, "bob")
    submit(alice)
    with services.engine.connect() as conn:
        task_id = conn.execute(select(tasks.c.task_id)).scalar_one()
    bob.post(f"/tasks/{task_id}/authorize", data={"csrf_token": csrf(bob)})
    assert bob.post("/logout", data={"csrf_token": csrf(bob)}).status_code == 303
    assert bob.post("/logout", data={"csrf_token": "x"}).status_code == 401
    with pytest.raises(AwaitingAuthorization):
        acquire_execution(services.engine, services.log, services.credentials, task_id, "drafting")
    assert audit(services.engine, "auth.sign_out")[0][0] == "succeeded"


def test_restart_forgets_sessions(services: Services, hub: FakeHub) -> None:
    alice = browser(services)
    sign_in(alice, hub, ALICE, "alice")
    token = csrf(alice)
    services.sessions, services.credentials = SessionStore(), CredentialStore()
    restarted = TestClient(
        create_app(services), base_url=BASE, follow_redirects=False, cookies=dict(alice.cookies)
    )
    assert 'href="/login"' in restarted.get("/").text
    assert restarted.post("/tasks", data={"csrf_token": token}).status_code == 401


# ------------------------------------------------------- credential refresh


def test_expiring_credentials_are_refreshed(services: Services, hub: FakeHub) -> None:
    hub.access_lifetime = 30  # inside the 60-second refresh margin
    alice = browser(services)
    sign_in(alice, hub, ALICE, "alice")
    first = services.credentials.get(ALICE, dt.datetime.now(dt.UTC))
    alice.get("/")
    second = services.credentials.get(ALICE, dt.datetime.now(dt.UTC))
    assert first is not None and second is not None and first != second


def test_refused_refresh_drops_the_credential(services: Services, hub: FakeHub) -> None:
    hub.access_lifetime = 30
    alice = browser(services)
    sign_in(alice, hub, ALICE, "alice")
    hub.refuse_refresh = True
    alice.get("/")
    assert services.credentials.get(ALICE, dt.datetime.now(dt.UTC)) is None
    assert audit(services.engine, "auth.credential_refreshed")[0][0] == "failed"


def test_hub_outage_during_refresh_keeps_the_credential(services: Services, hub: FakeHub) -> None:
    hub.access_lifetime = 30
    alice = browser(services)
    sign_in(alice, hub, ALICE, "alice")
    before = services.credentials.get(ALICE, dt.datetime.now(dt.UTC))
    hub.down = True
    alice.get("/")
    assert services.credentials.get(ALICE, dt.datetime.now(dt.UTC)) == before


# -------------------------------------------------------------- no leaks


def test_tokens_never_reach_pages_or_the_audit_log(services: Services, hub: FakeHub) -> None:
    alice = browser(services)
    sign_in(alice, hub, ALICE, "alice")
    submit(alice)
    credential = services.credentials.get(ALICE, dt.datetime.now(dt.UTC))
    assert credential is not None
    with services.engine.connect() as conn:
        everything = json.dumps(
            [list(r) for r in conn.execute(select(audit_events)).all()], default=str
        )
    for secret in (credential.access_token, CLIENT_SECRET):
        assert secret not in everything
        assert secret not in alice.get("/").text
    assert CLIENT_SECRET not in repr(services.hub) and CLIENT_SECRET not in repr(SETTINGS)
