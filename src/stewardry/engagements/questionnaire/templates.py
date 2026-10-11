"""The supported questionnaire templates (spec section 5.2).

Each template fixes where questions and answers live. Anything else in an
uploaded file is preserved on export (Milestone 3) but never treated as a
question. Unsupported layouts are rejected with an explanation.
"""

from functools import cache
from importlib import resources
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class XlsxColumns(_Strict):
    id: str
    section: str
    question: str
    answer: str
    notes: str


class OtherSheet(_Strict):
    name: str
    formulas: dict[str, str] = {}


class XlsxTemplate(_Strict):
    schema_version: Literal[1]
    template_id: str
    format: Literal["xlsx"]
    question_sheet: str
    header_row: int
    first_question_row: int
    columns: XlsxColumns
    headers: list[str]
    other_sheets: list[OtherSheet]


class DocxColumns(_Strict):
    id: int
    section: int
    question: int
    answer: int


class DocxTemplate(_Strict):
    schema_version: Literal[1]
    template_id: str
    format: Literal["docx"]
    table_index: int
    header_row: int
    columns: DocxColumns
    headers: list[str]


Template = XlsxTemplate | DocxTemplate


def _load(name: str) -> dict[str, object]:
    text = resources.files(__package__).joinpath("templates", name).read_text(encoding="utf-8")
    data: dict[str, object] = yaml.safe_load(text)
    return data


@cache
def xlsx_template() -> XlsxTemplate:
    return XlsxTemplate.model_validate(_load("sqt-xlsx-v1.yaml"))


@cache
def docx_template() -> DocxTemplate:
    return DocxTemplate.model_validate(_load("sqt-docx-v1.yaml"))
