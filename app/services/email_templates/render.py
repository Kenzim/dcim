"""Rendering an email event through its template (docs/billing-phase2-design.md 12.1)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from sqlalchemy.orm import Session


@dataclass(frozen=True)
class RenderedEmail:
    subject: str
    text: str
    html: Optional[str]
    template_id: Optional[int]
    template_version: Optional[int]
    fallback: bool


def render(
    db: Session,
    event_key: str,
    *,
    locale: str,
    variables: Mapping[str, Any],
    product_id: Optional[int] = None,
) -> RenderedEmail:
    raise NotImplementedError("render is built by the email templates worker")
