"""Migration billing_p2_fx_refs: references from invoices to the exchange rate they froze."""

from tests.migrations import helpers

REVISION = "billing_p2_fx_refs"
INVOICE_COLUMNS = {"fx_rate_id"}
NOTE_COLUMNS = {"fx_rate", "fx_period", "fx_source", "tax_total_gbp_cents", "fx_rate_id"}
RATE_COLUMNS = {"hand_entered", "compared_at"}


def _rate_and_invoice(engine) -> None:
    helpers.execute(
        engine,
        "INSERT INTO tax_exchange_rates (id, source, currency, period, units_per_gbp, period_start, period_end, "
        "fetched_at) VALUES (5, 'hmrc_monthly', 'USD', '2026-11', 1.3396, '2026-11-01', '2026-11-30', "
        "'2026-11-01 00:00:00')",
    )
    helpers.execute(
        engine,
        "INSERT INTO invoices (id, invoice_number, purpose, status, amount_cents, currency, fx_rate, fx_period, fx_source, "
        "tax_total_gbp_cents) VALUES (61, 'INV-FX-61', 'deploy', 'open', 1200, 'USD', 1.3396, '2026-11', 'hmrc_monthly', 149)",
    )
    helpers.execute(
        engine,
        "INSERT INTO invoices (id, invoice_number, purpose, status, amount_cents, currency) VALUES (62, 'INV-FX-62', "
        "'deploy', 'open', 1000, 'USD')",
    )


def test_up_down_up_and_the_frozen_rate_is_found(p2_engine_at):
    engine = p2_engine_at(helpers.before(REVISION))
    assert not INVOICE_COLUMNS & helpers.columns(engine, "invoices")
    assert not NOTE_COLUMNS & helpers.columns(engine, "invoice_adjustments")
    _rate_and_invoice(engine)

    helpers.migrate(engine, REVISION)
    assert INVOICE_COLUMNS <= helpers.columns(engine, "invoices")
    assert NOTE_COLUMNS <= helpers.columns(engine, "invoice_adjustments")
    assert RATE_COLUMNS <= helpers.columns(engine, "tax_exchange_rates")
    assert helpers.scalar(engine, "SELECT hand_entered FROM tax_exchange_rates WHERE id = 5") == 0
    assert ("ix_invoices_fx_rate_id", False, ("fx_rate_id", "fx_period")) in helpers.sqlite_indexes(
        engine, "invoices"
    )
    assert ("ix_invoice_adjustments_fx_rate_id", False, ("fx_rate_id",)) in helpers.sqlite_indexes(
        engine, "invoice_adjustments"
    )
    assert helpers.scalar(engine, "SELECT fx_rate_id FROM invoices WHERE id = 61") == 5
    assert helpers.scalar(engine, "SELECT fx_rate_id FROM invoices WHERE id = 62") is None

    helpers.migrate(engine, helpers.before(REVISION))
    assert not INVOICE_COLUMNS & helpers.columns(engine, "invoices")
    assert not NOTE_COLUMNS & helpers.columns(engine, "invoice_adjustments")
    assert not RATE_COLUMNS & helpers.columns(engine, "tax_exchange_rates")
    helpers.migrate(engine, REVISION)
    assert helpers.scalar(engine, "SELECT fx_rate_id FROM invoices WHERE id = 61") == 5
