"""Registry of the built-in and plugin tax rule packs (docs/billing-phase2-design.md 7.1)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session


@dataclass(frozen=True)
class PackInfo:
    pack_id: str  # "uk_vat", "eu_vat" or "<plugin_id>:<provider_id>"
    version: str
    source: str  # "builtin" or "plugin"


@dataclass(frozen=True)
class PackData:
    pack_id: str
    version: str
    rules: tuple[dict[str, Any], ...] = ()
    legal_notes: tuple[dict[str, Any], ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)


class PackRegistry:
    @staticmethod
    def list(db: Session) -> list[PackInfo]:
        raise NotImplementedError("PackRegistry.list is built by the packs and tax IDs worker")

    @staticmethod
    def load(db: Session, pack_id: str, version: str) -> PackData:
        raise NotImplementedError("PackRegistry.load is built by the packs and tax IDs worker")


# Section 7 of the design calls the registry ``TaxRulePackRegistry``.
TaxRulePackRegistry = PackRegistry
