"""Preview and apply of a tax rule pack (docs/billing-phase2-design.md 7.3).

Rows of an applied pack are matched to the pack by ``(pack_id, pack_rule_key)``.
Only rows still in effect at ``effective_from`` (``valid_to`` NULL or later) take
part in the diff; an apply never edits a row in place apart from closing it with
``valid_to = effective_from``, and ``effective_from`` may not be in the past, so
the rate a past tax point used never changes. Packs are only ever applied by an
administrator through ``apply``; nothing in Rackflow applies one on its own.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.tax import TaxClass, TaxClassCode, TaxLegalNote, TaxRule, TaxRuleSource
from app.services.audit_service import AuditService
from app.services.tax.packs.registry import PackData, PackNotFound, PackRegistry, PackUnavailable

AUDIT_ACTION = "billing.tax.pack_applied"

# Fields of a rule compared between the pack and the row in effect.
COMPARED_FIELDS = (
    "name",
    "country",
    "region",
    "postcode_pattern",
    "tax_class",
    "customer_type",
    "rate_percent",
    "priority",
    "compound",
    "compound_order",
    "jurisdiction",
    "reverse_charge",
    "reverse_charge_zone",
    "legal_note_key",
    "rounding_point",
    "rounding_rule",
    "valid_to",
)

# Names of classes an apply may have to create.
CLASS_NAMES = {
    "standard": ("Standard rate", "Lines with no tax class on their product use this class."),
    "reduced": ("Reduced rate", None),
    "zero": ("Zero rate", "Taxable at a rate of zero."),
    "exempt": ("Exempt", "Exempt from tax."),
    "outside_scope": ("Outside the scope of tax", "Not a taxable supply, for example credit top-ups and late fees."),
    "digital_service": (
        "Digital services",
        "Electronically supplied services, taxed where the customer is.",
    ),
}
_SYSTEM_CLASSES = {code.value for code in TaxClassCode}


class PackImportError(Exception):
    """Refusal of a preview or apply; ``code`` is the API error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PackDiff:
    pack_id: str
    version: str
    effective_from: datetime
    # Sorted by type then key. Every entry has "type" ("tax_class", "rule" or
    # "legal_note"), "key" and "action": "add", "unchanged", "change", "remove"
    # (rules) or "kept_edited" (a legal note an admin edited, which is kept).
    entries: tuple[dict[str, Any], ...]
    diff_hash: str

    def count(self, action: str) -> int:
        return sum(1 for entry in self.entries if entry["action"] == action)


def _utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    value = _utc(value)
    return value.isoformat() if value is not None else None


def _rate(value: Any) -> str:
    return f"{Decimal(str(value)):.4f}"


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _row_values(row: TaxRule, class_codes: dict[int, str]) -> dict[str, Any]:
    return {
        "name": row.name,
        "country": row.country,
        "region": row.region,
        "postcode_pattern": row.postcode_pattern,
        "tax_class": class_codes.get(row.tax_class_id),
        "customer_type": _enum_value(row.customer_type),
        "rate_percent": _rate(row.rate_percent),
        "priority": row.priority,
        "compound": bool(row.compound),
        "compound_order": row.compound_order,
        "jurisdiction": row.jurisdiction,
        "reverse_charge": bool(row.reverse_charge),
        "reverse_charge_zone": row.reverse_charge_zone,
        "legal_note_key": row.legal_note_key,
        "rounding_point": _enum_value(row.rounding_point),
        "rounding_rule": row.rounding_rule,
        "valid_from": _iso(row.valid_from),
        "valid_to": _iso(row.valid_to),
        "enabled": bool(row.enabled),
        "pack_version": row.pack_version,
    }


def _pack_values(rule: dict[str, Any], effective_from: datetime) -> dict[str, Any]:
    values = {name: rule[name] for name in COMPARED_FIELDS if name != "valid_to"}
    values["rate_percent"] = _rate(rule["rate_percent"])
    values["valid_from"] = _iso(max(effective_from, rule["valid_from"]))
    values["valid_to"] = _iso(rule["valid_to"])
    return values


def _load(db: Session, pack_id: str, version: str) -> PackData:
    try:
        return PackRegistry.load(db, pack_id, version)
    except PackNotFound as exc:
        raise PackImportError("pack_not_found", str(exc)) from exc
    except PackUnavailable as exc:
        raise PackImportError("pack_unavailable", str(exc)) from exc


def _check_effective_from(effective_from: datetime, now: Optional[datetime]) -> datetime:
    if not isinstance(effective_from, datetime):
        raise PackImportError("effective_from_invalid", "effective_from must be a date and time")
    effective_from = _utc(effective_from)
    current = _utc(now) if now is not None else datetime.now(timezone.utc)
    if effective_from < current:
        raise PackImportError(
            "effective_from_in_past",
            "effective_from may not be in the past: a pack never changes the rate of a past tax point",
        )
    return effective_from


def _in_effect_query(pack_id: str, effective_from: datetime, *, lock: bool):
    query = (
        select(TaxRule)
        .where(TaxRule.pack_id == pack_id)
        .where(or_(TaxRule.valid_to.is_(None), TaxRule.valid_to > effective_from))
        .order_by(TaxRule.id)
    )
    if not lock:
        return query
    # The tax engine's publish locks rule rows in ascending id, so the import does too, or the
    # two can deadlock. ORDER BY alone does not set the order the locks are taken in: once the
    # table holds other packs, MariaDB reads through ix_tax_rules_pack_rule (rule key order) and
    # sorts afterwards. Reading the primary key locks in id order; under REPEATABLE READ it also
    # holds the table's other rule rows and gaps until the import's transaction ends.
    for dialect in ("mysql", "mariadb"):
        query = query.with_hint(TaxRule, "FORCE INDEX (PRIMARY)", dialect)
    # A locking read sees the latest committed rows, not this transaction's snapshot.
    return query.with_for_update().execution_options(populate_existing=True)


def _rows_in_effect(
    db: Session, pack_id: str, effective_from: datetime, *, lock: bool = False
) -> dict[str, list[TaxRule]]:
    rows = db.scalars(_in_effect_query(pack_id, effective_from, lock=lock)).all()
    by_key: dict[str, list[TaxRule]] = {}
    for row in rows:
        if row.pack_rule_key:
            by_key.setdefault(row.pack_rule_key, []).append(row)
    return by_key


def _diff_hash(pack_id: str, version: str, effective_from: datetime, entries: list[dict[str, Any]]) -> str:
    payload = {
        "pack_id": pack_id,
        "version": version,
        "effective_from": _iso(effective_from),
        "entries": entries,
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _compute(
    db: Session, pack: PackData, effective_from: datetime, in_effect: dict[str, list[TaxRule]]
) -> list[dict[str, Any]]:
    classes = {row.code: row.id for row in db.scalars(select(TaxClass)).all()}
    class_codes = {class_id: code for code, class_id in classes.items()}
    entries: list[dict[str, Any]] = []

    needed = set(pack.extra.get("classes", ())) | {rule["tax_class"] for rule in pack.rules}
    for code in sorted(needed - set(classes)):
        entries.append({"type": "tax_class", "key": code, "action": "add"})

    wanted = {
        rule["pack_rule_key"]: rule
        for rule in pack.rules
        if rule["valid_to"] is None or rule["valid_to"] > effective_from
    }
    for key in sorted(set(wanted) | set(in_effect)):
        rows = in_effect.get(key, [])
        current = rows[-1] if rows else None
        old = _row_values(current, class_codes) if current is not None else None
        entry: dict[str, Any] = {
            "type": "rule",
            "key": key,
            "rule_ids": [row.id for row in rows],
            "old": old,
        }
        if key not in wanted:
            entry.update(action="remove", new=None)
        else:
            new = _pack_values(wanted[key], effective_from)
            if old is None:
                entry.update(action="add", new=new)
            elif len(rows) == 1 and all(old[name] == new[name] for name in COMPARED_FIELDS):
                entry.update(action="unchanged", new=new)
            else:
                entry.update(action="change", new=new)
                if new["valid_from"] != _iso(effective_from) and _utc(current.valid_from) <= effective_from:
                    entry["warning"] = "no row for this key between effective_from and the new valid_from"
        entries.append(entry)

    existing_notes = {
        (row.key, row.locale): row
        for row in db.scalars(
            select(TaxLegalNote).where(TaxLegalNote.key.in_({note["key"] for note in pack.legal_notes}))
        ).all()
    }
    for note in pack.legal_notes:
        row = existing_notes.get((note["key"], note["locale"]))
        entry = {
            "type": "legal_note",
            "key": note["key"],
            "locale": note["locale"],
            "old": row.text if row is not None else None,
            "new": note["text"],
        }
        if row is None:
            entry["action"] = "add"
        elif row.text == note["text"]:
            entry["action"] = "unchanged"
        elif row.edited_by_admin:
            entry["action"] = "kept_edited"
        else:
            entry["action"] = "change"
        entries.append(entry)

    order = {"tax_class": 0, "rule": 1, "legal_note": 2}
    entries.sort(key=lambda e: (order[e["type"]], e["key"], e.get("locale") or ""))
    return entries


def _build(
    db: Session, pack_id: str, version: str, effective_from: datetime, now: Optional[datetime], *, lock: bool = False
):
    effective_from = _check_effective_from(effective_from, now)
    # Loaded first: a plugin pack is fetched before any rule row is locked.
    pack = _load(db, pack_id, version)
    in_effect = _rows_in_effect(db, pack.pack_id, effective_from, lock=lock)
    entries = _compute(db, pack, effective_from, in_effect)
    diff = PackDiff(
        pack_id=pack_id,
        version=version,
        effective_from=effective_from,
        entries=tuple(entries),
        diff_hash=_diff_hash(pack_id, version, effective_from, entries),
    )
    return pack, diff, in_effect


def _close(row: TaxRule, at: datetime) -> None:
    start = _utc(row.valid_from)
    row.valid_to = at if start <= at else start


def _insert(
    db: Session,
    pack: PackData,
    rule: dict[str, Any],
    effective_from: datetime,
    classes: dict[str, int],
    *,
    supersedes: Optional[TaxRule],
) -> TaxRule:
    row = TaxRule(
        name=rule["name"],
        country=rule["country"],
        region=rule["region"],
        postcode_pattern=rule["postcode_pattern"],
        tax_class_id=classes[rule["tax_class"]],
        customer_type=rule["customer_type"],
        rate_percent=Decimal(rule["rate_percent"]),
        priority=rule["priority"],
        compound=rule["compound"],
        compound_order=rule["compound_order"],
        jurisdiction=rule["jurisdiction"],
        reverse_charge=rule["reverse_charge"],
        reverse_charge_zone=rule["reverse_charge_zone"],
        legal_note_key=rule["legal_note_key"],
        rounding_point=rule["rounding_point"],
        rounding_rule=rule["rounding_rule"],
        valid_from=max(effective_from, rule["valid_from"]),
        valid_to=rule["valid_to"],
        # A superseding row keeps an administrator's decision to disable the rule.
        enabled=bool(supersedes.enabled) if supersedes is not None else True,
        source=TaxRuleSource.PACK,
        pack_id=pack.pack_id,
        pack_version=pack.version,
        pack_rule_key=rule["pack_rule_key"],
        supersedes_id=supersedes.id if supersedes is not None else None,
    )
    db.add(row)
    return row


class PackImporter:
    @staticmethod
    def preview(
        db: Session,
        pack_id: str,
        version: str,
        effective_from: datetime,
        *,
        now: Optional[datetime] = None,
    ) -> PackDiff:
        """What ``apply`` would do. Reads only. Raises ``PackImportError``."""
        return _build(db, pack_id, version, effective_from, now)[1]

    @staticmethod
    def apply(
        db: Session,
        pack_id: str,
        version: str,
        effective_from: datetime,
        *,
        diff_hash: str,
        actor_user_id: Optional[int],
        now: Optional[datetime] = None,
    ) -> PackDiff:
        """Apply the diff the administrator previewed, in the caller's transaction (flushes, no commit).

        The rules in effect are locked first, in one statement in ascending id, and the
        diff is rebuilt from the locked rows. Raises ``PackImportError`` with code
        ``pack_changed`` when that diff is no longer the one whose hash the
        administrator saw. Nothing is written before that check; on any error the
        caller rolls the transaction back, which also removes the audit entry.
        """
        pack, diff, in_effect = _build(db, pack_id, version, effective_from, now, lock=True)
        if diff.diff_hash != diff_hash:
            raise PackImportError(
                "pack_changed",
                "the pack or its rules in effect changed since the preview; preview the pack again before applying it",
            )
        effective_from = diff.effective_from

        classes = {row.code: row.id for row in db.scalars(select(TaxClass)).all()}
        for entry in diff.entries:
            if entry["type"] == "tax_class" and entry["action"] == "add":
                name, description = CLASS_NAMES.get(entry["key"], (entry["key"].replace("_", " ").capitalize(), None))
                row = TaxClass(
                    code=entry["key"],
                    name=name,
                    description=description,
                    system=entry["key"] in _SYSTEM_CLASSES,
                )
                db.add(row)
                db.flush()
                classes[row.code] = row.id

        rules = {rule["pack_rule_key"]: rule for rule in pack.rules}
        for entry in diff.entries:
            if entry["type"] != "rule" or entry["action"] == "unchanged":
                continue
            rows = in_effect.get(entry["key"], [])
            for row in rows:
                _close(row, effective_from)
            if entry["action"] in ("add", "change"):
                _insert(
                    db,
                    pack,
                    rules[entry["key"]],
                    effective_from,
                    classes,
                    supersedes=rows[-1] if rows else None,
                )

        notes = {
            (row.key, row.locale): row
            for row in db.scalars(
                select(TaxLegalNote).where(TaxLegalNote.key.in_({note["key"] for note in pack.legal_notes}))
            ).all()
        }
        for entry in diff.entries:
            if entry["type"] != "legal_note":
                continue
            if entry["action"] == "add":
                db.add(TaxLegalNote(key=entry["key"], locale=entry["locale"], text=entry["new"], edited_by_admin=False))
            elif entry["action"] == "change":
                notes[(entry["key"], entry["locale"])].text = entry["new"]
        db.flush()

        changed = [entry for entry in diff.entries if entry["action"] != "unchanged"]
        AuditService.log(
            db,
            actor_user_id=actor_user_id,
            action=AUDIT_ACTION,
            resource_type="tax_rule_pack",
            resource_id=pack_id,
            metadata={
                "pack_id": pack_id,
                "version": version,
                "effective_from": _iso(effective_from),
                "diff_hash": diff.diff_hash,
                "counts": {
                    action: diff.count(action)
                    for action in ("add", "unchanged", "change", "remove", "kept_edited")
                },
                "entries": changed,
            },
        )
        return diff
