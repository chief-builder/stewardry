"""Access mapping: bootstrap, declarative apply, remapping, and sign-in decisions."""

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select

from stewardry.platform.access import (
    AccessError,
    AccessMapping,
    SignInRefused,
    apply_access_mapping,
    list_people,
    load_access_mapping,
    resolve_sign_in,
)
from stewardry.platform.audit import AuditLog, verify
from stewardry.platform.db.schema import audit_events, users
from tests.unit.conftest import ALICE, BOB, LARKSPUR, PLATFORM, QUILLON, ROOT, mapping_data


def mapping(data: dict[str, Any] | None = None) -> AccessMapping:
    return AccessMapping.model_validate(data or mapping_data())


def people(engine: Engine) -> dict[str, tuple[set[str], set[str]]]:
    with engine.connect() as conn:
        return {p.username: (set(p.roles), set(p.customers)) for p in list_people(conn)}


def outcomes(engine: Engine, event_type: str) -> list[str]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                select(audit_events.c.outcome)
                .where(audit_events.c.event_type == event_type)
                .order_by(audit_events.c.seq)
            ).scalars()
        )


def user(data: dict[str, Any], username: str) -> dict[str, Any]:
    entry: dict[str, Any] = next(u for u in data["users"] if u["username"] == username)
    return entry


# ----------------------------------------------------------------- apply


def test_bootstrap_creates_people_roles_and_customers(engine: Engine, log: AuditLog) -> None:
    summary = apply_access_mapping(engine, log, PLATFORM, mapping(), bootstrap=True)
    assert set(summary.users_added) == {"alice", "bob", "carol", "platform", "owner"}
    assert people(engine)["alice"] == ({"sales"}, {LARKSPUR})
    assert people(engine)["bob"] == ({"grc"}, {LARKSPUR, QUILLON})
    assert outcomes(engine, "access.mapping_applied") == ["succeeded"]


def test_bootstrap_only_works_once(engine: Engine, log: AuditLog) -> None:
    apply_access_mapping(engine, log, PLATFORM, mapping(), bootstrap=True)
    with pytest.raises(AccessError, match="before any platform engineer exists"):
        apply_access_mapping(engine, log, PLATFORM, mapping(), bootstrap=True)


def test_bootstrap_must_make_the_actor_a_platform_engineer(engine: Engine, log: AuditLog) -> None:
    with pytest.raises(AccessError, match="acting user a platform_engineer"):
        apply_access_mapping(engine, log, ALICE, mapping(), bootstrap=True)
    assert people(engine) == {}
    assert outcomes(engine, "access.mapping_applied") == ["denied"]


def test_only_a_platform_engineer_can_apply(engine: Engine, log: AuditLog) -> None:
    apply_access_mapping(engine, log, PLATFORM, mapping(), bootstrap=True)
    with pytest.raises(AccessError, match="requires the platform_engineer role"):
        apply_access_mapping(engine, log, BOB, mapping())
    assert outcomes(engine, "access.mapping_applied") == ["succeeded", "denied"]


def test_apply_is_declarative(engine: Engine, log: AuditLog) -> None:
    apply_access_mapping(engine, log, PLATFORM, mapping(), bootstrap=True)
    data = mapping_data()
    user(data, "bob")["customers"] = [QUILLON]
    data["users"] = [u for u in data["users"] if u["username"] != "carol"]
    summary = apply_access_mapping(engine, log, PLATFORM, mapping(data))
    assert people(engine)["bob"] == ({"grc"}, {QUILLON})
    assert people(engine)["carol"] == (set(), set())  # row kept for history
    assert summary.users_unmapped == ("carol",)
    assert "bob/customer:larkspur-synthetic" in summary.customers_revoked

    again = apply_access_mapping(engine, log, PLATFORM, mapping(data))
    assert again.roles_assigned == again.roles_revoked == ()


def test_changed_hub_subject_needs_an_explicit_remap(engine: Engine, log: AuditLog) -> None:
    apply_access_mapping(engine, log, PLATFORM, mapping(), bootstrap=True)
    data = mapping_data()
    user(data, "alice")["sub"] = "sub-alice-new"
    with pytest.raises(AccessError, match="--remap alice"):
        apply_access_mapping(engine, log, PLATFORM, mapping(data))

    summary = apply_access_mapping(engine, log, PLATFORM, mapping(data), remap=frozenset({"alice"}))
    assert summary.users_remapped == ("alice",)
    found = people(engine)
    assert found["alice"] == ({"sales"}, {LARKSPUR})
    assert found[f"alice~{ALICE[:12]}"] == (set(), set())  # old row keeps history, no access
    with engine.connect() as conn:
        assert conn.execute(select(users.c.username).where(users.c.sub == ALICE)).scalar() == (
            f"alice~{ALICE[:12]}"
        )
    assert outcomes(engine, "access.user_remapped") == ["succeeded"]


def test_a_username_cannot_be_taken_from_another_user(engine: Engine, log: AuditLog) -> None:
    """Moving a username to a different existing person needs the same explicit remap."""
    apply_access_mapping(engine, log, PLATFORM, mapping(), bootstrap=True)
    data = mapping_data()
    user(data, "alice")["username"] = "alice-old"
    user(data, "carol")["username"] = "alice"
    with pytest.raises(AccessError, match="--remap alice"):
        apply_access_mapping(engine, log, PLATFORM, mapping(data))
    assert people(engine)["alice"] == ({"sales"}, {LARKSPUR})


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d["users"].append(dict(d["users"][0])), "unique username"),
        (lambda d: user(d, "bob").update(sub="sub-alice"), "unique sub"),
        (lambda d: user(d, "bob").update(customers=["ghost"]), "unknown customers"),
        (lambda d: user(d, "platform").update(roles=["auditor"]), "at least one platform"),
        (lambda d: user(d, "bob").update(roles=["admin"]), "unknown role"),
        (lambda d: user(d, "bob").update(username="bob~x"), "username"),
    ],
)
def test_invalid_mappings_are_refused(change: Any, message: str) -> None:
    data = mapping_data()
    change(data)
    with pytest.raises(ValueError, match=message):
        mapping(data)


def test_example_mapping_file_is_valid(tmp_path: Path) -> None:
    example = load_access_mapping(ROOT / "deploy" / "access" / "access-mapping.example.yaml")
    assert {u.username for u in example.users} >= {"alice", "bob", "platform", "owner"}
    (tmp_path / "bad.yaml").write_text("users: [")
    with pytest.raises(AccessError):
        load_access_mapping(tmp_path / "bad.yaml")


# ---------------------------------------------------------------- sign-in


def test_provisioned_user_signs_in_with_roles_and_customers(engine: Engine, log: AuditLog) -> None:
    apply_access_mapping(engine, log, PLATFORM, mapping(), bootstrap=True)
    signed_in = resolve_sign_in(engine, log, BOB, "bob")
    assert signed_in.roles == {"grc"} and signed_in.customers == {LARKSPUR, QUILLON}
    assert outcomes(engine, "auth.sign_in") == ["succeeded"]


@pytest.mark.parametrize(
    ("sub", "username", "message"),
    [
        ("sub-stranger", "stranger", "not provisioned"),
        ("sub-imposter", "alice", "different hub subject"),
    ],
)
def test_sign_in_refusals(
    engine: Engine, log: AuditLog, sub: str, username: str, message: str
) -> None:
    apply_access_mapping(engine, log, PLATFORM, mapping(), bootstrap=True)
    with pytest.raises(SignInRefused, match=message):
        resolve_sign_in(engine, log, sub, username)
    assert outcomes(engine, "auth.sign_in") == ["denied"]


def test_user_without_roles_cannot_sign_in(engine: Engine, log: AuditLog) -> None:
    apply_access_mapping(engine, log, PLATFORM, mapping(), bootstrap=True)
    data = mapping_data()
    data["users"] = [u for u in data["users"] if u["username"] != "bob"]
    apply_access_mapping(engine, log, PLATFORM, mapping(data))
    with pytest.raises(SignInRefused, match="no roles"):
        resolve_sign_in(engine, log, BOB, "bob")
    with engine.connect() as conn:
        assert verify(conn).ok
