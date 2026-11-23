"""Migration billing_p2_tax_seed: system classes, default profile, legal notes and the tax_rates mirror."""

from decimal import Decimal

import pytest
import sqlalchemy as sa

from tests.migrations import helpers

REVISION = "billing_p2_tax_seed"
CLASSES = {"standard", "reduced", "zero", "exempt", "outside_scope"}
NOTES = {"gb_outside_scope_b2b", "eu_reverse_charge", "outside_scope", "exempt_customer", "credit_note_reference"}


def _mirror(engine):
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT r.legacy_tax_rate_id, r.name, r.country, r.region, c.code, r.customer_type, "
                "r.rate_percent, r.priority, r.compound, r.jurisdiction, r.reverse_charge, r.rounding_point, "
                "r.rounding_rule, r.valid_from, r.valid_to, r.enabled, r.source, r.pack_id "
                "FROM tax_rules r JOIN tax_classes c ON c.id = r.tax_class_id ORDER BY r.legacy_tax_rate_id"
            )
        ).mappings()
        return [dict(row) for row in rows]


def _assert_seeded(engine):
    assert {row[0] for row in helpers.execute(engine, "SELECT code FROM tax_classes WHERE system = 1")} == CLASSES
    assert helpers.scalar(engine, "SELECT COUNT(*) FROM tax_classes") == 5
    profile = helpers.execute(
        engine, "SELECT document_title, proforma_title, credit_note_title, locale, enabled FROM invoice_profiles "
        "WHERE name = 'default'"
    ).one()
    assert tuple(profile) == ("Invoice", "Proforma invoice", "Credit note", "en", 1)
    notes = {row[0]: row[1] for row in helpers.execute(engine, "SELECT key, text FROM tax_legal_notes WHERE locale = 'en'")}
    assert set(notes) == NOTES
    assert notes["exempt_customer"] == "VAT exempt: {reason}."
    assert notes["credit_note_reference"] == "Credit note for invoice {number} dated {date}."

    gb, ca = _mirror(engine)
    common = {
        "code": "standard",
        "customer_type": "any",
        "compound": 0,
        "jurisdiction": "ANY",
        "reverse_charge": 0,
        "rounding_point": "invoice_rate",
        "rounding_rule": "tax_floor",
        "valid_to": None,
        "source": "tax_rates",
        "pack_id": None,
    }
    for row in (gb, ca):
        assert {key: row[key] for key in common} == common
        assert str(row["valid_from"]).startswith("1970-01-01")
    assert (gb["legacy_tax_rate_id"], gb["name"], gb["country"], gb["region"]) == (7, "UK VAT", "GB", None)
    assert Decimal(str(gb["rate_percent"])) == Decimal("20")
    assert (gb["priority"], gb["enabled"]) == (0, 1)
    assert (ca["legacy_tax_rate_id"], ca["name"], ca["country"], ca["region"]) == (8, "California sales tax", "US", "CA")
    assert Decimal(str(ca["rate_percent"])) == Decimal("7.25")
    assert (ca["priority"], ca["enabled"]) == (10, 0)


def test_up_down_up_with_data(p2_engine_at):
    engine = p2_engine_at(helpers.before(REVISION))
    assert helpers.scalar(engine, "SELECT COUNT(*) FROM tax_rules") == 0

    helpers.migrate(engine, REVISION)
    _assert_seeded(engine)

    helpers.migrate(engine, helpers.before(REVISION))
    for table in ("tax_classes", "tax_rules", "invoice_profiles", "tax_legal_notes"):
        assert helpers.scalar(engine, f"SELECT COUNT(*) FROM {table}") == 0, table
    assert helpers.scalar(engine, "SELECT COUNT(*) FROM tax_rates") == 2

    helpers.migrate(engine, REVISION)
    _assert_seeded(engine)


@pytest.mark.parametrize("bad_bps", [10_001, 10_000_000, -1])
def test_upgrade_refuses_a_rate_outside_0_to_100_percent(p2_engine_at, bad_bps):
    engine = p2_engine_at(helpers.before(REVISION))
    helpers.execute(
        engine,
        "INSERT INTO tax_rates (id, country, region, rate_bps, name, enabled) VALUES (9, 'XX', NULL, :b, 'Bad', 1)",
        b=bad_bps,
    )
    with pytest.raises(RuntimeError, match=rf"tax_rates rows .*: id 9 has rate_bps {bad_bps}\. "):
        helpers.migrate(engine, REVISION)
    assert helpers.current(engine) == helpers.before(REVISION)
    for table in ("tax_classes", "tax_rules", "invoice_profiles", "tax_legal_notes"):
        assert helpers.scalar(engine, f"SELECT COUNT(*) FROM {table}") == 0, table

    helpers.execute(engine, "UPDATE tax_rates SET rate_bps = 10000 WHERE id = 9")
    helpers.migrate(engine, REVISION)
    rate = helpers.scalar(engine, "SELECT rate_percent FROM tax_rules WHERE legacy_tax_rate_id = 9")
    assert Decimal(str(rate)) == Decimal("100")


def test_rate_percent_is_exact():
    module = helpers.load_revision(REVISION)
    assert module.rate_percent_of(2000) == "20.0000"
    assert module.rate_percent_of(725) == "7.2500"
    assert module.rate_percent_of(1) == "0.0100"
    assert module.rate_percent_of(0) == "0.0000"
    assert module.rate_percent_of(10_000) == "100.0000"


def _exemption(engine, class_ids_json):
    helpers.execute(
        engine,
        "INSERT INTO tax_exemptions (id, billing_account_id, reason, valid_from, tax_class_ids) "
        "VALUES (60, 41, 'charity', '2026-01-01', :c)",
        c=class_ids_json,
    )


def _registration(engine, profile_id):
    helpers.execute(
        engine,
        "INSERT INTO seller_tax_registrations (id, jurisdiction, scheme, tax_number, country, valid_from, "
        "invoice_profile_id) VALUES (70, 'GB', 'domestic', 'GB999', 'GB', '2026-01-01', :p)",
        p=profile_id,
    )


@pytest.mark.parametrize(
    "block, message, unblock",
    [
        (
            lambda e, ids: _exemption(e, f"[999, {ids['exempt']}]"),
            r"1 tax_exemptions naming a system tax class in tax_class_ids \(ids 60\)",
            lambda e, ids: helpers.execute(e, "UPDATE tax_exemptions SET tax_class_ids = '[999]'"),
        ),
        (
            lambda e, ids: _registration(e, ids["profile"]),
            "1 seller_tax_registrations using the default invoice profile",
            lambda e, ids: helpers.execute(e, "UPDATE seller_tax_registrations SET invoice_profile_id = NULL"),
        ),
        (
            lambda e, ids: helpers.execute(
                e, "UPDATE tax_legal_notes SET text = 'Edited.', edited_by_admin = 1 WHERE key = 'outside_scope'"
            ),
            "1 seeded legal notes edited by an admin",
            lambda e, ids: helpers.execute(e, "UPDATE tax_legal_notes SET edited_by_admin = 0"),
        ),
    ],
    ids=["exemption", "registration", "edited_note"],
)
def test_downgrade_refuses_while_seeded_rows_are_referenced_or_edited(p2_engine_at, block, message, unblock):
    engine = p2_engine_at(REVISION)
    ids = {
        "exempt": helpers.scalar(engine, "SELECT id FROM tax_classes WHERE code = 'exempt'"),
        "profile": helpers.scalar(engine, "SELECT id FROM invoice_profiles WHERE name = 'default'"),
    }
    block(engine, ids)
    with pytest.raises(RuntimeError, match=message):
        helpers.migrate(engine, helpers.before(REVISION))
    assert helpers.current(engine) == REVISION
    assert helpers.scalar(engine, "SELECT COUNT(*) FROM tax_classes") == 5
    unblock(engine, ids)
    helpers.migrate(engine, helpers.before(REVISION))
    assert helpers.scalar(engine, "SELECT COUNT(*) FROM tax_classes") == 0


def test_downgrade_refuses_while_a_product_uses_a_system_class(p2_engine_at):
    engine = p2_engine_at(REVISION)
    standard = helpers.scalar(engine, "SELECT id FROM tax_classes WHERE code = 'standard'")
    helpers.execute(
        engine,
        "INSERT INTO products (id, name, code, overrides, enabled, tax_class_id) "
        "VALUES (90, 'VPS', 'vps', '{}', 1, :c)",
        c=standard,
    )
    with pytest.raises(RuntimeError, match="products with a system tax class"):
        helpers.migrate(engine, helpers.before(REVISION))
    assert helpers.current(engine) == REVISION
    helpers.execute(engine, "UPDATE products SET tax_class_id = NULL")
    helpers.migrate(engine, helpers.before(REVISION))
    assert helpers.scalar(engine, "SELECT COUNT(*) FROM tax_classes") == 0
