"""Revision ``billing_p1_series_shadow``: series and shadow tables, seeds, counters."""

import pytest
import sqlalchemy as sa

from tests.migrations import helpers
from tests.migrations.helpers import execute, scalar

REVISION = "billing_p1_series_shadow"


def test_chain_position():
    assert helpers.script().get_revision(REVISION).down_revision == helpers.before(REVISION)


def test_upgrade_seeds_four_series_and_counters(engine_at):
    engine = engine_at(REVISION)
    assert {"document_series", "billing_shadow_mismatches"} <= helpers.tables(engine)
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT code, document_type, format, number_source, gapless, default_for, enabled "
                "FROM document_series ORDER BY id"
            )
        ).all()
    assert [tuple(row) for row in rows] == [
        ("INV", "invoice", "{NUMBER}", "invoice_number", 0, "invoice", 1),
        ("PF", "proforma", "{SERIES}-{NUMBER}", "own", 0, "proforma", 1),
        ("CN", "credit_note", "{SERIES}-{NUMBER}", "own", 0, "credit_note", 1),
        ("DN", "debit_note", "{SERIES}-{NUMBER}", "own", 0, "debit_note", 1),
    ]
    ids = [scalar(engine, "SELECT id FROM document_series WHERE code = :c", c=c) for c in ("PF", "CN", "DN")]
    for series_id in ids:
        assert scalar(engine, "SELECT next_value FROM invoice_sequences WHERE name = :n", n=f"series:{series_id}:") == 1
    assert scalar(engine, "SELECT COUNT(*) FROM invoice_sequences WHERE name LIKE 'series:%'") == 3


def test_checks_and_uniques(engine_at):
    engine = engine_at(REVISION)
    bad = [
        "INSERT INTO document_series (code, document_type, name, number_source, gapless, reset_period) "
        "VALUES ('X1', 'credit_note', 'x', 'invoice_number', 0, 'never')",
        "INSERT INTO document_series (code, document_type, name, default_for) VALUES ('X2', 'invoice', 'x', 'proforma')",
        "INSERT INTO document_series (code, document_type, name, number_padding) VALUES ('X3', 'invoice', 'x', 13)",
        "INSERT INTO document_series (code, document_type, name) VALUES ('PF', 'proforma', 'dup')",
        "INSERT INTO document_series (code, document_type, name, default_for) VALUES ('X4', 'invoice', 'x', 'invoice')",
    ]
    for sql in bad:
        with pytest.raises(sa.exc.IntegrityError):
            execute(engine, sql)


def test_shadow_mismatch_unique_key(engine_at):
    engine = engine_at(REVISION)
    sql = (
        "INSERT INTO billing_shadow_mismatches (engine, check_key, subject_type, subject_id, legacy_value, "
        "v2_value, first_seen_at, last_seen_at) VALUES ('credit', 'c1_cache_pair', 'reseller', 1, '1', '2', "
        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
    )
    execute(engine, sql)
    with pytest.raises(sa.exc.IntegrityError):
        execute(engine, sql)


def test_downgrade_removes_and_upgrade_again(engine_at):
    engine = engine_at(REVISION)
    helpers.migrate(engine, helpers.before(REVISION))
    assert not {"document_series", "billing_shadow_mismatches"} & helpers.tables(engine)
    assert scalar(engine, "SELECT COUNT(*) FROM invoice_sequences WHERE name LIKE 'series:%'") == 0
    helpers.migrate(engine, REVISION)
    assert scalar(engine, "SELECT COUNT(*) FROM document_series") == 4
