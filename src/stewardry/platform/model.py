"""Controlled model access (spec sections 4.7 and 4.8, ADR 0003).

Every model call goes through `ModelAccess.call`, which:

1. passes the same authority boundary as tool calls (`Executor.check_boundary`):
   run running, agent runnable, grant, role, customer;
2. matches the transport to the run's provenance: live calls only for
   `connected` and `live-model-eval` runs, recorded responses only for
   `offline-replay` runs, and scripted responses (test doubles and docs
   examples) only for `scripted-example` runs (ADR 0007);
3. uses the model settings of the run's bound policy version, and refuses if
   the current version changed them since (a gated change, ADR 0009);
4. enforces budgets before calling: model calls and estimated USD per run (the
   stricter of the bound and current versions), and USD per UTC day;
5. retries 429, 5xx, and network errors a bounded number of times, auditing
   every attempt;
6. records model ID, prompt version, settings, usage, latency, request hash,
   and estimated cost under a named pricing reference. Missing usage is stored
   as unknown (NULL), never zero, and stops the run because its cost cannot be
   bounded.

A refusal (`stop_reason == "refusal"`) is a bounded failure: it is recorded
with its category and returned, and the caller routes that item to a person.
There is no server-side fallback to another model (ADR 0003).
"""

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Literal, NoReturn, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, func, insert, select

from stewardry.platform.audit import AuditLog
from stewardry.platform.db.schema import run_counters, usage_records, utcnow
from stewardry.platform.execution import Executor, RunContext, Verdict, increment_counters
from stewardry.platform.policy import Policy

LIVE_PROVENANCE = frozenset({"connected", "live-model-eval"})
REPLAY_PROVENANCE = frozenset({"offline-replay"})
SCRIPTED_PROVENANCE = frozenset({"scripted-example"})
TransportKind = Literal["live", "replay", "scripted"]
ALLOWED_PROVENANCE = {
    "live": LIVE_PROVENANCE,
    "replay": REPLAY_PROVENANCE,
    "scripted": SCRIPTED_PROVENANCE,
}


# ------------------------------------------------------------- settings files


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelPrice(_Strict):
    input: Decimal = Field(ge=0)
    output: Decimal = Field(ge=0)
    cache_write_5m: Decimal = Field(ge=0)
    cache_write_1h: Decimal = Field(ge=0)
    cache_read: Decimal = Field(ge=0)


class PricingTable(_Strict):
    schema_version: Literal[1]
    pricing_ref: str = Field(min_length=1)
    source: str
    retrieved_on: str
    currency: Literal["USD"]
    unit: Literal["per million tokens"]
    notes: str = ""
    models: dict[str, ModelPrice]


class ModelLimits(_Strict):
    schema_version: Literal[1]
    max_tokens: int = Field(gt=0, le=128_000)
    timeout_seconds: float = Field(gt=0)
    max_attempts: int = Field(ge=1, le=10)
    backoff_seconds: list[float] = Field(default_factory=list)
    daily_usd: Decimal = Field(gt=0)

    def backoff(self, attempt: int) -> float:
        """Seconds to wait before attempt `attempt + 1`."""
        if not self.backoff_seconds:
            return 0.0
        return self.backoff_seconds[min(attempt - 1, len(self.backoff_seconds) - 1)]


def load_pricing(path: Path) -> PricingTable:
    return PricingTable.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def load_limits(path: Path) -> ModelLimits:
    return ModelLimits.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


# --------------------------------------------------------- requests, usage


@dataclass(frozen=True)
class ModelRequest:
    """What the caller asks. Model ID, effort, and limits come from policy."""

    system: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_5m_tokens: int | None
    cache_write_1h_tokens: int | None

    @classmethod
    def from_response(cls, response: Mapping[str, Any]) -> Usage | None:
        usage = response.get("usage")
        if not isinstance(usage, Mapping):
            return None
        creation = usage.get("cache_creation")
        if isinstance(creation, Mapping):
            write_5m = creation.get("ephemeral_5m_input_tokens")
            write_1h = creation.get("ephemeral_1h_input_tokens")
        else:  # no breakdown: every write is a 5-minute write (all Stewardry requests)
            write_5m, write_1h = usage.get("cache_creation_input_tokens"), 0
        return cls(
            usage.get("input_tokens"),
            usage.get("output_tokens"),
            usage.get("cache_read_input_tokens"),
            write_5m,
            write_1h,
        )

    @property
    def complete(self) -> bool:
        return None not in (self.input_tokens, self.output_tokens)

    @property
    def cache_write_tokens(self) -> int | None:
        if self.cache_write_5m_tokens is None and self.cache_write_1h_tokens is None:
            return None
        return (self.cache_write_5m_tokens or 0) + (self.cache_write_1h_tokens or 0)


def estimate_cost_micro_usd(usage: Usage | None, price: ModelPrice | None) -> int | None:
    """Micro-USD (tokens x USD-per-million), or None when anything is unknown."""
    if usage is None or price is None or not usage.complete:
        return None
    parts = [
        (usage.input_tokens, price.input),
        (usage.output_tokens, price.output),
        (usage.cache_read_tokens or 0, price.cache_read),
        (usage.cache_write_5m_tokens or 0, price.cache_write_5m),
        (usage.cache_write_1h_tokens or 0, price.cache_write_1h),
    ]
    total = sum((Decimal(tokens or 0) * rate for tokens, rate in parts), Decimal(0))
    return int(total.quantize(Decimal(1), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class ModelResponse:
    content: list[dict[str, Any]]
    stop_reason: str | None
    refusal_category: str | None
    usage: Usage | None
    model: str
    request_hash: str
    latency_ms: int
    attempts: int
    estimated_cost_micro_usd: int | None

    @property
    def refused(self) -> bool:
        return self.stop_reason == "refusal"

    def text(self) -> str:
        return "".join(b.get("text", "") for b in self.content if b.get("type") == "text")


def build_payload(policy: Policy, limits: ModelLimits, request: ModelRequest) -> dict[str, Any]:
    """The exact request body. Thinking is omitted: Claude Opus 5.5 always uses
    adaptive thinking, and effort is the only control (ADR 0003)."""
    payload: dict[str, Any] = {
        "model": policy.model.id,
        "max_tokens": limits.max_tokens,
        "system": request.system,
        "messages": request.messages,
        "output_config": {"effort": policy.model.effort},
    }
    if request.tools:
        payload["tools"] = request.tools
    return payload


def request_hash(payload: Mapping[str, Any], prompt_version: str) -> str:
    """Canonical hash of everything that determines the response."""
    text = json.dumps(
        {"payload": payload, "prompt_version": prompt_version},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- transports


class TransportRetryable(Exception):
    """Rate limit, server error, or network failure: worth another attempt."""


class TransportFatal(Exception):
    """Any other failure: retrying will not help."""


class Transport(Protocol):
    kind: TransportKind

    def send(self, payload: Mapping[str, Any], request_hash: str) -> dict[str, Any]: ...


class AnthropicTransport:
    """Live calls through the official SDK. The SDK's own retries are off so
    every attempt is counted and audited here. The API key is resolved by the
    SDK from the environment (or an `ant auth` profile) and never passed in."""

    kind: TransportKind = "live"

    def __init__(self, limits: ModelLimits, client: Any = None) -> None:
        if client is None:
            import anthropic

            client = anthropic.Anthropic(max_retries=0, timeout=limits.timeout_seconds)
        self._client = client

    def send(self, payload: Mapping[str, Any], request_hash: str) -> dict[str, Any]:
        import anthropic

        try:
            message = self._client.messages.create(**payload)
        except (
            anthropic.RateLimitError,
            anthropic.InternalServerError,
            anthropic.APIConnectionError,
        ) as exc:
            raise TransportRetryable(type(exc).__name__) from exc
        except anthropic.APIStatusError as exc:
            raise TransportFatal(f"{type(exc).__name__} ({exc.status_code})") from exc
        except TypeError as exc:
            # The SDK raises TypeError before sending when it finds no credentials.
            if "authentication method" in str(exc):
                raise TransportFatal(
                    "no Anthropic credentials found; set ANTHROPIC_API_KEY or run `ant auth login`"
                ) from exc
            raise
        result: dict[str, Any] = message.to_dict()
        return result

    def close(self) -> None:
        """Close the SDK client's connection pool."""
        self._client.close()


# -------------------------------------------------------------- the boundary


class ModelAccess:
    def __init__(
        self,
        engine: Engine,
        log: AuditLog,
        executor: Executor,
        transport: Transport,
        pricing: PricingTable,
        limits: ModelLimits,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.engine = engine
        self.log = log
        self.executor = executor
        self.transport = transport
        self.pricing = pricing
        self.limits = limits
        self.sleep = sleep

    def call(self, run: RunContext, request: ModelRequest) -> ModelResponse:
        bound, current = self.executor.check_boundary(run, "model")
        if run.provenance not in ALLOWED_PROVENANCE[self.transport.kind]:
            self._fail(
                run, f"a {self.transport.kind} model transport cannot serve a {run.provenance} run"
            )
        if current.model != bound.model:
            self._fail(run, "model settings changed since this run started; start a new run")
        price = self.pricing.models.get(bound.model.id)
        if price is None:
            self._fail(run, f"no pricing reference for model {bound.model.id}")
        self._check_budgets(run, bound, current)

        payload = build_payload(bound, self.limits, request)
        digest = request_hash(payload, bound.model.prompt_version)
        base = {
            "request_hash": digest,
            "model": bound.model.id,
            "prompt_version": bound.model.prompt_version,
            "effort": bound.model.effort,
            "max_tokens": self.limits.max_tokens,
            "transport": self.transport.kind,
        }

        attempt = 0
        while True:
            attempt += 1
            started = time.monotonic()
            try:
                raw = self.transport.send(payload, digest)
                break
            except TransportRetryable as exc:
                self._audit(run, "failed", base | {"attempt": attempt, "error": str(exc)})
                if attempt >= self.limits.max_attempts:
                    self._fail(run, f"model unavailable after {attempt} attempts")
                self.sleep(self.limits.backoff(attempt))
            except TransportFatal as exc:
                self._audit(run, "failed", base | {"attempt": attempt, "error": str(exc)})
                self._fail(run, f"model request failed: {exc}")
            except Exception as exc:
                # Anything unexpected still leaves the run in a definite, audited state.
                error = type(exc).__name__
                self._audit(run, "failed", base | {"attempt": attempt, "error": error})
                self._fail(run, f"model request failed unexpectedly ({error})")
        latency_ms = int((time.monotonic() - started) * 1000)

        usage = Usage.from_response(raw)
        cost = estimate_cost_micro_usd(usage, price)
        details = raw.get("stop_details")
        response = ModelResponse(
            content=list(raw.get("content") or []),
            stop_reason=raw.get("stop_reason"),
            refusal_category=details.get("category") if isinstance(details, Mapping) else None,
            usage=usage,
            model=str(raw.get("model", bound.model.id)),
            request_hash=digest,
            latency_ms=latency_ms,
            attempts=attempt,
            estimated_cost_micro_usd=cost,
        )
        self._record(run, bound, response)
        if usage is None or not usage.complete:
            self._fail(run, "the response carried no usage; its cost cannot be bounded")
        return response

    # ------------------------------------------------------------- budgets

    def _spent(self, run: RunContext | None) -> int:
        query = select(func.coalesce(func.sum(usage_records.c.estimated_cost_micro_usd), 0))
        if run is not None:
            query = query.where(usage_records.c.run_id == run.run_id)
        else:
            midnight = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            query = query.where(usage_records.c.recorded_at >= midnight)
        with self.engine.connect() as conn:
            return int(conn.execute(query).scalar_one())

    def _check_budgets(self, run: RunContext, bound: Policy, current: Policy) -> None:
        with self.engine.connect() as conn:
            calls = (
                conn.execute(
                    select(run_counters.c.value).where(
                        run_counters.c.run_id == run.run_id, run_counters.c.name == "model_calls"
                    )
                ).scalar()
                or 0
            )
        max_calls = min(bound.budgets.per_run_model_calls, current.budgets.per_run_model_calls)
        run_usd = min(bound.budgets.per_run_usd, current.budgets.per_run_usd)
        if calls >= max_calls:
            self._fail(run, "model-call budget exhausted")
        if self._spent(run) >= run_usd * 1_000_000:
            self._fail(run, "per-run spend budget exhausted")
        if self._spent(None) >= self.limits.daily_usd * 1_000_000:
            self._fail(run, "daily spend limit reached")

    # ----------------------------------------------------------- recording

    def _record(self, run: RunContext, bound: Policy, response: ModelResponse) -> None:
        usage = response.usage
        cost = response.estimated_cost_micro_usd
        with self.engine.begin() as conn:
            conn.execute(
                insert(usage_records).values(
                    run_id=run.run_id,
                    model_id=bound.model.id,
                    prompt_version=bound.model.prompt_version,
                    input_tokens=usage.input_tokens if usage else None,
                    output_tokens=usage.output_tokens if usage else None,
                    cache_read_tokens=usage.cache_read_tokens if usage else None,
                    cache_write_tokens=usage.cache_write_tokens if usage else None,
                    estimated_cost_micro_usd=cost,
                    pricing_ref=self.pricing.pricing_ref if cost is not None else None,
                    latency_ms=response.latency_ms,
                    request_hash=response.request_hash,
                    stop_reason=response.stop_reason,
                    attempts=response.attempts,
                    recorded_at=utcnow(),
                )
            )
            increment_counters(conn, run.run_id, "model_calls")
            outcome = "failed" if response.refused else "succeeded"
            self.log.append(
                conn,
                self.executor.event(
                    run,
                    "model.call",
                    outcome,
                    {
                        "request_hash": response.request_hash,
                        "model": bound.model.id,
                        "response_model": response.model,
                        "prompt_version": bound.model.prompt_version,
                        "effort": bound.model.effort,
                        "stop_reason": response.stop_reason,
                        "refusal_category": response.refusal_category,
                        "usage": None if usage is None else usage.__dict__,
                        "estimated_cost_micro_usd": cost,
                        "pricing_ref": self.pricing.pricing_ref if cost is not None else None,
                        "latency_ms": response.latency_ms,
                        "attempts": response.attempts,
                        "transport": self.transport.kind,
                    },
                ),
            )

    def _audit(self, run: RunContext, outcome: str, details: dict[str, Any]) -> None:
        with self.engine.begin() as conn:
            self.log.append(conn, self.executor.event(run, "model.call", outcome, details))

    def _fail(self, run: RunContext, reason: str) -> NoReturn:
        self.executor.stop(run, Verdict(False, reason, "fail"), {}, "model.call")
