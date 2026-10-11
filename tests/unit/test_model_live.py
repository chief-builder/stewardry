"""Live-model smoke test: one small, real Anthropic API call (tier: live-model).

Skipped unless STEWARDRY_LIVE_MODEL=1. Run with `make test-live-model`.
It needs credentials the SDK can find (ANTHROPIC_API_KEY or an `ant auth`
profile) and costs a little money. Results are labeled live-model, never
connected (ADR 0007).
"""

import datetime as dt
import os
from decimal import Decimal

import pytest
from sqlalchemy import Engine, select

from stewardry.platform.audit import AuditLog
from stewardry.platform.credentials import CredentialStore, GatewayCredential
from stewardry.platform.db.schema import usage_records
from stewardry.platform.execution import Executor
from stewardry.platform.grants import authorize_stage, submit_task
from stewardry.platform.model import (
    AnthropicTransport,
    ModelAccess,
    ModelRequest,
    load_limits,
    load_pricing,
)
from stewardry.platform.policy import load_baseline
from tests.unit.conftest import AGENT, ALICE, BOB, LARKSPUR, ROOT

pytestmark = [
    pytest.mark.live_model,
    pytest.mark.skipif(
        os.environ.get("STEWARDRY_LIVE_MODEL") != "1",
        reason="live-model tier: set STEWARDRY_LIVE_MODEL=1 (make test-live-model)",
    ),
]


def test_one_real_call_is_recorded_with_usage_and_cost(world: Engine, log: AuditLog) -> None:
    credentials = CredentialStore()
    credentials.put(
        "s",
        BOB,
        GatewayCredential("unused-gateway-token", dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)),
    )
    executor = Executor(
        world,
        log,
        credentials,
        load_baseline(ROOT / "policies/platform-baseline.yaml"),
        {},
        lambda ref: frozenset(),
    )
    task_id = submit_task(world, log, ALICE, AGENT, LARKSPUR, "live-smoke")
    authorize_stage(world, log, BOB, task_id)
    run = executor.start_run(task_id, "drafting", "live-model-eval")
    limits = load_limits(ROOT / "policies/model-limits.yaml").model_copy(
        update={"max_tokens": 1024, "daily_usd": Decimal("1.00")}
    )
    model = ModelAccess(
        world,
        log,
        executor,
        AnthropicTransport(limits),
        load_pricing(ROOT / "policies/model-pricing.yaml"),
        limits,
    )
    response = model.call(
        run,
        ModelRequest(
            system="Answer in one word.",
            messages=[{"role": "user", "content": "Reply with the word: ready"}],
        ),
    )
    assert response.stop_reason in {"end_turn", "max_tokens"}
    assert response.usage is not None and response.usage.complete
    with world.connect() as conn:
        row = conn.execute(select(usage_records)).one()
    assert row.estimated_cost_micro_usd is not None and row.estimated_cost_micro_usd > 0
    print(
        f"live-model smoke: {response.model}, {row.input_tokens} in / {row.output_tokens} out,"
        f" ~${row.estimated_cost_micro_usd / 1_000_000:.4f}, {response.latency_ms} ms"
    )
