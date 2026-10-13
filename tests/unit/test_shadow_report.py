"""The S16 shadow harness's report, built from a scripted drafting world
(tier: offline). The connected run itself is manual (`make shadow-run`); this
checks its queries and output against the real schema before anyone spends a
session on it."""

import io
from pathlib import Path
from types import SimpleNamespace

import openpyxl
import pytest
from sqlalchemy import Engine

from stewardry.engagements.questionnaire.review import record_shadow_verdict
from stewardry.platform.audit import AuditLog
from stewardry.platform.catalog import Catalogs
from tests.connected.test_shadow_run import manual_answers, observe, render, write_comparison
from tests.fakes.drafting_setup import make_setup
from tests.fakes.scripted_model import ScriptedModel
from tests.unit.conftest import BOB, ROOT

QUESTIONNAIRE = ROOT / "fixtures" / "questionnaires" / "QN-LARKSPUR-02.xlsx"


@pytest.fixture
def drafted(world: Engine, log: AuditLog, tmp_path: Path) -> str:
    setup = make_setup(world, log, tmp_path, QUESTIONNAIRE.name, QUESTIONNAIRE.read_bytes())
    assert setup.drafting(ScriptedModel({})).run(setup.start("scripted-example"))
    record_shadow_verdict(world, log, BOB, setup.task_id, "Q01", "agree", "k1")
    record_shadow_verdict(world, log, BOB, setup.task_id, "Q03", "agree", "k3")
    record_shadow_verdict(
        world, log, BOB, setup.task_id, "Q02", "edit", "k2", edited_answer="Mine."
    )
    return setup.task_id


def test_observe_counts_drafts_usage_and_verdicts(world: Engine, drafted: str) -> None:
    app = SimpleNamespace(engine=world, catalogs=Catalogs(ROOT))
    seen = observe(app, drafted)  # type: ignore[arg-type]
    assert seen["drafts"]["processed"] == 30
    assert seen["usage"]["model_calls"] > 0 and seen["usage"]["calls_with_unknown_cost"] == 0
    assert seen["review"] == seen["review"] | {"reviewed": 3, "agree": 2, "edit": 1}
    assert seen["review"]["edit_rate"] == "1/3"
    assert seen["latency_seconds"]["to_last_draft"] >= seen["latency_seconds"]["to_first_draft"]


def test_comparison_puts_manual_draft_and_verdict_side_by_side(
    world: Engine, drafted: str, tmp_path: Path
) -> None:
    app = SimpleNamespace(engine=world, catalogs=Catalogs(ROOT))
    write_comparison(tmp_path, app, drafted, {"Q01": "Our own answer."})  # type: ignore[arg-type]
    text = (tmp_path / "comparison.md").read_text()
    assert "- **Manual:** Our own answer." in text
    assert "Edited: Mine." in text and text.count("## Q") == 30


def test_manual_answers_are_read_from_the_answer_column(tmp_path: Path) -> None:
    book = openpyxl.load_workbook(io.BytesIO(QUESTIONNAIRE.read_bytes()))
    book["Questionnaire"]["D2"] = "Filled by hand."
    filled = tmp_path / "manual.xlsx"
    book.save(filled)
    answers = manual_answers(filled)
    assert len(answers) == 30 and next(iter(answers.values())) == "Filled by hand."


def test_the_report_is_labeled_and_states_its_limits() -> None:
    text = render({"date": "2026-10-13", "provenance": "connected"})
    assert "**connected**" in text and "participant count: 1" in text
    assert "insufficient data for the 50% reviewer-time candidate target" in text
