"""Migration billing_p2_exchange_rates: the rates table, the invoice columns and the job's state row."""

from tests.migrations import helpers

REVISION = "billing_p2_exchange_rates"
INVOICE_COLUMNS = {"fx_rate", "fx_period", "fx_source", "tax_total_gbp_cents"}
JOB = "tax_exchange_rates_fetch"


def _job_mode(engine):
    return helpers.scalar(engine, "SELECT mode FROM billing_job_states WHERE job = :job", job=JOB)


def test_up_down_up(p2_engine_at):
    engine = p2_engine_at(helpers.before(REVISION))
    assert "tax_exchange_rates" not in helpers.tables(engine)
    assert not INVOICE_COLUMNS & helpers.columns(engine, "invoices")
    assert _job_mode(engine) is None

    helpers.migrate(engine, REVISION)
    assert "tax_exchange_rates" in helpers.tables(engine)
    assert INVOICE_COLUMNS <= helpers.columns(engine, "invoices")
    assert _job_mode(engine) == "off"
    helpers.execute(
        engine,
        "INSERT INTO tax_exchange_rates (source, currency, period, units_per_gbp, period_start, period_end, "
        "fetched_at) VALUES ('hmrc_monthly', 'USD', '2026-11', 1.3396, '2026-11-01', '2026-11-30', "
        "'2026-11-01 00:00:00')",
    )
    assert helpers.scalar(engine, "SELECT base FROM tax_exchange_rates") == "GBP"

    helpers.migrate(engine, helpers.before(REVISION))
    assert "tax_exchange_rates" not in helpers.tables(engine)
    assert not INVOICE_COLUMNS & helpers.columns(engine, "invoices")
    assert _job_mode(engine) is None

    helpers.migrate(engine, REVISION)
    assert _job_mode(engine) == "off"
