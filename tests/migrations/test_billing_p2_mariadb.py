"""Billing phase 2 chain on MariaDB 11 (optional; skipped unless a server is given).

Set ``RACKFLOW_TEST_MARIADB_URL`` to an empty database whose name starts with
``rackflow_test``, for example
``mysql+pymysql://root:pw@127.0.0.1:3307/rackflow_test_p2``. The test drops and
recreates every table in it and drops them again afterwards.
``.forgejo/workflows/billing-mariadb.yml`` runs it against a ``mariadb:11.8``
service container.

As in ``test_billing_p1_mariadb.py``, the schema is created from the models,
stamped at the head and taken down to the phase 1 head through the real phase 2
downgrades; then, with data present (two ``tax_rates`` rows, a billing profile,
an email message), the chain is driven up, down and up again.
"""

import os
import subprocess
import sys
from decimal import Decimal

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext

from tests.migrations import helpers

URL = os.environ.get("RACKFLOW_TEST_MARIADB_URL", "")

pytestmark = pytest.mark.skipif(not URL, reason="RACKFLOW_TEST_MARIADB_URL is not set")


@pytest.fixture
def mariadb():
    import app.models  # noqa: F401
    from app.core.database import Base, configure_engine

    database = sa.engine.make_url(URL).database or ""
    if not database.startswith("rackflow_test"):
        pytest.fail(f"RACKFLOW_TEST_MARIADB_URL database {database!r} must start with rackflow_test")
    engine = configure_engine(sa.create_engine(URL, pool_pre_ping=True))
    _drop_all(engine)
    Base.metadata.create_all(engine)
    models_shape = _head_shape(engine)
    with engine.begin() as conn:
        MigrationContext.configure(conn).stamp(helpers.script(), helpers.HEAD)
    helpers.migrate(engine, helpers.P1_HEAD)
    yield engine, models_shape
    _drop_all(engine)
    engine.dispose()


def _drop_all(engine) -> None:
    with engine.begin() as conn:
        conn.execute(sa.text("SET foreign_key_checks = 0"))
        for table in sa.inspect(conn).get_table_names():
            conn.execute(sa.text(f"DROP TABLE `{table}`"))
        conn.execute(sa.text("SET foreign_key_checks = 1"))


def _foreign_keys(engine, table):
    with engine.connect() as conn:
        return {
            (fk["name"], tuple(fk["constrained_columns"]), fk["referred_table"])
            for fk in sa.inspect(conn).get_foreign_keys(table)
        }


def _head_shape(engine):
    """Columns and named foreign keys of every table the phase 2 chain creates or alters."""
    return {
        table: (helpers.columns(engine, table), _foreign_keys(engine, table))
        for table in (*helpers.P2_COLUMNS, *helpers.P2_NEW_TABLES)
    }


def _shape(engine):
    return {
        table: (helpers.columns(engine, table), _foreign_keys(engine, table)) for table in helpers.P2_COLUMNS
    }


def _assert_head(engine, models_shape):
    assert helpers.current(engine) == helpers.HEAD
    assert _head_shape(engine) == models_shape
    rules = helpers.execute(
        engine,
        "SELECT legacy_tax_rate_id, rate_percent, priority, enabled, jurisdiction, rounding_point, rounding_rule, "
        "source FROM tax_rules ORDER BY legacy_tax_rate_id",
    ).all()
    assert [tuple(row) for row in rules] == [
        (7, Decimal("20.0000"), 0, 1, "ANY", "invoice_rate", "tax_floor", "tax_rates"),
        (8, Decimal("7.2500"), 10, 0, "ANY", "invoice_rate", "tax_floor", "tax_rates"),
    ]
    assert helpers.scalar(engine, "SELECT COUNT(*) FROM tax_classes WHERE `system` = 1") == 5
    assert helpers.scalar(engine, "SELECT COUNT(*) FROM billing_job_states WHERE mode = 'off'") == 7
    assert helpers.scalar(engine, "SELECT COUNT(*) FROM billing_nonpayment_policies") == 2
    assert helpers.scalar(engine, "SELECT COUNT(*) FROM email_templates WHERE enabled = 1") == 7
    assert helpers.scalar(engine, "SELECT tax_id_status FROM billing_profiles WHERE id = 41") == "unchecked"
    assert helpers.scalar(engine, "SELECT render_fallback FROM email_messages WHERE id = 41") == 0


def _alembic_upgrade_head() -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=helpers.ROOT,
        env={**os.environ, "DATABASE_URL": URL},
        capture_output=True,
        text=True,
        timeout=600,
    )


def test_failed_data_revision_keeps_the_last_completed_version_on_mariadb(mariadb):
    """``alembic upgrade head`` stopped by the seed check leaves ``alembic_version`` at
    the revision before it, so the rerun after the fix needs no manual stamp."""
    engine, _models_shape = mariadb
    helpers.seed_p2_data(engine)
    helpers.execute(
        engine,
        "INSERT INTO tax_rates (id, country, region, rate_bps, name, enabled) "
        "VALUES (9, 'XX', NULL, 10000000, 'Bad', 1)",
    )

    failed = _alembic_upgrade_head()
    assert failed.returncode != 0
    assert "id 9 has rate_bps 10000000" in failed.stderr
    assert helpers.current(engine) == helpers.before("billing_p2_tax_seed")
    assert "tax_id_status" in helpers.columns(engine, "billing_profiles")
    assert helpers.scalar(engine, "SELECT COUNT(*) FROM tax_classes") == 0

    helpers.execute(engine, "UPDATE tax_rates SET rate_bps = 2000 WHERE id = 9")
    rerun = _alembic_upgrade_head()
    assert rerun.returncode == 0, rerun.stderr[-3000:]
    assert helpers.current(engine) == helpers.HEAD
    assert helpers.scalar(engine, "SELECT COUNT(*) FROM tax_rules WHERE source = 'tax_rates'") == 3


def test_p2_chain_up_down_up_on_mariadb(mariadb):
    engine, models_shape = mariadb
    assert helpers.current(engine) == helpers.P1_HEAD
    assert not helpers.P2_NEW_TABLES & helpers.tables(engine)
    at_p1 = _shape(engine)
    helpers.seed_p2_data(engine)

    helpers.migrate(engine, helpers.HEAD)
    _assert_head(engine, models_shape)

    # MariaDB enforces the policy scope check.
    for scope, product_id in (("product", 0), ("retail_default", 90)):
        with pytest.raises(sa.exc.DBAPIError, match="ck_billing_nonpayment_policies_scope_product"):
            helpers.execute(
                engine,
                "INSERT INTO billing_nonpayment_policies (scope, product_id) VALUES (:s, :p)",
                s=scope, p=product_id,
            )
    helpers.execute(engine, "INSERT INTO billing_nonpayment_policies (scope, product_id) VALUES ('product', 90)")

    # The seed downgrade refuses while a product uses a system class; the
    # revisions above it have been downgraded and committed by then.
    standard = helpers.scalar(engine, "SELECT id FROM tax_classes WHERE code = 'standard'")
    helpers.execute(
        engine,
        "INSERT INTO products (id, name, code, overrides, enabled, tax_class_id) "
        "VALUES (90, 'VPS', 'vps-p2', '{}', 1, :c)",
        c=standard,
    )
    with pytest.raises(RuntimeError, match="products with a system tax class"):
        helpers.migrate(engine, helpers.P1_HEAD)
    assert helpers.current(engine) == "billing_p2_tax_seed"
    helpers.execute(engine, "UPDATE products SET tax_class_id = NULL")

    helpers.migrate(engine, helpers.P1_HEAD)
    assert helpers.current(engine) == helpers.P1_HEAD
    assert not helpers.P2_NEW_TABLES & helpers.tables(engine)
    assert _shape(engine) == at_p1
    assert helpers.scalar(engine, "SELECT COUNT(*) FROM tax_rates") == 2
    assert helpers.scalar(engine, "SELECT tax_id FROM billing_profiles WHERE id = 41") == "GB123456789"

    helpers.migrate(engine, helpers.HEAD)
    _assert_head(engine, models_shape)
