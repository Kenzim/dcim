"""The tax engine's mode (docs/billing-phase2-design.md section 5)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.services.billing_shadow import billing_engine_mode


def tax_engine_mode(db: Session) -> str:
    """``legacy``, ``shadow`` or ``v2``; ``v2`` only with the engine switch record."""
    return billing_engine_mode(db, "tax")
