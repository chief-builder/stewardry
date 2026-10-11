"""A small synthetic world for identity and grant tests."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine

from stewardry.platform import registry
from stewardry.platform.access import AccessMapping, apply_access_mapping
from stewardry.platform.audit import AuditLog
from stewardry.platform.policy import load_agent_policy, load_baseline
from stewardry.platform.redaction import Redactor

ROOT = Path(__file__).resolve().parents[2]
AGENT = "questionnaire-agent"
ALICE, BOB, CAROL, PLATFORM, OWNER = "sub-alice", "sub-bob", "sub-carol", "sub-plat", "sub-owner"
LARKSPUR, QUILLON = "larkspur-synthetic", "quillon-synthetic"


def mapping_data() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "customers": [
            {"customer_id": LARKSPUR, "display_name": "Larkspur"},
            {"customer_id": QUILLON, "display_name": "Quillon"},
        ],
        "users": [
            {
                "username": "alice",
                "sub": ALICE,
                "display_name": "Alice",
                "roles": ["sales"],
                "customers": [LARKSPUR],
            },
            {
                "username": "bob",
                "sub": BOB,
                "display_name": "Bob",
                "roles": ["grc"],
                "customers": [LARKSPUR, QUILLON],
            },
            {
                "username": "carol",
                "sub": CAROL,
                "display_name": "Carol",
                "roles": ["grc"],
                "customers": [QUILLON],
            },
            {
                "username": "platform",
                "sub": PLATFORM,
                "display_name": "Platform",
                "roles": ["platform_engineer"],
            },
            {
                "username": "owner",
                "sub": OWNER,
                "display_name": "Owner",
                "roles": ["engagement_owner"],
            },
        ],
    }


@pytest.fixture
def log() -> AuditLog:
    return AuditLog(Redactor())


def populate(engine: Engine, log: AuditLog, mapping: dict[str, Any] | None = None) -> None:
    """People applied by bootstrap; the questionnaire agent registered and in build."""
    apply_access_mapping(
        engine,
        log,
        PLATFORM,
        AccessMapping.model_validate(mapping or mapping_data()),
        bootstrap=True,
    )
    registry.apply_policy(
        engine,
        log,
        PLATFORM,
        load_agent_policy(ROOT / "policies" / "agents" / "questionnaire-agent.yaml"),
        load_baseline(ROOT / "policies" / "platform-baseline.yaml"),
    )
    registry.transition_status(engine, log, OWNER, AGENT, "build", "docs/brief.md")


@pytest.fixture
def world(engine: Engine, log: AuditLog) -> Iterator[Engine]:
    populate(engine, log)
    yield engine
