"""Rendering of legal note keys in a locale (docs/billing-phase2-design.md 1.8 and 7.3).

Texts live in ``tax_legal_notes`` and may be edited by administrators, so a
placeholder is only ever a bare name in braces (``{reason}``): no attribute or
index access, no format specification. Literal braces are written ``{{`` and ``}}``.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.tax import TaxLegalNote

DEFAULT_LOCALE = "en"

_TOKEN = re.compile(r"\{\{|\}\}|\{([a-z_][a-z0-9_]*)\}|[{}]")


class LegalNoteNotFound(LookupError):
    """No text for the key in the locale, its language or the default locale."""


class LegalNoteValueMissing(KeyError):
    """The text has a placeholder no value was given for."""


def _candidates(locale: Optional[str]) -> list[str]:
    out: list[str] = []
    for candidate in (locale, (locale or "").split("-")[0], DEFAULT_LOCALE):
        if candidate and candidate not in out:
            out.append(candidate)
    return out


def note_text(db: Session, key: str, locale: Optional[str]) -> str:
    """The stored text for ``key``: the locale, then its language (``en`` for ``en-GB``), then ``en``."""
    candidates = _candidates(locale)
    rows = {
        row.locale: row.text
        for row in db.scalars(
            select(TaxLegalNote).where(TaxLegalNote.key == key).where(TaxLegalNote.locale.in_(candidates))
        ).all()
    }
    for candidate in candidates:
        if candidate in rows:
            return rows[candidate]
    raise LegalNoteNotFound(f"no legal note {key!r} for locale {locale!r}")


def placeholders(text: str) -> set[str]:
    return {match.group(1) for match in _TOKEN.finditer(text) if match.group(1)}


def fill(text: str, values: dict[str, Any]) -> str:
    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        if token == "{{":
            return "{"
        if token == "}}":
            return "}"
        name = match.group(1)
        if name is None:
            return token
        if name not in values:
            raise LegalNoteValueMissing(name)
        return str(values[name])

    return _TOKEN.sub(replace, text)


def render_legal_note(db: Session, key: str, locale: str, **values: Any) -> str:
    """The note's text in ``locale`` with its placeholders filled from ``values``.

    Raises ``LegalNoteNotFound`` when the key has no text and
    ``LegalNoteValueMissing`` when a placeholder has no value. Extra values are ignored.
    """
    return fill(note_text(db, key, locale), values)
