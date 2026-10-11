"""A drafting world for tests: a submitted task with an ingested
questionnaire, drafting authorized by Bob, and the fixture policies served
by a fake gateway (ADR 0007: the "fixture adapter")."""

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine

from stewardry.engagements.questionnaire.drafting import Drafting
from stewardry.engagements.questionnaire.ingest import ingest_questionnaire
from stewardry.platform.artifacts import ArtifactStore
from stewardry.platform.atlassian.adapter import AtlassianReads, AtlassianSettings
from stewardry.platform.audit import AuditLog
from stewardry.platform.catalog import Catalogs
from stewardry.platform.credentials import CredentialStore, GatewayCredential
from stewardry.platform.execution import Executor, RunContext
from stewardry.platform.grants import authorize_stage, submit_task
from stewardry.platform.model import ModelAccess, ModelLimits, Transport, load_limits, load_pricing
from stewardry.platform.policy import load_baseline
from tests.fakes.gateway import CLOUD_ID, CONSENT_PREFIX
from tests.fakes.policy_pages import PolicyGateway
from tests.unit.conftest import AGENT, ALICE, BOB, LARKSPUR, ROOT

AS_OF = dt.date(2026, 10, 10)  # the eval dataset's as_of: freshness is stable
CATALOG = "policies/catalogs/secpol.yaml"
TOKEN = f"token-{BOB}"


@dataclass
class Setup:
    world: Engine
    log: AuditLog
    credentials: CredentialStore
    gateway: PolicyGateway
    executor: Executor
    task_id: str

    def drafting(self, transport: Transport, limits: ModelLimits | None = None) -> Drafting:
        access = ModelAccess(
            self.world,
            self.log,
            self.executor,
            transport,
            load_pricing(ROOT / "policies/model-pricing.yaml"),
            limits or load_limits(ROOT / "policies/model-limits.yaml"),
            sleep=lambda _: None,
        )
        return Drafting(
            self.world,
            self.log,
            self.executor,
            access,
            Catalogs(ROOT).get(CATALOG),
            as_of=lambda: AS_OF,
        )

    def start(self, provenance: str = "scripted-example") -> RunContext:
        return self.executor.start_run(self.task_id, "drafting", provenance)


def make_setup(
    world: Engine,
    log: AuditLog,
    tmp_path: Path,
    filename: str,
    data: bytes,
    customer: str = LARKSPUR,
    submission_key: str = "req-1",
) -> Setup:
    credentials = CredentialStore()
    later = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
    credentials.put("s-bob", BOB, GatewayCredential(TOKEN, later))
    gateway = PolicyGateway(connected={TOKEN})
    adapter = AtlassianReads(
        gateway, AtlassianSettings(CLOUD_ID, CONSENT_PREFIX), sleep=lambda _: None
    )
    catalogs = Catalogs(ROOT)
    executor = Executor(
        world,
        log,
        credentials,
        load_baseline(ROOT / "policies/platform-baseline.yaml"),
        adapter.tool_specs(catalogs),
        catalogs,
    )
    task_id = submit_task(world, log, ALICE, AGENT, customer, submission_key, "SQR-1")
    ingest_questionnaire(
        world, log, ArtifactStore(tmp_path / "artifacts"), ALICE, task_id, filename, data
    )
    authorize_stage(world, log, BOB, task_id)
    return Setup(world, log, credentials, gateway, executor, task_id)
