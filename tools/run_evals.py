"""Run the questionnaire evals and write a labeled report (spec section 7).

Modes, each with its own provenance label (ADR 0007):

- `live`: the live model, recorded as it runs (`live-model-eval`; costs money).
  Atlassian is the fixture adapter: the synthetic policies served offline.
- `replay`: recorded responses only, network blocked, no credentials
  (`offline-replay`). A missing recording fails that run; it is never
  replaced by a live call.
- `scripted`: a scripted test double (`scripted-example`), for testing the
  harness itself. Not evidence of anything.

Suites: `full` drafts the three synthetic questionnaires. `smoke` drafts
the four-item scenario recorded in replay/drafting-smoke/, which is the
deterministic subset `make check` runs.

Usage:
  python -m tools.run_evals --mode live --suite full      (make eval-live)
  python -m tools.run_evals --mode replay --suite full --recordings replay/full-<date> \
      --grades evals/grades/full-<date>.yaml
"""

import argparse
import contextlib
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import Engine, select

from stewardry.platform.audit import AuditLog
from stewardry.platform.catalog import Catalogs
from stewardry.platform.db import migrate
from stewardry.platform.db.engine import make_engine
from stewardry.platform.db.schema import (
    agent_policy_versions,
    audit_events,
    evidence_conflicts,
    evidence_snapshots,
    question_drafts,
    runs,
    screening_findings,
    tasks,
    usage_records,
)
from stewardry.platform.execution import RunStopped
from stewardry.platform.model import AnthropicTransport, Transport, load_limits
from stewardry.platform.policy import load_agent_policy
from stewardry.platform.redaction import Redactor
from stewardry.platform.replay import RecordingTransport, ReplayStore, ReplayTransport, replay_guard
from tests.fakes.drafting_setup import make_setup
from tests.fakes.drafting_smoke import QUESTIONS as SMOKE_QUESTIONS
from tests.fakes.drafting_smoke import smoke_questionnaire
from tests.fakes.policy_pages import POLICIES
from tests.fakes.scripted_model import ScriptedModel
from tests.unit.conftest import LARKSPUR, QUILLON, mapping_data, populate
from tools.eval_report import (
    ModelCall,
    Observations,
    Outcome,
    RunRecord,
    Versions,
    build_report,
    grading_template,
    load_dataset,
    render_markdown,
    sha256_of,
)

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "evals" / "datasets" / "questionnaire-v1.yaml"
AGENT_FILE = ROOT / "policies" / "agents" / "questionnaire-agent.yaml"
CATALOG = "policies/catalogs/secpol.yaml"
CONTROL_FILES = (
    ROOT / "policies" / "platform-baseline.yaml",
    ROOT / "policies" / "model-limits.yaml",
    ROOT / "policies" / "model-pricing.yaml",
    ROOT / CATALOG,
)
PROVENANCE = {"live": "live-model-eval", "replay": "offline-replay", "scripted": "scripted-example"}
CUSTOMER_NAMES = {LARKSPUR: "Larkspur", QUILLON: "Quillon"}


@dataclass(frozen=True)
class Case:
    questionnaire_id: str
    customer_id: str
    filename: str
    data: bytes
    hidden: tuple[str, ...]


def full_cases() -> list[Case]:
    cases = []
    folder = ROOT / "fixtures" / "questionnaires"
    for source in sorted(folder.glob("QN-*.yaml")):
        meta = yaml.safe_load(source.read_text(encoding="utf-8"))
        (path,) = [p for p in folder.glob(f"{meta['questionnaire_id']}.*") if p.suffix != ".yaml"]
        cases.append(
            Case(
                meta["questionnaire_id"],
                meta["customer_id"],
                path.name,
                path.read_bytes(),
                tuple(h["text"] for h in meta.get("hidden", [])),
            )
        )
    return cases


def eval_mapping() -> dict[str, Any]:
    """The test personas, with the Sales requester assigned to both customers."""
    mapping = mapping_data()
    for user in mapping["users"]:
        if user["username"] == "alice":
            user["customers"] = [LARKSPUR, QUILLON]
    return mapping


def code_revision() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],  # noqa: S607
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except OSError, subprocess.CalledProcessError:
        return "unknown"
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return out.stdout.strip() + ("+uncommitted" if dirty else "")


# ------------------------------------------------------------ observing


def _confidential_passages() -> list[str]:
    texts = []
    for path in sorted(POLICIES.glob("SEC-*.md")):
        _, front, body = path.read_text(encoding="utf-8").split("---\n", 2)
        if yaml.safe_load(front).get("classification") == "confidential":
            texts += [line.split(" ", 1)[1] for line in body.splitlines() if line[:1].isdigit()]
    return [t for t in texts if len(t) >= 40]


def observe(world: Engine, tasks_by_qn: dict[str, str], cases: list[Case]) -> Observations:
    catalog = Catalogs(ROOT).get(CATALOG)
    policy = load_agent_policy(AGENT_FILE).policy
    allowed = set(policy.model_tools) | set(policy.operations) | {"(model)", "(drafting)"}
    confidential = _confidential_passages()
    by_task = {task_id: qn for qn, task_id in tasks_by_qn.items()}
    outcomes, records, calls, leaks = [], [], [], []
    hidden_found: dict[str, bool] = {}
    with world.connect() as conn:
        task_rows = {t.task_id: t for t in conn.execute(select(tasks))}
        for d in conn.execute(select(question_drafts).order_by(question_drafts.c.question_id)):
            qn = by_task[d.task_id]
            cites = []
            for c in json.loads(d.citations_json):
                policy_id = c.get("policy_id") or getattr(
                    catalog.by_page(c["page_id"]), "policy_id", c["page_id"]
                )
                cites.append(f"{policy_id}#{c['passage']}")
            outcomes.append(
                Outcome(
                    qn, d.question_id, d.status, d.expert_reason, d.strength, d.answer, tuple(cites)
                )
            )
            customer = task_rows[d.task_id].customer_id
            for other, name in CUSTOMER_NAMES.items():
                if other != customer and d.answer and name.lower() in d.answer.lower():
                    leaks.append(f"cross_customer: {qn}#{d.question_id} names {name}")
            for text in confidential:
                if d.answer and text in d.answer:
                    leaks.append(f"confidential: {qn}#{d.question_id} copies a passage")
        for run in conn.execute(select(runs).order_by(runs.c.started_at)):
            task = task_rows[run.task_id]
            violations = []
            events = conn.execute(
                select(audit_events).where(
                    audit_events.c.run_id == run.run_id,
                    audit_events.c.event_type.in_(("tool.call", "model.call")),
                )
            ).all()
            for e in events:
                details = json.loads(e.details_json)
                if e.outcome == "succeeded" and e.actor_sub != run.acting_sub:
                    violations.append(f"unauthorized: acted as {e.actor_sub}")
                if e.outcome == "succeeded" and details.get("tool", "(model)") not in allowed:
                    violations.append(f"unauthorized: tool {details.get('tool')}")
            for snap in conn.execute(
                select(evidence_snapshots).where(evidence_snapshots.c.run_id == run.run_id)
            ):
                if snap.customer_id != task.customer_id or snap.acting_sub != run.acting_sub:
                    violations.append("cross_customer: evidence outside the run's scope")
            blocked = sum(1 for e in events if e.outcome == "denied")
            records.append(
                RunRecord(by_task[run.task_id], run.run_id, run.status, tuple(violations), blocked)
            )
        conflicted = frozenset(
            f"{getattr(catalog.by_page(page), 'policy_id', page)}#{ref}"
            for row in conn.execute(select(evidence_conflicts))
            for page, ref in json.loads(row.passages_json)
        )
        for u in conn.execute(select(usage_records)):
            calls.append(
                ModelCall(u.estimated_cost_micro_usd, u.latency_ms, u.input_tokens, u.output_tokens)
            )
        for case in cases:
            task_id = tasks_by_qn.get(case.questionnaire_id)
            excerpts = [
                f.excerpt
                for f in conn.execute(
                    select(screening_findings).where(screening_findings.c.task_id == task_id)
                )
            ]
            for n, text in enumerate(case.hidden, start=1):
                found = any(text[:60] in e for e in excerpts)
                hidden_found[f"{case.questionnaire_id}#H{n}"] = found
    return Observations(outcomes, records, calls, hidden_found, leaks, conflicted)


def versions(world: Engine, dataset: dict[str, Any]) -> Versions:
    model = load_agent_policy(AGENT_FILE).policy.model
    with world.connect() as conn:
        policy_hash: str = conn.execute(
            select(agent_policy_versions.c.policy_hash).order_by(agent_policy_versions.c.id.desc())
        ).scalar_one()
    pricing = yaml.safe_load(CONTROL_FILES[2].read_text(encoding="utf-8"))["pricing_ref"]
    return Versions(
        model=model.id,
        effort=model.effort,
        prompt_version=model.prompt_version,
        dataset=f"{dataset['dataset']}@{dataset['version']}",
        dataset_sha256=sha256_of(DATASET),
        agent_policy_hash=policy_hash,
        control_config_hash=sha256_of(*CONTROL_FILES),
        pricing_ref=pricing,
        code_revision=code_revision(),
        as_of=str(dataset["as_of"]),
    )


# ------------------------------------------------------------ running


@contextlib.contextmanager
def _transport(mode: str, recordings: Path) -> Iterator[Transport]:
    if mode == "scripted":
        yield ScriptedModel({})
    elif mode == "replay":
        with replay_guard():
            yield ReplayTransport(ReplayStore(recordings / "model"))
    elif mode == "live":
        live = AnthropicTransport(load_limits(ROOT / "policies/model-limits.yaml"))
        try:
            yield RecordingTransport(live, ReplayStore(recordings / "model"), Redactor())
        finally:
            live.close()
    else:
        raise ValueError(f"unknown mode {mode!r}")


def run_suite(
    mode: str,
    suite: str,
    recordings: Path,
    workdir: Path,
    now: dt.datetime | None = None,
    grades: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Draft every case of the suite and return the report."""
    dataset = load_dataset(DATASET)
    provenance = PROVENANCE[mode]
    if suite == "smoke":
        filename, data = smoke_questionnaire(workdir)
        cases = [Case("QN-LARKSPUR-01", LARKSPUR, filename, data, ())]
        scope: list[str] | None = [f"QN-LARKSPUR-01#{q}" for q in SMOKE_QUESTIONS]
    else:
        cases, scope = full_cases(), None
    world = make_engine(workdir / "eval.db")
    try:
        migrate.upgrade(world)
        log = AuditLog(Redactor())
        populate(world, log, eval_mapping())
        tasks_by_qn = {}
        with _transport(mode, recordings) as transport:
            for case in cases:
                setup = make_setup(
                    world,
                    log,
                    workdir / case.questionnaire_id,
                    case.filename,
                    case.data,
                    customer=case.customer_id,
                    submission_key=case.questionnaire_id,
                )
                tasks_by_qn[case.questionnaire_id] = setup.task_id
                with contextlib.suppress(RunStopped):  # a stopped run is reported, not hidden
                    setup.drafting(transport).run(setup.start(provenance))
        observed = observe(world, tasks_by_qn, cases)
        stamp = (now or dt.datetime.now(dt.UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return build_report(
            dataset,
            observed,
            versions(world, dataset),
            provenance,
            stamp,
            scope=scope,
            grades=grades,
        )
    finally:
        world.dispose()


def write_report(report: dict[str, Any], out: Path, name: str) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    paths = [out / f"{name}.json", out / f"{name}.md"]
    paths[0].write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    paths[1].write_text(render_markdown(report), encoding="utf-8")
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run_evals", description=__doc__.split("\n")[0])
    parser.add_argument("--mode", choices=sorted(PROVENANCE), required=True)
    parser.add_argument("--suite", choices=("full", "smoke"), default="full")
    parser.add_argument("--recordings", type=Path)
    parser.add_argument("--out", type=Path, default=ROOT / "evals" / "reports")
    parser.add_argument("--grades", type=Path, help="author grades to merge (grades/*.yaml)")
    args = parser.parse_args(argv)
    today = dt.datetime.now(dt.UTC).date().isoformat()
    if args.mode == "live" and os.environ.get("STEWARDRY_LIVE_MODEL") != "1":
        print("live mode costs money: set STEWARDRY_LIVE_MODEL=1 (make eval-live)", file=sys.stderr)
        return 2
    recordings = args.recordings or ROOT / "replay" / f"{args.suite}-{today}"
    grades = None
    if args.grades is not None:
        grades = yaml.safe_load(args.grades.read_text(encoding="utf-8"))["grades"]
    with tempfile.TemporaryDirectory() as tmp:
        report = run_suite(args.mode, args.suite, recordings, Path(tmp), grades=grades)
    name = f"{today}-{args.suite}-{report['provenance']}"
    for path in write_report(report, args.out, name):
        print(f"wrote {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")
    # Grades belong to the recorded answers, so they are named after the recordings
    # and apply to the live report and to any replay of the same recordings.
    template_path = ROOT / "evals" / "grades" / f"{recordings.name}.yaml"
    if args.mode != "scripted" and args.grades is None and not template_path.exists():
        template_path.parent.mkdir(parents=True, exist_ok=True)
        template = grading_template(report, load_dataset(DATASET))
        template_path.write_text(yaml.safe_dump(template, sort_keys=False), encoding="utf-8")
        print(f"wrote {template_path.relative_to(ROOT)} (fill in, then rerun with --grades)")
    print(f"recordings: {recordings}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
