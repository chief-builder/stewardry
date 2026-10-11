"""Records the drafting smoke scenario against the live model (tier: live-model).

Skipped unless STEWARDRY_LIVE_MODEL=1. Run with `make record-drafting`. It
needs Anthropic credentials the SDK can find and costs a little money (four
questionnaire items; the per-run budget in the agent policy caps it).
Atlassian is the fixture adapter, so the run is labeled `live-model-eval`,
never `connected` (ADR 0007). The recordings and their outcomes are written
to replay/drafting-smoke/, where the offline replay test reads them.
"""

import datetime as dt
import json
import os
import shutil
from pathlib import Path

import pytest
from sqlalchemy import Engine

from stewardry.platform.audit import AuditLog
from stewardry.platform.model import AnthropicTransport, load_limits
from stewardry.platform.policy import load_agent_policy
from stewardry.platform.redaction import Redactor
from stewardry.platform.replay import RecordingTransport, ReplayStore
from tests.fakes.drafting_setup import AS_OF, make_setup
from tests.fakes.drafting_smoke import (
    MANIFEST,
    QUESTIONS,
    RECORDINGS,
    outcomes,
    smoke_questionnaire,
)
from tests.unit.conftest import ROOT

pytestmark = [
    pytest.mark.live_model,
    pytest.mark.skipif(
        os.environ.get("STEWARDRY_LIVE_MODEL") != "1",
        reason="live-model tier: set STEWARDRY_LIVE_MODEL=1 (make record-drafting)",
    ),
]


def test_record_the_drafting_smoke_scenario(world: Engine, log: AuditLog, tmp_path: Path) -> None:
    setup = make_setup(world, log, tmp_path, *smoke_questionnaire(tmp_path))
    shutil.rmtree(RECORDINGS, ignore_errors=True)
    limits = load_limits(ROOT / "policies/model-limits.yaml")
    transport = RecordingTransport(
        AnthropicTransport(limits), ReplayStore(RECORDINGS / "model"), Redactor()
    )
    run = setup.start("live-model-eval")
    assert setup.drafting(transport).run(run) is True
    result = outcomes(world)
    assert sorted(result) == sorted(QUESTIONS)
    model = load_agent_policy(ROOT / "policies/agents/questionnaire-agent.yaml").policy.model
    MANIFEST.write_text(
        json.dumps(
            {
                "provenance": "live-model-eval",
                "note": "Recorded live model responses; Atlassian was the fixture adapter.",
                "recorded_on": dt.date.today().isoformat(),
                "model": model.id,
                "effort": model.effort,
                "prompt_version": model.prompt_version,
                "as_of": AS_OF.isoformat(),
                "outcomes": result,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    for question_id, outcome in result.items():
        print(question_id, outcome["status"], outcome["expert_reason"], outcome["citations"])
