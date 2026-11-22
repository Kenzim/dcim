"""Tax pack import against a concurrent publish on MariaDB (optional; skipped unless a server is given).

Set ``RACKFLOW_TEST_MARIADB_URL`` to an empty throwaway database whose name starts
with ``rackflow_test``, for example ``mysql+pymysql://root:pw@127.0.0.1:3307/rackflow_test``.
The tests create every table from the models and drop them all afterwards.
``.forgejo/workflows/billing-mariadb.yml`` runs them against a ``mariadb:11.8`` service container.

The tax engine's publish locks the rule rows an invoice uses in ascending id
(``LOCK IN SHARE MODE``), and ``PackImporter.apply`` locks the rules in effect in
ascending id (``FOR UPDATE``) before it writes, so the two wait for each other
instead of deadlocking (to-do b245eae1). The UK rules get ids that run against
their rule keys, so the importer's diff visits them in the opposite order to the
ids, and the EU pack is applied too, so that MariaDB would read the UK rules
through ``ix_tax_rules_pack_rule`` (rule key order) if the lock did not force the
primary key.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

URL = os.environ.get("RACKFLOW_TEST_MARIADB_URL", "")

pytestmark = pytest.mark.skipif(not URL, reason="RACKFLOW_TEST_MARIADB_URL is not set")

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
CHANGE_AT = datetime(2027, 4, 1, tzinfo=timezone.utc)
CHANGE_NOW = datetime(2027, 1, 10, tzinfo=timezone.utc)
VERSION = "2027.1.1"
# Rules whose rates the new version changes; in rule key order the higher id comes first.
FIRST_IN_DIFF, LAST_IN_DIFF = "gb_digital_service", "gb_standard"


def _drop_all(engine) -> None:
    with engine.begin() as conn:
        conn.execute(sa.text("SET foreign_key_checks = 0"))
        for table in sa.inspect(conn).get_table_names():
            conn.execute(sa.text(f"DROP TABLE `{table}`"))
        conn.execute(sa.text("SET foreign_key_checks = 1"))


@pytest.fixture
def engine(monkeypatch):
    import app.models  # noqa: F401
    from app.core.database import Base, configure_engine
    from app.plugin_host import providers as plugin_providers
    from app.services.tax.packs import registry as registry_module
    from app.services.tax.packs.schema import validate_pack

    database = sa.engine.make_url(URL).database or ""
    if not database.startswith("rackflow_test"):
        pytest.fail(f"RACKFLOW_TEST_MARIADB_URL database {database!r} must start with rackflow_test")
    built = configure_engine(sa.create_engine(URL, pool_pre_ping=True, pool_size=10, max_overflow=0))
    _drop_all(built)
    Base.metadata.create_all(built)

    monkeypatch.setattr(plugin_providers, "providers_of", lambda kind: [])
    registry_module._builtin.cache_clear()
    original = registry_module._builtin
    data = json.loads((Path(registry_module.__file__).resolve().parent / "uk_vat.json").read_text())
    data["version"] = VERSION
    for rule in data["rules"]:
        if Decimal(rule["rate_percent"]) > 0:
            rule["rate_percent"] = str(Decimal(rule["rate_percent"]) + 1)
    new_uk = registry_module._pack_data("uk_vat", validate_pack(data), "builtin")
    _seed(built)
    monkeypatch.setattr(
        registry_module, "_builtin", lambda pack_id: new_uk if pack_id == "uk_vat" else original(pack_id)
    )
    yield built
    _drop_all(built)
    built.dispose()


def _seed(engine) -> None:
    from tests.services.tax_pack_helpers import apply_pack, seed_tax_basics

    with sessionmaker(bind=engine)() as db:
        seed_tax_basics(db)
        apply_pack(db, "eu_vat", now=NOW)
        apply_pack(db, "uk_vat", now=NOW)
        db.commit()
    with engine.begin() as conn:
        # UK rule ids now descend in rule key order.
        conn.execute(sa.text("UPDATE tax_rules SET id = 100000 - id WHERE pack_id = 'uk_vat' ORDER BY id DESC"))


def _rule_ids(engine) -> dict[str, int]:
    with engine.connect() as conn:
        rows = conn.execute(sa.text("SELECT pack_rule_key, id FROM tax_rules WHERE pack_id = 'uk_vat'")).all()
    return dict(rows)


def _state(engine):
    with engine.connect() as conn:
        rules = conn.execute(
            sa.text("SELECT id, rate_percent, valid_to FROM tax_rules WHERE pack_id = 'uk_vat' ORDER BY id")
        ).all()
        audits = conn.execute(
            sa.text("SELECT COUNT(*) FROM user_audit_events WHERE action = 'billing.tax.pack_applied'")
        ).scalar()
    return [tuple(row) for row in rules], audits


def _preview(db):
    from app.services.tax.pack_import import PackImporter

    return PackImporter.preview(db, "uk_vat", VERSION, CHANGE_AT, now=CHANGE_NOW)


def _apply(db, diff_hash):
    from app.services.tax.pack_import import PackImporter

    return PackImporter.apply(
        db, "uk_vat", VERSION, CHANGE_AT, diff_hash=diff_hash, actor_user_id=None, now=CHANGE_NOW
    )


def _wait_until_import_blocks(engine, worker, outcome, timeout=8.0) -> None:
    """Wait until the import's locking read has been running for a while (it is waiting for a lock).

    The process list is used because INNODB_TRX does not reliably list a transaction that
    has not written yet.
    """
    query = sa.text(
        "SELECT MAX(time_ms) FROM information_schema.PROCESSLIST "
        "WHERE info LIKE 'SELECT tax_rules.id%' AND info LIKE '%FOR UPDATE'"
    )
    deadline = time.monotonic() + timeout
    with engine.connect() as conn:
        while time.monotonic() < deadline:
            if not worker.is_alive():
                pytest.fail(f"the import finished without waiting: {outcome!r}")
            if (conn.execute(query).scalar() or 0) > 300:
                return
            conn.rollback()
            time.sleep(0.05)
    pytest.fail("the import never waited for the publish's lock")


def test_import_and_publish_locking_rules_in_opposite_orders_wait_instead_of_deadlocking(engine):
    ids = _rule_ids(engine)
    low, high = ids[LAST_IN_DIFF], ids[FIRST_IN_DIFF]
    assert low < high
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with sessions() as db:
        diff_hash = _preview(db).diff_hash

    outcome: dict[str, object] = {}

    def run_import():
        with sessions() as db:
            db.execute(sa.text("SET SESSION innodb_lock_wait_timeout = 10"))
            try:
                outcome["diff"] = _apply(db, diff_hash)
                db.commit()
            except Exception as exc:  # reported by the assertions below
                db.rollback()
                outcome["error"] = exc

    worker = threading.Thread(target=run_import)
    with engine.connect() as publish:
        publish.execute(sa.text("SET SESSION innodb_lock_wait_timeout = 10"))
        publish.execute(sa.text("SELECT id FROM tax_rules WHERE id = :id LOCK IN SHARE MODE"), {"id": low})
        try:
            worker.start()
            _wait_until_import_blocks(engine, worker, outcome)
            # The import waits on the lower id, so it holds no UK rule above it: this lock is granted.
            started = time.monotonic()
            publish.execute(sa.text("SELECT id FROM tax_rules WHERE id = :id LOCK IN SHARE MODE"), {"id": high})
            assert time.monotonic() - started < 5
            publish.commit()
        finally:
            publish.rollback()
            if worker.is_alive():
                worker.join(30)

    assert "error" not in outcome, outcome.get("error")
    rules, audits = _state(engine)
    assert audits == 3  # the EU and UK seeds, then this apply
    by_id = {row[0]: row for row in rules}
    assert by_id[low][2] is not None and by_id[high][2] is not None
    assert {str(row[1]) for row in rules if row[2] is None} >= {"21.0000", "6.0000"}


def test_apply_rebuilds_the_diff_from_locked_rows_not_its_snapshot(engine):
    ids = _rule_ids(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with sessions() as db:
        diff_hash = _preview(db).diff_hash  # REPEATABLE READ: this read fixes the snapshot
        with engine.begin() as other:
            other.execute(
                sa.text("UPDATE tax_rules SET rate_percent = 19.5 WHERE id = :id"), {"id": ids["gb_standard"]}
            )
        before = _state(engine)
        from app.services.tax.pack_import import PackImportError

        with pytest.raises(PackImportError) as err:
            _apply(db, diff_hash)
        assert err.value.code == "pack_changed"
        db.rollback()
    assert _state(engine) == before


def test_an_import_that_times_out_on_the_lock_leaves_nothing(engine):
    ids = _rule_ids(engine)
    before = _state(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with sessions() as db:
        diff_hash = _preview(db).diff_hash
    with engine.connect() as publish:
        publish.execute(
            sa.text("SELECT id FROM tax_rules WHERE id = :id LOCK IN SHARE MODE"), {"id": ids[LAST_IN_DIFF]}
        )
        with sessions() as db:
            db.execute(sa.text("SET SESSION innodb_lock_wait_timeout = 1"))
            with pytest.raises(sa.exc.OperationalError) as err:
                _apply(db, diff_hash)
            assert err.value.orig.args[0] == 1205
            db.rollback()
        publish.rollback()
    assert _state(engine) == before
