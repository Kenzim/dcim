"""Migration billing_p2_automation: job tables, one off-mode state per job, two nonpayment policies."""

import pytest
import sqlalchemy as sa

from tests.migrations import helpers

REVISION = "billing_p2_automation"
TABLES = {
    "billing_job_runs",
    "billing_job_states",
    "billing_job_run_items",
    "billing_job_actions",
    "billing_nonpayment_policies",
    "cancellation_requests",
}
JOBS = {
    "generate_invoices_ahead",
    "invoice_reminders",
    "late_fees",
    "nonpayment_suspension",
    "nonpayment_termination",
    "cancellations",
    "tax_id_recheck",
}


def _assert_seeded(engine):
    states = dict(helpers.execute(engine, "SELECT job, mode FROM billing_job_states").all())
    assert states == {job: "off" for job in JOBS}
    assert helpers.scalar(engine, "SELECT COUNT(*) FROM billing_job_states WHERE live_since IS NOT NULL "
                                  "OR dry_run_since IS NOT NULL OR config_hash IS NOT NULL") == 0
    policies = helpers.execute(
        engine,
        "SELECT scope, product_id, suspend_after_days, terminate_after_days, termination_notice_days, "
        "unpaid_invoice_action_on_terminate, enabled FROM billing_nonpayment_policies ORDER BY scope",
    ).all()
    assert [tuple(row) for row in policies] == [
        ("reseller_default", 0, None, None, 7, "keep", 1),
        ("retail_default", 0, None, None, 7, "keep", 1),
    ]


def test_up_down_up_with_data(p2_engine_at):
    engine = p2_engine_at(helpers.before(REVISION))
    assert not TABLES & helpers.tables(engine)

    helpers.migrate(engine, REVISION)
    assert TABLES <= helpers.tables(engine)
    _assert_seeded(engine)
    assert ("", True, ("job", "subject_key")) in helpers.sqlite_indexes(engine, "billing_job_actions")
    assert ("billing_job_runs", "run_id", "id", "SET NULL") in helpers.sqlite_foreign_keys(
        engine, "billing_job_actions"
    )
    assert ("billing_job_runs", "run_id", "id", "CASCADE") in helpers.sqlite_foreign_keys(
        engine, "billing_job_run_items"
    )
    # The job list matches the model registry.
    from app.models.billing_jobs import BILLING_JOB_NAMES

    assert set(BILLING_JOB_NAMES) == JOBS

    helpers.migrate(engine, helpers.before(REVISION))
    assert not TABLES & helpers.tables(engine)
    assert helpers.scalar(engine, "SELECT COUNT(*) FROM tax_rules") == 2

    helpers.migrate(engine, REVISION)
    _assert_seeded(engine)


@pytest.mark.parametrize(
    "scope, product_id, allowed",
    [
        ("product", 0, False),
        ("retail_default", 5, False),
        ("reseller_default", 5, False),
        ("product", -1, False),
        ("product", 5, True),
    ],
)
def test_policy_scope_matches_product_id(p2_engine_at, scope, product_id, allowed):
    engine = p2_engine_at(REVISION)
    sql = "INSERT INTO billing_nonpayment_policies (scope, product_id) VALUES (:s, :p)"
    if allowed:
        helpers.execute(engine, sql, s=scope, p=product_id)
        assert helpers.scalar(engine, "SELECT COUNT(*) FROM billing_nonpayment_policies") == 3
    else:
        with pytest.raises(sa.exc.IntegrityError, match="ck_billing_nonpayment_policies_scope_product|CHECK"):
            helpers.execute(engine, sql, s=scope, p=product_id)


def test_unique_keys(p2_engine_at):
    engine = p2_engine_at(REVISION)
    with pytest.raises(sa.exc.IntegrityError):
        helpers.execute(engine, "INSERT INTO billing_nonpayment_policies (scope, product_id) "
                                "VALUES ('retail_default', 0)")
    helpers.execute(engine, "INSERT INTO billing_job_actions (job, subject_key, claimed_at) "
                            "VALUES ('late_fees', 'invoice:1', CURRENT_TIMESTAMP)")
    with pytest.raises(sa.exc.IntegrityError):
        helpers.execute(engine, "INSERT INTO billing_job_actions (job, subject_key, claimed_at) "
                                "VALUES ('late_fees', 'invoice:1', CURRENT_TIMESTAMP)")
