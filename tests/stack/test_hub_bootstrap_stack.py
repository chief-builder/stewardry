"""`stewardry hub bootstrap` against a real Keycloak (S14, tier: stack).

Uses the broker's pinned Keycloak image and realm file (read-only). Checks
idempotence, that the broker's clients, scopes, and users are byte-identical
afterwards, whether fixed user IDs survive partial import, and a full
browser-style sign-in to Stewardry as a bootstrapped persona.
"""

import base64
import json
import re
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from stewardry.platform import hub_bootstrap as hb
from stewardry.platform.access import AccessMapping, apply_access_mapping
from stewardry.platform.artifacts import ArtifactStore
from stewardry.platform.audit import AuditLog
from stewardry.platform.credentials import CredentialStore
from stewardry.platform.grants import TaskError, authorize_stage, submit_task
from stewardry.web.app import Services, create_app
from stewardry.web.hub import HubClient
from stewardry.web.sessions import SessionStore
from stewardry.web.settings import WebSettings
from tests.stack.conftest import Hub

pytestmark = pytest.mark.stack

ROOT = Path(__file__).resolve().parents[2]
ADDITIONS = json.loads((ROOT / "deploy/hub/stewardry-realm-additions.json").read_text())
EXAMPLE = ROOT / "deploy/access/access-mapping.example.yaml"
APP = "http://localhost:8700"


def admin_session(hub: Hub, http: httpx2.Client) -> hb.KeycloakAdmin:
    admin = hb.KeycloakAdmin(hub.base, "mcp", http)
    admin.login(hub.issuer, hub.admin_user, hub.admin_password)
    return admin


def broker_state(admin: hb.KeycloakAdmin) -> str:
    """Everything the broker's realm defines, as canonical JSON."""
    clients = [c for c in admin.clients() if c["clientId"] != "stewardry"]
    users = [admin.find_user(u) for u in ("alice", "bob")]
    return json.dumps(
        {"clients": clients, "scopes": admin.client_scopes(), "users": users}, sort_keys=True
    )


@pytest.fixture(scope="module")
def bootstrapped(hub: Hub, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    secrets_file = tmp_path_factory.mktemp("hub") / ".secrets.env"
    with httpx2.Client(timeout=30) as http:
        admin = admin_session(hub, http)
        before = broker_state(admin)
        first = hb.bootstrap(admin, hub.issuer, ADDITIONS, secrets_file)
        middle = broker_state(admin)
        second = hb.bootstrap(admin, hub.issuer, ADDITIONS, secrets_file)
        after = broker_state(admin)
        client = next(c for c in admin.clients() if c["clientId"] == "stewardry")
    return {
        "first": first,
        "second": second,
        "client": client,
        "secrets": secrets_file,
        "states": (before, middle, after),
    }


def test_the_bootstrap_is_idempotent(bootstrapped: dict[str, Any]) -> None:
    first, second = bootstrapped["first"], bootstrapped["second"]
    assert first.client == "created" and second.client == "existed"
    assert first.created_users == ["legal", "platform", "auditor", "owner", "sme"]
    assert second.created_users == [] and second.entries_written == []
    assert second.drift == []
    assert second.subjects == first.subjects


def test_broker_clients_scopes_and_users_are_byte_identical(bootstrapped: dict[str, Any]) -> None:
    before, middle, after = bootstrapped["states"]
    assert before == middle == after


def test_the_client_has_only_the_agreed_settings(bootstrapped: dict[str, Any]) -> None:
    client = bootstrapped["client"]
    assert client["publicClient"] is False and client["directAccessGrantsEnabled"] is False
    assert client["serviceAccountsEnabled"] is False
    assert client["redirectUris"] == ["http://localhost:8700/auth/callback"]
    assert client["attributes"]["pkce.code.challenge.method"] == "S256"
    assert "offline_access" not in client.get("optionalClientScopes", [])
    assert "offline_access" not in client.get("defaultClientScopes", [])
    assert "mcp-gateway" in client.get("optionalClientScopes", [])
    names = [m["name"] for m in client.get("protocolMappers", [])]
    assert names == ["stewardry-username"]


def test_fixed_ids_after_partial_import(bootstrapped: dict[str, Any]) -> None:
    """ADR 0008 left open whether partial import keeps a supplied ID. Either way
    the proposed mapping carries the subjects the hub actually has."""
    first = bootstrapped["first"]
    kept = set(first.fixed_ids_kept.values())
    assert len(kept) == 1  # all or nothing
    print(f"\nfixed IDs kept by partial import: {kept.pop()}")
    mapping = AccessMapping.model_validate(hb.proposed_mapping(EXAMPLE, first.subjects))
    assert {u.username: u.sub for u in mapping.users} == first.subjects


def _sign_in(app: TestClient, username: str, password: str) -> httpx2.Response:
    """Drive the hub's login form the way a browser would."""
    start = app.get("/login")
    assert start.status_code == 303
    with httpx2.Client(timeout=30, follow_redirects=True) as browser:
        page = browser.get(start.headers["location"])
        action = re.search(r'<form[^>]*id="kc-form-login"[^>]*action="([^"]+)"', page.text)
        assert action, "no login form"
        # Keycloak marks its login cookies Secure even on http://localhost (a
        # browser treats localhost as a secure context); an HTTP client does not
        # send Secure cookies over http, so send them explicitly here.
        cookie = "; ".join(f"{c.name}={c.value}" for c in browser.cookies.jar)
        done = browser.post(
            action[1].replace("&amp;", "&"),
            data={"username": username, "password": password, "credentialId": ""},
            headers={"Cookie": cookie},
            follow_redirects=False,
        )
    assert done.status_code == 302, f"the hub did not accept the sign-in: {done.status_code}"
    location = httpx2.URL(done.headers["location"])
    assert str(location).startswith(f"{APP}/auth/callback")
    return app.get("/auth/callback", params=dict(location.params))


def test_a_persona_signs_in_and_platform_has_no_source_access(
    hub: Hub, bootstrapped: dict[str, Any], engine: Engine, tmp_path: Path
) -> None:
    secrets_file = bootstrapped["secrets"]
    entries = hb.read_secrets(secrets_file)
    subjects = bootstrapped["first"].subjects
    log = AuditLog()
    mapping = AccessMapping.model_validate(hb.proposed_mapping(EXAMPLE, subjects))
    apply_access_mapping(engine, log, subjects["platform"], mapping, bootstrap=True)
    from stewardry.platform import registry
    from stewardry.platform.policy import load_agent_policy, load_baseline

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

    settings = WebSettings(APP, hub.issuer, "stewardry", entries[hb.CLIENT_ENTRY])
    with httpx2.Client(timeout=30) as http:
        client = HubClient(
            hub.issuer, "stewardry", settings.hub_client_secret, settings.redirect_uri, http
        )
        credentials = CredentialStore()
        services = Services(
            settings,
            engine,
            client,
            log,
            SessionStore(),
            credentials,
            ArtifactStore(tmp_path / "a"),
        )
        app = TestClient(create_app(services), base_url=APP, follow_redirects=False)
        response = _sign_in(app, "platform", entries[hb.persona_entry("platform")])
        assert response.status_code == 303, response.text
        assert "Roles: platform_engineer." in app.get("/").text
        assert app.get("/registry").status_code == 200

    # The token Stewardry holds is a gateway token (audience = the gateway resource).
    import datetime as dt

    credential = credentials.get(subjects["platform"], dt.datetime.now(dt.UTC))
    assert credential is not None
    payload = credential.access_token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    audiences = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
    assert "http://localhost:8500/mcp" in audiences and claims["azp"] == "stewardry"
    assert "groups" not in claims

    # The platform role is not a drafting role: it can never act on sources.
    task_id = submit_task(
        engine, log, subjects["alice"], "questionnaire-agent", "larkspur-synthetic", "stack-1"
    )
    with pytest.raises(TaskError, match="role"):
        authorize_stage(engine, log, subjects["platform"], task_id)
