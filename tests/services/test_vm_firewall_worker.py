"""VM firewall worker against the fake Proxmox: apply queue, claims, per-cluster concurrency,
coalescing, backoff, ``vm_missing``, eligibility, drift sweep, readiness, staged-rollout pause,
settings that queue applies, bridge moves, alert detectors and client notices."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.models.alerting import ClientNotificationPref
from app.models.commerce_audit import UserAuditEvent
from app.models.service import ServiceStatus
from app.models.vm_firewall import FirewallLocationSettings, FirewallState, ServiceFirewall
from app.services import settings_store
from app.services.alerting import detectors, lease
from app.services.alerting.catalogue import effective_settings
from app.services.vm_firewall import reconcile
from app.services.vm_firewall import service as fw
from app.services.vm_firewall.render import IPFILTER_SET, render
from tests.fakes.proxmox_firewall import FakeProxmoxFirewall
from tests.fakes.vm_firewall_world import make_world

OWNER = "worker-test"
T0 = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
RULES = [{"direction": "in", "action": "ACCEPT", "protocol": "tcp", "ports": "22"}]


class FakeAccess:
    def __init__(self, fake: FakeProxmoxFirewall):
        self.fake = fake

    async def vm_client(self, db, service):
        return self.fake.client(service.vm.proxmox_vmid)

    async def cluster_reader(self, db, cluster):
        return self.fake.reader()


@pytest.fixture
def world(db_session):
    return make_world(db_session)


@pytest.fixture
def fake():
    return FakeProxmoxFirewall()


@pytest.fixture
def access(fake):
    return FakeAccess(fake)


def _enabled_vm(world, fake, *, owner="alice", net0=None, rules=RULES):
    vm = world.vm(owner)
    kw = {"net0": net0} if net0 else {}
    fake_vm = fake.add_vm(vm.vm.proxmox_vmid, **kw)
    row = fw.get_or_create(world.db, vm.id)
    fw.set_firewall(world.db, vm.id, world.actor(owner), revision=row.revision, client_enabled=True)
    fw.replace_rules(world.db, vm.id, world.actor(owner), list(rules), revision=row.revision)
    world.db.commit()
    return vm, row, fake_vm


async def _queue(world, access, now=T0):
    return await reconcile.process_queue(world.db, access, owner=OWNER, now=now)


def _ipfilter(fake_vm):
    return sorted(entry["cidr"] for entry in fake_vm.ipsets.get(IPFILTER_SET, []))


# Apply queue


@pytest.mark.asyncio
async def test_queue_applies_pending_row_and_then_idles(world, fake, access):
    vm, row, fake_vm = _enabled_vm(world, fake)
    result = await _queue(world, access)
    assert result.applied == [vm.id]
    assert fake_vm.options["enable"] == 1
    assert row.applied_revision == row.revision and row.state.value == "applied"
    assert row.claimed_by is None and row.claimed_until is None
    writes = len(fake.writes)
    again = await _queue(world, access, now=T0 + timedelta(seconds=5))
    assert again.attempted == [] and len(fake.writes) == writes


@pytest.mark.asyncio
async def test_claimed_row_is_left_until_the_claim_expires(world, fake, access):
    vm, row, _fake_vm = _enabled_vm(world, fake)
    assert reconcile.claim(world.db, vm.id, owner="other", now=T0)
    assert (await _queue(world, access)).attempted == []
    later = T0 + timedelta(seconds=reconcile.CLAIM_SECONDS + 1)
    assert (await _queue(world, access, now=later)).applied == [vm.id]


LONG_WAIT = timedelta(seconds=reconcile.CLAIM_SECONDS + 180)


class _LateProbe(FakeAccess):
    """Asks, while the worker holds the claim, whether the deployment step (``apply_for_provision``)
    could claim the VM too, and whether another worker's queue would list it."""

    def __init__(self, fake, tick_now):
        super().__init__(fake)
        self.at = tick_now + LONG_WAIT
        self.seen = []

    async def vm_client(self, db, service):
        listed = service.id in [sid for sid, _ in reconcile.due_for_apply(db, now=self.at)]
        self.seen.append((reconcile.claim(db, service.id, owner="deployment", now=self.at), listed))
        return await super().vm_client(db, service)


def _long_wait(monkeypatch):
    """The wall clock: the tick starts, then the VM's turn comes ``LONG_WAIT`` later."""
    calls = []
    wall = datetime(2000, 1, 1, tzinfo=timezone.utc)

    def utcnow():
        calls.append(1)
        return wall if len(calls) == 1 else wall + LONG_WAIT

    monkeypatch.setattr(reconcile, "utcnow", utcnow)


@pytest.mark.asyncio
async def test_claim_taken_after_waiting_in_the_cluster_queue_runs_from_when_it_is_taken(world, fake, monkeypatch):
    """One apply at a time per cluster: the second VM waits while the first one's apply takes
    ``LONG_WAIT``, longer than ``CLAIM_SECONDS``; its claim must still hold when its turn comes."""
    settings_store.set_setting(world.db, "vm_firewall_cluster_concurrency", 1)
    vms = [_enabled_vm(world, fake)[0] for _ in range(2)]
    wall = {"now": datetime(2000, 1, 1, tzinfo=timezone.utc)}
    start = wall["now"]
    monkeypatch.setattr(reconcile, "utcnow", lambda: wall["now"])
    seen = []

    class SlowCluster(FakeAccess):
        async def vm_client(self, db, service):
            at = T0 + (wall["now"] - start)
            listed = service.id in [sid for sid, _ in reconcile.due_for_apply(db, now=at)]
            seen.append((service.id, reconcile.claim(db, service.id, owner="deployment", now=at), listed))
            wall["now"] += LONG_WAIT  # this apply is slow; the next VM of the cluster waits for it
            return await super().vm_client(db, service)

    result = await reconcile.process_queue(world.db, SlowCluster(fake), owner=OWNER, now=T0)
    assert sorted(result.applied) == sorted(vm.id for vm in vms)
    assert sorted(seen) == sorted((vm.id, False, False) for vm in vms)


@pytest.mark.asyncio
async def test_sweep_claim_after_a_long_queue_runs_from_when_it_is_taken(world, fake, access, monkeypatch):
    vm, row, _fake_vm = _enabled_vm(world, fake)
    assert (await _queue(world, access)).applied == [vm.id]
    later = T0 + timedelta(seconds=reconcile.reconcile_interval(world.db) + 1)
    probe = _LateProbe(fake, later)
    _long_wait(monkeypatch)
    result = await reconcile.tick(world.db, probe, owner=OWNER, now=later)
    assert result.sweep.checked == [vm.id]
    assert probe.seen and all(claimed is False for claimed, _listed in probe.seen)


def _other_session(world):
    """A session of another process (deployment step, second worker, client request)."""
    from sqlalchemy.orm import Session

    return Session(bind=world.db.get_bind(), autoflush=False)


def _on_every_write(fake, callback):
    def hook(f):
        callback(f)
        f.on_next_write(hook)

    fake.on_next_write(hook)


@pytest.mark.asyncio
async def test_claim_is_kept_while_a_long_apply_writes(world, fake, access, monkeypatch):
    """Every Proxmox write takes 50 s of the controlled clock, so the apply runs well past
    ``CLAIM_SECONDS``; the deployment step must not be able to claim the VM at any write."""
    vm, row, _fake_vm = _enabled_vm(world, fake)
    wall = {"now": datetime(2000, 1, 1, tzinfo=timezone.utc)}
    start = wall["now"]
    monkeypatch.setattr(reconcile, "utcnow", lambda: wall["now"])
    refused = []

    def slow_write(_fake):
        wall["now"] += timedelta(seconds=50)
        with _other_session(world) as other:
            at = T0 + (wall["now"] - start)
            refused.append(not reconcile.claim(other, vm.id, owner="deployment", now=at))

    _on_every_write(fake, slow_write)
    result = await _queue(world, access)
    assert result.applied == [vm.id]
    assert len(refused) * 50 > reconcile.CLAIM_SECONDS + 50
    assert all(refused), refused
    assert row.applied_revision == row.revision


@pytest.mark.asyncio
async def test_apply_whose_claim_is_taken_writes_nothing_more_and_records_nothing(world, fake, access):
    vm, row, _fake_vm = _enabled_vm(world, fake)
    state = row.state.value

    def taken(_fake):
        # The worker's claim has expired and the deployment step takes the VM.
        with _other_session(world) as other:
            assert reconcile.claim(other, vm.id, owner="deployment", now=T0 + timedelta(hours=1))

    fake.on_next_write(taken)
    result = await _queue(world, access)
    assert len(fake.writes) == 1
    world.db.expire_all()
    row = world.db.get(ServiceFirewall, vm.id)
    assert row.applied_revision is None and row.applied_hash is None and row.applied_digest is None
    assert row.last_applied_at is None
    assert row.state.value == state and row.last_error is None and row.failing_since is None
    assert row.claimed_by == "deployment"
    assert result.attempted == [vm.id] and result.applied == [] and result.failed == []


@pytest.mark.asyncio
async def test_apply_failing_after_its_claim_is_taken_records_nothing(world, fake, access):
    """A Proxmox write that fails after the claim expired and another applier took the VM and
    recorded its outcome: the failure must not be written over that outcome, nor counted towards
    a staged-rollout pause."""
    from app.services.vm_firewall.proxmox_fw import classify

    vm, row, _fake_vm = _enabled_vm(world, fake)
    row.rollout_released_at = T0
    world.db.commit()
    revision = row.revision

    def taken_then_failed(_fake):
        with _other_session(world) as other:
            assert reconcile.claim(other, vm.id, owner="deployment", now=T0 + timedelta(hours=1))
            theirs = other.get(ServiceFirewall, vm.id)
            theirs.state = FirewallState.APPLIED
            theirs.applied_revision = revision
            theirs.applied_hash = "theirs"
            theirs.last_applied_at = T0 + timedelta(hours=1)
            other.commit()
        raise classify(500, "Proxmox write timed out", method="PUT", path="/firewall/options")

    fake.on_next_write(taken_then_failed)
    result = await _queue(world, access)
    world.db.expire_all()
    row = world.db.get(ServiceFirewall, vm.id)
    assert row.state.value == "applied" and row.applied_hash == "theirs" and row.applied_revision == revision
    assert row.last_error is None and row.failing_since is None and row.next_attempt_at is None
    assert row.rollout_released_at is not None
    location = world.db.get(FirewallLocationSettings, world.location.id)
    assert (location.rollout_failures or 0) == 0
    assert row.claimed_by == "deployment"
    assert result.attempted == [vm.id] and result.applied == [] and result.failed == []


@pytest.mark.asyncio
async def test_drift_sweep_counts_a_repair_that_lost_its_claim(world, fake, access):
    vm, row, fake_vm = _enabled_vm(world, fake)
    await _queue(world, access)
    fake_vm.add_foreign_rule({"type": "in", "action": "ACCEPT", "proto": "tcp", "dport": "3389", "enable": 1})
    later = T0 + timedelta(seconds=reconcile.reconcile_interval(world.db) + 1)

    def taken(_fake):
        with _other_session(world) as other:
            assert reconcile.claim(other, vm.id, owner="deployment", now=later + timedelta(hours=1))

    fake.on_next_write(taken)
    result = await reconcile.drift_sweep(world.db, access, owner=OWNER, now=later)
    assert result.checked == [vm.id]
    assert result.claim_lost == [vm.id]
    assert result.repaired == [] and result.in_sync == [] and result.reported == [] and result.queued == []
    assert world.db.query(UserAuditEvent).filter_by(action="firewall.drift_repaired").count() == 0


class _SlowBetweenOps(FakeProxmoxFirewall):
    """Runs ``pause`` once, at the first read after a write made since it was set: the applier is
    slow between two operations."""

    def __init__(self):
        super().__init__()
        self.pause = None
        self.after = 0

    def pause_after_next_write(self, pause):
        self.pause, self.after = pause, len(self.writes)

    async def request(self, method, path, params=None):
        if method == "GET" and self.pause is not None and len(self.writes) > self.after:
            pause, self.pause = self.pause, None
            await pause()
        return await super().request(method, path, params)


@pytest.mark.asyncio
async def test_stale_apply_never_overwrites_a_newer_revision(world, monkeypatch):
    """Applier A renders r1; the client saves r2; A, slow, loses its claim; applier B (another
    worker replica) applies r2. A must not write r1 back, and the row must say r2 is applied."""
    fake = _SlowBetweenOps()
    access = FakeAccess(fake)
    vm, row, fake_vm = _enabled_vm(world, fake)
    assert (await _queue(world, access)).applied == [vm.id]
    fw.add_rule(world.db, vm.id, world.actor("alice"),
                {"direction": "in", "action": "ACCEPT", "protocol": "tcp", "ports": "80"}, revision=row.revision)
    world.db.commit()
    r1 = row.revision
    wall = {"now": datetime(2000, 1, 1, tzinfo=timezone.utc)}
    start = wall["now"]
    monkeypatch.setattr(reconcile, "utcnow", lambda: wall["now"])
    tick = T0 + timedelta(seconds=5)
    b_results = []

    async def meanwhile():
        with _other_session(world) as client_db:
            current = client_db.get(ServiceFirewall, vm.id).revision
            fw.replace_rules(client_db, vm.id, world.actor("alice"),
                             [{"direction": "in", "action": "ACCEPT", "protocol": "tcp", "ports": "8443"}],
                             revision=current)
            client_db.commit()
        wall["now"] += timedelta(seconds=reconcile.CLAIM_SECONDS + 60)
        with _other_session(world) as b_db:
            b_results.append(await reconcile.process_queue(b_db, access, owner="worker-b",
                                                           now=tick + (wall["now"] - start)))

    fake.pause_after_next_write(meanwhile)
    a = await _queue(world, access, now=tick)
    assert b_results and b_results[0].applied == [vm.id]
    ports = {rule.get("dport") for rule in fake_vm.rules}
    assert "8443" in ports and "80" not in ports and "22" not in ports
    world.db.expire_all()
    row = world.db.get(ServiceFirewall, vm.id)
    assert row.revision > r1
    assert row.applied_revision == row.revision
    assert row.applied_hash == render(world.db, vm.id).desired_hash
    assert row.claimed_by is None
    assert a.attempted == [vm.id] and a.applied == []


class _PauseOnRead(FakeProxmoxFirewall):
    """Runs ``pause`` once, at the next read of a VM config: the applier is slow reading Proxmox."""

    def __init__(self):
        super().__init__()
        self.pause = None

    async def request(self, method, path, params=None):
        if method == "GET" and path.endswith("/config") and self.pause is not None:
            pause, self.pause = self.pause, None
            await pause()
        return await super().request(method, path, params)


def _client_saves(world, vm_id, port):
    """A client save committed from another session (saves take the row lock, never the claim)."""
    with _other_session(world) as other:
        current = other.get(ServiceFirewall, vm_id).revision
        fw.add_rule(other, vm_id, world.actor("alice"),
                    {"direction": "in", "action": "ACCEPT", "protocol": "tcp", "ports": port}, revision=current)
        other.commit()
        return current + 1


MOVED_NET0 = "virtio=BC:24:11:00:00:01,bridge=vmbr7"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["report", "repair"])
async def test_bridge_bump_during_an_apply_does_not_swallow_a_concurrent_save(world, fake, access, mode):
    """The apply observes a moved bridge and bumps the revision it loaded; meanwhile the client
    saves, raising the same revision in the database. The apply must neither reuse the save's
    number nor record it as applied, and the queue alone (no drift sweep) must apply the save."""
    settings_store.set_setting(world.db, "vm_firewall_drift_mode", mode)
    vm, row, fake_vm = _enabled_vm(world, fake)
    assert (await _queue(world, access)).applied == [vm.id]
    fw.add_rule(world.db, vm.id, world.actor("alice"),
                {"direction": "in", "action": "ACCEPT", "protocol": "tcp", "ports": "80"}, revision=row.revision)
    world.db.commit()
    rendered = row.revision
    fake_vm.config["net0"] = MOVED_NET0
    saved = []
    fake.on_next_write(lambda _fake: saved.append(_client_saves(world, vm.id, "8443")))

    assert (await _queue(world, access)).applied == [vm.id]
    world.db.expire_all()
    row = world.db.get(ServiceFirewall, vm.id)
    ports = {rule.get("dport") for rule in fake_vm.rules}
    assert "80" in ports and "8443" not in ports
    assert row.revision == saved[0] + 1 and row.net0_bridge == "vmbr7"
    assert row.applied_revision == rendered and row.state.value == "pending"

    again = await _queue(world, access, now=T0 + timedelta(seconds=5))
    assert again.applied == [vm.id]
    world.db.expire_all()
    row = world.db.get(ServiceFirewall, vm.id)
    assert "8443" in {rule.get("dport") for rule in fake_vm.rules}
    assert row.applied_revision == row.revision and row.applied_hash == render(world.db, vm.id).desired_hash
    assert row.state.value == "applied"
    later = T0 + timedelta(seconds=reconcile.reconcile_interval(world.db) + 10)
    sweep = await reconcile.drift_sweep(world.db, access, owner=OWNER, now=later)
    assert sweep.in_sync == [vm.id] and sweep.reported == [] and sweep.repaired == []


@pytest.mark.asyncio
async def test_sweep_that_lost_its_claim_never_moves_the_revision_backwards(world):
    """Sweep S loads revision r and reads a moved bridge; while it reads, the client saves (r+1)
    and another worker, S's claim having expired, applies with the bridge (r+2). S must not then
    write its bridge bump (r+1) over it."""
    fake = _PauseOnRead()
    access = FakeAccess(fake)
    vm, row, fake_vm = _enabled_vm(world, fake)
    assert (await _queue(world, access)).applied == [vm.id]
    fake_vm.config["net0"] = MOVED_NET0
    later = T0 + timedelta(seconds=reconcile.reconcile_interval(world.db) + 1)
    other_worker = []

    async def meanwhile():
        _client_saves(world, vm.id, "8443")
        with _other_session(world) as b_db:
            other_worker.append(await reconcile.process_queue(b_db, access, owner="worker-b",
                                                              now=later + timedelta(hours=1)))

    fake.pause = meanwhile
    sweep = await reconcile.drift_sweep(world.db, access, owner=OWNER, now=later)
    assert other_worker[0].applied == [vm.id]
    world.db.expire_all()
    row = world.db.get(ServiceFirewall, vm.id)
    assert row.revision >= row.applied_revision
    assert row.revision == row.applied_revision and row.net0_bridge == "vmbr7"
    assert "8443" in {rule.get("dport") for rule in fake_vm.rules}
    assert sweep.claim_lost == [vm.id] and sweep.queued == []


@pytest.mark.asyncio
async def test_sweep_bridge_bump_is_settled_on_a_save_made_during_its_read(world):
    fake = _PauseOnRead()
    access = FakeAccess(fake)
    vm, row, fake_vm = _enabled_vm(world, fake)
    assert (await _queue(world, access)).applied == [vm.id]
    fake_vm.config["net0"] = MOVED_NET0
    later = T0 + timedelta(seconds=reconcile.reconcile_interval(world.db) + 1)
    saved = []
    fake.pause = lambda: asyncio.sleep(0, saved.append(_client_saves(world, vm.id, "8443")))
    sweep = await reconcile.drift_sweep(world.db, access, owner=OWNER, now=later)
    assert sweep.queued == [vm.id]
    world.db.expire_all()
    row = world.db.get(ServiceFirewall, vm.id)
    assert row.revision == saved[0] + 1 and row.net0_bridge == "vmbr7"
    assert (await _queue(world, access, now=later + timedelta(seconds=5))).applied == [vm.id]
    world.db.expire_all()
    assert world.db.get(ServiceFirewall, vm.id).applied_revision == saved[0] + 1
    assert "8443" in {rule.get("dport") for rule in fake_vm.rules}


def _taken_while_reading(world, vm_id, owner="deployment"):
    async def take():
        with _other_session(world) as other:
            assert reconcile.claim(other, vm_id, owner=owner, now=T0 + timedelta(hours=1))

    return take


@pytest.mark.asyncio
async def test_queue_holds_an_ineligible_vm_only_while_it_has_the_claim(world):
    fake = _PauseOnRead()
    access = FakeAccess(fake)
    vm, row, fake_vm = _enabled_vm(world, fake)
    assert (await _queue(world, access)).applied == [vm.id]
    fw.add_rule(world.db, vm.id, world.actor("alice"),
                {"direction": "in", "action": "ACCEPT", "protocol": "tcp", "ports": "80"}, revision=row.revision)
    row.rollout_released_at = T0
    world.db.commit()
    before = (row.revision, row.state.value, row.net0_bridge)
    settings_store.set_setting(world.db, "vm_firewall_infrastructure_bridges", "vmbr230")
    fake_vm.config["net0"] = "virtio=BC:24:11:00:00:01,bridge=vmbr230"
    fake.pause = _taken_while_reading(world, vm.id)
    result = await _queue(world, access, now=T0 + timedelta(seconds=5))
    assert result.attempted == [vm.id] and result.applied == [] and result.failed == []
    world.db.expire_all()
    row = world.db.get(ServiceFirewall, vm.id)
    assert (row.revision, row.state.value, row.net0_bridge) == before
    assert row.last_error is None and row.rollout_released_at is not None
    assert row.claimed_by == "deployment"


def _new_vm_for_provision(world, fake, net0="virtio=BC:24:11:00:00:01,bridge=vmbr0"):
    world.enable_location(nic_flag_on_provision=True)
    vm = world.vm()
    fake.add_vm(vm.vm.proxmox_vmid, net0=net0)
    fw.init_for_new_vm(world.db, vm.id, fw.Actor.system())
    world.db.commit()
    row = world.db.get(ServiceFirewall, vm.id)
    return vm, (row.revision, row.state.value, row.net0_bridge)


@pytest.mark.asyncio
@pytest.mark.parametrize("ineligible", [True, False], ids=["ineligible_hold", "nic_flag_renewal"])
async def test_deployment_step_records_nothing_after_its_claim_is_taken(world, ineligible):
    """While the step reads the VM config another applier takes its claim; the step must then
    neither hold the VM as ineligible nor commit the bridge it observed, and is retried."""
    fake = _PauseOnRead()
    if ineligible:
        settings_store.set_setting(world.db, "vm_firewall_infrastructure_bridges", "vmbr230")
    vm, before = _new_vm_for_provision(
        world, fake, net0="virtio=BC:24:11:00:00:01,bridge=" + ("vmbr230" if ineligible else "vmbr0"))
    fake.pause = _taken_while_reading(world, vm.id, owner="worker-b")
    with pytest.raises(reconcile.ProvisionFirewallError, match="being applied"):
        await reconcile.apply_for_provision(world.db, vm.id, fake.client(vm.vm.proxmox_vmid),
                                            owner="deployment-job-1-a", now=T0)
    world.db.expire_all()
    row = world.db.get(ServiceFirewall, vm.id)
    assert (row.revision, row.state.value, row.net0_bridge) == before
    assert row.last_error is None and row.claimed_by == "worker-b"
    assert fake.writes == []


@pytest.mark.asyncio
async def test_cluster_concurrency_limits_parallel_applies(world, fake, access, monkeypatch):
    settings_store.set_setting(world.db, "vm_firewall_cluster_concurrency", 2)
    vms = [_enabled_vm(world, fake)[0] for _ in range(5)]
    active = {"now": 0, "max": 0}

    async def slow_apply(db, service_id, access, *, now=None, held=None):
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        await asyncio.sleep(0.01)
        active["now"] -= 1
        return None

    monkeypatch.setattr(reconcile, "apply_one", slow_apply)
    result = await _queue(world, access)
    assert sorted(result.attempted) == sorted(vm.id for vm in vms)
    assert active["max"] == 2


@pytest.mark.asyncio
async def test_changes_before_a_tick_are_coalesced_into_one_apply(world, fake, access):
    vm, row, fake_vm = _enabled_vm(world, fake)
    first = row.revision
    fw.add_rule(world.db, vm.id, world.actor("alice"),
                {"direction": "in", "action": "ACCEPT", "protocol": "tcp", "ports": "80"}, revision=row.revision)
    fw.add_rule(world.db, vm.id, world.actor("alice"),
                {"direction": "in", "action": "ACCEPT", "protocol": "tcp", "ports": "443"}, revision=row.revision)
    world.db.commit()
    assert row.revision >= first + 2
    result = await _queue(world, access)
    assert result.attempted == [vm.id]
    assert row.applied_revision == row.revision
    assert {r.get("dport") for r in fake_vm.rules} >= {"22", "80", "443"}


@pytest.mark.asyncio
async def test_failed_apply_backs_off_then_recovers(world, fake, access):
    vm, row, fake_vm = _enabled_vm(world, fake)
    fake.fail_next(500, "Internal Server Error", count=1)
    result = await _queue(world, access)
    assert result.failed == [vm.id]
    assert row.state.value == "error" and "500" in row.last_error
    assert reconcile.as_utc(row.next_attempt_at) == T0 + timedelta(seconds=30)
    assert (await _queue(world, access, now=T0 + timedelta(seconds=10))).attempted == []
    assert (await _queue(world, access, now=T0 + timedelta(seconds=31))).applied == [vm.id]
    assert row.state.value == "applied" and row.last_error is None and row.failing_since is None
    assert fake_vm.options["enable"] == 1


@pytest.mark.asyncio
async def test_digest_conflict_is_replanned_inside_one_attempt(world, fake, access):
    vm, row, fake_vm = _enabled_vm(world, fake)
    fake.on_next_write(lambda f: fake_vm.set_option("policy_in", "ACCEPT"))
    result = await _queue(world, access)
    assert result.applied == [vm.id]
    assert fake_vm.options["policy_in"] == "DROP"


@pytest.mark.asyncio
async def test_missing_vm_is_vm_missing_without_an_alert(world, fake, access):
    vm, row, _fake_vm = _enabled_vm(world, fake)
    del fake.vms[vm.vm.proxmox_vmid]
    vm.status = ServiceStatus.TERMINATED
    world.db.commit()
    await _queue(world, access)
    assert row.state.value == "vm_missing"
    assert row.next_attempt_at is not None
    assert (await _queue(world, access, now=T0 + timedelta(seconds=5))).attempted == []
    params = effective_settings(world.db)["vm_firewall_apply_failed"].params
    assert detectors.vm_firewall_apply_failed(world.db, params, (T0 + timedelta(hours=2)).replace(tzinfo=None)) == []


@pytest.mark.asyncio
async def test_infrastructure_vm_is_never_written(world, fake, access):
    settings_store.set_setting(world.db, "vm_firewall_infrastructure_bridges", "vmbr230")
    vm, row, fake_vm = _enabled_vm(world, fake, net0="virtio=BC:24:11:00:00:09,bridge=vmbr230")
    await _queue(world, access)
    assert [w for w in fake.writes if f"/qemu/{fake_vm.vmid}/" in w[1]] == []
    assert row.state.value == "unmanaged" and "infrastructure_bridge" in row.last_error


@pytest.mark.asyncio
async def test_disabled_location_writes_enable_0_on_written_vms(world, fake, access):
    vm, row, fake_vm = _enabled_vm(world, fake)
    await _queue(world, access)
    fw.update_location_settings(world.db, world.staff_actor, world.location.id, enabled=False)
    world.db.commit()
    await _queue(world, access, now=T0 + timedelta(seconds=5))
    assert fake_vm.options["enable"] == 0 and row.state.value == "off"


@pytest.mark.asyncio
async def test_never_written_vm_in_disabled_location_is_left_alone(world, fake, access):
    world.db.get(FirewallLocationSettings, world.location.id).enabled = False
    vm = world.vm()
    fake.add_vm(vm.vm.proxmox_vmid)
    fw.get_or_create(world.db, vm.id)
    world.db.commit()
    assert (await _queue(world, access)).attempted == []
    assert fake.writes == []


# Drift sweep


@pytest.mark.asyncio
async def test_drift_sweep_repairs_and_audits(world, fake, access):
    vm, row, fake_vm = _enabled_vm(world, fake)
    await _queue(world, access)
    fake_vm.add_foreign_rule({"type": "in", "action": "ACCEPT", "proto": "tcp", "dport": "3389", "enable": 1})
    later = T0 + timedelta(seconds=reconcile.reconcile_interval(world.db) + 1)
    result = await reconcile.drift_sweep(world.db, access, owner=OWNER, now=later)
    assert result.repaired == [vm.id] and result.completed
    assert all(r.get("dport") != "3389" for r in fake_vm.rules)
    audit = world.db.query(UserAuditEvent).filter_by(action="firewall.drift_repaired").one()
    assert audit.resource_id == str(vm.id) and audit.actor_user_id is None
    assert audit.event_metadata["actor"] == {"kind": "system"} and audit.event_metadata["differences"]
    assert row.drift_repairs_24h == 1 and row.state.value == "applied"
    # Checked again only after another interval.
    assert (await reconcile.drift_sweep(world.db, access, owner=OWNER, now=later + timedelta(seconds=5))).checked == []


@pytest.mark.asyncio
async def test_drift_sweep_in_report_mode_only_records(world, fake, access):
    vm, row, fake_vm = _enabled_vm(world, fake)
    await _queue(world, access)
    settings_store.set_setting(world.db, "vm_firewall_drift_mode", "report")
    fake_vm.set_option("enable", 0)
    later = T0 + timedelta(hours=1)
    result = await reconcile.drift_sweep(world.db, access, owner=OWNER, now=later)
    assert result.reported == [vm.id]
    assert row.state.value == "drift" and "drift" in row.last_error
    assert fake_vm.options["enable"] == 0


@pytest.mark.asyncio
async def test_drift_sweep_in_sync_vm_stays_applied(world, fake, access):
    vm, row, fake_vm = _enabled_vm(world, fake)
    await _queue(world, access)
    writes = len(fake.writes)
    result = await reconcile.drift_sweep(world.db, access, owner=OWNER, now=T0 + timedelta(hours=1))
    assert result.in_sync == [vm.id] and len(fake.writes) == writes


@pytest.mark.asyncio
async def test_drift_sweep_spreads_checks_over_the_interval(world, fake, access):
    for _ in range(4):
        _enabled_vm(world, fake)
    await _queue(world, access)
    interval = reconcile.reconcile_interval(world.db)
    later = T0 + timedelta(seconds=interval + 1)
    result = await reconcile.drift_sweep(world.db, access, owner=OWNER, now=later, tick_seconds=interval // 2)
    assert len(result.checked) == 2 and not result.completed


# Readiness


@pytest.mark.asyncio
async def test_readiness_flips_vms_to_not_enforced_and_back(world, fake, access):
    vm, row, _fake_vm = _enabled_vm(world, fake)
    await _queue(world, access)
    fake.cluster_options["enable"] = 0
    assert await reconcile.check_readiness(world.db, access, now=T0) == {world.cluster.id: False}
    assert row.state.value == "not_enforced"
    location = world.db.get(FirewallLocationSettings, world.location.id)
    assert location.readiness["clusters"][str(world.cluster.id)]["ready"] is False
    params = effective_settings(world.db)["vm_firewall_cluster_not_ready"].params
    findings = detectors.vm_firewall_cluster_not_ready(world.db, params, T0.replace(tzinfo=None))
    assert [f.resource_id for f in findings] == [str(world.cluster.id)]
    assert "datacenter firewall disabled" in findings[0].message
    # A later apply on a not-ready cluster also lands in not_enforced.
    fw.add_rule(world.db, vm.id, world.actor("alice"),
                {"direction": "in", "action": "ACCEPT", "protocol": "tcp", "ports": "80"}, revision=row.revision)
    world.db.commit()
    await _queue(world, access, now=T0 + timedelta(seconds=5))
    assert row.state.value == "not_enforced"
    fake.cluster_options["enable"] = 1
    await reconcile.check_readiness(world.db, access, now=T0 + timedelta(minutes=10))
    assert row.state.value == "applied"
    assert detectors.vm_firewall_cluster_not_ready(world.db, params, T0.replace(tzinfo=None)) == []


# Settings that queue applies without the drift sweep


@pytest.mark.asyncio
async def test_location_prefix_change_reaches_proxmox_through_the_queue_only(world, fake, access, monkeypatch):
    settings_store.set_setting(world.db, "vm_firewall_ipv6_ra_bridges", "vmbr750")
    vm, row, fake_vm = _enabled_vm(world, fake, net0="virtio=BC:24:11:00:00:02,bridge=vmbr750")
    monkeypatch.setattr(reconcile, "drift_sweep", None)
    await _queue(world, access)
    assert fake_vm.options["ipfilter"] == 0
    fw.update_location_settings(world.db, world.staff_actor, world.location.id,
                                ipv6_allowed_prefixes=["2001:db8:750::/48"])
    world.db.commit()
    await _queue(world, access, now=T0 + timedelta(seconds=5))
    assert "2001:db8:750::/48" in _ipfilter(fake_vm) and fake_vm.options["ipfilter"] == 1


@pytest.mark.asyncio
async def test_ra_bridge_setting_change_reaches_proxmox_through_the_queue_only(world, fake, access, monkeypatch):
    world.enable_location(ipv6_allowed_prefixes=["2001:db8:750::/48"])
    vm, row, fake_vm = _enabled_vm(world, fake, net0="virtio=BC:24:11:00:00:03,bridge=vmbr750")
    monkeypatch.setattr(reconcile, "drift_sweep", None)
    await _queue(world, access)
    assert "2001:db8:750::/48" not in _ipfilter(fake_vm)
    before = row.revision
    settings_store.set_setting(world.db, "vm_firewall_ipv6_ra_bridges", "vmbr750", actor_user_id=world.staff.id)
    world.db.commit()
    assert row.revision > before
    await _queue(world, access, now=T0 + timedelta(seconds=5))
    assert "2001:db8:750::/48" in _ipfilter(fake_vm)
    settings_store.reset_setting(world.db, "vm_firewall_ipv6_ra_bridges")
    world.db.commit()
    await _queue(world, access, now=T0 + timedelta(seconds=10))
    assert "2001:db8:750::/48" not in _ipfilter(fake_vm)


def test_bridge_setting_hook_picks_affected_rows(world, fake):
    from app.services.vm_firewall.settings_hook import affected_by_bridge_change

    rows = {}
    for bridge in ("vmbr0", "vmbr750", None):
        vm = world.vm()
        rows[bridge] = fw.get_or_create(world.db, vm.id)
        rows[bridge].net0_bridge = bridge
    world.db.flush()
    pick = lambda old, new: affected_by_bridge_change(world.db, "vm_firewall_ipv6_ra_bridges", old, new)  # noqa: E731
    assert pick("", "vmbr750") == sorted([rows["vmbr750"].service_id, rows[None].service_id])
    assert pick("vmbr750", "vmbr750,vmbr751") == []
    assert pick("vmbr750", "vmbr0") == sorted([rows["vmbr0"].service_id, rows["vmbr750"].service_id])
    assert affected_by_bridge_change(world.db, "vm_firewall_infrastructure_bridges", "", "vmbr0") == sorted(
        [rows["vmbr0"].service_id, rows[None].service_id]
    )
    assert affected_by_bridge_change(world.db, "vm_firewall_infrastructure_bridges", "vmbr0", "vmbr0,vmbr9") == []


# Bridge moves


@pytest.mark.asyncio
async def test_bridge_move_found_by_sweep_and_rendered_by_queue(world, fake, access):
    settings_store.set_setting(world.db, "vm_firewall_ipv6_ra_bridges", "vmbr750")
    world.enable_location(ipv6_allowed_prefixes=["2001:db8:750::/48"])
    vm, row, fake_vm = _enabled_vm(world, fake)
    await _queue(world, access)
    assert row.net0_bridge == "vmbr0" and "2001:db8:750::/48" not in _ipfilter(fake_vm)

    fake_vm.config["net0"] = fake_vm.net0.replace("bridge=vmbr0", "bridge=vmbr750")
    later = T0 + timedelta(hours=1)
    sweep = await reconcile.drift_sweep(world.db, access, owner=OWNER, now=later)
    assert sweep.queued == [vm.id] and row.revision != row.applied_revision
    await _queue(world, access, now=later)
    assert row.net0_bridge == "vmbr750" and "2001:db8:750::/48" in _ipfilter(fake_vm)
    assert fake_vm.options["ipfilter"] == 1

    # Moved back: the queue path alone (any apply) re-reads net0 before rendering.
    fake_vm.config["net0"] = fake_vm.net0.replace("bridge=vmbr750", "bridge=vmbr0")
    fw.bump_revisions(world.db, [vm.id])
    world.db.commit()
    await _queue(world, access, now=later + timedelta(seconds=5))
    assert row.net0_bridge == "vmbr0" and "2001:db8:750::/48" not in _ipfilter(fake_vm)


@pytest.mark.asyncio
async def test_bridge_move_to_ra_bridge_without_prefix_turns_ipfilter_off_with_reason(world, fake, access):
    settings_store.set_setting(world.db, "vm_firewall_ipv6_ra_bridges", "vmbr750")
    vm, row, fake_vm = _enabled_vm(world, fake)
    await _queue(world, access)
    assert fake_vm.options["ipfilter"] == 1
    fake_vm.config["net0"] = fake_vm.net0.replace("bridge=vmbr0", "bridge=vmbr750")
    fw.bump_revisions(world.db, [vm.id])
    world.db.commit()
    result = await reconcile.apply_one(world.db, vm.id, access, now=T0 + timedelta(seconds=5))
    assert result.ok and "ipv6_ra_bridge_needs_prefix" in result.reasons
    assert fake_vm.options["ipfilter"] == 0 and fake_vm.options["enable"] == 1


# Staged rollout pause


def _rollout(world, fake, count):
    vms = []
    for _ in range(count):
        vm = world.vm()
        fake.add_vm(vm.vm.proxmox_vmid)
        fw.get_or_create(world.db, vm.id)
        vms.append(vm)
    fw.create_baseline(world.db, world.staff_actor, name="g", scope_type="global", settings={"smtp_out": "block"},
                       rules=[{"direction": "in", "action": "DROP", "protocol": "tcp", "ports": "3306"}])
    world.db.commit()
    preview = fw.preview_enforce_baseline_on_all(world.db, world.location.id)
    fw.update_location_settings(world.db, world.staff_actor, world.location.id, enforce_baseline_on_all=True,
                                confirm_existing_vms=preview["changed"], now=T0)
    world.db.commit()
    return vms


@pytest.mark.asyncio
async def test_rollout_pauses_after_three_failures_and_resumes(world, fake, access):
    settings_store.set_setting(world.db, "vm_firewall_rollout_per_node_per_minute", 3)
    vms = _rollout(world, fake, 6)
    fake.fail_next(500, "boom", count=100, method="PUT")
    await _queue(world, access, now=T0)
    location = world.db.get(FirewallLocationSettings, world.location.id)
    assert location.rollout_paused_at is not None and location.rollout_failures == 3
    assert "boom" in location.rollout_pause_reason
    assert world.db.query(UserAuditEvent).filter_by(action="firewall.rollout_paused").count() == 1
    # The second batch is due but stays held while paused.
    assert fw.release_due_rollouts(world.db, now=T0 + timedelta(minutes=5)) == []
    fake._failures.clear()
    reconcile.resume_rollout(world.db, world.staff_actor, world.location.id, now=T0 + timedelta(minutes=5))
    world.db.commit()
    assert location.rollout_paused_at is None and location.rollout_failures == 0
    result = await _queue(world, access, now=T0 + timedelta(minutes=5))
    assert len(result.released) == 3
    assert all(fake.vms[vm.vm.proxmox_vmid].options.get("enable") == 1 for vm in vms if vm.id in result.released)


@pytest.mark.asyncio
async def test_rollout_success_resets_failure_count(world, fake, access):
    _rollout(world, fake, 2)
    location = world.db.get(FirewallLocationSettings, world.location.id)
    location.rollout_failures = 2
    world.db.commit()
    await _queue(world, access, now=T0)
    assert location.rollout_failures == 0 and location.rollout_paused_at is None


def test_rollout_failures_in_concurrent_sessions_all_count(world, fake):
    """The queue applies VMs concurrently, each in its own session that loaded the location before
    its Proxmox calls; every failure must count, so the pause comes after 3 as designed."""
    from sqlalchemy.orm import sessionmaker

    from app.services.vm_firewall.apply import ApplyResult

    vms = _rollout(world, fake, 3)
    for vm in vms:
        fw.get_or_create(world.db, vm.id).rollout_released_at = T0
    world.db.commit()
    make = sessionmaker(bind=world.db.get_bind(), autoflush=False)
    sessions = [make() for _ in vms]
    try:
        # Every session holds the location as read before any failure lands (on MariaDB, the
        # transaction's snapshot), as the worker's sessions do across their Proxmox calls.
        loaded = [session.get(FirewallLocationSettings, world.location.id) for session in sessions]
        assert [location.rollout_failures for location in loaded] == [0, 0, 0]
        for session, vm in zip(sessions, vms):
            row = session.get(fw.ServiceFirewall, vm.id)
            reconcile._count_rollout_outcome(session, row, ApplyResult(vm.id, False, "error", error="locked"), T0)
            session.commit()
    finally:
        for session in sessions:
            session.close()
    world.db.expire_all()
    location = world.db.get(FirewallLocationSettings, world.location.id)
    assert location.rollout_failures == 3 and location.rollout_paused_at is not None
    assert world.db.query(UserAuditEvent).filter_by(action="firewall.rollout_paused").count() == 1


# Alerts, notices, status, lease


@pytest.mark.asyncio
async def test_apply_failed_alert_and_client_notice_after_threshold(world, fake, access):
    vm, row, _fake_vm = _enabled_vm(world, fake)
    world.db.add(ClientNotificationPref(user_id=vm.owner_user_id, email_enabled=True, categories={"firewall": True}))
    world.db.commit()
    fake.fail_next(500, "Proxmox down", count=1000)
    await _queue(world, access)
    params = effective_settings(world.db)["vm_firewall_apply_failed"].params
    assert detectors.vm_firewall_apply_failed(world.db, params, (T0 + timedelta(minutes=5)).replace(tzinfo=None)) == []
    late = T0 + timedelta(minutes=16)
    findings = detectors.vm_firewall_apply_failed(world.db, params, late.replace(tzinfo=None))
    assert [f.resource_id for f in findings] == [str(vm.id)] and "Proxmox down" in findings[0].message
    assert reconcile.notify_apply_failures(world.db, now=T0 + timedelta(minutes=5)) == 0
    assert reconcile.notify_apply_failures(world.db, now=late) == 1
    assert reconcile.notify_apply_failures(world.db, now=late + timedelta(minutes=5)) == 0


def test_drift_repeated_detector(world):
    vm = world.vm()
    row = fw.get_or_create(world.db, vm.id)
    row.drift_repairs_24h = 4
    world.db.flush()
    params = effective_settings(world.db)["vm_firewall_drift_repeated"].params
    assert [f.resource_id for f in detectors.vm_firewall_drift_repeated(world.db, params, T0)] == [str(vm.id)]
    row.drift_repairs_24h = 3
    world.db.flush()
    assert detectors.vm_firewall_drift_repeated(world.db, params, T0) == []


@pytest.mark.asyncio
async def test_worker_stale_detector_follows_completed_sweeps(world, fake, access):
    params = effective_settings(world.db)["vm_firewall_worker_stale"].params
    naive_t0 = T0.replace(tzinfo=None)
    assert len(detectors.vm_firewall_worker_stale(world.db, params, naive_t0)) == 1
    _enabled_vm(world, fake)
    result = await reconcile.tick(world.db, access, owner=OWNER, now=T0)
    assert result.sweep.completed and result.readiness == {world.cluster.id: True}
    assert detectors.vm_firewall_worker_stale(world.db, params, naive_t0) == []
    interval = reconcile.reconcile_interval(world.db)
    assert detectors.vm_firewall_worker_stale(world.db, params, naive_t0 + timedelta(seconds=3 * interval - 1)) == []
    assert len(detectors.vm_firewall_worker_stale(world.db, params, naive_t0 + timedelta(seconds=3 * interval + 1))) == 1


def test_worker_stale_detector_is_quiet_without_an_enabled_location(db_session):
    make_world(db_session, location_enabled=False)
    params = effective_settings(db_session)["vm_firewall_worker_stale"].params
    assert detectors.vm_firewall_worker_stale(db_session, params, T0.replace(tzinfo=None)) == []


def test_worker_lease_is_separate_from_the_alert_lease(db_session):
    now = T0.replace(tzinfo=None)
    assert lease.acquire(db_session, owner="a", seconds=60, now=now, name=reconcile.LEASE_NAME)
    assert not lease.acquire(db_session, owner="b", seconds=60, now=now, name=reconcile.LEASE_NAME)
    assert lease.acquire(db_session, owner="b", seconds=60, now=now)
    lease.release(db_session, owner="a", name=reconcile.LEASE_NAME)
    assert lease.acquire(db_session, owner="b", seconds=60, now=now, name=reconcile.LEASE_NAME)


def test_staff_firewall_changes_notify_opted_in_owner(world):
    from app.models.alerting import NotificationDelivery

    vm = world.vm("bob")
    world.db.add(ClientNotificationPref(user_id=vm.owner_user_id, email_enabled=True, categories={"firewall": True}))
    world.db.flush()
    row = fw.get_or_create(world.db, vm.id)
    fw.set_firewall(world.db, vm.id, world.actor("bob"), revision=row.revision, client_enabled=True)
    fw.add_rule(world.db, vm.id, world.actor("bob"),
                {"direction": "in", "action": "ACCEPT", "protocol": "tcp", "ports": "22"}, revision=row.revision)
    fw.add_rule(world.db, vm.id, world.staff_actor,
                {"direction": "in", "action": "ACCEPT", "protocol": "tcp", "ports": "80"}, revision=row.revision)
    world.db.flush()
    messages = [d.payload for d in world.db.query(NotificationDelivery).filter_by(user_id=vm.owner_user_id)]
    text = " ".join(str(m) for m in messages)
    assert len(messages) == 2
    assert "was turned on" in text and "Staff changed the firewall" in text
