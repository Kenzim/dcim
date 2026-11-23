"""VM firewall claims and their row lock on MariaDB (optional, docs/vm-firewall.md section 6.2).

Skipped unless ``RACKFLOW_TEST_MARIADB_URL`` names an empty throwaway database
whose name starts with ``rackflow_test``. Every table is created from the
models and dropped afterwards.

SQLite ignores ``FOR UPDATE`` and the SQLite tests share one connection, so only
here do the claim's renewal (a short session of its own) and its confirmation
(``SELECT ... FOR UPDATE`` in the apply's session) run on separate connections
against real InnoDB locks. ``innodb_lock_wait_timeout`` is 2 s on every
connection, so a renewal or claim that waits for a lock it must not need fails
with error 1205 instead of passing slowly.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from datetime import timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from tests.services.test_billing_p1c_credit_mariadb import _drop_all

URL = os.environ.get("RACKFLOW_TEST_MARIADB_URL", "")

pytestmark = pytest.mark.skipif(not URL, reason="RACKFLOW_TEST_MARIADB_URL is not set")

LOCK_WAIT_SECONDS = 2


@pytest.fixture(params=["default_isolation", "read_committed"])
def factory(request):
    import app.models  # noqa: F401
    from app.core.database import Base, configure_engine

    database = sa.engine.make_url(URL).database or ""
    if not database.startswith("rackflow_test"):
        pytest.fail("RACKFLOW_TEST_MARIADB_URL must name a database starting with rackflow_test")
    options = {"pool_pre_ping": True, "pool_size": 8, "max_overflow": 0}
    if request.param == "read_committed":
        options["isolation_level"] = "READ COMMITTED"
    engine = configure_engine(sa.create_engine(URL, **options))

    @sa.event.listens_for(engine, "connect")
    def _short_lock_wait(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f"SET SESSION innodb_lock_wait_timeout = {LOCK_WAIT_SECONDS}")
        cursor.close()

    _drop_all(engine)
    Base.metadata.create_all(engine)
    # As the application's SessionLocal.
    yield sessionmaker(autocommit=False, autoflush=False, bind=engine)
    _drop_all(engine)
    engine.dispose()


def _world_vm(db, fake):
    from tests.fakes.vm_firewall_world import make_world
    from tests.services.test_vm_firewall_worker import _enabled_vm

    world = make_world(db)
    vm, _row, fake_vm = _enabled_vm(world, fake)
    return world, vm.id, fake_vm


def _claimed_vm(factory, owner="a"):
    from app.services.vm_firewall import reconcile
    from tests.fakes.proxmox_firewall import FakeProxmoxFirewall
    from tests.services.test_vm_firewall_worker import T0

    with factory() as db:
        _world, service_id, _fake_vm = _world_vm(db, FakeProxmoxFirewall())
        db.commit()
        assert reconcile.claim(db, service_id, owner=owner, now=T0)
    return service_id


def _row(factory, service_id):
    from app.models.vm_firewall import ServiceFirewall

    with factory() as db:
        row = db.get(ServiceFirewall, service_id)
        db.expunge(row)
        return row


def test_a_takeover_waits_for_the_applier_that_confirmed_its_claim(factory):
    """Between the applier's confirmation and its commit the row is locked, so a takeover (here of
    an expired claim) cannot land in between: it waits, and lands only after the outcome."""
    from app.models.vm_firewall import ServiceFirewall
    from app.services.vm_firewall import reconcile
    from tests.services.test_vm_firewall_worker import T0

    service_id = _claimed_vm(factory)
    later = T0 + timedelta(hours=1)
    took: dict[str, object] = {}
    with factory() as a:
        row = a.get(ServiceFirewall, service_id)
        held = reconcile.Claim(a.get_bind(), service_id, "a", lambda: T0)
        assert held.confirm(a)

        def takeover():
            with factory() as b:
                took["claim"] = reconcile.claim(b, service_id, owner="b", now=later)
                took["applied_hash"] = b.get(ServiceFirewall, service_id).applied_hash

        thread = threading.Thread(target=takeover)
        thread.start()
        thread.join(1.0)
        assert thread.is_alive(), "the takeover must wait for the applier's row lock"
        row.applied_revision = row.revision
        row.applied_hash = "recorded-by-a"
        a.flush()
        a.commit()
    thread.join(30)
    assert not thread.is_alive()
    assert took == {"claim": True, "applied_hash": "recorded-by-a"}
    row = _row(factory, service_id)
    assert row.applied_hash == "recorded-by-a" and row.claimed_by == "b"


def test_a_claim_taken_first_is_seen_by_the_confirmation_after_an_earlier_read(factory):
    """The applier read the row before the takeover committed (under REPEATABLE READ that fixes its
    snapshot); the locking read must still see the takeover and refuse."""
    from app.models.vm_firewall import ServiceFirewall
    from app.services.vm_firewall import reconcile
    from tests.services.test_vm_firewall_worker import T0

    service_id = _claimed_vm(factory)
    with factory() as a:
        assert a.get(ServiceFirewall, service_id).claimed_by == "a"
        with factory() as b:
            assert reconcile.claim(b, service_id, owner="b", now=T0 + timedelta(hours=1))
        held = reconcile.Claim(a.get_bind(), service_id, "a", lambda: T0)
        assert not held.renew()
        assert not held.confirm(a)
        a.rollback()
    assert _row(factory, service_id).claimed_by == "b"


def test_renewal_neither_waits_on_nor_commits_the_apply_sessions_pending_changes(factory):
    from app.models.vm_firewall import ServiceFirewall
    from app.services.vm_firewall import reconcile
    from tests.services.test_vm_firewall_worker import T0

    service_id = _claimed_vm(factory)
    renewed_at = T0 + timedelta(seconds=100)
    with factory() as a:
        row = a.get(ServiceFirewall, service_id)
        row.desired_hash = "pending-in-a"
        row.net0_bridge = "vmbr9"
        held = reconcile.Claim(a.get_bind(), service_id, "a", lambda: renewed_at)
        started = time.monotonic()
        assert held.renew()
        assert time.monotonic() - started < LOCK_WAIT_SECONDS / 2
        between = _row(factory, service_id)
        assert between.desired_hash != "pending-in-a" and between.net0_bridge != "vmbr9"
        assert between.claimed_until == (renewed_at + timedelta(seconds=reconcile.CLAIM_SECONDS)).replace(tzinfo=None)
        assert held.confirm(a)
        a.flush()
        a.commit()
    row = _row(factory, service_id)
    assert row.desired_hash == "pending-in-a" and row.net0_bridge == "vmbr9" and row.claimed_by == "a"


def test_queue_apply_longer_than_a_claim_renews_on_its_own_connections(factory, monkeypatch):
    """The whole queue path: every write moves the clock 50 s, the deployment step tries to claim
    at each, and the apply records its outcome. A renewal waiting on the apply's session would
    fail with error 1205 here."""
    from datetime import datetime, timezone

    from app.services.vm_firewall import reconcile
    from tests.fakes.proxmox_firewall import FakeProxmoxFirewall
    from tests.services.test_vm_firewall_worker import OWNER, T0, FakeAccess, _on_every_write

    fake = FakeProxmoxFirewall()
    wall = {"now": datetime(2000, 1, 1, tzinfo=timezone.utc)}
    start = wall["now"]
    monkeypatch.setattr(reconcile, "utcnow", lambda: wall["now"])
    refused = []

    def slow_write(_fake):
        wall["now"] += timedelta(seconds=50)
        with factory() as other:
            refused.append(not reconcile.claim(other, service_id, owner="deployment", now=T0 + (wall["now"] - start)))

    with factory() as db:
        _world, service_id, fake_vm = _world_vm(db, fake)
        db.commit()
        _on_every_write(fake, slow_write)
        result = asyncio.run(reconcile.process_queue(db, FakeAccess(fake), owner=OWNER, now=T0))
    assert result.applied == [service_id]
    assert len(refused) * 50 > reconcile.CLAIM_SECONDS + 50 and all(refused), refused
    row = _row(factory, service_id)
    assert row.applied_revision == row.revision and row.claimed_by is None
    assert fake_vm.options["enable"] == 1


@pytest.mark.parametrize("then", ["next_write", "write_fails"])
def test_an_apply_that_lost_its_claim_records_nothing(factory, then):
    """Another applier takes the VM during a write; the apply either stops before its next write or
    sees that write fail. Either way nothing of its outcome is recorded and no staged-rollout
    failure is counted."""
    from app.models.vm_firewall import FirewallLocationSettings, ServiceFirewall
    from app.services.vm_firewall import reconcile
    from app.services.vm_firewall.proxmox_fw import classify
    from tests.fakes.proxmox_firewall import FakeProxmoxFirewall
    from tests.services.test_vm_firewall_worker import OWNER, T0, FakeAccess

    fake = FakeProxmoxFirewall()

    def taken(_fake):
        with factory() as other:
            assert reconcile.claim(other, service_id, owner="deployment", now=T0 + timedelta(hours=1))
        if then == "write_fails":
            raise classify(500, "Proxmox write timed out", method="PUT", path="/firewall/options")

    with factory() as db:
        world, service_id, _fake_vm = _world_vm(db, fake)
        db.get(ServiceFirewall, service_id).rollout_released_at = T0
        db.commit()
        location_id = world.location.id
        before = _row(factory, service_id)
        fake.on_next_write(taken)
        result = asyncio.run(reconcile.process_queue(db, FakeAccess(fake), owner=OWNER, now=T0))
    assert result.attempted == [service_id] and result.applied == [] and result.failed == []
    assert len(fake.writes) == (1 if then == "next_write" else 0)
    row = _row(factory, service_id)
    assert row.applied_revision is None and row.applied_hash is None and row.applied_digest is None
    assert row.state == before.state and row.last_error is None and row.failing_since is None
    assert row.next_attempt_at == before.next_attempt_at and row.desired_hash == before.desired_hash
    assert row.rollout_released_at is not None and row.claimed_by == "deployment"
    with factory() as db:
        assert (db.get(FirewallLocationSettings, location_id).rollout_failures or 0) == 0
