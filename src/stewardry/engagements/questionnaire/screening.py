"""Screening untrusted questionnaire content (spec section 4.4).

Two kinds of findings, both recorded for reviewers:

- **Location findings:** any text in a hidden or secondary location that the
  template does not use for questions (hidden cells, rows, columns, or text;
  comments; tracked changes; text outside the template's columns). These
  are reported whatever the text says.
- **Instruction-like text:** phrases that address an AI system, try to
  change its behavior, ask for approval, or ask for other customers' data or
  confidential source text, anywhere in the file.

Screening supplements authorization; it is not the injection defense
(spec section 4.4). The phrase list is a heuristic. A clean screen does not
mean the content is safe, and a match does not mean it is malicious. All
document text is treated as data either way.
"""

import re
from dataclasses import dataclass
from typing import Literal

FindingKind = Literal[
    "hidden_cell",
    "hidden_row",
    "hidden_column",
    "hidden_text",
    "comment",
    "tracked_change",
    "outside_template",
    "instruction_like",
]

EXCERPT_LIMIT = 200

INSTRUCTION_PATTERNS: dict[str, re.Pattern[str]] = {
    "addresses an AI": re.compile(
        r"\b(assistant|AI|LLM|language model|chatbot|system note|system prompt)\b\s*[:,]",
        re.IGNORECASE,
    ),
    "note to an AI": re.compile(r"\bnote (to|for) the (AI|assistant|model)\b", re.IGNORECASE),
    "override instructions": re.compile(
        r"\b(ignore|disregard|forget|override)\b[^.]{0,40}\b(instructions?|prompt|rules|"
        r"questions)\b",
        re.IGNORECASE,
    ),
    "approval or release": re.compile(
        r"\b(approve|approved|skip (the )?review|release the file|mark (it|this|the "
        r"questionnaire) as approved)\b",
        re.IGNORECASE,
    ),
    "verbatim or confidential text": re.compile(
        r"\b(verbatim|full text|complete text|confidential polic(y|ies))\b", re.IGNORECASE
    ),
    "other customers": re.compile(
        r"\b(other customers?|every customer|answers you gave|another customer)\b", re.IGNORECASE
    ),
    "blanket answers": re.compile(
        r"\b(answer|reply)\b[^.]{0,30}\b(yes|fully (compliant|implemented))\b[^.]{0,30}\b"
        r"(every|all)\b"
        r"|\b(answer|reply)\b[^.]{0,10}\b(every|all)\b[^.]{0,40}\b(yes|fully (compliant|"
        r"implemented))\b",
        re.IGNORECASE,
    ),
}


@dataclass(frozen=True)
class ScreeningFinding:
    kind: FindingKind
    location: str
    excerpt: str
    question_id: str | None = None
    reason: str | None = None


def excerpt(text: str) -> str:
    clean = " ".join(text.split())
    return clean if len(clean) <= EXCERPT_LIMIT else clean[: EXCERPT_LIMIT - 1] + "…"


def instruction_reasons(text: str) -> list[str]:
    return [name for name, pattern in INSTRUCTION_PATTERNS.items() if pattern.search(text)]


def screen_text(text: str, location: str, question_id: str | None = None) -> list[ScreeningFinding]:
    """Instruction-like findings for one piece of visible text."""
    reasons = instruction_reasons(text)
    if not reasons:
        return []
    return [
        ScreeningFinding(
            "instruction_like", location, excerpt(text), question_id, "; ".join(reasons)
        )
    ]


def location_finding(
    kind: FindingKind, location: str, text: str, question_id: str | None = None
) -> ScreeningFinding:
    """Text in a hidden or secondary location, with any instruction-like reasons."""
    reasons = instruction_reasons(text)
    return ScreeningFinding(
        kind, location, excerpt(text), question_id, "; ".join(reasons) if reasons else None
    )
