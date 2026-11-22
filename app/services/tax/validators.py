"""Tax-ID checks (docs/billing-phase2-design.md 7.4)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.tax import TaxIdCheck


class TaxIdService:
    @staticmethod
    def check(db: Session, profile: Any, *, force: bool = False) -> TaxIdCheck:
        raise NotImplementedError("TaxIdService.check is built by the packs and tax IDs worker")
