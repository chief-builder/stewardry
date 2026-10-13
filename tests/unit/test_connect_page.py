"""The user's own Connect Atlassian action in the web app (S15 prep, ADR 0010)."""

import datetime as dt
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select

from stewardry.platform.atlassian.adapter import AtlassianReads, AtlassianSettings
from stewardry.platform.atlassian.client import GatewayUnreachable
from stewardry.platform.audit import AuditLog
from stewardry.platform.db.schema import audit_events
from stewardry.web.app import Services
from tests.fakes.gateway import CLOUD_ID, CONSENT_PREFIX, FakeGateway
from tests.unit.conftest import ALICE
from tests.unit.test_views import as_user, csrf, web_services


@pytest.fixture
def services(world: Engine, log: AuditLog, tmp_path: Path) -> Iterator[Services]:
    with web_services(world, log, tmp_path) as built:
        yield built


def with_atlassian(services: Services, gateway: FakeGateway) -> Services:
    services.atlassian = AtlassianReads(
        gateway, AtlassianSettings(CLOUD_ID, CONSENT_PREFIX), sleep=lambda _: None
    )
    return services


def connect_events(world: Engine) -> list[tuple[str, dict[str, Any]]]:
    with world.connect() as conn:
        rows = conn.execute(
            select(audit_events.c.outcome, audit_events.c.details_json).where(
                audit_events.c.event_type == "atlassian.connect"
            )
        ).all()
    return [(r.outcome, json.loads(r.details_json)) for r in rows]


def test_an_unconnected_user_sees_their_link_once(services: Services, world: Engine) -> None:
    gateway = FakeGateway()
    alice = as_user(with_atlassian(services, gateway), ALICE)
    assert "Connect Atlassian" in alice.get("/").text
    response = alice.post("/connect", data={"csrf_token": csrf(alice, "/connect")})
    assert response.status_code == 200
    assert f'href="{CONSENT_PREFIX}' in response.text
    assert gateway.calls[-1].interactive is True
    assert (
        gateway.calls[-1].token
        == services.credentials.get(ALICE, dt.datetime.now(dt.UTC)).access_token
    )  # type: ignore[union-attr]
    ((outcome, details),) = connect_events(world)
    assert (outcome, details) == ("succeeded", {"outcome": "consent_needed"})
    assert CONSENT_PREFIX.split("?")[0] not in json.dumps(details)  # the link is never kept
    assert CONSENT_PREFIX not in alice.get("/connect").text  # nor shown again


def test_a_connected_user_is_told_so(services: Services) -> None:
    gateway = FakeGateway()
    with_atlassian(services, gateway)
    alice = as_user(services, ALICE)
    token = services.credentials.get(ALICE, dt.datetime.now(dt.UTC))
    assert token is not None
    gateway.connected.add(token.access_token)
    response = alice.post("/connect", data={"csrf_token": csrf(alice, "/connect")})
    assert "Your Atlassian account is connected." in response.text


def test_an_outage_is_not_shown_as_consent(services: Services, world: Engine) -> None:
    gateway = FakeGateway(forced=[GatewayUnreachable("down")] * 3)
    alice = as_user(with_atlassian(services, gateway), ALICE)
    response = alice.post("/connect", data={"csrf_token": csrf(alice, "/connect")})
    assert "unavailable" in response.text and CONSENT_PREFIX not in response.text
    assert connect_events(world)[0][0] == "failed"


def test_connect_needs_atlassian_configured_and_a_live_credential(services: Services) -> None:
    alice = as_user(services, ALICE)
    assert "not configured" in alice.get("/connect").text
    assert alice.post("/connect", data={"csrf_token": csrf(alice, "/connect")}).status_code == 503
    with_atlassian(services, FakeGateway())
    for session_id in list(services.sessions._sessions):  # the credential lapses
        services.credentials.remove_session(session_id)
    assert alice.post("/connect", data={"csrf_token": csrf(alice, "/connect")}).status_code == 401


def test_connect_needs_the_csrf_token(services: Services) -> None:
    alice = as_user(with_atlassian(services, FakeGateway()), ALICE)
    assert alice.post("/connect", data={"csrf_token": "forged"}).status_code == 403


@pytest.mark.parametrize("path", ["/connect"])
def test_connect_needs_a_session(services: Services, path: str) -> None:
    from fastapi.testclient import TestClient

    from stewardry.web.app import create_app
    from tests.unit.test_web import BASE

    anonymous = TestClient(create_app(services), base_url=BASE, follow_redirects=False)
    assert anonymous.get(path).status_code == 401
