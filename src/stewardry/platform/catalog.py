"""Policy page catalogs (`policies/catalogs/*.yaml`), named by agent policies.

A catalog maps the synthetic Confluence page IDs an agent may read to policy
IDs. It limits what may be read; the page itself is the authority for status,
dates, and content (`platform/evidence.py`).
"""

from functools import cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CatalogPage(_Strict):
    page_id: str = Field(pattern=r"^[0-9]{1,20}$")
    policy_id: str = Field(pattern=r"^[A-Z]{2,8}-\d{1,4}$")
    title: str
    classification: Literal["public", "internal", "confidential"]
    status: Literal["current", "superseded"]


class Catalog(_Strict):
    schema_version: Literal[1]
    catalog: str
    space: str = Field(pattern=r"^[A-Z][A-Z0-9]{1,31}$")
    description: str = ""
    pages: list[CatalogPage]

    def by_policy(self, policy_id: str) -> CatalogPage | None:
        return next((p for p in self.pages if p.policy_id == policy_id), None)

    def by_page(self, page_id: str) -> CatalogPage | None:
        return next((p for p in self.pages if p.page_id == page_id), None)

    @property
    def page_ids(self) -> frozenset[str]:
        return frozenset(p.page_id for p in self.pages)


def load_catalog(path: Path) -> Catalog:
    return Catalog.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


class Catalogs:
    """Resolves the catalog references in policies (repo-relative paths)
    against a root directory. Callable as the executor's `CatalogLookup`."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    @cache  # noqa: B019 - one instance per process; catalogs are small
    def get(self, ref: str) -> Catalog:
        path = (self.root / ref).resolve()
        if not path.is_relative_to(self.root / "policies" / "catalogs"):
            raise ValueError(f"catalog reference {ref!r} is outside policies/catalogs")
        return load_catalog(path)

    def __call__(self, ref: str) -> frozenset[str]:
        return self.get(ref).page_ids
