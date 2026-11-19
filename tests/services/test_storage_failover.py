"""NFS storage failover: takeover order, moves, drain, rebalance, return, runner config and status, SFTP master,
host key and virtual IP reservation, and the failover alerts."""

from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import settings
from app.dao.ipam_dao import IPAMDAO
from app.dao.storage_dao import StorageDAO
from app.models.ipam import IPAddress
from app.models.location import Location
from app.models.private_network import PrivateNetwork
from app.models.server import Server
from app.models.service import Service, ServiceStatus, ServiceType
from app.models.storage import StorageEvent, StorageLocation, StorageTenant
from app.services import storage_ha, storage_location
from app.services.alerting import detectors as d
from app.services.alerting.catalogue import CATALOGUE
from app.services.storage_errors import StorageError
from app.services.storage_service import apply_runner_status, build_runner_config


def _now():
    return datetime.now(timezone.utc)


class World:
    def __init__(self, db):
        self.db = db
        self.location = Location(name="London", description="")
        db.add(self.location)
        db.commit()
        self.nodes = []
        self.tenants = []

    def node(self, name=None, *, version="0.3.0", online=True, sftp=False, location=None, healthy_for=None):
        index = len(self.nodes) + 1
        loc = location or self.location
        server = Server(name=f"srv-{index}-{loc.id}", server_ip=f"10.9.{loc.id}.{index}", location_id=loc.id,
                        plugin_name="ipmi", plugin_config={})
        self.db.add(server)
        self.db.commit()
        node, _key = StorageDAO.create_node(self.db, name=name or f"stor-{index:02d}", server_id=server.id)
        node.runner_version = version
        node.ceph_ok = True
        node.sftp_enabled = sftp
        if sftp:
            node.sftp_host = f"sftp{index}.example.com"
        if online:
            node.last_seen_at = datetime.utcnow()
        if healthy_for is not None:
            node.healthy_since = datetime.utcnow() - timedelta(seconds=healthy_for)
        self.db.commit()
        self.nodes.append(node)
        return node

    def tenant(self, preferred, *, current=None, current_for=None, terminated=False, active=True, location=None):
        index = len(self.tenants) + 1
        loc = location or self.location
        service = Service(
            name=f"nfs-{index}", service_type=ServiceType.NFS_STORAGE,
            status=ServiceStatus.ACTIVE if active else ServiceStatus.TERMINATED,
        )
        self.db.add(service)
        self.db.flush()
        network = PrivateNetwork(name=f"net-{index}", location_id=loc.id, segment_id=2000 + index, cidr=f"10.{index}.0.0/24")
        self.db.add(network)
        self.db.flush()
        tenant = StorageTenant(
            service_id=service.id, node_id=preferred.id, network_id=network.id, location_id=loc.id,
            vlan=2000 + index, network_cidr=f"10.{index}.0.0/24", endpoint_ip=f"10.{index}.0.254", size_bytes=1 << 30,
            current_node_id=current.id if current else None,
            current_since=_now() - timedelta(seconds=current_for) if current and current_for is not None else (
                _now() if current else None),
            terminated_at=_now() if terminated else None,
            destroy_after=_now() + timedelta(days=3) if terminated else None,
        )
        self.db.add(tenant)
        self.db.commit()
        self.tenants.append(tenant)
        return tenant


@pytest.fixture
def world(db_session, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setattr(settings, "service_instance_encryption_key", Fernet.generate_key().decode())
    return World(db_session)


def report(node, entries=None, **extra):
    body = {
        "runner_version": node.runner_version,
        "node": {"ceph_ok": True},
        "ha": {"lease_ok": True, "fenced": False, "sftp_master": False, "ha_active": True},
        "tenants": entries or [],
    }
    body.update(extra)
    return body


def serving(tenant, since=None, **extra):
    return {"id": tenant.id, "state": "ready", "shares": [], "serving": True, "since": since, **extra}


def send(db, node, body):
    apply_runner_status(db, node, body, "10.0.0.1")


# --- HA active, takeover order -------------------------------------------------


def test_parse_version():
    assert storage_ha.parse_version("0.3.0") == (0, 3, 0)
    assert storage_ha.parse_version("v0.3.1-dev") == (0, 3, 1)
    assert storage_ha.parse_version("0.10") == (0, 10)
    assert storage_ha.parse_version("dev") is None and storage_ha.parse_version(None) is None


def test_ha_active_needs_switch_and_every_enabled_node_on_0_3_0(world):
    a, b = world.node(), world.node()
    db, loc = world.db, world.location.id
    assert storage_ha.ha_active(db, loc) is True
    b.runner_version = "0.2.2"
    db.commit()
    assert storage_ha.ha_active(db, loc) is False
    b.enabled = False
    db.commit()
    assert storage_ha.ha_active(db, loc) is True
    b.enabled = True
    b.runner_version = "0.3.0"
    db.commit()
    row = storage_location.ensure_location(db, loc)
    row.ha_enabled = False
    db.commit()
    assert storage_ha.ha_active(db, loc) is False
    assert storage_ha.ha_active(db, None) is False


def test_takeover_order_preferred_first_then_by_load_rotated_by_tenant_id(world):
    a, b, c, d_ = world.node(), world.node(), world.node(), world.node()
    t1, t2, t3, t4 = (world.tenant(a) for _ in range(4))
    world.tenant(b)
    counts = StorageDAO.live_tenant_counts(world.db)
    # Others by (tenants preferred there, id): c(0), d(0), b(1).
    orders = {t.id: storage_ha.takeover_order(t, world.nodes, counts) for t in (t1, t2, t3, t4)}
    for t in (t1, t2, t3, t4):
        assert orders[t.id][0] == a.id and sorted(orders[t.id]) == sorted(n.id for n in world.nodes)
    base = [c.id, d_.id, b.id]
    for t in (t1, t2, t3, t4):
        shift = t.id % 3
        assert orders[t.id][1:] == base[shift:] + base[:shift]
    # Dead node's tenants spread over the survivors instead of one.
    assert len({orders[t.id][1] for t in (t1, t2, t3)}) == 3


def test_takeover_order_leaves_out_draining_and_disabled_and_puts_move_target_first(world):
    a, b, c, d_ = world.node(), world.node(), world.node(), world.node()
    b.draining = True
    c.enabled = False
    world.db.commit()
    tenant = world.tenant(a)
    counts = StorageDAO.live_tenant_counts(world.db)
    assert storage_ha.takeover_order(tenant, world.nodes, counts) == [a.id, d_.id]
    tenant.move_to_node_id = d_.id
    assert storage_ha.takeover_order(tenant, world.nodes, counts) == [d_.id, a.id]
    a.draining = True
    assert storage_ha.takeover_order(tenant, world.nodes, counts) == [d_.id]


# --- planned moves ----------------------------------------------------------------


def test_move_success_sets_preferred_and_records_handoff_not_failover(world):
    a, b = world.node(), world.node()
    tenant = world.tenant(a, current=a)
    storage_ha.request_move(world.db, tenant, b.id)
    assert tenant.move_to_node_id == b.id and tenant.move_id.startswith("m-")
    config = build_runner_config(world.db, a)
    entry = config["tenants"][0]
    assert entry["move"] == {"id": tenant.move_id, "to_node_id": b.id}
    assert entry["takeover_order"][0] == b.id

    send(world.db, a, report(a, [serving(tenant)]))
    assert tenant.move_to_node_id == b.id
    send(world.db, b, report(b, [serving(tenant, move_id=tenant.move_id)]))
    world.db.refresh(tenant)
    assert tenant.move_to_node_id is None and tenant.move_id is None
    assert tenant.current_node_id == b.id and tenant.node_id == b.id
    assert tenant.last_failover_at is None
    kinds = [e.kind for e in world.db.query(StorageEvent).all()]
    assert kinds == ["handoff"]


def test_move_without_set_preferred_keeps_preferred_node(world):
    a, b = world.node(), world.node()
    tenant = world.tenant(a, current=a)
    storage_ha.request_move(world.db, tenant, b.id, set_preferred=False)
    send(world.db, b, report(b, [serving(tenant)]))
    assert tenant.node_id == a.id and tenant.current_node_id == b.id and tenant.move_to_node_id is None


def test_move_times_out_and_is_reported_as_failed(world, monkeypatch):
    a, b = world.node(), world.node()
    tenant = world.tenant(a, current=a)
    storage_ha.request_move(world.db, tenant, b.id)
    now = _now()
    storage_ha.advance_location(world.db, world.location.id, now + timedelta(seconds=599))
    assert tenant.move_to_node_id == b.id
    storage_ha.advance_location(world.db, world.location.id, now + timedelta(seconds=601))
    world.db.refresh(tenant)
    assert tenant.move_to_node_id is None and tenant.node_id == a.id
    event = world.db.query(StorageEvent).one()
    assert event.kind == "move_failed" and event.tenant_id == tenant.id and event.node_id == b.id


def test_move_is_refused_when_not_possible(world):
    a, b = world.node(), world.node()
    other_location = Location(name="Paris", description="")
    world.db.add(other_location)
    world.db.commit()
    foreign = world.node(location=other_location)
    tenant = world.tenant(a, current=a)
    with pytest.raises(StorageError, match="not in this storage's location"):
        storage_ha.request_move(world.db, tenant, foreign.id)
    with pytest.raises(StorageError, match="already serves"):
        storage_ha.request_move(world.db, tenant, a.id)
    b.last_seen_at = datetime.utcnow() - timedelta(hours=1)
    with pytest.raises(StorageError, match="not online"):
        storage_ha.request_move(world.db, tenant, b.id)
    b.last_seen_at = datetime.utcnow()
    b.draining = True
    with pytest.raises(StorageError, match="draining"):
        storage_ha.request_move(world.db, tenant, b.id)
    b.draining = False
    storage_ha.request_move(world.db, tenant, b.id)
    with pytest.raises(StorageError, match="already in progress"):
        storage_ha.request_move(world.db, tenant, b.id)


def test_move_refused_without_active_failover(world):
    a, b = world.node(), world.node(version="0.2.2")
    tenant = world.tenant(a, current=a)
    with pytest.raises(StorageError, match="Failover is not active"):
        storage_ha.request_move(world.db, tenant, b.id)


# --- failover detection ----------------------------------------------------------


def test_takeover_without_move_is_a_failover(world):
    a, b = world.node(), world.node()
    tenant = world.tenant(a, current=a)
    since = (_now() - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    send(world.db, b, report(b, [serving(tenant, since=since)]))
    world.db.refresh(tenant)
    assert tenant.current_node_id == b.id and tenant.node_id == a.id
    assert tenant.last_failover_at is not None and tenant.last_failover_from_node_id == a.id
    event = world.db.query(StorageEvent).one()
    assert event.kind == "takeover" and event.node_id == b.id and event.from_node_id == a.id

    # The same report again is not a second failover.
    send(world.db, b, report(b, [serving(tenant, since=since)]))
    assert world.db.query(StorageEvent).count() == 1


def test_runner_reported_takeover_event_is_not_duplicated(world):
    a, b = world.node(), world.node()
    tenant = world.tenant(a, current=a)
    event = {"at": _now().strftime("%Y-%m-%dT%H:%M:%SZ"), "kind": "takeover", "tenant_id": tenant.id,
             "from_node_id": a.id, "fence": "node", "detail": "lease expired"}
    send(world.db, b, report(b, [serving(tenant)], events=[event, {"kind": "bogus"}, {"kind": "lost", "tenant_id": 9999}]))
    events = world.db.query(StorageEvent).all()
    assert [e.kind for e in events] == ["takeover"]
    assert "fence: node" in events[0].detail


def test_first_report_on_preferred_node_is_not_a_failover(world):
    a, _b = world.node(), world.node()
    tenant = world.tenant(a)
    send(world.db, a, report(a, [serving(tenant)]))
    assert tenant.current_node_id == a.id and tenant.last_failover_at is None
    assert world.db.query(StorageEvent).count() == 0


def test_state_comes_only_from_the_serving_node(world):
    a, b = world.node(), world.node()
    tenant = world.tenant(a, current=b)
    send(world.db, a, report(a, [{"id": tenant.id, "state": "error", "error": "stale", "serving": False}]))
    assert tenant.runner_state is None and tenant.runner_error is None
    assert tenant.current_node_id == b.id
    send(world.db, b, report(b, [serving(tenant)]))
    assert tenant.runner_state == "ready"


def test_current_node_cleared_when_it_reports_not_serving_or_goes_offline(world):
    a, b = world.node(), world.node()
    tenant = world.tenant(a, current=a)
    send(world.db, a, report(a, [{"id": tenant.id, "serving": False}]))
    assert tenant.current_node_id is None
    tenant.current_node_id = b.id
    world.db.commit()
    b.last_seen_at = datetime.utcnow() - timedelta(hours=1)
    storage_ha.reconcile(world.db)
    world.db.refresh(tenant)
    assert tenant.current_node_id is None and tenant.current_since is not None


def test_destroyed_ids_are_accepted_from_any_node_of_the_location_only(world):
    a, b = world.node(), world.node()
    other_location = Location(name="Paris", description="")
    world.db.add(other_location)
    world.db.commit()
    foreign_node = world.node(location=other_location)
    gone = world.tenant(a, terminated=True)
    kept = world.tenant(a, terminated=True)
    gone_id, kept_id = gone.id, kept.id
    send(world.db, foreign_node, report(foreign_node, destroyed_tenants=[kept_id]))
    assert world.db.get(StorageTenant, kept_id) is not None
    send(world.db, b, report(b, destroyed_tenants=[gone_id]))
    assert world.db.get(StorageTenant, gone_id) is None


def test_old_runner_status_keeps_its_own_tenants_only(world):
    a = world.node(version="0.2.2")
    b = world.node(version="0.2.2")
    mine, theirs = world.tenant(a), world.tenant(b)
    send(world.db, a, {"runner_version": "0.2.2", "node": {"ceph_ok": True}, "tenants": [
        {"id": mine.id, "state": "ready", "shares": []},
        {"id": theirs.id, "state": "ready", "shares": []},
    ]})
    assert mine.runner_state == "ready" and mine.current_node_id == a.id
    assert theirs.runner_state is None and theirs.current_node_id is None


# --- SFTP master ---------------------------------------------------------------


def test_sftp_master_is_tracked_and_the_latest_claim_wins(world):
    a, b = world.node(sftp=True), world.node(sftp=True)
    claim = {"lease_ok": True, "fenced": False, "sftp_master": True, "ha_active": True}
    send(world.db, a, report(a, ha=claim))
    assert a.sftp_master is True and b.sftp_master is False
    send(world.db, b, report(b, ha=claim))
    world.db.refresh(a)
    assert a.sftp_master is False and b.sftp_master is True
    events = world.db.query(StorageEvent).filter(StorageEvent.kind == "sftp_master").order_by(StorageEvent.id).all()
    assert len(events) == 2 and events[0].tenant_id is None
    assert "both reported" in events[1].detail
    send(world.db, b, report(b, ha={"lease_ok": True, "sftp_master": False}))
    assert b.sftp_master is False


# --- drain -------------------------------------------------------------------------


def test_drain_moves_one_tenant_at_a_time_to_the_least_loaded_nodes(world):
    a, b, c = world.node(), world.node(), world.node()
    t1, t2, t3 = (world.tenant(a, current=a) for _ in range(3))
    world.tenant(b, current=b)
    storage_ha.set_draining(world.db, a, True)
    moving = [t for t in (t1, t2, t3) if t.move_to_node_id is not None]
    assert len(moving) == 1 and moving[0].move_to_node_id == c.id and moving[0].move_set_preferred is False
    assert storage_ha.advance_location(world.db, world.location.id) is None
    assert len([t for t in (t1, t2, t3) if t.move_to_node_id is not None]) == 1

    send(world.db, c, report(c, [serving(moving[0])]))
    nxt = [t for t in (t1, t2, t3) if t.move_to_node_id is not None]
    assert len(nxt) == 1 and nxt[0].id != moving[0].id
    # c now serves one, b one: the next goes to the least loaded (c has 1, b has 1: lower id is b).
    assert nxt[0].move_to_node_id in (b.id, c.id)
    for t in (t1, t2, t3):
        assert t.node_id == a.id
    # Draining node is out of the takeover order and not eligible.
    config = build_runner_config(world.db, a)
    assert config["ha"]["draining"] is True and config["sftp"]["master_eligible"] is False
    assert all(a.id not in t["takeover_order"] for t in config["tenants"])


def test_drain_refused_when_no_other_healthy_node_and_undrain_clears(world):
    a = world.node()
    world.tenant(a, current=a)
    with pytest.raises(StorageError, match="No other healthy node"):
        storage_ha.set_draining(world.db, a, True)
    world.node()
    storage_ha.set_draining(world.db, a, True)
    assert a.draining is True and a.healthy_since is None
    storage_ha.set_draining(world.db, a, False)
    assert a.draining is False


def test_draining_node_is_not_picked_for_new_storage(world):
    a, b = world.node(), world.node()
    b.draining = True
    world.db.commit()
    assert [n.id for n in StorageDAO.list_nodes(world.db) if n.enabled and not n.draining] == [a.id]


# --- rebalance ---------------------------------------------------------------------


def test_rebalance_gives_fair_shares_with_fewest_changes_and_moves_one_at_a_time(world):
    a, b = world.node(), world.node()
    tenants = [world.tenant(a, current=a) for _ in range(4)]
    result = storage_ha.rebalance(world.db, world.location.id)
    assert len(result["changed"]) == 2 and result["rebalancing"] is True
    assert {c["to_node_id"] for c in result["changed"]} == {b.id}
    assert sorted(t.node_id for t in tenants) == [a.id, a.id, b.id, b.id]
    pending = [t for t in tenants if t.move_to_node_id is not None]
    assert len(pending) == 1 and pending[0].move_set_preferred is False

    send(world.db, b, report(b, [serving(pending[0])]))
    pending = [t for t in tenants if t.move_to_node_id is not None]
    assert len(pending) == 1
    send(world.db, b, report(b, [serving(pending[0])]))
    assert not [t for t in tenants if t.move_to_node_id is not None]
    assert storage_ha.advance_location(world.db, world.location.id) is None
    assert storage_location.get_location(world.db, world.location.id).rebalancing is False
    assert world.db.query(StorageEvent).filter(StorageEvent.kind == "takeover").count() == 0


def test_rebalance_already_fair_changes_nothing_and_needs_ha(world):
    a, b = world.node(), world.node()
    world.tenant(a, current=a)
    world.tenant(b, current=b)
    assert storage_ha.rebalance(world.db, world.location.id)["changed"] == []
    b.runner_version = "0.2.2"
    world.db.commit()
    with pytest.raises(StorageError, match="not active"):
        storage_ha.rebalance(world.db, world.location.id)


# --- return to preferred node ------------------------------------------------------


def test_return_waits_for_both_timers_and_does_not_flap(world):
    a = world.node(healthy_for=100)
    b = world.node(healthy_for=5000)
    tenant = world.tenant(a, current=b, current_for=5000)
    assert storage_ha.advance_location(world.db, world.location.id) is None
    a.healthy_since = datetime.utcnow() - timedelta(seconds=901)
    tenant.current_since = _now() - timedelta(seconds=100)
    world.db.commit()
    assert storage_ha.advance_location(world.db, world.location.id) is None
    tenant.current_since = _now() - timedelta(seconds=901)
    world.db.commit()
    assert storage_ha.advance_location(world.db, world.location.id) == "return"
    assert tenant.move_to_node_id == a.id and tenant.move_set_preferred is False
    send(world.db, a, report(a, [serving(tenant)]))
    assert tenant.current_node_id == a.id and tenant.node_id == a.id and tenant.move_to_node_id is None
    assert storage_ha.advance_location(world.db, world.location.id) is None


def test_return_starts_at_most_one_move_per_location(world):
    a = world.node(healthy_for=5000)
    b = world.node(healthy_for=5000)
    t1 = world.tenant(a, current=b, current_for=5000)
    t2 = world.tenant(a, current=b, current_for=5000)
    assert storage_ha.advance_location(world.db, world.location.id) == "return"
    assert storage_ha.advance_location(world.db, world.location.id) is None
    assert [t.move_to_node_id for t in (t1, t2)].count(a.id) == 1


def test_return_honours_auto_failback_and_needs_a_healthy_preferred_node(world):
    a = world.node(healthy_for=5000)
    b = world.node(healthy_for=5000)
    tenant = world.tenant(a, current=b, current_for=5000)
    row = storage_location.ensure_location(world.db, world.location.id)
    row.auto_failback = False
    world.db.commit()
    assert storage_ha.advance_location(world.db, world.location.id) is None
    row.auto_failback = True
    a.ha_state = {"lease_ok": True, "fenced": True}
    world.db.commit()
    assert storage_ha.advance_location(world.db, world.location.id) is None
    a.ha_state = None
    a.draining = True
    world.db.commit()
    assert storage_ha.advance_location(world.db, world.location.id) is None
    assert tenant.move_to_node_id is None


def test_status_report_restarts_the_healthy_timer_after_an_outage(world):
    a = world.node(healthy_for=5000)
    world.node()
    old = a.healthy_since
    a.last_seen_at = datetime.utcnow() - timedelta(minutes=10)
    send(world.db, a, report(a))
    assert a.healthy_since > old
    send(world.db, a, report(a, ha={"lease_ok": False}))
    assert a.healthy_since is None
    send(world.db, a, report(a))
    assert a.healthy_since is not None


# --- runner config -----------------------------------------------------------------


def test_config_with_ha_active_lists_every_tenant_of_the_location_with_host_key(world):
    a, b = world.node(sftp=True), world.node(sftp=True)
    live = world.tenant(a, current=a)
    gone = world.tenant(b, terminated=True)
    other = Location(name="Paris", description="")
    world.db.add(other)
    world.db.commit()
    world.node(location=other)
    world.tenant(world.nodes[-1], location=other)
    storage_location.update_location(
        world.db, world.location.id, sftp_enabled=True, sftp_host="Files.Example.com", sftp_port=2200,
        sftp_virtual_ip="203.0.113.10/24",
    )
    storage_location.set_host_key(world.db, world.location.id, generate=True)

    config = build_runner_config(world.db, a)
    assert [t["id"] for t in config["tenants"]] == [live.id, gone.id]
    assert config["node"] == {"id": a.id, "name": a.name, "location_id": world.location.id}
    ha = config["ha"]
    assert ha["enabled"] is True and ha["lease_seconds"] == 20 and ha["renew_seconds"] == 4
    assert ha["stop_margin_seconds"] == 6 and ha["guard_margin_seconds"] == 3 and ha["poll_seconds"] == 2
    assert ha["rank_step_seconds"] == 6 and ha["fence_seconds"] == 600
    assert ha["nfs_lease_lifetime"] == 30 and ha["nfs_grace_period"] == 45 and ha["draining"] is False
    assert ha["nodes"] == [{"id": a.id, "name": a.name, "draining": False}, {"id": b.id, "name": b.name, "draining": False}]
    sftp = config["sftp"]
    assert sftp["location_id"] == world.location.id and sftp["virtual_ip"] == "203.0.113.10/24"
    assert sftp["port"] == 2200 and sftp["enabled"] is True and sftp["master_eligible"] is True
    assert sftp["master_order"] == sorted([a.id, b.id])
    assert sftp["host_key"].startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
    entry = config["tenants"][0]
    assert entry["preferred_node_id"] == a.id and entry["takeover_order"][0] == a.id and "move" not in entry
    assert config["tenants"][1]["state"] == "terminated" or config["tenants"][1]["terminated_at"]
    # The other location's node never sees this location's tenants or key.
    foreign = build_runner_config(world.db, world.nodes[-1])
    assert all(t["id"] != live.id for t in foreign["tenants"]) and foreign["sftp"]["host_key"] is None


def test_config_without_active_ha_lists_preferred_tenants_only_and_still_has_host_key(world):
    a, b = world.node(version="0.3.0", sftp=True), world.node(version="0.2.2", sftp=True)
    mine, theirs = world.tenant(a), world.tenant(b)
    storage_location.update_location(world.db, world.location.id, sftp_virtual_ip="203.0.113.10/24")
    storage_location.set_host_key(world.db, world.location.id, generate=True)
    config = build_runner_config(world.db, a)
    assert [t["id"] for t in config["tenants"]] == [mine.id]
    assert config["ha"]["enabled"] is False
    assert config["tenants"][0]["takeover_order"] == [a.id] and config["tenants"][0]["preferred_node_id"] == a.id
    assert config["sftp"]["virtual_ip"] is None and config["sftp"]["host_key"]
    assert config["sftp"]["master_eligible"] is True


def test_config_for_runner_older_than_0_3_0_is_exactly_todays(world):
    a, b = world.node(version="0.2.2", sftp=True), world.node(version="0.3.0")
    mine, _theirs = world.tenant(a), world.tenant(b)
    storage_location.update_location(world.db, world.location.id, sftp_virtual_ip="203.0.113.10/24")
    storage_location.set_host_key(world.db, world.location.id, generate=True)
    config = build_runner_config(world.db, a)
    assert set(config) == {"version", "node", "settings", "sftp", "tenants"}
    assert config["node"] == {"id": a.id, "name": a.name}
    assert set(config["sftp"]) == {"enabled", "port", "max_auth_failures", "failure_window_seconds", "ban_seconds"}
    assert [t["id"] for t in config["tenants"]] == [mine.id]
    assert not {"preferred_node_id", "takeover_order", "move"} & set(config["tenants"][0])
    assert "BEGIN" not in str(config)


def test_host_key_is_only_in_runner_config_never_in_admin_output(world):
    world.node(sftp=True)
    row = storage_location.set_host_key(world.db, world.location.id, generate=True)
    assert row.sftp_host_key.startswith("enc:") and "BEGIN" not in row.sftp_host_key
    out = storage_location.location_out(world.db, row, world.location.id)
    assert out["sftp_host_key_set"] is True and out["sftp_host_key_fingerprint"].startswith("SHA256:")
    assert out["sftp_host_key_public"].startswith("ssh-ed25519 ")
    assert "BEGIN" not in str(out) and "sftp_host_key'" not in str(out.keys())
    assert "BEGIN OPENSSH PRIVATE KEY" in build_runner_config(world.db, world.nodes[0])["sftp"]["host_key"]


def test_host_key_import_and_validation(world):
    from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

    world.node()
    key = ed25519.Ed25519PrivateKey.generate()
    pem = key.private_bytes(Encoding.PEM, PrivateFormat.OpenSSH, NoEncryption()).decode()
    first = storage_location.set_host_key(world.db, world.location.id, private_key=pem)
    fingerprint = first.sftp_host_key_fingerprint
    assert storage_location.runner_host_key(first).strip() == pem.strip()
    again = storage_location.set_host_key(world.db, world.location.id, private_key=pem)
    assert again.sftp_host_key_fingerprint == fingerprint
    assert storage_location.set_host_key(world.db, world.location.id, generate=True).sftp_host_key_fingerprint != fingerprint
    rsa_pem = rsa.generate_private_key(65537, 2048).private_bytes(Encoding.PEM, PrivateFormat.OpenSSH, NoEncryption()).decode()
    with pytest.raises(StorageError, match="ed25519"):
        storage_location.set_host_key(world.db, world.location.id, private_key=rsa_pem)
    with pytest.raises(StorageError, match="valid"):
        storage_location.set_host_key(world.db, world.location.id, private_key="nonsense")
    with pytest.raises(StorageError, match="either"):
        storage_location.set_host_key(world.db, world.location.id)


def test_customers_see_the_location_host_name(world):
    a = world.node(sftp=True)
    tenant = world.tenant(a)
    assert storage_location.endpoint_for_tenant(tenant)["host"] == "sftp1.example.com"
    storage_location.update_location(world.db, world.location.id, sftp_enabled=True, sftp_host="files.example.com", sftp_port=2300)
    world.db.refresh(tenant)
    endpoint = storage_location.endpoint_for_tenant(tenant)
    assert endpoint["host"] == "files.example.com" and endpoint["port"] == 2300
    storage_location.update_location(world.db, world.location.id, sftp_enabled=False)
    assert storage_location.endpoint_for_tenant(tenant) is None


# --- IPAM reservation of the VIP ------------------------------------------------------


def test_vip_in_an_ipam_subnet_is_reserved_released_and_never_allocated(world):
    world.node(sftp=True)
    subnet = IPAMDAO.create_subnet(world.db, "pub", "203.0.113.0/28", location_id=world.location.id)
    row, warnings = storage_location.update_location(world.db, world.location.id, sftp_virtual_ip="203.0.113.5/28")
    assert warnings == []
    ip = world.db.query(IPAddress).filter_by(ip_address="203.0.113.5").one()
    assert ip.state == "reserved" and ip.details["reserved_for"] == "storage_sftp_vip"
    free = {r.ip_address for r in world.db.query(IPAddress).filter_by(subnet_id=subnet.id, state="free")}
    assert "203.0.113.5" not in free and len(free) == 13

    _row, warnings = storage_location.update_location(world.db, world.location.id, sftp_virtual_ip="203.0.113.6/28")
    assert warnings == []
    assert world.db.query(IPAddress).filter_by(ip_address="203.0.113.5").one().state == "free"
    assert "reserved_for" not in world.db.query(IPAddress).filter_by(ip_address="203.0.113.5").one().details
    assert world.db.query(IPAddress).filter_by(ip_address="203.0.113.6").one().state == "reserved"

    storage_location.update_location(world.db, world.location.id, sftp_virtual_ip="")
    assert world.db.query(IPAddress).filter_by(ip_address="203.0.113.6").one().state == "free"
    assert storage_location.get_location(world.db, world.location.id).sftp_virtual_ip is None


def test_vip_must_be_free_and_unique_and_outside_subnets_only_warns(world):
    world.node(sftp=True)
    IPAMDAO.create_subnet(world.db, "pub", "203.0.113.0/28", location_id=world.location.id)
    taken = world.db.query(IPAddress).filter_by(ip_address="203.0.113.7").one()
    taken.state = "assigned"
    world.db.commit()
    with pytest.raises(StorageError, match="not free"):
        storage_location.update_location(world.db, world.location.id, sftp_virtual_ip="203.0.113.7/28")
    assert storage_location.get_location(world.db, world.location.id).sftp_virtual_ip is None
    _row, warnings = storage_location.update_location(world.db, world.location.id, sftp_virtual_ip="198.51.100.9/24")
    assert len(warnings) == 1 and "outside every IPAM subnet" in warnings[0]
    for bad in ("198.51.100.9", "nonsense", "127.0.0.1/8", "198.51.100.0/24"):
        with pytest.raises(StorageError):
            storage_location.update_location(world.db, world.location.id, sftp_virtual_ip=bad)

    other = Location(name="Paris", description="")
    world.db.add(other)
    world.db.commit()
    world.node(location=other)
    with pytest.raises(StorageError, match="another storage location"):
        storage_location.update_location(world.db, other.id, sftp_virtual_ip="198.51.100.9/24")


def test_location_settings_validation(world):
    world.node()
    with pytest.raises(StorageError, match="sftp_host"):
        storage_location.update_location(world.db, world.location.id, sftp_enabled=True)
    with pytest.raises(StorageError, match="port"):
        storage_location.update_location(world.db, world.location.id, sftp_port=2049)
    with pytest.raises(StorageError, match="no storage node"):
        storage_location.require_storage_location(world.db, 9999)


# --- alerts ------------------------------------------------------------------------


def run_alert(db, name, now=None):
    return d.DETECTORS[name](db, CATALOGUE[name].defaults(), now or datetime.utcnow())


def test_failover_alert_fires_for_failover_and_resolves_on_return_or_planned_move(world):
    a, b = world.node(), world.node()
    tenant = world.tenant(a, current=a)
    assert run_alert(world.db, "storage_tenant_failover") == []
    send(world.db, b, report(b, [serving(tenant)]))
    found = run_alert(world.db, "storage_tenant_failover")
    assert len(found) == 1 and found[0].resource_id == str(tenant.id)
    send(world.db, a, report(a, [serving(tenant)]))
    assert run_alert(world.db, "storage_tenant_failover") == []
    send(world.db, b, report(b, [serving(tenant)]))
    assert len(run_alert(world.db, "storage_tenant_failover")) == 1
    tenant.node_id = b.id
    world.db.commit()
    assert run_alert(world.db, "storage_tenant_failover") == []


def test_planned_move_does_not_raise_the_failover_alert(world):
    a, b = world.node(), world.node()
    tenant = world.tenant(a, current=a)
    storage_ha.request_move(world.db, tenant, b.id, set_preferred=False)
    send(world.db, b, report(b, [serving(tenant)]))
    assert tenant.current_node_id == b.id and tenant.node_id == a.id
    assert run_alert(world.db, "storage_tenant_failover") == []


def test_location_degraded_alert(world):
    a, b = world.node(), world.node()
    assert run_alert(world.db, "storage_location_degraded") == []
    b.ha_state = {"lease_ok": False}
    world.db.commit()
    found = run_alert(world.db, "storage_location_degraded")
    assert len(found) == 1 and found[0].resource_type == "storage_location" and found[0].resource_id == str(world.location.id)
    assert CATALOGUE["storage_location_degraded"].severity == "critical"
    assert CATALOGUE["storage_tenant_failover"].severity == "warning"
    b.ha_state = None
    world.db.commit()
    assert run_alert(world.db, "storage_location_degraded") == []
    b.draining = True
    world.db.commit()
    assert len(run_alert(world.db, "storage_location_degraded")) == 1


def _vip_location(world, age_seconds=600):
    storage_location.update_location(
        world.db, world.location.id, sftp_enabled=True, sftp_host="files.example.com", sftp_virtual_ip="198.51.100.9/24"
    )
    row = storage_location.get_location(world.db, world.location.id)
    row.updated_at = datetime.utcnow() - timedelta(seconds=age_seconds)
    world.db.commit()
    return row


def test_sftp_no_master_alert_fires_without_an_online_master(world):
    a, b = world.node(sftp=True), world.node(sftp=True)
    _vip_location(world)
    found = run_alert(world.db, "storage_sftp_no_master")
    assert len(found) == 1 and found[0].resource_type == "storage_location"
    assert found[0].resource_id == str(world.location.id)
    assert CATALOGUE["storage_sftp_no_master"].severity == "critical"

    a.sftp_master = True
    world.db.commit()
    assert run_alert(world.db, "storage_sftp_no_master") == []

    # The master stops reporting: the alert waits for the threshold from its last report.
    a.last_seen_at = datetime.utcnow() - timedelta(seconds=200)
    world.db.commit()
    assert len(run_alert(world.db, "storage_sftp_no_master")) == 1
    a.last_seen_at = datetime.utcnow() - timedelta(seconds=30)
    world.db.commit()
    assert run_alert(world.db, "storage_sftp_no_master") == []
    assert b.sftp_master is False


def test_sftp_no_master_alert_waits_after_a_master_change_and_needs_a_vip_and_ha(world):
    a, b = world.node(sftp=True), world.node(sftp=True)
    _vip_location(world)
    a.sftp_master = True
    world.db.commit()
    send(world.db, a, report(a))  # a reports it is no longer master: an sftp_master event now
    assert a.sftp_master is False
    assert run_alert(world.db, "storage_sftp_no_master") == []
    assert len(run_alert(world.db, "storage_sftp_no_master", now=datetime.utcnow() + timedelta(seconds=180))) == 1

    b.runner_version = "0.2.2"
    world.db.commit()
    assert run_alert(world.db, "storage_sftp_no_master", now=datetime.utcnow() + timedelta(seconds=180)) == []
    b.runner_version = "0.3.0"
    storage_location.update_location(world.db, world.location.id, sftp_virtual_ip="")
    assert run_alert(world.db, "storage_sftp_no_master", now=datetime.utcnow() + timedelta(seconds=180)) == []


def test_location_with_one_node_is_never_degraded(world):
    a = world.node()
    a.last_seen_at = datetime.utcnow() - timedelta(hours=1)
    world.db.commit()
    assert run_alert(world.db, "storage_location_degraded") == []


def test_unserved_tenant_raises_the_error_alert_only_while_ha_is_active(world):
    a, _b = world.node(), world.node()
    tenant = world.tenant(a)
    tenant.updated_at = datetime.utcnow() - timedelta(hours=1)
    tenant.current_since = _now() - timedelta(seconds=300)
    world.db.commit()
    found = run_alert(world.db, "storage_tenant_error")
    assert len(found) == 1 and "no node has served it" in found[0].message
    tenant.current_since = _now() - timedelta(seconds=60)
    world.db.commit()
    assert run_alert(world.db, "storage_tenant_error") == []
    tenant.current_since = _now() - timedelta(seconds=300)
    tenant.current_node_id = a.id
    world.db.commit()
    assert run_alert(world.db, "storage_tenant_error") == []
    tenant.current_node_id = None
    _b.runner_version = "0.2.2"
    world.db.commit()
    assert run_alert(world.db, "storage_tenant_error") == []


def test_events_are_pruned_after_90_days(world):
    world.node()
    world.db.add_all([
        StorageEvent(kind="takeover", location_id=world.location.id, at=_now() - timedelta(days=91)),
        StorageEvent(kind="takeover", location_id=world.location.id, at=_now() - timedelta(days=89)),
    ])
    world.db.commit()
    storage_ha.reconcile(world.db)
    assert world.db.query(StorageEvent).count() == 1
    assert settings.storage_events_keep_days == 90


def test_non_master_node_not_listening_is_normal_behind_a_virtual_ip(world):
    a, b = world.node(sftp=True), world.node(sftp=True)
    for node in (a, b):
        node.last_seen_at = datetime.utcnow()
        node.sftp_listening = False
    a.sftp_master = True
    world.db.commit()
    assert len(run_alert(world.db, "storage_node_error")) == 2
    storage_location.update_location(world.db, world.location.id, sftp_virtual_ip="198.51.100.9/24")
    found = run_alert(world.db, "storage_node_error")
    assert [f.resource_id for f in found] == [str(a.id)]
