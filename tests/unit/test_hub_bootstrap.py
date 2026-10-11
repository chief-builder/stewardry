"""`stewardry hub bootstrap` against a fake Keycloak (S14, tier: offline).

The same behavior is checked against a real Keycloak in the stack tier
(tests/stack/test_hub_bootstrap_stack.py)."""

import json
import stat
from pathlib import Path
from typing import Any

import httpx2
import pytest
from sqlalchemy import Engine

from stewardry.cli import main
from stewardry.platform import hub_bootstrap as hb
from stewardry.platform.access import AccessMapping, apply_access_mapping
from stewardry.platform.audit import AuditLog
from tests.fakes.keycloak import ADMIN_PASSWORD, ADMIN_USER, BASE, ISSUER, FakeKeycloak
from tests.unit.conftest import ROOT

ADDITIONS = json.loads((ROOT / "deploy/hub/stewardry-realm-additions.json").read_text())
EXAMPLE = ROOT / "deploy/access/access-mapping.example.yaml"
PERSONAS = ["legal", "platform", "auditor", "owner", "sme"]


def run(fake: FakeKeycloak, secrets_file: Path, **kwargs: Any) -> hb.BootstrapResult:
    with httpx2.Client(transport=fake.transport()) as http:
        admin = hb.KeycloakAdmin(BASE, "mcp", http)
        admin.login(ISSUER, ADMIN_USER, ADMIN_PASSWORD)
        return hb.bootstrap(admin, ISSUER, ADDITIONS, secrets_file, **kwargs)


def test_first_run_adds_the_client_and_personas(tmp_path: Path) -> None:
    fake = FakeKeycloak()
    secrets_file = tmp_path / "hub" / ".secrets.env"
    result = run(fake, secrets_file)
    assert result.client == "created"
    assert result.created_users == PERSONAS
    assert result.existing_users == ["alice", "bob"]
    assert all(result.fixed_ids_kept.values())
    assert result.subjects["platform"] == "91a7f000-0000-4000-8000-000000000004"
    assert result.broker_config_unchanged

    client = next(c for c in fake.clients if c["clientId"] == "stewardry")
    assert client["redirectUris"] == ["http://localhost:8700/auth/callback"]
    assert client["optionalClientScopes"] == ["mcp-gateway"]
    assert client["directAccessGrantsEnabled"] is False
    assert client["serviceAccountsEnabled"] is False
    assert client["attributes"]["pkce.code.challenge.method"] == "S256"
    assert "offline_access" not in json.dumps(client)
    (mapper,) = client["protocolMappers"]  # only the username claim, ID token only
    assert mapper["config"]["claim.name"] == "preferred_username"
    assert mapper["config"]["access.token.claim"] == "false"
    for user in fake.imported:
        assert "groups" not in user and "realmRoles" not in user
        assert user["credentials"][0]["temporary"] is False

    assert stat.S_IMODE(secrets_file.stat().st_mode) == 0o600
    entries = hb.read_secrets(secrets_file)
    assert set(entries) == {hb.CLIENT_ENTRY} | {hb.persona_entry(u) for u in PERSONAS}
    assert entries[hb.CLIENT_ENTRY] == client["secret"]
    assert sorted(result.entries_written) == sorted(entries)
    assert not any(v in repr(result) for v in entries.values())


def test_a_second_run_changes_nothing(tmp_path: Path) -> None:
    fake = FakeKeycloak()
    secrets_file = tmp_path / ".secrets.env"
    first = run(fake, secrets_file)
    before = secrets_file.read_bytes()
    writes = len(fake.writes())
    second = run(fake, secrets_file)
    assert len(fake.writes()) == writes  # no POSTs the second time
    assert secrets_file.read_bytes() == before
    assert second.client == "existed" and second.created_users == []
    assert second.entries_written == [] and second.drift == []
    assert second.subjects == first.subjects


def test_an_existing_client_that_differs_is_reported_not_changed(tmp_path: Path) -> None:
    fake = FakeKeycloak()
    fake.clients.append(
        {"id": "x", "clientId": "stewardry", "publicClient": True, "redirectUris": ["http://x"]}
    )
    result = run(fake, tmp_path / ".secrets.env")
    assert ("POST", "/admin/realms/mcp/clients") not in fake.requests
    assert any("publicClient differs" in d for d in result.drift)
    assert any("redirectUris differs" in d for d in result.drift)
    assert any("secret is not in" in d for d in result.drift)
    assert hb.CLIENT_ENTRY not in hb.read_secrets(tmp_path / ".secrets.env")


def test_when_ids_are_not_kept_the_real_subjects_are_proposed(
    tmp_path: Path, engine: Engine, log: AuditLog
) -> None:
    fake = FakeKeycloak(keep_ids=False)
    result = run(fake, tmp_path / ".secrets.env")
    assert result.fixed_ids_kept == dict.fromkeys(PERSONAS, False)
    mapping = AccessMapping.model_validate(hb.proposed_mapping(EXAMPLE, result.subjects))
    by_name = {u.username: u for u in mapping.users}
    assert by_name["platform"].sub == result.subjects["platform"] != ADDITIONS["users"][1]["id"]
    assert by_name["alice"].sub == "a11ce000-0000-4000-8000-000000000001"

    # After a stack recreate the subjects change again: applying the new proposal
    # is refused until a platform engineer re-maps those users explicitly.
    from stewardry.platform.access import AccessError

    platform_sub = by_name["platform"].sub
    apply_access_mapping(engine, log, platform_sub, mapping, bootstrap=True, remap=frozenset())
    recreated = run(FakeKeycloak(keep_ids=False), tmp_path / "other.env")
    again = AccessMapping.model_validate(hb.proposed_mapping(EXAMPLE, recreated.subjects))
    with pytest.raises(AccessError, match="remap"):
        apply_access_mapping(engine, log, platform_sub, again, bootstrap=False, remap=frozenset())
    apply_access_mapping(
        engine,
        log,
        platform_sub,
        again,
        bootstrap=False,
        remap=frozenset({"legal", "platform", "auditor", "owner", "sme"}),
    )


def test_sales2_is_optional(tmp_path: Path) -> None:
    result = run(FakeKeycloak(), tmp_path / ".secrets.env", include_optional=True)
    assert result.created_users == [*PERSONAS, "sales2"]
    mapping = hb.proposed_mapping(EXAMPLE, result.subjects)
    sales2 = next(u for u in mapping["users"] if u["username"] == "sales2")
    assert sales2["roles"] == ["sales"] and sales2["customers"] == ["quillon-synthetic"]


@pytest.mark.parametrize(
    "issuer",
    [
        "http://hub.example.com/realms/mcp",
        "https://10.0.0.5/realms/mcp",
        "http://localhost:8180/realms/master",
        "http://localhost:8180/",
        "ftp://localhost/realms/mcp",
    ],
)
def test_only_a_local_hub_realm_is_accepted(issuer: str) -> None:
    with pytest.raises(hb.BootstrapError):
        hb.check_issuer(issuer, "mcp")


def test_a_hub_naming_another_issuer_never_gets_the_admin_credential(tmp_path: Path) -> None:
    fake = FakeKeycloak(issuer="http://localhost:9999/realms/mcp")
    with pytest.raises(hb.BootstrapError, match="different issuer"):
        run(fake, tmp_path / ".secrets.env")
    assert ("POST", "/realms/master/protocol/openid-connect/token") not in fake.requests
    assert fake.writes() == []
    assert not (tmp_path / ".secrets.env").exists()


def test_a_change_to_broker_clients_stops_the_bootstrap(tmp_path: Path) -> None:
    with pytest.raises(hb.BootstrapError, match="broker's clients"):
        run(FakeKeycloak(tamper_on_import=True), tmp_path / ".secrets.env")


def test_missing_broker_users_stop_the_bootstrap(tmp_path: Path) -> None:
    fake = FakeKeycloak()
    fake.users = [u for u in fake.users if u["username"] != "bob"]
    with pytest.raises(hb.BootstrapError, match="bob"):
        run(fake, tmp_path / ".secrets.env")


def test_a_wrong_admin_credential_fails_cleanly() -> None:
    with httpx2.Client(transport=FakeKeycloak().transport()) as http:
        admin = hb.KeycloakAdmin(BASE, "mcp", http)
        with pytest.raises(hb.BootstrapError, match="admin sign-in failed"):
            admin.login(ISSUER, ADMIN_USER, "wrong")


# ------------------------------------------------------------------ CLI


def test_the_command_prints_names_never_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeKeycloak()
    real_client = httpx2.Client
    monkeypatch.setattr(
        httpx2, "Client", lambda **kw: real_client(transport=fake.transport(), **kw)
    )
    monkeypatch.setenv("STEWARDRY_HUB_ISSUER", ISSUER)
    monkeypatch.setenv("KEYCLOAK_ADMIN_USER", ADMIN_USER)
    monkeypatch.setenv("KEYCLOAK_ADMIN_PASSWORD", ADMIN_PASSWORD)
    secrets_file = tmp_path / ".secrets.env"
    mapping_out = tmp_path / "proposed.yaml"
    args = [
        "hub",
        "bootstrap",
        "--secrets-file",
        str(secrets_file),
        "--mapping-out",
        str(mapping_out),
    ]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert "client stewardry: created" in out
    assert hb.CLIENT_ENTRY in out and "(0600)" in out
    for value in [*hb.read_secrets(secrets_file).values(), ADMIN_PASSWORD]:
        assert value not in out
    assert "platform" in mapping_out.read_text()

    assert main(args) == 0
    second = capsys.readouterr().out
    assert "users created: none" in second and "wrote" not in second


def test_the_command_names_missing_settings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("STEWARDRY_HUB_ISSUER", "KEYCLOAK_ADMIN_USER", "KEYCLOAK_ADMIN_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    assert main(["hub", "bootstrap"]) == 2
    assert "KEYCLOAK_ADMIN_PASSWORD" in capsys.readouterr().err


def test_the_command_refuses_a_remote_hub(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("STEWARDRY_HUB_ISSUER", "https://hub.example.com/realms/mcp")
    monkeypatch.setenv("KEYCLOAK_ADMIN_USER", ADMIN_USER)
    monkeypatch.setenv("KEYCLOAK_ADMIN_PASSWORD", ADMIN_PASSWORD)
    assert main(["hub", "bootstrap"]) == 1
    assert "local hub" in capsys.readouterr().err
