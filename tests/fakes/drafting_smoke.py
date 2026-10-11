"""The drafting smoke scenario shared by the live recording and the offline
replay: four items from QN-LARKSPUR-01, one per case (covered, conflict,
stale, uncovered), in a questionnaire built from the supported template."""

import json
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import Engine, select

from stewardry.platform.db.schema import question_drafts
from tests.unit.conftest import ROOT
from tools.build_fixtures import TEMPLATES, build_xlsx

QUESTIONS = ("Q09", "Q14", "Q25", "Q26")  # covered, conflict, stale, uncovered
RECORDINGS = ROOT / "replay" / "drafting-smoke"
MANIFEST = RECORDINGS / "outcomes.json"


def smoke_questionnaire(tmp_path: Path) -> tuple[str, bytes]:
    source = yaml.safe_load(
        (ROOT / "fixtures/questionnaires/QN-LARKSPUR-01.yaml").read_text(encoding="utf-8")
    )
    source["questions"] = [q for q in source["questions"] if q["id"] in QUESTIONS]
    source["hidden"] = []
    template = yaml.safe_load((TEMPLATES / "sqt-xlsx-v1.yaml").read_text(encoding="utf-8"))
    out = tmp_path / "QN-SMOKE.xlsx"
    build_xlsx(source, template, out)
    return out.name, out.read_bytes()


def outcomes(world: Engine) -> dict[str, Any]:
    with world.connect() as conn:
        rows = conn.execute(select(question_drafts).order_by(question_drafts.c.question_id))
        return {
            r.question_id: {
                "status": r.status,
                "strength": r.strength,
                "expert_reason": r.expert_reason,
                "citations": [(c["page_id"], c["passage"]) for c in json.loads(r.citations_json)],
            }
            for r in rows
        }
