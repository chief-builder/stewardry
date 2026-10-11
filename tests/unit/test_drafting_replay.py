"""Offline replay of the recorded drafting smoke scenario (tier: offline).

Replays the live-model recordings in replay/drafting-smoke/ with the network
blocked and no credentials, and checks that the workflow reproduces the
recorded outcomes. A replay shows the workflow is reproducible and behaves as
recorded; it does not show current model quality (spec section 1). Skipped
until `make record-drafting` has produced recordings.
"""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select

from stewardry.platform.audit import AuditLog
from stewardry.platform.db import migrate
from stewardry.platform.db.engine import make_engine
from stewardry.platform.db.schema import runs, usage_records
from stewardry.platform.redaction import Redactor
from stewardry.platform.replay import (
    CREDENTIAL_ENV_VARS,
    RecordingTransport,
    ReplayStore,
    ReplayTransport,
    replay_guard,
)
from tests.fakes.drafting_setup import make_setup
from tests.fakes.drafting_smoke import MANIFEST, RECORDINGS, outcomes, smoke_questionnaire
from tests.fakes.scripted_model import ScriptedModel
from tests.unit.conftest import populate
from tests.unit.test_drafting import PLANS


@pytest.mark.skipif(
    not MANIFEST.exists(), reason="no drafting recordings yet (make record-drafting)"
)
def test_replay_reproduces_the_recorded_outcomes(
    world: Engine, log: AuditLog, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path / "no-anthropic-config"))
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    setup = make_setup(world, log, tmp_path, *smoke_questionnaire(tmp_path))
    run = setup.start("offline-replay")
    with replay_guard():
        done = setup.drafting(ReplayTransport(ReplayStore(RECORDINGS / "model"))).run(run)
    assert done is True
    result = json.loads(json.dumps(outcomes(world)))  # tuples -> lists, as in the manifest
    assert result == manifest["outcomes"]
    with world.connect() as conn:
        assert conn.execute(select(runs.c.provenance)).scalar() == "offline-replay"
        recorded = conn.execute(select(usage_records.c.request_hash)).scalars().all()
    assert {f"{h}.json" for h in recorded} <= {p.name for p in (RECORDINGS / "model").iterdir()}


class FakeLive:
    """The scripted model posing as a live transport, only to prove that a
    recorded drafting run replays request for request (no evidence value)."""

    kind = "live"

    def __init__(self) -> None:
        self.inner = ScriptedModel(PLANS)

    def send(self, payload: Mapping[str, Any], request_hash: str) -> dict[str, Any]:
        return self.inner.send(payload, request_hash)


def test_a_recorded_run_replays_request_for_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path / "no-anthropic-config"))
    store = ReplayStore(tmp_path / "model")
    results = []
    for provenance in ("live-model-eval", "offline-replay"):
        (tmp_path / provenance).mkdir()
        world, log = make_engine(tmp_path / provenance / "db.sqlite"), AuditLog(Redactor())
        migrate.upgrade(world)
        populate(world, log)
        setup = make_setup(world, log, tmp_path / provenance, *smoke_questionnaire(tmp_path))
        transport: Any = (
            RecordingTransport(FakeLive(), store, Redactor())
            if provenance == "live-model-eval"
            else ReplayTransport(store)
        )
        run = setup.start(provenance)
        if provenance == "offline-replay":
            with replay_guard():
                assert setup.drafting(transport).run(run)
        else:
            assert setup.drafting(transport).run(run)
        results.append(outcomes(world))
        world.dispose()
    assert results[0] == results[1]
    assert len(results[0]) == 4
