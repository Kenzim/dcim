"""Revision ``billing_p1_backfill_payments``: refunded_cents for refunded payments."""

from app.services import billing_backfill
from tests.migrations import helpers
from tests.migrations.helpers import execute, scalar

REVISION = "billing_p1_backfill_payments"


def _payments(engine):
    for payment_id, status, amount in [
        (1, "refunded", 500), (2, "succeeded", 300), (3, "refunded", 250), (4, "failed", 100), (5, "refunded", 75),
    ]:
        execute(
            engine,
            "INSERT INTO payments (id, invoice_id, gateway, status, amount_cents, metadata) "
            "VALUES (:id, 1, 'stripe', :status, :amount, '{}')",
            id=payment_id, status=status, amount=amount,
        )


def _refunded(engine):
    with engine.connect() as conn:
        import sqlalchemy as sa

        return dict(conn.execute(sa.text("SELECT id, refunded_cents FROM payments ORDER BY id")).all())


def test_chain_position():
    assert helpers.script().get_revision(REVISION).down_revision == helpers.before(REVISION)


def test_upgrade_records_refunded_amounts(engine_at):
    engine = engine_at(helpers.before(REVISION))
    _payments(engine)
    helpers.migrate(engine, REVISION)
    assert _refunded(engine) == {1: 500, 2: 0, 3: 250, 4: 0, 5: 75}


def test_idempotent_and_batched(engine_at):
    engine = engine_at(helpers.before(REVISION))
    _payments(engine)
    execute(engine, "UPDATE payments SET refunded_cents = 100 WHERE id = 3")
    with engine.begin() as conn:
        result = billing_backfill.backfill_payment_refunds(conn, batch_size=2)
    assert (result.batches, result.rows) == (3, 2)
    # A partially recorded refund is left alone; only zero rows are filled.
    assert _refunded(engine) == {1: 500, 2: 0, 3: 100, 4: 0, 5: 75}
    with engine.begin() as conn:
        assert billing_backfill.backfill_payment_refunds(conn, batch_size=2).rows == 0


def test_downgrade_and_upgrade_again(engine_at):
    engine = engine_at(REVISION)
    helpers.migrate(engine, helpers.before(REVISION))
    helpers.migrate(engine, REVISION)
    assert scalar(engine, "SELECT COUNT(*) FROM payments") == 0
