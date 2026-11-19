"""NFS storage: endpoint reservation, placement, attachments in both VLAN modes, shares, lifecycle and runner state."""
from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import settings
from app.dao.private_network_dao import PrivateNetworkDAO
from app.dao.service_dao import ServiceDAO
from app.dao.storage_dao import StorageDAO
from app.models.cable_run import CableRun
from app.models.location import Location
from app.models.network_port import NetworkPort
from app.models.network_switch import NetworkSwitch
from app.models.private_network import AttachmentState
from app.models.server import Server
from app.models.service import ProvisioningSource, ServiceStatus, ServiceType
from app.models.storage import ShareState
from app.models.switch_port import SwitchPort
from app.models.user import User
from app.services import private_network_service as pns
from app.services.private_network_service import (
    PrivateNetworkError,
    PrivateNetworkService,
    preview_uplink_ports,
    storage_endpoint_ip,
)
from app.services.provisioning import ProvisioningActor, ProvisioningService, ProvisionRequest
from app.services.provisioning.errors import ProvisioningError
from app.services.storage_service import (
    GIB,
    StorageError,
    StorageNodeService,
    StorageService,
    apply_runner_status,
    build_runner_config,
    share_clients,
    tenant_status,
)

PUBLIC_VLAN = 100
ACTOR = ProvisioningActor(kind="admin", actor_id=1, name="test", source="test")


class FakeSwitch:
    def __init__(self):
        self.vlans = {}
        self.tagged = {}
        self.calls = 0

    async def create_vlan(self, vlan_id, name):
        self.calls += 1
        self.vlans[vlan_id] = name

    async def delete_vlan(self, vlan_id, name=None):
        self.calls += 1
        self.vlans.pop(vlan_id, None)

    async def get_port_vlans(self, port):
        self.calls += 1
        tagged = self.tagged.get(port, set())
        return {
            "interface": port,
            "mode": "trunk" if tagged else "access",
            "native_vlan": PUBLIC_VLAN,
            "allowed_all": False,
            "allowed_vlans": sorted(tagged | {PUBLIC_VLAN}),
        }

    async def add_port_vlan(self, port, vlan_id):
        self.calls += 1
        self.tagged.setdefault(port, set()).add(vlan_id)

    async def remove_port_vlan(self, port, vlan_id):
        self.calls += 1
        self.tagged.get(port, set()).discard(vlan_id)


@pytest.fixture
def env(db_session, monkeypatch):
    location = Location(name="London", description="")
    db_session.add(location)
    db_session.commit()
    pool = PrivateNetworkDAO.create_pool(
        db_session,
        location_id=location.id,
        vlan_start=2000,
        vlan_end=2009,
        bridge="vmbr0",
        max_networks_per_client=5,
        max_networks_per_service=4,
    )
    switches = {}
    monkeypatch.setattr(pns, "_switch_plugin", lambda switch: switches.setdefault(switch.name, FakeSwitch()))

    class Env:
        pass

    e = Env()
    e.db, e.location, e.pool, e.switches = db_session, location, pool, switches
    e.owner = _user(db_session, "alice")
    e.other = _user(db_session, "bob")
    return e


def _user(db, username):
    user = User(username=username, email=f"{username}@example.com", is_admin=False)
    user.set_password("secret123")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _server(env, name, *, plugin="nxos", ports=("Ethernet1/1",), location=None):
    switch = NetworkSwitch(name=f"{name}-tor", location_id=(location or env.location).id, plugin_name=plugin, plugin_config={})
    server = Server(
        name=name, server_ip="10.88.0.1", location_id=(location or env.location).id, plugin_name="ipmi", plugin_config={}
    )
    env.db.add_all([switch, server])
    env.db.flush()
    port_rows = []
    for index, switch_port_name in enumerate(ports):
        port = NetworkPort(server_id=server.id, name=f"eth{index}", speed_mbps=25000, pxe_boot=index == 0)
        switch_port = SwitchPort(switch_id=switch.id, name=switch_port_name)
        env.db.add_all([port, switch_port])
        env.db.flush()
        env.db.add(CableRun(end_a_server_port_id=port.id, end_b_switch_port_id=switch_port.id))
        port_rows.append(port)
    env.db.commit()
    return server, port_rows


def _node(env, name="store-01", **kwargs):
    server, ports = _server(env, name, **{k: kwargs.pop(k) for k in ("plugin", "ports", "location") if k in kwargs})
    node, key = StorageNodeService.create(env.db, server_id=server.id, name=name, **kwargs)
    return node, key, ports


async def _network(env, name="net", owner=None, cidr=None):
    return await PrivateNetworkService.create_network(
        env.db, owner_user_id=(owner or env.owner).id, location_id=env.location.id, name=name, cidr=cidr
    )


def _storage(env, name="store", owner=None, size_gb=100):
    return ProvisioningService.create(
        env.db,
        ProvisionRequest(
            name=name,
            service_type=ServiceType.NFS_STORAGE,
            owner_user_id=(owner or env.owner).id,
            provisioning_source=ProvisioningSource.INTERNAL,
            size_gb=size_gb,
        ),
        ACTOR,
    )


def _runner_ready(env, node, tenant):
    apply_runner_status(
        env.db,
        node,
        {"node": {"ceph_ok": True}, "tenants": [{"id": tenant.id, "state": "ready", "shares": []}]},
        "10.0.9.9",
    )


# --- endpoint reservation and network validation ---


def test_endpoint_is_the_last_usable_address():
    assert storage_endpoint_ip("10.0.0.0/24") == "10.0.0.254"
    assert storage_endpoint_ip("192.168.4.0/29") == "192.168.4.6"


@pytest.mark.asyncio
async def test_reserved_endpoint_is_never_handed_to_an_attachment(env, monkeypatch):
    network = await _network(env, cidr="192.168.7.0/29")
    services = []
    for index in range(6):
        server, _ = _server(env, f"dedi-{index}")
        services.append(
            ServiceDAO.create_bare_metal(
                env.db,
                name=f"dedi-{index}-svc",
                server_id=server.id,
                owner_user_id=env.owner.id,
                provisioning_source=ProvisioningSource.INTERNAL,
                status=ServiceStatus.ACTIVE,
            )
        )
    with pytest.raises(PrivateNetworkError, match="kept for the network's storage endpoint"):
        await PrivateNetworkService.attach(env.db, network.id, services[0].id, ip_address="192.168.7.6")
    picked = []
    for service in services[:5]:
        attachment = await PrivateNetworkService.attach(env.db, network.id, service.id)
        picked.append(attachment.ip_address)
    assert "192.168.7.6" not in picked
    assert sorted(picked) == [f"192.168.7.{n}" for n in range(1, 6)]
    with pytest.raises(PrivateNetworkError, match="no free addresses"):
        await PrivateNetworkService.attach(env.db, network.id, services[5].id)


@pytest.mark.asyncio
async def test_network_size_and_refused_ranges(env):
    with pytest.raises(PrivateNetworkError, match="/29"):
        await _network(env, cidr="10.1.0.0/30")
    for cidr in ("127.0.0.0/24", "169.254.1.0/24", "0.0.0.0/24", "224.0.0.0/24"):
        with pytest.raises(PrivateNetworkError):
            await _network(env, cidr=cidr)
    env.pool.reserved_cidrs = pns.parse_reserved_cidrs(["10.50.0.0/16", "172.20.0.0/24"])
    env.db.commit()
    with pytest.raises(PrivateNetworkError) as refused:
        await _network(env, cidr="10.50.3.0/24")
    assert refused.value.message == "This address range is not available, please choose another"
    assert "10.50" not in refused.value.message
    ok = await _network(env, cidr="10.51.3.0/24")
    assert ok.cidr == "10.51.3.0/24"
    with pytest.raises(PrivateNetworkError):
        pns.parse_reserved_cidrs(["nonsense"])


# --- nodes ---


def test_node_key_is_hashed_and_rotates(env):
    node, key, _ = _node(env)
    assert key.startswith("srk_") and node.api_key_hash != key
    assert StorageDAO.get_node_by_key(env.db, key).id == node.id
    _, new_key = StorageNodeService.rotate_key(env.db, node.id)
    assert StorageDAO.get_node_by_key(env.db, key) is None
    assert StorageDAO.get_node_by_key(env.db, new_key).id == node.id


def test_node_ports_must_belong_to_its_server(env):
    other, other_ports = _server(env, "other")
    with pytest.raises(StorageError, match="do not belong"):
        _node(env, vlan_port_ids=[other_ports[0].id])
    with pytest.raises(StorageError, match="vlan_mode"):
        _node(env, "store-02", vlan_mode="magic")


def test_vlan_port_preview_flags_switches_without_vlan_management(env):
    node, _, ports = _node(env, plugin="snmpv3", ports=("Ethernet1/1", "Ethernet1/2"))
    preview = preview_uplink_ports(env.db, node.server_id)
    assert preview["ok"] is False and preview["vlan_mode"] == "switch"
    assert [p["server_port"] for p in preview["ports"]] == ["eth0"]
    assert "no VLAN management" in preview["ports"][0]["problem"]

    StorageNodeService.update(env.db, node.id, vlan_port_ids=[ports[1].id])
    preview = preview_uplink_ports(env.db, node.server_id)
    assert [p["switch_port"] for p in preview["ports"]] == ["Ethernet1/2"]

    StorageNodeService.update(env.db, node.id, vlan_mode="preconfigured")
    preview = preview_uplink_ports(env.db, node.server_id)
    assert preview["ok"] is True and preview["vlan_mode"] == "preconfigured"


# --- placement and attachments ---


@pytest.mark.asyncio
async def test_link_places_on_least_loaded_node_and_trunks_its_vlan_ports(env):
    busy, _, _ = _node(env, "store-01")
    quiet, _, quiet_ports = _node(env, "store-02", ports=("Ethernet1/1", "Ethernet1/9"))
    StorageNodeService.update(env.db, quiet.id, vlan_port_ids=[quiet_ports[1].id])
    first_net = await _network(env, "a")
    await StorageService.link_network(env.db, _storage(env, "s1"), first_net.id, node_id=busy.id)

    network = await _network(env, "b")
    service = _storage(env, "s2")
    tenant = await StorageService.link_network(env.db, service, network.id)

    assert tenant.node_id == quiet.id
    assert tenant.endpoint_ip == "10.0.0.254" and tenant.vlan == network.segment_id
    assert service.status == ServiceStatus.ACTIVE
    attachment = PrivateNetworkDAO.attachments_for_service(env.db, service.id)[0]
    assert attachment.server_id == quiet.server_id and attachment.ip_address == "10.0.0.254"
    assert attachment.state == AttachmentState.ACTIVE
    # Only the chosen VLAN port is trunked, not the PXE port.
    assert env.switches["store-02-tor"].tagged == {"Ethernet1/9": {network.segment_id}}


@pytest.mark.asyncio
async def test_storage_attachment_uses_the_attachment_server_in_apply_remove_and_reconcile(env):
    node, _, _ = _node(env)
    network = await _network(env)
    service = _storage(env)
    await StorageService.link_network(env.db, service, network.id)
    switch = env.switches["store-01-tor"]
    assert switch.tagged["Ethernet1/1"] == {network.segment_id}

    switch.tagged["Ethernet1/1"].clear()
    counts = await PrivateNetworkService.reconcile(env.db)
    assert counts["applied"] >= 1 and counts["failed"] == 0
    assert switch.tagged["Ethernet1/1"] == {network.segment_id}

    service.status = ServiceStatus.TERMINATED
    env.db.commit()
    StorageService.mark_terminated(env.db, service.id)
    counts = await PrivateNetworkService.reconcile(env.db)
    assert counts["detached"] == 1
    assert switch.tagged["Ethernet1/1"] == set()


@pytest.mark.asyncio
async def test_preconfigured_node_makes_no_switch_calls_and_needs_no_cabling(env):
    node, _, _ = _node(env, plugin="snmpv3", vlan_mode="preconfigured")
    network = await _network(env)
    service = _storage(env)
    tenant = await StorageService.link_network(env.db, service, network.id)
    attachment = PrivateNetworkDAO.attachments_for_service(env.db, service.id)[0]
    assert attachment.state == AttachmentState.ACTIVE and attachment.last_error is None
    assert "store-01-tor" not in env.switches

    await PrivateNetworkService.reconcile(env.db)
    assert "store-01-tor" not in env.switches

    assert tenant_status(env.db, tenant)[0] == "provisioning"
    _runner_ready(env, node, tenant)
    assert tenant_status(env.db, tenant)[0] == "ready"

    await PrivateNetworkService.release_service(env.db, service.id)
    assert "store-01-tor" not in env.switches
    assert PrivateNetworkDAO.attachments_for_service(env.db, service.id) == []


@pytest.mark.asyncio
async def test_switch_mode_on_unmanaged_switch_is_an_error_and_not_ready(env):
    node, _, _ = _node(env, plugin="snmpv3")
    network = await _network(env)
    tenant = await StorageService.link_network(env.db, _storage(env), network.id)
    _runner_ready(env, node, tenant)
    status, detail = tenant_status(env.db, tenant)
    assert status == "error" and "connected" in detail


@pytest.mark.asyncio
async def test_ready_needs_applied_attachment_and_runner_ready(env):
    node, _, _ = _node(env)
    network = await _network(env)
    tenant = await StorageService.link_network(env.db, _storage(env), network.id)
    assert tenant_status(env.db, tenant)[0] == "provisioning"
    _runner_ready(env, node, tenant)
    assert tenant_status(env.db, tenant)[0] == "ready"
    attachment = PrivateNetworkDAO.attachments_for_service(env.db, tenant.service_id)[0]
    attachment.state = AttachmentState.PENDING
    env.db.commit()
    assert tenant_status(env.db, tenant)[0] == "provisioning"


@pytest.mark.asyncio
async def test_link_refusals(env):
    _node(env)
    network = await _network(env)
    foreign = await _network(env, "theirs", owner=env.other)
    service = _storage(env)
    with pytest.raises(StorageError, match="not found"):
        await StorageService.link_network(env.db, service, foreign.id)
    await StorageService.link_network(env.db, service, network.id)
    with pytest.raises(StorageError, match="already linked"):
        await StorageService.link_network(env.db, service, network.id)
    with pytest.raises(StorageError, match="already has storage"):
        await StorageService.link_network(env.db, _storage(env, "second"), network.id)
    # The storage attachment cannot be detached by hand, and the network cannot be deleted.
    attachment = PrivateNetworkDAO.attachments_for_service(env.db, service.id)[0]
    with pytest.raises(PrivateNetworkError, match="terminated"):
        await PrivateNetworkService.detach(env.db, attachment.id)
    with pytest.raises(PrivateNetworkError, match="Storage is linked"):
        await PrivateNetworkService.delete_network(env.db, network.id)


@pytest.mark.asyncio
async def test_no_node_in_location_or_disabled(env):
    node, _, _ = _node(env)
    StorageNodeService.update(env.db, node.id, enabled=False)
    network = await _network(env)
    service = _storage(env)
    with pytest.raises(StorageError, match="No storage node"):
        await StorageService.link_network(env.db, service, network.id)
    env.db.refresh(service)
    assert service.status == ServiceStatus.PENDING
    assert StorageDAO.get_tenant_for_service(env.db, service.id).network_id is None


def test_size_comes_from_request_or_product_and_is_required(env):
    with pytest.raises(ProvisioningError, match="size_gb"):
        _storage(env, size_gb=None)
    service = _storage(env, size_gb=250)
    assert service.bare_metal is None
    assert StorageDAO.get_tenant_for_service(env.db, service.id).size_bytes == 250 * GIB


# --- shares ---


async def _linked(env, size_gb=100):
    node, key, _ = _node(env)
    network = await _network(env)
    service = _storage(env, size_gb=size_gb)
    tenant = await StorageService.link_network(env.db, service, network.id)
    return node, key, network, service, tenant


@pytest.mark.asyncio
async def test_share_quota_and_client_validation(env):
    _, _, _, _, tenant = await _linked(env, size_gb=100)
    share = StorageService.create_share(env.db, tenant, name="data", quota_gb=60, clients=["10.0.0.5", "10.0.0.5"])
    assert share.clients == ["10.0.0.5"] and not share.all_clients
    assert share_clients(tenant, share) == ["10.0.0.5"]
    with pytest.raises(StorageError, match="Not enough"):
        StorageService.create_share(env.db, tenant, name="more", quota_gb=41)
    with pytest.raises(StorageError, match="already exists"):
        StorageService.create_share(env.db, tenant, name="data", quota_gb=1)
    for bad_name in ("Data", "1data", "a" * 33, "da ta", "../x"):
        with pytest.raises(StorageError, match="Share name"):
            StorageService.create_share(env.db, tenant, name=bad_name, quota_gb=1)
    for bad_ip in ("10.0.1.5", "10.0.0.254", "10.0.0.0", "10.0.0.255", "nope"):
        with pytest.raises(StorageError):
            StorageService.create_share(env.db, tenant, name="x", quota_gb=1, clients=[bad_ip])
    more = StorageService.create_share(env.db, tenant, name="more", quota_gb=40)
    assert more.all_clients and share_clients(tenant, more) == ["10.0.0.0/24"]

    share.used_bytes = 30 * GIB + 1
    env.db.commit()
    with pytest.raises(StorageError, match="at least 31 GB"):
        StorageService.update_share(env.db, tenant, share.id, quota_gb=30)
    StorageService.update_share(env.db, tenant, share.id, quota_gb=31, clients=["10.0.0.9"], read_only=True)
    assert share.quota_bytes == 31 * GIB and share.clients == ["10.0.0.9"] and share.read_only

    with pytest.raises(StorageError, match="shrink or delete shares"):
        StorageService.resize(env.db, tenant.service, 70)
    StorageService.resize(env.db, tenant.service, 71)
    assert tenant.size_bytes == 71 * GIB


@pytest.mark.asyncio
async def test_delete_share_keeps_it_for_the_grace_period(env, monkeypatch):
    monkeypatch.setattr(settings, "storage_destroy_grace_seconds", 3600)
    _, _, _, _, tenant = await _linked(env)
    share = StorageService.create_share(env.db, tenant, name="data", quota_gb=100)
    before = datetime.now(timezone.utc)
    StorageService.delete_share(env.db, tenant, share.id)
    assert share.state == ShareState.DELETING
    destroy_after = share.destroy_after.replace(tzinfo=timezone.utc)
    assert before + timedelta(minutes=59) < destroy_after <= datetime.now(timezone.utc) + timedelta(hours=1)
    # Its quota is free again, but the name stays taken until it is destroyed.
    StorageService.create_share(env.db, tenant, name="fresh", quota_gb=100)
    with pytest.raises(StorageError, match="being deleted"):
        StorageService.create_share(env.db, tenant, name="data", quota_gb=1)


@pytest.mark.asyncio
async def test_share_writes_refused_when_suspended_or_unlinked(env):
    _, _, _, service, tenant = await _linked(env)
    service.status = ServiceStatus.SUSPENDED
    env.db.commit()
    with pytest.raises(StorageError, match="suspended"):
        StorageService.create_share(env.db, tenant, name="data", quota_gb=1)
    unlinked = StorageDAO.get_tenant_for_service(env.db, _storage(env, "pending").id)
    with pytest.raises(StorageError, match="Link the storage"):
        StorageService.create_share(env.db, unlinked, name="data", quota_gb=1)


# --- lifecycle and runner ---


@pytest.mark.asyncio
async def test_config_lists_only_this_nodes_tenants_and_follows_the_lifecycle(env, monkeypatch):
    monkeypatch.setattr(settings, "storage_destroy_grace_seconds", 7 * 24 * 3600)
    node, _, network, service, tenant = await _linked(env)
    other_node, _, _ = _node(env, "store-02")
    other_net = await _network(env, "other")
    other_tenant = await StorageService.link_network(env.db, _storage(env, "other"), other_net.id, node_id=other_node.id)
    StorageService.create_share(env.db, tenant, name="data", quota_gb=10, clients=["10.0.0.5"])
    StorageService.create_share(env.db, tenant, name="all", quota_gb=10)

    config = build_runner_config(env.db, node)
    assert [t["id"] for t in config["tenants"]] == [tenant.id]
    assert other_tenant.id not in [t["id"] for t in config["tenants"]]
    entry = config["tenants"][0]
    assert entry["state"] == "active" and entry["vlan"] == network.segment_id
    assert entry["endpoint_ip"] == "10.0.0.254" and entry["prefix_len"] == 24
    assert entry["size_bytes"] == 100 * GIB and entry["limits"] is None
    assert [s["name"] for s in entry["shares"]] == ["data", "all"]
    assert entry["shares"][0]["clients"] == ["10.0.0.5"]
    assert entry["shares"][1]["clients"] == ["10.0.0.0/24"]
    assert config["settings"]["destroy_grace_seconds"] == 7 * 24 * 3600
    assert config["version"] == build_runner_config(env.db, node)["version"]
    assert "key" not in str(config).lower() and "password" not in str(config).lower()

    service.status = ServiceStatus.SUSPENDED
    env.db.commit()
    suspended = build_runner_config(env.db, node)
    assert suspended["tenants"][0]["state"] == "suspended"
    assert suspended["version"] != config["version"]

    service.status = ServiceStatus.TERMINATED
    env.db.commit()
    StorageService.mark_terminated(env.db, service.id)
    await PrivateNetworkService.release_service(env.db, service.id)
    terminated = build_runner_config(env.db, node)["tenants"][0]
    assert terminated["state"] == "terminated" and terminated["destroy_after"].endswith("Z")
    assert terminated["vlan"] == network.segment_id
    # The network is free for deletion or new storage once the storage is terminated.
    assert StorageDAO.get_tenant_for_network(env.db, network.id) is None
    await PrivateNetworkService.delete_network(env.db, network.id)


@pytest.mark.asyncio
async def test_node_with_undestroyed_tenant_on_the_same_vlan_is_skipped(env):
    node, _, network, service, tenant = await _linked(env)
    service.status = ServiceStatus.TERMINATED
    env.db.commit()
    StorageService.mark_terminated(env.db, service.id)
    await PrivateNetworkService.release_service(env.db, service.id)
    with pytest.raises(StorageError, match="No storage node"):
        await StorageService.link_network(env.db, _storage(env, "again"), network.id)
    apply_runner_status(env.db, node, {"destroyed_tenants": [tenant.id]}, None)
    assert StorageDAO.get_tenant(env.db, tenant.id) is None
    relinked = await StorageService.link_network(env.db, _storage(env, "again2"), network.id)
    assert relinked.node_id == node.id


@pytest.mark.asyncio
async def test_vlan_of_storage_awaiting_destruction_is_not_handed_out(env):
    from app.dao.private_network_dao import PrivateNetworkDAO

    node, _, network, service, tenant = await _linked(env)
    held = network.segment_id
    service.status = ServiceStatus.TERMINATED
    env.db.commit()
    StorageService.mark_terminated(env.db, service.id)
    await PrivateNetworkService.release_service(env.db, service.id)
    await PrivateNetworkService.delete_network(env.db, network.id)
    pool = next(p for p in PrivateNetworkDAO.list_pools(env.db) if p.location_id == env.location.id)

    async def next_network(name):
        pool.last_allocated = held - 1
        env.db.commit()
        return await PrivateNetworkService.create_network(
            env.db, owner_user_id=env.owner.id, location_id=env.location.id, name=name, enforce_limit=False
        )

    replacement = await next_network("next")
    assert replacement.segment_id != held
    linked = await StorageService.link_network(env.db, _storage(env, "again"), replacement.id)
    assert linked.node_id == node.id

    apply_runner_status(env.db, node, {"destroyed_tenants": [tenant.id]}, None)
    assert (await next_network("after")).segment_id == held


@pytest.mark.asyncio
async def test_status_updates_only_this_nodes_rows_and_destroys_only_marked_ones(env):
    """Tenant state comes only from the serving node; destroyed ids count from any node of the location."""
    node, _, _, _, tenant = await _linked(env)
    other_node, _, _ = _node(env, "store-02")
    other_net = await _network(env, "other")
    other_tenant = await StorageService.link_network(env.db, _storage(env, "other"), other_net.id, node_id=other_node.id)
    keep = StorageService.create_share(env.db, tenant, name="keep", quota_gb=10)
    gone = StorageService.create_share(env.db, tenant, name="gone", quota_gb=10)
    foreign = StorageService.create_share(env.db, other_tenant, name="theirs", quota_gb=10)
    StorageService.delete_share(env.db, tenant, gone.id)
    StorageService.delete_share(env.db, other_tenant, foreign.id)
    foreign_id = foreign.id

    apply_runner_status(
        env.db,
        node,
        {
            "version_applied": "abc",
            "runner_version": "0.1.0",
            "node": {"ceph_ok": True, "free_bytes": 123, "error": None},
            "tenants": [
                {"id": tenant.id, "state": "ready", "shares": [{"id": keep.id, "state": "ready", "used_bytes": 77}]},
                {"id": other_tenant.id, "state": "error", "error": "not yours"},
            ],
            "destroyed_tenants": [tenant.id, other_tenant.id],
            "destroyed_shares": [keep.id, gone.id, foreign_id],
            "orphans": ["subvol-x"],
        },
        "10.0.9.9",
    )
    env.db.expire_all()
    assert (node.runner_version, node.ceph_ok, node.free_bytes, node.orphans) == ("0.1.0", True, 123, ["subvol-x"])
    assert node.last_seen_ip == "10.0.9.9" and node.version_applied == "abc"
    assert tenant.runner_state == "ready" and StorageDAO.get_tenant(env.db, tenant.id) is not None
    assert StorageDAO.get_share(env.db, keep.id).used_bytes == 77
    assert StorageDAO.get_share(env.db, gone.id) is None
    assert StorageDAO.get_share(env.db, foreign_id) is None
    assert other_tenant.runner_state is None


@pytest.mark.asyncio
async def test_terminating_unlinked_storage_removes_its_record(env):
    service = _storage(env)
    StorageService.mark_terminated(env.db, service.id)
    assert StorageDAO.get_tenant_for_service(env.db, service.id) is None


@pytest.mark.asyncio
async def test_reconcile_marks_storage_terminated_elsewhere(env):
    _, _, _, service, tenant = await _linked(env)
    service.status = ServiceStatus.TERMINATED
    env.db.commit()
    assert StorageService.reconcile_terminations(env.db) == 1
    assert tenant.terminated_at is not None and tenant.network_id is None


def test_node_with_storage_cannot_be_deleted(env):
    node, _, _ = _node(env)
    StorageDAO.create_tenant(env.db, service_id=None, node_id=node.id, size_bytes=GIB)
    env.db.commit()
    with pytest.raises(StorageError, match="still placed"):
        StorageNodeService.delete(env.db, node.id)
