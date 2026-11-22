"""Preview and apply of a tax rule pack (docs/billing-phase2-design.md 7.2)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session


@dataclass(frozen=True)
class PackDiff:
    pack_id: str
    version: str
    effective_from: datetime
    # Each entry: the pack rule key and "add", "unchanged", "change" or "remove".
    entries: tuple[dict[str, Any], ...]
    diff_hash: str


class PackImporter:
    @staticmethod
    def preview(db: Session, pack_id: str, version: str, effective_from: datetime) -> PackDiff:
        raise NotImplementedError("PackImporter.preview is built by the packs and tax IDs worker")

    @staticmethod
    def apply(
        db: Session,
        pack_id: str,
        version: str,
        effective_from: datetime,
        *,
        diff_hash: str,
        actor_user_id: Optional[int],
    ) -> PackDiff:
        raise NotImplementedError("PackImporter.apply is built by the packs and tax IDs worker")
