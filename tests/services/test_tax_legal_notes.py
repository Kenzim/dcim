"""Legal note rendering (design 1.8)."""

from __future__ import annotations

import pytest

from app.models.tax import TaxLegalNote
from app.services.tax.legal_notes import LegalNoteNotFound, LegalNoteValueMissing, render_legal_note
from tests.services.tax_pack_helpers import seed_tax_basics


@pytest.fixture
def notes(db_session):
    seed_tax_basics(db_session)
    db_session.add(TaxLegalNote(key="outside_scope", locale="fr", text="Hors du champ de la TVA."))
    db_session.add(TaxLegalNote(key="braces", locale="en", text="Literal {{x}} and {name}."))
    db_session.add(TaxLegalNote(key="attack", locale="en", text="{reason.__class__} {0} {reason!r}"))
    db_session.flush()
    return db_session


def test_seeded_notes_render(notes):
    assert render_legal_note(notes, "outside_scope", "en") == "Outside the scope of VAT."
    assert render_legal_note(notes, "exempt_customer", "en", reason="charity") == "VAT exempt: charity."
    assert (
        render_legal_note(notes, "credit_note_reference", "en", number="INV-7", date="2026-11-22")
        == "Credit note for invoice INV-7 dated 2026-11-22."
    )


def test_locale_falls_back_to_language_then_english(notes):
    assert render_legal_note(notes, "outside_scope", "fr-FR") == "Hors du champ de la TVA."
    assert render_legal_note(notes, "outside_scope", "de") == "Outside the scope of VAT."
    assert render_legal_note(notes, "eu_reverse_charge", "en-GB").startswith("Reverse charge")


def test_missing_key_and_missing_value(notes):
    with pytest.raises(LegalNoteNotFound):
        render_legal_note(notes, "nope", "en")
    with pytest.raises(LegalNoteValueMissing):
        render_legal_note(notes, "exempt_customer", "en")


def test_only_bare_placeholders_are_filled(notes):
    assert render_legal_note(notes, "braces", "en", name="Acme", x="no") == "Literal {x} and Acme."
    assert render_legal_note(notes, "attack", "en", reason="r") == "{reason.__class__} {0} {reason!r}"
