"""Senders of the email events added in billing phase 2 (docs/billing-phase2-design.md 12.2).

One small function per sender, called by other services.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session


def send_service_provisioned(db: Session, service: Any) -> None:
    raise NotImplementedError("send_service_provisioned is built by the email templates worker")


def send_welcome(db: Session, user: Any) -> None:
    raise NotImplementedError("send_welcome is built by the email templates worker")
