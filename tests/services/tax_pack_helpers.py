"""Shared set-up for the tax pack, tax ID and appendix C tests."""

from __future__ import annotations

from datetime import datetime, timezone

from app.models.tax import TaxClass, TaxLegalNote
from app.services.tax.pack_import import PackImporter
from app.services.tax.packs.registry import PackRegistry

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
EFFECTIVE = datetime(2026, 12, 1, tzinfo=timezone.utc)

# The rows of the billing_p2_tax_seed revision, which create_all does not insert.
SYSTEM_CLASSES = ("standard", "reduced", "zero", "exempt", "outside_scope")
SEEDED_NOTES = {
    "gb_outside_scope_b2b": "Outside the scope of UK VAT: reverse charge. The customer accounts for any VAT due.",
    "eu_reverse_charge": "Reverse charge: VAT to be accounted for by the customer (Article 196, Directive 2006/112/EC).",
    "outside_scope": "Outside the scope of VAT.",
    "exempt_customer": "VAT exempt: {reason}.",
    "credit_note_reference": "Credit note for invoice {number} dated {date}.",
}


def seed_tax_basics(db) -> None:
    for code in SYSTEM_CLASSES:
        db.add(TaxClass(code=code, name=code.replace("_", " ").capitalize(), system=True))
    for key, text in SEEDED_NOTES.items():
        db.add(TaxLegalNote(key=key, locale="en", text=text))
    db.flush()


def apply_pack(db, pack_id: str, effective_from: datetime = EFFECTIVE, now: datetime = NOW, actor_user_id=None):
    version = PackRegistry.load(db, pack_id, _version(db, pack_id)).version
    diff = PackImporter.preview(db, pack_id, version, effective_from, now=now)
    return PackImporter.apply(
        db,
        pack_id,
        version,
        effective_from,
        diff_hash=diff.diff_hash,
        actor_user_id=actor_user_id,
        now=now,
    )


def _version(db, pack_id: str) -> str:
    return next(info.version for info in PackRegistry.list(db) if info.pack_id == pack_id)
