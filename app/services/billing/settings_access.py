"""Read billing settings through the runtime settings store."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.services.settings_store import get_setting


def billing_setting(db: Session, key: str) -> Any:
    """Stored value of a billing setting, else its environment default.

    Raises ``KeyError`` for a key the settings store does not define.
    """
    return get_setting(db, key)
