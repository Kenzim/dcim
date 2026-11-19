"""Proxy fast path: address plan, subnet pinning, access network lifecycle and runner config."""
import ipaddress

import pytest

from app.dao.ipam_dao import IPAMDAO
from app.dao.private_network_dao import PrivateNetworkDAO
from app.dao.proxy_runner_dao import ProxyRunnerDAO
from app.dao.service_dao import ServiceDAO
from app.models.cable_run import CableRun
from app.models.ipam import IPSubnet
from app.models.location import Location
from app.models.network_port import NetworkPort
from app.models.network_switch import NetworkSwitch
from app.models.private_network import NetworkKind, PrivateNetworkStatus
from app.models.proxy_runner import ProxyRunnerVlanMode
from app.models.server import Server
from app.models.service import ProvisioningSource, ServiceStatus, ServiceType
from app.models.switch_port import SwitchPort
from app.models.user import User
from app.services import private_network_service as pns
from app.services import proxy_fast_path as fp
from app.services.private_network_service import PrivateNetworkError, PrivateNetworkService
from app.services.proxy_fast_path import FastPathError, ProxyAccessService

# Most tests turn the fast path on without setting up the client's server and proxy;
# the eligibility test puts the real check back.
_REAL_ELIGIBILITY = fp.eligibility_problem


class FakeSwitch:
    def __init__(self):
        self.vlans = {}
        self.tagged = {}

    async def create_vlan(self, vlan_id, name):
        self.vlans[vlan_id] = name

    async def delete_vlan(self, vlan_id):
        self.vlans.pop(vlan_id, None)

    async def get_port_vlans(self, port):
        tagged = self.tagged.get(port, set())
        return {"mode": "trunk" if tagged else "access", "allowed_all": False, "allowed_vlans": sorted(tagged)}

    async def add_port_vlan(self, port, vlan_id):
        self.tagged.setdefault(port, set()).add(vlan_id)

    async def remove_port_vlan(self, port, vlan_id):
        self.tagged.get(port, set()).discard(vlan_id)


class FakeProxmox:
    def __init__(self):
        self.nics = {}

    async def attach_private_nic(self, bridge, vlan_tag, *, ip_cidr=None, mac=None, vmid=None):
        self.nics[(vmid, vlan_tag)] = ip_cidr
        return {"changed": True, "index": 1, "mac": "BC:24:11:AA:BB:CC"}

    async def detach_private_nic(self, bridge, vlan_tag, *, vmid=None):
        self.nics.pop((vmid, vlan_tag), None)
        return {"changed": True, "removed": [1]}


def _user(db, username):
    user = User(username=username, email=f"{username}@example.com", is_admin=False)
    user.set_password("secret123")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def env(db_session, monkeypatch):
    location = Location(name="London", description="")
    other = Location(name="Paris", description="")
    db_session.add_all([location, other])
    db_session.commit()
    pool = PrivateNetworkDAO.create_pool(
        db_session,
        location_id=location.id,
        vlan_start=2000,
        vlan_end=2009,
        bridge="vmbr0",
        max_networks_per_client=1,
        max_networks_per_service=4,
    )
    pool.proxy_access_enabled = True
    db_session.commit()
    monkeypatch.setattr(fp, "eligibility_problem", lambda *args, **kwargs: None)
    switches = {}
    proxmox = FakeProxmox()
    monkeypatch.setattr(pns, "_switch_plugin", lambda switch: switches.setdefault(switch.name, FakeSwitch()))

    async def resolve(db, service):
        return proxmox, None, "epyc", service.vm.proxmox_vmid

    monkeypatch.setattr(pns, "resolve_proxmox_plugin_for_service", resolve)

    class Env:
        pass

    e = Env()
    e.db, e.location, e.other, e.pool, e.switches, e.proxmox = db_session, location, other, pool, switches, proxmox
    e.alice, e.bob = _user(db_session, "alice"), _user(db_session, "bob")
    return e


def _runner(env, name, *, location=None, **fields):
    row, _ = ProxyRunnerDAO.create(env.db, name=name, api_key=f"key-{name}")
    row.location_id = location.id if location is not None else None
    for key, value in fields.items():
        setattr(row, key, value)
    env.db.commit()
    return row


def _subnet(env, name, cidr, *, max_resale=1):
    return IPAMDAO.create_subnet(env.db, name=name, cidr=cidr, location_id=env.location.id, max_resale_count=max_resale)


def _proxy(env, owner, name, subnet, *, username, password="pw-" + "x" * 8, status=ServiceStatus.ACTIVE):
    server = Server(name=f"{name}-srv", server_ip="10.70.0.1", location_id=env.location.id, plugin_name="ipmi", plugin_config={})
    env.db.add(server)
    env.db.commit()
    service = ServiceDAO.create_bare_metal(
        env.db,
        name=name,
        server_id=server.id,
        owner_user_id=owner.id,
        service_type=ServiceType.HTTP_PROXY,
        status=status,
        provisioning_source=ProvisioningSource.INTERNAL,
    )
    assignment = IPAMDAO.assign_ip(env.db, service.id, subnet_id=subnet.id, username=username, password=password)
    return service, assignment


def _cabled_server(env, name, port_name="Ethernet1/9"):
    switch = NetworkSwitch(name=f"{name}-tor", location_id=env.location.id, plugin_name="nxos", plugin_config={})
    server = Server(name=name, server_ip="10.88.0.9", location_id=env.location.id, plugin_name="ipmi", plugin_config={})
    env.db.add_all([switch, server])
    env.db.flush()
    port = NetworkPort(server_id=server.id, name="eth0", speed_mbps=10000, pxe_boot=True)
    switch_port = SwitchPort(switch_id=switch.id, name=port_name)
    env.db.add_all([port, switch_port])
    env.db.flush()
    env.db.add(CableRun(end_a_server_port_id=port.id, end_b_switch_port_id=switch_port.id))
    env.db.commit()
    return server, switch


# --- address plan ---


def test_access_range_is_reserved_and_split():
    assert str(pns.proxy_access_network()) == "100.80.0.0/16"
    assert str(pns.proxy_subnet_ip_block()) == "100.80.0.0/20"
    with pytest.raises(PrivateNetworkError):
        pns.parse_cidr("100.80.4.0/24")


@pytest.mark.asyncio
async def test_client_cannot_create_a_standard_network_in_the_access_range(env):
    with pytest.raises(PrivateNetworkError):
        await PrivateNetworkService.create_network(
            env.db, owner_user_id=env.alice.id, location_id=env.location.id, name="x", cidr="100.80.0.0/16"
        )


# --- pinning ---


def test_pin_allocates_lowest_free_ip_and_keeps_it(env):
    runner = _runner(env, "r1", location=env.location)
    other = _runner(env, "r2", location=env.location)
    first = fp.pin_subnet(env.db, _subnet(env, "a", "203.0.113.0/29"), runner.id)
    second = fp.pin_subnet(env.db, _subnet(env, "b", "203.0.113.8/29"), runner.id)
    assert (first.fast_path_ip, second.fast_path_ip) == ("100.80.0.1", "100.80.0.2")

    fp.pin_subnet(env.db, first, None)
    assert first.proxy_runner_id is None and first.fast_path_ip == "100.80.0.1"
    moved = fp.pin_subnet(env.db, first, other.id)
    assert (moved.proxy_runner_id, moved.fast_path_ip) == (other.id, "100.80.0.1")
    third = fp.pin_subnet(env.db, _subnet(env, "c", "203.0.113.16/29"), runner.id)
    assert third.fast_path_ip == "100.80.0.3"


def test_pin_needs_a_runner_with_a_location(env):
    nowhere = _runner(env, "r0")
    subnet = _subnet(env, "a", "203.0.113.0/29")
    with pytest.raises(FastPathError) as exc:
        fp.pin_subnet(env.db, subnet, nowhere.id)
    assert exc.value.status_code == 409
    with pytest.raises(FastPathError) as exc:
        fp.pin_subnet(env.db, subnet, 9999)
    assert exc.value.status_code == 404
    assert subnet.fast_path_ip is None


def test_subnet_ips_never_leave_the_reserved_block(env):
    runner = _runner(env, "r1", location=env.location)
    block = pns.proxy_subnet_ip_block()
    for index in range(5):
        subnet = fp.pin_subnet(env.db, _subnet(env, f"s{index}", f"198.51.100.{index * 8}/29"), runner.id)
        assert ipaddress.ip_address(subnet.fast_path_ip) in block


# --- access network lifecycle ---


@pytest.mark.asyncio
async def test_enable_creates_access_network_joined_by_location_runners(env):
    here = _runner(env, "here", location=env.location)
    _runner(env, "elsewhere", location=env.other)
    _runner(env, "unplaced")
    # The client's one standard network does not block the fast path, and the fast path does not use it up.
    await PrivateNetworkService.create_network(
        env.db, owner_user_id=env.alice.id, location_id=env.location.id, name="lan"
    )
    network = await ProxyAccessService.enable(env.db, owner_user_id=env.alice.id, location_id=env.location.id)

    assert network.kind == NetworkKind.PROXY_ACCESS
    assert network.cidr == "100.80.0.0/16"
    assert [a.proxy_runner_id for a in network.attachments] == [here.id]
    assert network.attachments[0].service_id is None
    again = await ProxyAccessService.enable(env.db, owner_user_id=env.alice.id, location_id=env.location.id)
    assert again.id == network.id


@pytest.mark.asyncio
async def test_enable_refused_where_not_offered(env):
    with pytest.raises(FastPathError) as exc:
        await ProxyAccessService.enable(env.db, owner_user_id=env.alice.id, location_id=env.other.id)
    assert exc.value.status_code == 409
    env.pool.proxy_access_enabled = False
    env.db.commit()
    with pytest.raises(FastPathError):
        await ProxyAccessService.enable(env.db, owner_user_id=env.alice.id, location_id=env.location.id)


def _dedicated(env, owner, location, name, *, status=ServiceStatus.ACTIVE):
    server = Server(name=name, server_ip="10.71.0.1", location_id=location.id, plugin_name="ipmi", plugin_config={})
    env.db.add(server)
    env.db.commit()
    return ServiceDAO.create_bare_metal(
        env.db,
        name=name,
        server_id=server.id,
        owner_user_id=owner.id,
        service_type=ServiceType.BARE_METAL,
        status=status,
        provisioning_source=ProvisioningSource.INTERNAL,
    )


async def _refused(env, user):
    with pytest.raises(FastPathError) as exc:
        await ProxyAccessService.enable(env.db, owner_user_id=user.id, location_id=env.location.id)
    assert exc.value.status_code == 409
    return exc.value.message


@pytest.mark.asyncio
async def test_fast_path_needs_a_server_and_a_fast_path_proxy_in_the_location(env, monkeypatch):
    monkeypatch.setattr(fp, "eligibility_problem", _REAL_ELIGIBILITY)
    runner = _runner(env, "here", location=env.location)
    assert "an active server and an active proxy" in await _refused(env, env.alice)

    # A proxy whose subnet is not served over the fast path does not count.
    _proxy(env, env.alice, "px-plain", _subnet(env, "plain", "198.51.100.0/29"), username="alice0")
    _dedicated(env, env.alice, env.location, "alice-bm")
    assert await _refused(env, env.alice) == (
        "The proxy fast path needs an active proxy served over the fast path in this location"
    )

    subnet = fp.pin_subnet(env.db, _subnet(env, "a", "203.0.113.0/29"), runner.id)
    _proxy(env, env.alice, "px", subnet, username="alice1")
    # Bob has a proxy, but his servers are elsewhere or suspended, and a proxy is not a server.
    _proxy(env, env.bob, "px-bob", subnet, username="bob1")
    _dedicated(env, env.bob, env.other, "bob-paris")
    _dedicated(env, env.bob, env.location, "bob-held", status=ServiceStatus.SUSPENDED)
    assert await _refused(env, env.bob) == "The proxy fast path needs an active server in this location"

    view = ProxyAccessService.overview(env.db, env.bob.id)
    assert [(loc["eligible"], loc["reason"]) for loc in view["locations"]] == [
        (False, "The proxy fast path needs an active server in this location")
    ]
    network = await ProxyAccessService.enable(env.db, owner_user_id=env.alice.id, location_id=env.location.id)
    assert network.kind == NetworkKind.PROXY_ACCESS
    assert ProxyAccessService.overview(env.db, env.alice.id)["locations"][0]["eligible"] is True

    staff = await ProxyAccessService.enable(
        env.db, owner_user_id=env.bob.id, location_id=env.location.id, check_eligibility=False
    )
    assert staff.kind == NetworkKind.PROXY_ACCESS


@pytest.mark.asyncio
async def test_client_servers_get_addresses_outside_the_proxy_block(env):
    cluster_vm = await _vm(env, env.alice, "vm-a", 201)
    network = await ProxyAccessService.enable(env.db, owner_user_id=env.alice.id, location_id=env.location.id)
    attachment = await PrivateNetworkService.attach(env.db, network.id, cluster_vm.id)
    assert attachment.ip_address == "100.80.16.1"
    assert env.proxmox.nics[(201, network.segment_id)] == "100.80.16.1/16"

    other_vm = await _vm(env, env.alice, "vm-b", 202)
    with pytest.raises(PrivateNetworkError):
        await PrivateNetworkService.attach(env.db, network.id, other_vm.id, ip_address="100.80.0.1")
    second = await PrivateNetworkService.attach(env.db, network.id, other_vm.id, ip_address="100.80.200.7")
    assert second.ip_address == "100.80.200.7"


async def _vm(env, owner, name, vmid):
    from app.dao.proxmox_inventory_dao import ProxmoxInventoryDAO

    cluster = ProxmoxInventoryDAO.create_cluster(
        env.db, name=f"{name}-cluster", api_url="https://pve.example:8006", username="root@pam", password="x",
        location_id=env.location.id,
    )
    return ServiceDAO.create_vm(
        env.db,
        name=name,
        owner_user_id=owner.id,
        provisioning_source=ProvisioningSource.INTERNAL,
        status=ServiceStatus.ACTIVE,
        proxmox_cluster_id=cluster.id,
        proxmox_node_name="epyc",
        proxmox_vmid=vmid,
    )


@pytest.mark.asyncio
async def test_disable_refused_with_servers_then_removes_runners_and_network(env):
    _runner(env, "here", location=env.location)
    vm = await _vm(env, env.alice, "vm-a", 201)
    network = await ProxyAccessService.enable(env.db, owner_user_id=env.alice.id, location_id=env.location.id)
    attachment = await PrivateNetworkService.attach(env.db, network.id, vm.id)
    with pytest.raises(FastPathError) as exc:
        await ProxyAccessService.disable(env.db, owner_user_id=env.alice.id, location_id=env.location.id)
    assert exc.value.status_code == 409

    await PrivateNetworkService.detach(env.db, attachment.id)
    await ProxyAccessService.disable(env.db, owner_user_id=env.alice.id, location_id=env.location.id)
    env.db.expire_all()
    assert PrivateNetworkDAO.get_access_network(env.db, env.alice.id, env.location.id) is None
    assert PrivateNetworkDAO.list_attachments(env.db) == []


@pytest.mark.asyncio
async def test_storage_cannot_link_to_an_access_network(env):
    network = await ProxyAccessService.enable(env.db, owner_user_id=env.alice.id, location_id=env.location.id)
    with pytest.raises(PrivateNetworkError) as exc:
        await PrivateNetworkService.attach_storage(env.db, network.id, 1, server_id=1)
    assert "proxy access" in exc.value.message


@pytest.mark.asyncio
async def test_switch_mode_runner_gets_vlan_trunked_and_untrunked(env):
    server, switch = _cabled_server(env, "runner-host")
    runner = _runner(env, "sw", location=env.location, server_id=server.id, vlan_mode=ProxyRunnerVlanMode.SWITCH)
    network = await ProxyAccessService.enable(env.db, owner_user_id=env.alice.id, location_id=env.location.id)
    fake = env.switches[switch.name]
    assert network.segment_id in fake.tagged["Ethernet1/9"]

    runner.vlan_mode = ProxyRunnerVlanMode.PRECONFIGURED
    env.db.commit()
    await ProxyAccessService.sync_runner(env.db, runner.id)
    assert network.segment_id not in fake.tagged["Ethernet1/9"]


@pytest.mark.asyncio
async def test_runner_moves_join_and_leave_and_reconcile_adds_new_runners(env):
    network = await ProxyAccessService.enable(env.db, owner_user_id=env.alice.id, location_id=env.location.id)
    late = _runner(env, "late", location=env.location)
    assert await ProxyAccessService.reconcile(env.db) == 1
    env.db.expire_all()
    assert [a.proxy_runner_id for a in PrivateNetworkDAO.get_network(env.db, network.id).attachments] == [late.id]

    late.location_id = env.other.id
    env.db.commit()
    await ProxyAccessService.sync_runner(env.db, late.id)
    env.db.expire_all()
    assert PrivateNetworkDAO.get_network(env.db, network.id).attachments == []


@pytest.mark.asyncio
async def test_release_runner_unpins_subnets_and_leaves_networks(env):
    runner = _runner(env, "gone", location=env.location)
    subnet = fp.pin_subnet(env.db, _subnet(env, "a", "203.0.113.0/29"), runner.id)
    await ProxyAccessService.enable(env.db, owner_user_id=env.alice.id, location_id=env.location.id)
    await ProxyAccessService.release_runner(env.db, runner.id)
    env.db.expire_all()
    assert env.db.get(IPSubnet, subnet.id).proxy_runner_id is None
    assert env.db.get(IPSubnet, subnet.id).fast_path_ip == "100.80.0.1"
    assert PrivateNetworkDAO.list_attachments(env.db) == []


# --- runner config ---


@pytest.mark.asyncio
async def test_runner_config_has_one_tenant_per_client_with_only_their_assignments(env):
    r1 = _runner(env, "r1", location=env.location)
    r2 = _runner(env, "r2", location=env.location)
    shared = fp.pin_subnet(env.db, _subnet(env, "shared", "203.0.113.0/29", max_resale=2), r1.id)
    elsewhere = fp.pin_subnet(env.db, _subnet(env, "r2-only", "198.51.100.0/29"), r2.id)
    _subnet(env, "unpinned", "192.0.2.0/29")

    alice_px, alice_a = _proxy(env, env.alice, "alice-px", shared, username="alice1")
    _, alice_b = _proxy(env, env.alice, "alice-px2", elsewhere, username="alice2")
    _, bob_a = _proxy(env, env.bob, "bob-px", shared, username="bob1")
    _proxy(env, env.bob, "bob-old", shared, username="bob2", status=ServiceStatus.SUSPENDED)

    alice_net = await ProxyAccessService.enable(env.db, owner_user_id=env.alice.id, location_id=env.location.id)
    bob_net = await ProxyAccessService.enable(env.db, owner_user_id=env.bob.id, location_id=env.location.id)

    config = ProxyAccessService.runner_config(env.db, r1)
    assert config["cidr"] == "100.80.0.0/16" and config["port"] == 11080
    tenants = {t["network_id"]: t for t in config["tenants"]}
    assert set(tenants) == {alice_net.id, bob_net.id}
    alice_t, bob_t = tenants[alice_net.id], tenants[bob_net.id]
    assert alice_t["vlan"] == alice_net.segment_id and bob_t["vlan"] == bob_net.segment_id
    assert alice_t["addresses"] == bob_t["addresses"] == [shared.fast_path_ip]
    assert [a["username"] for a in alice_t["assignments"]] == ["alice1"]
    assert [a["username"] for a in bob_t["assignments"]] == ["bob1"]
    assert alice_t["assignments"][0] == {
        "service_id": alice_px.id,
        "private_ip": shared.fast_path_ip,
        "bind_ip": alice_a.ip.ip_address,
        "username": "alice1",
        "password": alice_a.password,
    }
    assert bob_a.ip.ip_address == bob_t["assignments"][0]["bind_ip"]

    on_r2 = {t["network_id"]: t for t in ProxyAccessService.runner_config(env.db, r2)["tenants"]}
    assert on_r2[alice_net.id]["addresses"] == [elsewhere.fast_path_ip]
    assert [a["bind_ip"] for a in on_r2[alice_net.id]["assignments"]] == [alice_b.ip.ip_address]
    assert on_r2[bob_net.id] == {"network_id": bob_net.id, "vlan": bob_net.segment_id, "addresses": [], "assignments": []}


@pytest.mark.asyncio
async def test_runner_config_absent_without_location_or_offer(env):
    assert ProxyAccessService.runner_config(env.db, _runner(env, "unplaced")) is None
    assert ProxyAccessService.runner_config(env.db, _runner(env, "paris", location=env.other)) is None
    london = _runner(env, "london", location=env.location)
    assert ProxyAccessService.runner_config(env.db, london)["tenants"] == []
    env.pool.proxy_access_enabled = False
    env.db.commit()
    assert ProxyAccessService.runner_config(env.db, london) is None


@pytest.mark.asyncio
async def test_deleting_network_drops_tenant(env):
    runner = _runner(env, "r1", location=env.location)
    await ProxyAccessService.enable(env.db, owner_user_id=env.alice.id, location_id=env.location.id)
    assert len(ProxyAccessService.runner_config(env.db, runner)["tenants"]) == 1
    await ProxyAccessService.disable(env.db, owner_user_id=env.alice.id, location_id=env.location.id)
    env.db.expire_all()
    assert ProxyAccessService.runner_config(env.db, runner)["tenants"] == []


@pytest.mark.asyncio
async def test_overview_lists_fast_path_endpoints(env):
    runner = _runner(env, "r1", location=env.location)
    subnet = fp.pin_subnet(env.db, _subnet(env, "a", "203.0.113.0/29"), runner.id)
    _proxy(env, env.alice, "alice-px", subnet, username="alice1")
    _proxy(env, env.bob, "bob-px", _subnet(env, "b", "198.51.100.0/29"), username="bob1")

    before = ProxyAccessService.overview(env.db, env.alice.id)
    assert [loc["location_id"] for loc in before["locations"]] == [env.location.id]
    assert before["locations"][0]["enabled"] is False
    assert [(e["fast_path_ip"], e["port"], e["active"]) for e in before["endpoints"]] == [("100.80.0.1", 11080, False)]

    await ProxyAccessService.enable(env.db, owner_user_id=env.alice.id, location_id=env.location.id)
    after = ProxyAccessService.overview(env.db, env.alice.id)
    assert after["locations"][0]["enabled"] is True and after["endpoints"][0]["active"] is True
    assert ProxyAccessService.overview(env.db, env.bob.id)["endpoints"] == []
    assert PrivateNetworkDAO.get_access_network(env.db, env.bob.id, env.location.id) is None
    assert all(n.status == PrivateNetworkStatus.ACTIVE for n in PrivateNetworkDAO.list_access_networks(env.db))
