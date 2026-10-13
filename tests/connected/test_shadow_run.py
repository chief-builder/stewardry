"""The first connected shadow run (S16, tiers: connected and live-model).

Run manually, after the manual completion (docs/brief.md, "Shadow session"):

    make shadow-run

Stewardry serves http://localhost:8700 against the real hub and gateway.
People do the workflow in their own browser windows:

1. Sales submits the request (SQR-1) and uploads the questionnaire.
2. Stewardry reads the approved Jira fields as Sales.
3. GRC authorizes drafting. Stewardry drafts every question with the live
   model, reading Confluence as GRC.
4. GRC reviews each draft (agree, edit, or reject).

The report is written to docs/connected/<date>-shadow/ (or
STEWARDRY_CONNECTED_OUT), labeled `connected`. The model's requests and
responses are recorded, redacted, under build/shadow/<date>/ for replay,
next to the run's database and uploads (kept; see docs/retention.md). They
are not committed. Nothing is exported or shown to Sales.
"""

import contextlib
import datetime as dt
import io
import json
import os
import time
from collections import Counter
from pathlib import Path
from typing import Any

import openpyxl
import pytest
import yaml
from sqlalchemy import func, select

from stewardry.engagements.questionnaire.drafting import Drafting
from stewardry.engagements.questionnaire.templates import xlsx_template
from stewardry.platform import registry
from stewardry.platform.audit import verify
from stewardry.platform.db.schema import (
    audit_events,
    question_drafts,
    runs,
    shadow_verdicts,
    task_grants,
    task_questions,
    tasks,
    usage_records,
)
from stewardry.platform.execution import Executor, RunStopped, ToolCall
from stewardry.platform.model import AnthropicTransport, ModelAccess, load_limits, load_pricing
from stewardry.platform.policy import load_agent_policy, load_baseline
from stewardry.platform.redaction import Redactor
from stewardry.platform.replay import RecordingTransport, ReplayStore
from tests.connected.live_app import (
    AGENT_POLICY,
    APP,
    BASELINE,
    CATALOG,
    ROOT,
    LiveApp,
    ask,
    broker_revision,
    live_app,
    say,
)

pytestmark = [
    pytest.mark.connected,
    pytest.mark.live_model,
    pytest.mark.skipif(
        os.environ.get("STEWARDRY_CONNECTED") != "1"
        or os.environ.get("STEWARDRY_LIVE_MODEL") != "1",
        reason="connected and live-model tiers: run manually with `make shadow-run`",
    ),
]

AGENT = "questionnaire-agent"
CUSTOMER = "larkspur-synthetic"
QUESTIONNAIRE = "QN-LARKSPUR-02"
EVAL_REPORT = "evals/reports/2026-10-11-full-live-model-eval.md"
BROKER = Path(os.environ.get("STEWARDRY_BROKER_CHECKOUT", ROOT.parent / "vendor-token-broker"))
WAIT = dt.timedelta(minutes=60)


def _wait_for(what: str, check: Any) -> Any:
    deadline = dt.datetime.now(dt.UTC) + WAIT
    while dt.datetime.now(dt.UTC) < deadline:
        found = check()
        if found:
            return found
        time.sleep(5)
    pytest.fail(f"timed out waiting for {what}")


def _uploaded_task(app: LiveApp) -> str | None:
    with app.engine.connect() as conn:
        return conn.execute(
            select(tasks.c.task_id)
            .join(task_questions, task_questions.c.task_id == tasks.c.task_id)
            .where(tasks.c.requester_sub == app.subjects["sales"])
            .limit(1)
        ).scalar()


def _drafting_granted(app: LiveApp, task_id: str) -> bool:
    with app.engine.connect() as conn:
        return (
            conn.execute(
                select(task_grants.c.grant_id).where(
                    task_grants.c.task_id == task_id,
                    task_grants.c.stage == "drafting",
                    task_grants.c.revoked_at.is_(None),
                )
            ).first()
            is not None
        )


def _count(app: LiveApp, table: Any, task_id: str) -> int:
    with app.engine.connect() as conn:
        return int(
            conn.execute(select(func.count()).where(table.c.task_id == task_id)).scalar_one()
        )


def manual_answers(path: Path) -> dict[str, str]:
    """Answers by question ID from a filled copy of the questionnaire."""
    template = xlsx_template()
    sheet = openpyxl.load_workbook(io.BytesIO(path.read_bytes()), read_only=True)[
        template.question_sheet
    ]
    answers = {}
    for row in sheet.iter_rows(min_row=template.first_question_row):
        cells = {c.column_letter: c.value for c in row if hasattr(c, "column_letter")}
        qid = cells.get(template.columns.id)
        if qid:
            answers[str(qid).strip()] = str(cells.get(template.columns.answer) or "").strip()
    return answers


def _minutes(question: str) -> float | None:
    answer = ask(question)
    try:
        return float(answer)
    except ValueError:
        return None


def test_first_connected_shadow_run() -> None:
    today = dt.date.today().isoformat()
    out = Path(os.environ.get("STEWARDRY_CONNECTED_OUT", ROOT / f"docs/connected/{today}-shadow"))
    raw = ROOT / "build" / "shadow" / today
    out.mkdir(parents=True, exist_ok=True)
    manual_file = ROOT / "build" / "shadow" / f"manual-{QUESTIONNAIRE}.xlsx"
    assert manual_file.exists(), f"fill in your manual completion first: {manual_file}"
    report: dict[str, Any] = {
        "provenance": "connected",
        "kind": "shadow run: connected Atlassian reads and the live model",
        "date": today,
        "participants": "1 person operating every role",
        "broker_revision": broker_revision(BROKER),
        "questionnaire": QUESTIONNAIRE,
        "steps": {},
    }
    assert not (raw / "db.sqlite").exists(), f"a shadow run already used {raw}"
    with live_app(data_dir=raw) as app:
        task_id = None
        try:
            # The rollout gate: an accepted eval report covers this policy version.
            policy_hash = load_agent_policy(AGENT_POLICY).policy_hash()
            registry.accept_evaluation(
                app.engine, app.log, app.owner, AGENT, policy_hash, EVAL_REPORT
            )
            registry.transition_status(
                app.engine, app.log, app.owner, AGENT, "shadow", "docs/brief.md"
            )
            report["versions"] = {"agent_policy_hash": policy_hash, "eval_report": EVAL_REPORT}

            app.wait_until_connected(["sales", "grc"])
            say(
                f"As alice (Sales), at {APP}: submit a request for customer {CUSTOMER} with "
                f"Jira issue {app.config['jira']['request_key']}, then upload "
                f"fixtures/questionnaires/{QUESTIONNAIRE}.xlsx on its page. Waiting..."
            )
            task_id = _wait_for("the upload", lambda: _uploaded_task(app))
            report["steps"]["submitted and uploaded"] = {
                "questions": _count(app, task_questions, task_id)
            }

            executor = Executor(
                app.engine,
                app.log,
                app.credentials,
                load_baseline(BASELINE),
                app.reads.tool_specs(app.catalogs),
                app.catalogs,
            )
            intake = executor.start_run(task_id, "intake", "connected")
            jira = executor.call(
                intake,
                ToolCall("jira.get_request", {"issue_key": app.config["jira"]["request_key"]}),
            )
            executor.finish_run(intake, "succeeded" if jira.ok else "failed", jira.error or "")
            report["steps"]["Jira read as Sales"] = (
                {"fields": sorted(k for k, v in jira.content["fields"].items() if v)}
                if jira.ok
                else {"error": jira.error}
            )

            say(f"As bob (GRC), at {APP}: authorize drafting for the new request. Waiting...")
            _wait_for("drafting authorization", lambda: _drafting_granted(app, task_id))

            limits = load_limits(ROOT / "policies/model-limits.yaml")
            live = AnthropicTransport(limits)
            try:
                access = ModelAccess(
                    app.engine,
                    app.log,
                    executor,
                    RecordingTransport(live, ReplayStore(raw / "model"), Redactor()),
                    load_pricing(ROOT / "policies/model-pricing.yaml"),
                    limits,
                )
                drafting = Drafting(
                    app.engine, app.log, executor, access, app.catalogs.get(CATALOG)
                )
                say("Drafting with the live model, reading Confluence as GRC...")
                done = False
                with contextlib.suppress(RunStopped):  # a stopped run is reported, not hidden
                    done = drafting.run(executor.start_run(task_id, "drafting", "connected"))
            finally:
                live.close()
            report["steps"]["drafting"] = {"completed": done}

            if done:
                say(
                    f"As bob (GRC): review every draft at {APP}/review/{task_id} and record a "
                    "verdict for each. Time your active review minutes. Waiting..."
                )
                questions = _count(app, task_questions, task_id)
                _wait_for(
                    "a verdict on every question",
                    lambda: _count(app, shadow_verdicts, task_id) >= questions,
                )
            report["timing_self_reported_minutes"] = {
                "manual_completion": _minutes("Active minutes for your manual completion?"),
                "assisted_review": _minutes("Active minutes for reviewing the drafts?"),
            }
        finally:
            if task_id is not None:
                report |= observe(app, task_id)
                write_comparison(out, app, task_id, manual_answers(manual_file))
            with app.engine.connect() as conn:
                chain = verify(conn)
            report["audit_chain"] = {"ok": chain.ok, "events": chain.events}
            (out / "report.json").write_text(
                json.dumps(report, indent=2, sort_keys=True, default=str) + "\n"
            )
            (out / "report.md").write_text(render(report))
    assert report["steps"]["drafting"]["completed"]
    assert report["audit_chain"]["ok"]


def observe(app: LiveApp, task_id: str) -> dict[str, Any]:
    """What the run did, from Stewardry's own records."""
    with app.engine.connect() as conn:
        drafting_runs = conn.execute(
            select(runs).where(runs.c.task_id == task_id, runs.c.stage == "drafting")
        ).all()
        run_ids = [r.run_id for r in drafting_runs]
        drafts = conn.execute(
            select(question_drafts)
            .where(question_drafts.c.task_id == task_id)
            .order_by(question_drafts.c.created_at)
        ).all()
        verdicts = conn.execute(
            select(shadow_verdicts)
            .where(shadow_verdicts.c.task_id == task_id)
            .order_by(shadow_verdicts.c.created_at)
        ).all()
        usage = conn.execute(select(usage_records).where(usage_records.c.run_id.in_(run_ids))).all()
        events = Counter(
            (e.event_type, e.outcome)
            for e in conn.execute(select(audit_events).where(audit_events.c.task_id == task_id))
        )
    started = min((r.started_at for r in drafting_runs), default=None)
    statuses = Counter(d.status for d in drafts)
    reviewed = len(verdicts)
    by_verdict = Counter(v.verdict for v in verdicts)
    costs = [u.estimated_cost_micro_usd for u in usage]
    return {
        "runs": [{"status": r.status, "provenance": r.provenance} for r in drafting_runs],
        "model": sorted({u.model_id for u in usage}),
        "prompt_version": sorted({u.prompt_version for u in usage}),
        "pricing_ref": sorted({u.pricing_ref for u in usage if u.pricing_ref}),
        "drafts": {"processed": len(drafts), **dict(statuses)},
        "expert_reasons": dict(Counter(d.expert_reason for d in drafts if d.expert_reason)),
        "abstention_rate": f"{statuses.get('needs_expert', 0)}/{len(drafts)}",
        "latency_seconds": {
            "to_first_draft": (drafts[0].created_at - started).total_seconds()
            if drafts and started
            else None,
            "to_last_draft": (drafts[-1].created_at - started).total_seconds()
            if drafts and started
            else None,
        },
        "usage": {
            "model_calls": len(usage),
            "input_tokens": sum(u.input_tokens or 0 for u in usage),
            "output_tokens": sum(u.output_tokens or 0 for u in usage),
            "estimated_cost_usd": round(sum(c or 0 for c in costs) / 1e6, 4),
            "calls_with_unknown_cost": sum(1 for c in costs if c is None),
        },
        "review": {
            "reviewed": reviewed,
            **dict(by_verdict),
            "edit_rate": f"{by_verdict.get('edit', 0)}/{reviewed}",
            "verdict_span_minutes": round(
                (verdicts[-1].created_at - verdicts[0].created_at).total_seconds() / 60, 1
            )
            if verdicts
            else None,
        },
        "denied_or_failed_events": {
            f"{t} {o}": n for (t, o), n in sorted(events.items()) if o in ("denied", "failed")
        },
    }


def write_comparison(out: Path, app: LiveApp, task_id: str, manual: dict[str, str]) -> None:
    """Side by side, per question: the manual answer, the draft, and the verdict."""
    catalog = app.catalogs.get(CATALOG)
    with app.engine.connect() as conn:
        questions = conn.execute(
            select(task_questions)
            .where(task_questions.c.task_id == task_id)
            .order_by(task_questions.c.ordinal)
        ).all()
        drafts = {
            d.question_id: d
            for d in conn.execute(
                select(question_drafts).where(question_drafts.c.task_id == task_id)
            )
        }
        verdicts = {
            v.question_id: v
            for v in conn.execute(
                select(shadow_verdicts).where(shadow_verdicts.c.task_id == task_id)
            )
        }
    lines = [
        f"# Shadow run {QUESTIONNAIRE}: manual completion and drafts side by side",
        "",
        "Labeled **connected**. One person wrote the manual answers, operated GRC, and "
        "recorded the verdicts. The manual completion came first.",
        "",
    ]
    for q in questions:
        d, v = drafts.get(q.question_id), verdicts.get(q.question_id)
        cites = []
        for c in json.loads(d.citations_json) if d else []:
            entry = catalog.by_page(c["page_id"])
            cites.append(f"{getattr(entry, 'policy_id', c['page_id'])}#{c['passage']}")
        lines += [
            f"## {q.question_id}. {q.text}",
            "",
            f"- **Manual:** {manual.get(q.question_id) or '(blank)'}",
            f"- **Draft ({d.status if d else 'none'}):** {(d.answer if d else '') or '(none)'}",
            f"- **Citations:** {', '.join(cites) or 'none'}",
            f"- **Verdict:** {v.verdict if v else 'none'}"
            + (f". Edited: {v.edited_answer}" if v and v.edited_answer else "")
            + (f". Reason: {v.reason}" if v and v.reason else ""),
            "",
        ]
    (out / "comparison.md").write_text("\n".join(lines))


def render(report: dict[str, Any]) -> str:
    timing = report.get("timing_self_reported_minutes", {})
    lines = [
        f"# Shadow run, {report['date']}: **connected**",
        "",
        "Connected Atlassian reads and the live model. One person operates every role "
        "(participant count: 1). Nothing was exported or shown to Sales.",
        "",
        "```yaml",
        yaml.safe_dump(report, sort_keys=True, default_flow_style=False).rstrip(),
        "```",
        "",
        "## Reading this report",
        "",
        "- **One sample.** The reviewer-time comparison is one questionnaire, done "
        "manually first and then reviewed. It is insufficient data for the 50% "
        "reviewer-time candidate target.",
        "- **Learning effects.** The same person wrote the questionnaire, its golden "
        "answers, and the manual completion before reviewing the drafts.",
        f"- **Timing is self-reported:** manual {timing.get('manual_completion')} min, "
        f"review {timing.get('assisted_review')} min. The verdict span is measured from "
        "Stewardry's timestamps.",
        "- **Side by side:** [comparison.md](comparison.md).",
    ]
    return "\n".join(lines) + "\n"
