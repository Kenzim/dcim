"""Rendering of legal note keys in a locale (docs/billing-phase2-design.md 7.3)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session


def render_legal_note(db: Session, key: str, locale: str, **values: Any) -> str:
    raise NotImplementedError("render_legal_note is built by the packs and tax IDs worker")
