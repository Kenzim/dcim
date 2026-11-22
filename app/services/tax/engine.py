"""Tax calculation (docs/billing-phase2-design.md 4.3)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.services.tax.context import TaxContext
from app.services.tax.result import TaxResult


class TaxEngine:
    @staticmethod
    def calculate(db: Session, context: TaxContext) -> TaxResult:
        """Pure apart from reads of rules, registrations and exemptions; no locks."""
        raise NotImplementedError("TaxEngine.calculate is built by the tax engine worker")

    @staticmethod
    def prepare(db: Session, context: TaxContext) -> TaxResult:
        """May call a ``tax_provider`` plugin; never called under a billing lock."""
        raise NotImplementedError("TaxEngine.prepare is built by the tax engine worker")
