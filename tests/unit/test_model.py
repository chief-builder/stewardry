"""Model access boundary: costs, budgets, retries, refusals, provenance, replay."""

import datetime as dt
import json
import socket
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import anthropic
import httpx2
import pytest
import yaml
from sqlalchemy import Engine, select

from stewardry.platform import registry
from stewardry.platform.audit import AuditLog
from stewardry.platform.credentials import CredentialStore, GatewayCredential
from stewardry.platform.db.schema import audit_events, run_counters, runs, usage_records
from stewardry.platform.execution import Executor, RunContext, RunStopped
from stewardry.platform.grants import authorize_stage, submit_task
from stewardry.platform.model import (
    AnthropicTransport,
    ModelAccess,
    ModelLimits,
    ModelPrice,
    ModelRequest,
    PricingTable,
    TransportFatal,
    TransportRetryable,
    Usage,
    build_payload,
    estimate_cost_micro_usd,
    load_limits,
    load_pricing,
    request_hash,
)
from stewardry.platform.policy import AgentPolicyFile, load_agent_policy, load_baseline
from stewardry.platform.redaction import Redactor
from stewardry.platform.replay import (
    NetworkBlocked,
    RecordingTransport,
    ReplayMiss,
    ReplayStore,
    ReplayTransport,
    ReplayUnsafe,
    replay_guard,
)
from tests.unit.conftest import AGENT, ALICE, BOB, LARKSPUR, PLATFORM, ROOT

BASELINE = load_baseline(ROOT / "policies/platform-baseline.yaml")
PRICING = load_pricing(ROOT / "policies/model-pricing.yaml")
LIMITS = load_limits(ROOT / "policies/model-limits.yaml")
POLICY = load_agent_policy(ROOT / "policies/agents/questionnaire-agent.yaml").policy
REQUEST = ModelRequest(
    system="You draft cited answers.",
    messages=[{"role": "user", "content": "Q1: Do you encrypt data?"}],
)


def message(
    stop_reason: str = "end_turn", usage: dict[str, Any] | None = None, **extra: Any
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": "Yes, per SEC-3 section 2."}],
        "stop_reason": stop_reason,
        "stop_details": None,
        "usage": usage
        if usage is not None
        else {
            "input_tokens": 1000,
            "output_tokens": 500,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    }
    return body | extra


class FakeTransport:
    def __init__(self, script: list[Any], kind: Literal["live", "replay"] = "live") -> None:
        self.script = list(script)
        self.kind = kind
        self.payloads: list[dict[str, Any]] = []

    def send(self, payload: Any, request_hash: str) -> dict[str, Any]:
        self.payloads.append(dict(payload))
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(step, Exception):
            raise step
        result: dict[str, Any] = step
        return result


@pytest.fixture
def executor(world: Engine, log: AuditLog) -> Iterator[Executor]:
    store = CredentialStore()
    store.put(
        "s-bob",
        BOB,
        GatewayCredential("token-bob", dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)),
    )
    yield Executor(world, log, store, BASELINE, {}, lambda ref: frozenset())


def start(
    world: Engine,
    log: AuditLog,
    executor: Executor,
    provenance: str = "live-model-eval",
    key: str = "req-1",
) -> RunContext:
    task_id = submit_task(world, log, ALICE, AGENT, LARKSPUR, key)
    authorize_stage(world, log, BOB, task_id)
    return executor.start_run(task_id, "drafting", provenance)


def access(
    world: Engine,
    log: AuditLog,
    executor: Executor,
    transport: Any,
    limits: ModelLimits = LIMITS,
    pricing: PricingTable = PRICING,
) -> ModelAccess:
    sleeps: list[float] = []
    model = ModelAccess(world, log, executor, transport, pricing, limits, sleep=sleeps.append)
    model.sleeps = sleeps  # type: ignore[attr-defined]
    return model


def apply(world: Engine, log: AuditLog, **policy_changes: Any) -> None:
    data = yaml.safe_load((ROOT / "policies/agents/questionnaire-agent.yaml").read_text())
    data["policy"].update(policy_changes)
    registry.apply_policy(world, log, PLATFORM, AgentPolicyFile.model_validate(data), BASELINE)


def status(world: Engine, run: RunContext) -> str:
    with world.connect() as conn:
        return str(
            conn.execute(select(runs.c.status).where(runs.c.run_id == run.run_id)).scalar_one()
        )


def model_events(world: Engine) -> list[tuple[str, dict[str, Any]]]:
    with world.connect() as conn:
        rows = conn.execute(
            select(audit_events.c.outcome, audit_events.c.details_json)
            .where(audit_events.c.event_type == "model.call")
            .order_by(audit_events.c.seq)
        ).all()
    return [(r.outcome, json.loads(r.details_json)) for r in rows]


# -------------------------------------------------------------- pure parts


def test_cost_is_tokens_times_dated_prices() -> None:
    usage = Usage(1000, 500, 2000, 100, 10)
    price = PRICING.models["claude-opus-5-5"]
    # 1000*4 + 500*20 + 2000*0.20 + 100*5 + 10*8 micro-USD
    assert estimate_cost_micro_usd(usage, price) == 4000 + 10000 + 400 + 500 + 80
    assert PRICING.pricing_ref == "anthropic-pricing-2026-10-10"
    assert price.input == Decimal("4.00") and price.cache_read == Decimal("0.20")


def test_unknown_usage_or_price_gives_unknown_cost() -> None:
    price = PRICING.models["claude-opus-5-5"]
    assert estimate_cost_micro_usd(None, price) is None
    assert estimate_cost_micro_usd(Usage(None, 10, 0, 0, 0), price) is None
    assert estimate_cost_micro_usd(Usage(10, 10, 0, 0, 0), None) is None


def test_usage_parsing() -> None:
    with_breakdown = Usage.from_response(
        {
            "usage": {
                "input_tokens": 5,
                "output_tokens": 6,
                "cache_read_input_tokens": 7,
                "cache_creation_input_tokens": 9,
                "cache_creation": {"ephemeral_5m_input_tokens": 4, "ephemeral_1h_input_tokens": 5},
            }
        }
    )
    assert with_breakdown == Usage(5, 6, 7, 4, 5)
    assert with_breakdown.cache_write_tokens == 9
    flat = Usage.from_response(
        {"usage": {"input_tokens": 1, "output_tokens": 2, "cache_creation_input_tokens": 3}}
    )
    assert flat == Usage(1, 2, None, 3, 0)
    assert Usage.from_response({}) is None
    assert Usage(1, 1, None, None, None).cache_write_tokens is None


def test_payload_comes_from_policy_and_limits() -> None:
    payload = build_payload(POLICY, LIMITS, REQUEST)
    assert payload["model"] == "claude-opus-5-5"
    assert payload["output_config"] == {"effort": "medium"}
    assert payload["max_tokens"] == LIMITS.max_tokens
    assert "thinking" not in payload and "tools" not in payload  # Opus 5.5: always adaptive
    tooled = build_payload(POLICY, LIMITS, ModelRequest("s", [], [{"name": "submit_answer"}]))
    assert tooled["tools"] == [{"name": "submit_answer"}]
    assert request_hash(payload, "a@1") == request_hash(dict(payload), "a@1")
    assert request_hash(payload, "a@1") != request_hash(payload, "a@2")


def test_limits_backoff_schedule() -> None:
    assert [LIMITS.backoff(1), LIMITS.backoff(2), LIMITS.backoff(5)] == [2, 8, 8]
    assert (
        ModelLimits(
            schema_version=1, max_tokens=1, timeout_seconds=1, max_attempts=1, daily_usd=Decimal(1)
        ).backoff(1)
        == 0.0
    )


# ------------------------------------------------------------- live calls


def test_successful_call_records_usage_cost_and_audit(
    world: Engine, log: AuditLog, executor: Executor
) -> None:
    run = start(world, log, executor)
    transport = FakeTransport([message()])
    response = access(world, log, executor, transport).call(run, REQUEST)
    assert response.text() == "Yes, per SEC-3 section 2." and not response.refused
    assert response.estimated_cost_micro_usd == 1000 * 4 + 500 * 20
    with world.connect() as conn:
        row = conn.execute(select(usage_records)).one()
        calls = conn.execute(
            select(run_counters.c.value).where(run_counters.c.name == "model_calls")
        ).scalar_one()
    assert (row.model_id, row.prompt_version, row.input_tokens, row.output_tokens) == (
        "claude-opus-5-5",
        "questionnaire-draft@1",
        1000,
        500,
    )
    assert row.pricing_ref == PRICING.pricing_ref and row.attempts == 1
    assert row.request_hash == response.request_hash and calls == 1
    outcome, details = model_events(world)[-1]
    assert outcome == "succeeded" and details["effort"] == "medium"
    assert "SEC-3 section 2" not in json.dumps(details)  # content is not audited
    assert "Do you encrypt" not in json.dumps(details)


@pytest.mark.parametrize(
    ("transport_kind", "provenance"),
    [("replay", "live-model-eval"), ("live", "offline-replay"), ("live", "scripted-example")],
)
def test_transport_must_match_run_provenance(
    world: Engine, log: AuditLog, executor: Executor, transport_kind: Any, provenance: str
) -> None:
    run = start(world, log, executor, provenance)
    model = access(world, log, executor, FakeTransport([message()], transport_kind))
    with pytest.raises(RunStopped, match="cannot serve"):
        model.call(run, REQUEST)
    assert status(world, run) == "failed"


def test_retries_are_bounded_and_audited(world: Engine, log: AuditLog, executor: Executor) -> None:
    run = start(world, log, executor)
    transport = FakeTransport(
        [TransportRetryable("RateLimitError"), TransportRetryable("InternalServerError"), message()]
    )
    model = access(world, log, executor, transport)
    assert model.call(run, REQUEST).attempts == 3
    assert model.sleeps == [2, 8]  # type: ignore[attr-defined]
    assert [o for o, _ in model_events(world)] == ["failed", "failed", "succeeded"]

    stuck = start(world, log, executor, key="req-2")
    always = access(world, log, executor, FakeTransport([TransportRetryable("APITimeoutError")]))
    with pytest.raises(RunStopped, match="unavailable after 3 attempts"):
        always.call(stuck, REQUEST)
    assert status(world, stuck) == "failed"


def test_non_retryable_errors_fail_at_once(
    world: Engine, log: AuditLog, executor: Executor
) -> None:
    run = start(world, log, executor)
    transport = FakeTransport([TransportFatal("BadRequestError (400)"), message()])
    with pytest.raises(RunStopped, match="BadRequestError"):
        access(world, log, executor, transport).call(run, REQUEST)
    assert len(transport.payloads) == 1


def test_refusal_is_a_bounded_failure_and_the_run_continues(
    world: Engine, log: AuditLog, executor: Executor
) -> None:
    run = start(world, log, executor)
    refusal = message(
        "refusal", stop_details={"type": "refusal", "category": "cyber", "explanation": "declined"}
    )
    response = access(world, log, executor, FakeTransport([refusal])).call(run, REQUEST)
    assert response.refused and response.refusal_category == "cyber"
    outcome, details = model_events(world)[-1]
    assert outcome == "failed" and details["refusal_category"] == "cyber"
    assert status(world, run) == "running"


def test_missing_usage_is_unknown_not_zero_and_stops_the_run(
    world: Engine, log: AuditLog, executor: Executor
) -> None:
    run = start(world, log, executor)
    no_usage = message()
    no_usage.pop("usage")
    with pytest.raises(RunStopped, match="cost cannot be bounded"):
        access(world, log, executor, FakeTransport([no_usage])).call(run, REQUEST)
    with world.connect() as conn:
        row = conn.execute(select(usage_records)).one()
    assert row.input_tokens is None and row.estimated_cost_micro_usd is None
    assert row.pricing_ref is None


# ------------------------------------------------------------------ budgets


def test_model_call_budget_fails_the_run(world: Engine, log: AuditLog, executor: Executor) -> None:
    apply(
        world, log, budgets={"per_run_usd": 5, "per_run_model_calls": 2, "per_run_tool_calls": 10}
    )
    run = start(world, log, executor)
    model = access(world, log, executor, FakeTransport([message()]))
    model.call(run, REQUEST)
    model.call(run, REQUEST)
    with pytest.raises(RunStopped, match="model-call budget exhausted"):
        model.call(run, REQUEST)
    assert status(world, run) == "failed"


def test_spend_budget_per_run_and_per_day(world: Engine, log: AuditLog, executor: Executor) -> None:
    expensive = message(usage={"input_tokens": 1_000_000, "output_tokens": 100_000})  # $6
    run = start(world, log, executor)
    model = access(world, log, executor, FakeTransport([expensive]))
    model.call(run, REQUEST)  # the first call is allowed; it spends $6 of the $5 budget
    with pytest.raises(RunStopped, match="per-run spend budget exhausted"):
        model.call(run, REQUEST)

    other = start(world, log, executor, key="req-2")
    tight = LIMITS.model_copy(update={"daily_usd": Decimal("1")})
    with pytest.raises(RunStopped, match="daily spend limit reached"):
        access(world, log, executor, FakeTransport([message()]), limits=tight).call(other, REQUEST)


def test_missing_price_fails_the_run(world: Engine, log: AuditLog, executor: Executor) -> None:
    run = start(world, log, executor)
    empty = PRICING.model_copy(
        update={
            "models": {
                "other-model": ModelPrice(
                    input=1, output=1, cache_write_5m=1, cache_write_1h=1, cache_read=1
                )
            }
        }
    )
    with pytest.raises(RunStopped, match="no pricing reference"):
        access(world, log, executor, FakeTransport([message()]), pricing=empty).call(run, REQUEST)


# ------------------------------------------------------------ boundaries


def test_model_change_mid_run_stops_the_run(
    world: Engine, log: AuditLog, executor: Executor
) -> None:
    run = start(world, log, executor)
    apply(
        world,
        log,
        model={
            "id": "claude-opus-5-5",
            "effort": "high",
            "prompt_version": "questionnaire-draft@1",
        },
    )
    with pytest.raises(RunStopped, match="model settings changed"):
        access(world, log, executor, FakeTransport([message()])).call(run, REQUEST)


def test_paused_agent_stops_the_next_model_call(
    world: Engine, log: AuditLog, executor: Executor
) -> None:
    run = start(world, log, executor)
    registry.pause(world, log, PLATFORM, AGENT, "incident")
    transport = FakeTransport([message()])
    with pytest.raises(RunStopped, match="paused") as stopped:
        access(world, log, executor, transport).call(run, REQUEST)
    assert stopped.value.kind == "pause" and transport.payloads == []


def test_keys_never_reach_the_audit_log(
    world: Engine, executor: Executor, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "sk-" + "ant-" + "k" * 40
    monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    log = AuditLog(Redactor.from_environment())
    executor.log = log
    run = start(world, log, executor)
    transport = FakeTransport([TransportRetryable(f"connection reset ({key})"), message()])
    access(world, log, executor, transport).call(run, REQUEST)
    with world.connect() as conn:
        stored = json.dumps(
            [list(r) for r in conn.execute(select(audit_events)).all()], default=str
        )
    assert key not in stored and "[REDACTED" in stored


# --------------------------------------------------------- SDK transport


def _status_error(cls: type[anthropic.APIStatusError], code: int) -> anthropic.APIStatusError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("error", response=httpx2.Response(code, request=request), body=None)


class FakeSdk:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.messages = self

    def create(self, **payload: Any) -> Any:
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class FakeMessage:
    def to_dict(self) -> dict[str, Any]:
        return message()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_status_error(anthropic.RateLimitError, 429), TransportRetryable),
        (_status_error(anthropic.InternalServerError, 529), TransportRetryable),
        (anthropic.APITimeoutError(httpx2.Request("POST", "https://x")), TransportRetryable),
        (_status_error(anthropic.BadRequestError, 400), TransportFatal),
        (_status_error(anthropic.AuthenticationError, 401), TransportFatal),
    ],
)
def test_sdk_errors_map_to_retryable_or_fatal(error: Exception, expected: type) -> None:
    with pytest.raises(expected):
        AnthropicTransport(LIMITS, client=FakeSdk(error)).send({}, "h")


def test_sdk_success_returns_a_plain_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    assert AnthropicTransport(LIMITS, client=FakeSdk(FakeMessage())).send({}, "h")["id"] == (
        "msg_test"
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-" + "ant-" + "x" * 30)
    assert AnthropicTransport(LIMITS).kind == "live"  # builds the real client, no request sent


# ---------------------------------------------------------------- replay


def test_recording_then_offline_replay_reproduces_the_response(
    world: Engine,
    log: AuditLog,
    executor: Executor,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ReplayStore(tmp_path / "replay" / "model")
    live = start(world, log, executor, "live-model-eval")
    recorder = RecordingTransport(FakeTransport([message()]), store, Redactor())
    recorded = access(world, log, executor, recorder).call(live, REQUEST)

    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_PROFILE",
        "STEWARDRY_HUB_CLIENT_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    with replay_guard():
        replay_run = start(world, log, executor, "offline-replay", key="req-2")
        replayed = access(world, log, executor, ReplayTransport(store)).call(replay_run, REQUEST)
        with pytest.raises(RunStopped, match="no recording"):
            other = ModelRequest("You draft cited answers.", [{"role": "user", "content": "?"}])
            access(world, log, executor, ReplayTransport(store)).call(replay_run, other)
    assert replayed.content == recorded.content
    assert replayed.request_hash == recorded.request_hash


def test_replay_guard_refuses_credentials_and_blocks_the_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-" + "ant-" + "y" * 30)
    with pytest.raises(ReplayUnsafe, match="ANTHROPIC_API_KEY") as refused, replay_guard():
        pass
    assert "sk-ant" not in str(refused.value)
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with replay_guard(), pytest.raises(NetworkBlocked):
        socket.create_connection(("192.0.2.1", 443))
    # Restored afterwards (the test suite's own guard is what remains).
    with pytest.raises(RuntimeError, match="offline tests"):
        socket.create_connection(("192.0.2.1", 443))


def test_recordings_are_redacted_and_names_checked(tmp_path: Path) -> None:
    store = ReplayStore(tmp_path)
    secret = "sk-" + "ant-" + "z" * 30
    store.save("a" * 64, {"messages": [secret]}, {"content": []}, Redactor())
    assert secret not in (tmp_path / f"{'a' * 64}.json").read_text()
    with pytest.raises(ValueError, match="not a request hash"):
        store.load("../etc/passwd")
    with pytest.raises(ReplayMiss):
        store.load("b" * 64)
    with pytest.raises(ValueError, match="only a live transport"):
        RecordingTransport(ReplayTransport(store), store, Redactor())


def test_missing_credentials_fail_the_run_with_a_clear_message(
    world: Engine, log: AuditLog, executor: Executor, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = TypeError(
        '"Could not resolve authentication method. Expected one of api_key, '
        'auth_token, or credentials to be set."'
    )
    with pytest.raises(TransportFatal, match="no Anthropic credentials found"):
        AnthropicTransport(LIMITS, client=FakeSdk(error)).send({}, "h")
    with pytest.raises(TypeError, match="unrelated"):
        AnthropicTransport(LIMITS, client=FakeSdk(TypeError("unrelated"))).send({}, "h")

    run = start(world, log, executor)
    transport = AnthropicTransport(LIMITS, client=FakeSdk(error))
    with pytest.raises(RunStopped, match="ANTHROPIC_API_KEY"):
        access(world, log, executor, transport).call(run, REQUEST)
    assert status(world, run) == "failed"


def test_unexpected_transport_errors_still_fail_the_run_cleanly(
    world: Engine, log: AuditLog, executor: Executor
) -> None:
    run = start(world, log, executor)
    transport = FakeTransport([KeyError("surprise")])
    with pytest.raises(RunStopped, match="unexpectedly \\(KeyError\\)"):
        access(world, log, executor, transport).call(run, REQUEST)
    assert status(world, run) == "failed"
    assert model_events(world)[0][1]["error"] == "KeyError"
