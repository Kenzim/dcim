"""Private network service: allocation, VM and dedicated attach, detach and repair."""
from unittest.mock import AsyncMock, patch

import pytest

from app.dao.private_network_dao import PrivateNetworkDAO
from app.dao.proxmox_inventory_dao import ProxmoxInventoryDAO
from app.dao.service_dao import ServiceDAO
from app.models.cable_run import CableRun
from app.models.location import Location
from app.models.network_port import NetworkPort
from app.models.network_switch import NetworkSwitch
from app.models.private_network import AttachmentState, PrivateNetworkStatus
from app.models.server import Server
from app.models.server_group import ServerGroup
from app.models.service import ProvisioningSource, ServiceStatus
from app.models.switch_port import SwitchPort
from app.models.user import User
from app.services import private_network_service as pns
from app.services.private_network_service import PrivateNetworkError, PrivateNetworkService
from app.services.provisioning import bare_metal
from app.services.provisioning.errors import ProvisioningError
from app.services.proxmox_placement import ProxmoxPlacementError

PUBLIC_VLAN = 100


class FakeSwitch:
    """Stands in for a VLAN-capable switch plugin and remembers its state."""

    def __init__(self):
        self.vlans = {}
        self.tagged = {}
        self.fail = False

    def _check(self):
        if self.fail:
            raise RuntimeError("switch unreachable")

    async def create_vlan(self, vlan_id, name):
        self._check()
        self.vlans[vlan_id] = name

    async def delete_vlan(self, vlan_id, name=None):
        self._check()
        self.vlans.pop(vlan_id, None)

    async def get_port_vlans(self, port):
        self._check()
        tagged = self.tagged.get(port, set())
        return {
            "interface": port,
            "mode": "trunk" if tagged else "access",
            "native_vlan": PUBLIC_VLAN,
            "allowed_all": False,
            "allowed_vlans": sorted(tagged | {PUBLIC_VLAN}),
        }

    async def add_port_vlan(self, port, vlan_id):
        self._check()
        self.tagged.setdefault(port, set()).add(vlan_id)

    async def remove_port_vlan(self, port, vlan_id):
        self._check()
        self.tagged.get(port, set()).discard(vlan_id)


class FakeProxmox:
    def __init__(self):
        self.nics = {}
        self.fail = False
        self.attach_calls = []

    async def attach_private_nic(self, bridge, vlan_tag, *, ip_cidr=None, mac=None, vmid=None):
        if self.fail:
            raise RuntimeError("hypervisor unreachable")
        self.attach_calls.append({"bridge": bridge, "tag": vlan_tag, "ip_cidr": ip_cidr, "mac": mac, "vmid": vmid})
        self.nics[(vmid, vlan_tag)] = bridge
        return {"changed": True, "index": 1, "mac": mac or "BC:24:11:AA:BB:CC"}

    async def detach_private_nic(self, bridge, vlan_tag, *, vmid=None):
        if self.fail:
            raise RuntimeError("hypervisor unreachable")
        self.nics.pop((vmid, vlan_tag), None)
        return {"changed": True, "removed": [1]}


@pytest.fixture
def env(db_session, monkeypatch):
    """One location with a pool, a client, fake switches and a fake hypervisor."""
    location = Location(name="London", description="")
    db_session.add(location)
    db_session.commit()
    PrivateNetworkDAO.create_pool(
        db_session,
        location_id=location.id,
        vlan_start=2000,
        vlan_end=2003,
        bridge="vmbr0",
        max_networks_per_client=3,
        max_networks_per_service=2,
    )
    owner = _user(db_session, "pn-owner")

    switches = {}
    proxmox = FakeProxmox()
    monkeypatch.setattr(pns, "_switch_plugin", lambda switch: switches.setdefault(switch.name, FakeSwitch()))

    async def resolve(db, service):
        return proxmox, None, "epyc", service.vm.proxmox_vmid

    monkeypatch.setattr(pns, "resolve_proxmox_plugin_for_service", resolve)

    class Env:
        pass

    e = Env()
    e.db, e.location, e.owner, e.switches, e.proxmox = db_session, location, owner, switches, proxmox
    return e


def _user(db, username):
    user = User(username=username, email=f"{username}@example.com", is_admin=False)
    user.set_password("secret123")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _vm(env, name="vm-1", *, owner=None, location=None, vmid=101):
    cluster = ProxmoxInventoryDAO.create_cluster(
        env.db, name=f"{name}-cluster", api_url="https://pve.example:8006", username="root@pam", password="x"
    )
    cluster.location_id = (location or env.location).id
    env.db.commit()
    return ServiceDAO.create_vm(
        env.db,
        name=name,
        owner_user_id=(owner or env.owner).id,
        provisioning_source=ProvisioningSource.INTERNAL,
        status=ServiceStatus.ACTIVE,
        proxmox_cluster_id=cluster.id,
        proxmox_node_name="epyc",
        proxmox_vmid=vmid,
    )


def _switch(env, name="tor-1"):
    switch = NetworkSwitch(name=name, location_id=env.location.id, plugin_name="nxos", plugin_config={})
    env.db.add(switch)
    env.db.commit()
    return switch


def _server(env, name="dedi-1", *, switch=None, ports=("Ethernet1/5",), lag=None, cabled=True):
    """A server whose first port is the PXE port; each port is cabled to ``switch``."""
    switch = switch or _switch(env, f"{name}-tor")
    server = Server(
        name=name, server_ip="10.88.0.1", location_id=env.location.id, plugin_name="ipmi", plugin_config={}
    )
    env.db.add(server)
    env.db.flush()
    for index, switch_port_name in enumerate(ports):
        port = NetworkPort(
            server_id=server.id, name=f"eth{index}", speed_mbps=10000, pxe_boot=index == 0, lag_group=lag
        )
        switch_port = SwitchPort(switch_id=switch.id, name=switch_port_name)
        env.db.add_all([port, switch_port])
        env.db.flush()
        if cabled:
            env.db.add(CableRun(end_a_server_port_id=port.id, end_b_switch_port_id=switch_port.id))
    env.db.commit()
    return server, switch


def _dedicated(env, name="dedi-1", **kwargs):
    server, switch = _server(env, name, **kwargs)
    service = ServiceDAO.create_bare_metal(
        env.db,
        name=f"{name}-svc",
        server_id=server.id,
        owner_user_id=env.owner.id,
        provisioning_source=ProvisioningSource.INTERNAL,
        status=ServiceStatus.ACTIVE,
    )
    return service, server, switch


async def _network(env, name="net", **kwargs):
    return await PrivateNetworkService.create_network(
        env.db, owner_user_id=env.owner.id, location_id=env.location.id, name=name, **kwargs
    )


# --- networks ---


@pytest.mark.asyncio
async def test_create_allocates_next_vlan_and_creates_it_on_managed_switches(env):
    _switch(env, "tor-1")
    snmp_only = NetworkSwitch(name="ipmi-sw", location_id=env.location.id, plugin_name="snmpv3", plugin_config={})
    env.db.add(snmp_only)
    env.db.commit()

    first = await _network(env, "one")
    second = await _network(env, "two", cidr="192.168.5.0/24")

    assert (first.segment_id, second.segment_id) == (2000, 2001)
    assert first.cidr == "10.0.0.0/24" and second.cidr == "192.168.5.0/24"
    assert first.switch_synced is True and first.last_error is None
    assert env.switches["tor-1"].vlans == {2000: f"pn-{first.id}", 2001: f"pn-{second.id}"}
    assert "ipmi-sw" not in env.switches


@pytest.mark.asyncio
async def test_released_vlan_is_not_reused_until_the_range_wraps(env):
    first = await _network(env, "one")
    await PrivateNetworkService.delete_network(env.db, first.id)

    second = await _network(env, "two")
    assert second.segment_id == 2001

    await _network(env, "three", enforce_limit=False)
    await _network(env, "four", enforce_limit=False)
    wrapped = await _network(env, "five", enforce_limit=False)
    assert wrapped.segment_id == 2000


@pytest.mark.asyncio
async def test_create_refuses_when_pool_is_missing_disabled_full_or_limit_reached(env):
    other = Location(name="Nowhere", description="")
    env.db.add(other)
    env.db.commit()
    with pytest.raises(PrivateNetworkError, match="not available") as missing:
        await PrivateNetworkService.create_network(
            env.db, owner_user_id=env.owner.id, location_id=other.id, name="x"
        )
    assert missing.value.status_code == 409

    for name in ("a", "b", "c"):
        await _network(env, name)
    with pytest.raises(PrivateNetworkError, match="Limit of 3"):
        await _network(env, "d")

    await _network(env, "d", enforce_limit=False)
    with pytest.raises(PrivateNetworkError, match="capacity"):
        await _network(env, "e", enforce_limit=False)

    pool = PrivateNetworkDAO.get_pool_for_location(env.db, env.location.id)
    pool.enabled = False
    env.db.commit()
    with pytest.raises(PrivateNetworkError, match="not available"):
        await _network(env, "f", enforce_limit=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("cidr", ["8.8.8.0/24", "10.0.0.5/24", "10.0.0.0/8", "10.0.0.0/30", "fd00::/64", "junk"])
async def test_create_rejects_unusable_address_ranges(env, cidr):
    with pytest.raises(PrivateNetworkError):
        await _network(env, cidr=cidr)
    assert PrivateNetworkDAO.list_networks(env.db) == []


@pytest.mark.asyncio
async def test_switch_failure_on_create_is_recorded_and_repaired_later(env):
    _switch(env, "tor-1")
    env.switches["tor-1"] = FakeSwitch()
    env.switches["tor-1"].fail = True

    network = await _network(env)
    assert network.switch_synced is False and "tor-1: switch unreachable" in network.last_error

    env.switches["tor-1"].fail = False
    counts = await PrivateNetworkService.reconcile(env.db)
    env.db.refresh(network)
    assert counts["synced"] == 1 and network.switch_synced is True and network.last_error is None
    assert network.segment_id in env.switches["tor-1"].vlans


@pytest.mark.asyncio
async def test_delete_removes_the_vlan_and_waits_for_unreachable_switches(env):
    _switch(env, "tor-1")
    network = await _network(env)
    network_id, vlan = network.id, network.segment_id
    env.switches["tor-1"].fail = True

    await PrivateNetworkService.delete_network(env.db, network_id)
    kept = PrivateNetworkDAO.get_network(env.db, network_id)
    assert kept.status == PrivateNetworkStatus.DELETING and "switch unreachable" in kept.last_error
    # The VLAN ID stays reserved while a switch may still carry it.
    assert vlan in PrivateNetworkDAO.used_segment_ids(env.db, env.location.id, kept.segmentation_type)

    env.switches["tor-1"].fail = False
    counts = await PrivateNetworkService.reconcile(env.db)
    assert counts["deleted"] == 1
    assert PrivateNetworkDAO.get_network(env.db, network_id) is None
    assert vlan not in env.switches["tor-1"].vlans


@pytest.mark.asyncio
async def test_delete_can_skip_switches_and_refuses_networks_in_use(env):
    _switch(env, "tor-1")
    network = await _network(env)
    service = _vm(env)
    await PrivateNetworkService.attach(env.db, network.id, service.id)
    with pytest.raises(PrivateNetworkError, match="Detach every service") as in_use:
        await PrivateNetworkService.delete_network(env.db, network.id)
    assert in_use.value.status_code == 409

    await PrivateNetworkService.release_service(env.db, service.id)
    env.switches["tor-1"].fail = True
    await PrivateNetworkService.delete_network(env.db, network.id, skip_switches=True)
    assert PrivateNetworkDAO.get_network(env.db, network.id) is None


# --- VM attachments ---


@pytest.mark.asyncio
async def test_attach_vm_adds_tagged_nic_with_first_free_address(env):
    network = await _network(env)
    first = await PrivateNetworkService.attach(env.db, network.id, _vm(env, "vm-1", vmid=101).id)
    second = await PrivateNetworkService.attach(env.db, network.id, _vm(env, "vm-2", vmid=102).id)

    assert (first.ip_address, second.ip_address) == ("10.0.0.1", "10.0.0.2")
    assert first.state == AttachmentState.ACTIVE and first.last_error is None
    assert first.vm_net_index == 1 and first.mac_address == "BC:24:11:AA:BB:CC"
    assert env.proxmox.attach_calls[0] == {
        "bridge": "vmbr0",
        "tag": network.segment_id,
        "ip_cidr": "10.0.0.1/24",
        "mac": None,
        "vmid": 101,
    }


@pytest.mark.asyncio
async def test_attach_accepts_a_chosen_address_and_rejects_bad_ones(env):
    network = await _network(env)
    chosen = await PrivateNetworkService.attach(env.db, network.id, _vm(env, "vm-1").id, ip_address="10.0.0.50")
    assert chosen.ip_address == "10.0.0.50"

    other = _vm(env, "vm-2", vmid=102)
    for bad, code in [("10.0.0.50", 409), ("10.0.1.5", 400), ("10.0.0.0", 400), ("10.0.0.255", 400), ("x", 400)]:
        with pytest.raises(PrivateNetworkError) as exc:
            await PrivateNetworkService.attach(env.db, network.id, other.id, ip_address=bad)
        assert exc.value.status_code == code
    assert len(PrivateNetworkDAO.attachments_for_service(env.db, other.id)) == 0


@pytest.mark.asyncio
async def test_failed_vm_attach_is_kept_as_error_and_retried(env):
    network = await _network(env)
    env.proxmox.fail = True
    attachment = await PrivateNetworkService.attach(env.db, network.id, _vm(env).id)
    assert attachment.state == AttachmentState.ERROR and "hypervisor unreachable" in attachment.last_error

    env.proxmox.fail = False
    counts = await PrivateNetworkService.reconcile(env.db)
    env.db.refresh(attachment)
    assert counts["applied"] == 1
    assert attachment.state == AttachmentState.ACTIVE and attachment.last_error is None
    assert (101, network.segment_id) in env.proxmox.nics


@pytest.mark.asyncio
async def test_reapply_keeps_the_same_mac(env):
    network = await _network(env)
    await PrivateNetworkService.attach(env.db, network.id, _vm(env).id)
    await PrivateNetworkService.reconcile(env.db)
    assert env.proxmox.attach_calls[-1]["mac"] == "BC:24:11:AA:BB:CC"


@pytest.mark.asyncio
async def test_attach_refuses_services_the_network_must_not_join(env):
    network = await _network(env)
    stranger = _user(env.db, "pn-stranger")
    elsewhere = Location(name="Elsewhere", description="")
    env.db.add(elsewhere)
    env.db.commit()

    async def refused(service, code):
        with pytest.raises(PrivateNetworkError) as exc:
            await PrivateNetworkService.attach(env.db, network.id, service.id)
        assert exc.value.status_code == code

    # Another client's service is reported as missing, not as forbidden.
    await refused(_vm(env, "theirs", owner=stranger, vmid=201), 404)
    await refused(_vm(env, "far", location=elsewhere, vmid=202), 409)

    suspended = _vm(env, "suspended", vmid=203)
    suspended.status = ServiceStatus.SUSPENDED
    env.db.commit()
    await refused(suspended, 409)

    attached = _vm(env, "mine", vmid=204)
    await PrivateNetworkService.attach(env.db, network.id, attached.id)
    await refused(attached, 409)

    with pytest.raises(PrivateNetworkError) as missing:
        await PrivateNetworkService.attach(env.db, network.id, 999999)
    assert missing.value.status_code == 404
    assert env.proxmox.attach_calls == [env.proxmox.attach_calls[0]]


@pytest.mark.asyncio
async def test_attach_enforces_the_per_service_limit(env):
    service = _vm(env)
    for name in ("a", "b"):
        await PrivateNetworkService.attach(env.db, (await _network(env, name)).id, service.id)
    third = await _network(env, "c")
    with pytest.raises(PrivateNetworkError, match="at most 2"):
        await PrivateNetworkService.attach(env.db, third.id, service.id)


@pytest.mark.asyncio
async def test_detach_vm_removes_nic_and_tolerates_a_vm_that_is_gone(env, monkeypatch):
    network = await _network(env)
    attachment = await PrivateNetworkService.attach(env.db, network.id, _vm(env).id)
    assert await PrivateNetworkService.detach(env.db, attachment.id) is True
    assert env.proxmox.nics == {}
    assert PrivateNetworkDAO.get_attachment(env.db, attachment.id) is None

    gone = await PrivateNetworkService.attach(env.db, network.id, _vm(env, "vm-2", vmid=102).id)

    async def not_found(db, service):
        raise ProxmoxPlacementError("VM not found", status_code=404)

    monkeypatch.setattr(pns, "resolve_proxmox_plugin_for_service", not_found)
    assert await PrivateNetworkService.detach(env.db, gone.id) is True
    assert PrivateNetworkDAO.get_attachment(env.db, gone.id) is None


@pytest.mark.asyncio
async def test_failed_vm_detach_keeps_the_row_until_cleanup_succeeds(env):
    network = await _network(env)
    attachment = await PrivateNetworkService.attach(env.db, network.id, _vm(env).id)
    env.proxmox.fail = True

    assert await PrivateNetworkService.detach(env.db, attachment.id) is False
    env.db.refresh(attachment)
    assert attachment.state == AttachmentState.DETACHING and "hypervisor unreachable" in attachment.last_error

    env.proxmox.fail = False
    counts = await PrivateNetworkService.reconcile(env.db)
    assert counts["detached"] == 1
    assert PrivateNetworkDAO.get_attachment(env.db, attachment.id) is None


# --- dedicated server attachments ---


@pytest.mark.asyncio
async def test_attach_dedicated_tags_the_vlan_on_its_uplink_port_only(env):
    service, server, switch = _dedicated(env)
    _server(env, "neighbour", switch=switch, ports=("Ethernet1/6",))
    network = await _network(env)

    attachment = await PrivateNetworkService.attach(env.db, network.id, service.id)

    assert attachment.state == AttachmentState.ACTIVE and attachment.server_id == server.id
    assert attachment.vm_net_index is None
    assert env.switches[switch.name].tagged == {"Ethernet1/5": {network.segment_id}}
    assert env.proxmox.attach_calls == []


@pytest.mark.asyncio
async def test_attach_dedicated_covers_every_port_of_a_bond_across_switches(env):
    first, second = _switch(env, "leaf-1"), _switch(env, "leaf-2")
    service, server, _ = _dedicated(env, switch=first, ports=("Ethernet1/9",), lag="bond0")
    partner = NetworkPort(server_id=server.id, name="eth1", speed_mbps=25000, lag_group="bond0")
    unrelated = NetworkPort(server_id=server.id, name="eth2", speed_mbps=1000)
    partner_switch_port = SwitchPort(switch_id=second.id, name="Ethernet1/9")
    env.db.add_all([partner, unrelated, partner_switch_port])
    env.db.flush()
    # Cabled the other way round: switch port on end A.
    env.db.add(CableRun(end_a_switch_port_id=partner_switch_port.id, end_b_server_port_id=partner.id))
    env.db.commit()
    network = await _network(env)

    attachment = await PrivateNetworkService.attach(env.db, network.id, service.id)

    assert attachment.state == AttachmentState.ACTIVE
    assert env.switches["leaf-1"].tagged == {"Ethernet1/9": {network.segment_id}}
    assert env.switches["leaf-2"].tagged == {"Ethernet1/9": {network.segment_id}}


@pytest.mark.asyncio
async def test_dedicated_attach_touches_no_port_when_part_of_the_uplink_is_unresolved(env):
    switch = _switch(env, "leaf-1")
    service, server, _ = _dedicated(env, switch=switch, ports=("Ethernet1/9",), lag="bond0")
    env.db.add(NetworkPort(server_id=server.id, name="eth1", speed_mbps=25000, lag_group="bond0"))
    env.db.commit()
    network = await _network(env)

    attachment = await PrivateNetworkService.attach(env.db, network.id, service.id)

    assert attachment.state == AttachmentState.ERROR and "eth1" in attachment.last_error
    assert env.switches["leaf-1"].tagged == {}


@pytest.mark.asyncio
async def test_dedicated_attach_needs_a_pxe_port_and_a_configurable_switch(env):
    no_pxe, server, _ = _dedicated(env, "dedi-nopxe")
    env.db.query(NetworkPort).filter(NetworkPort.server_id == server.id).update({"pxe_boot": False})
    snmp_switch = NetworkSwitch(name="old-sw", location_id=env.location.id, plugin_name="snmpv3", plugin_config={})
    env.db.add(snmp_switch)
    env.db.commit()
    on_snmp, _, _ = _dedicated(env, "dedi-snmp", switch=snmp_switch)
    network = await _network(env)

    first = await PrivateNetworkService.attach(env.db, network.id, no_pxe.id)
    second = await PrivateNetworkService.attach(env.db, network.id, on_snmp.id)

    assert first.state == AttachmentState.ERROR and "PXE port" in first.last_error
    assert second.state == AttachmentState.ERROR and "cannot be configured automatically" in second.last_error


@pytest.mark.asyncio
async def test_detach_dedicated_cleans_the_port_and_holds_the_row_on_failure(env):
    service, server, switch = _dedicated(env)
    network = await _network(env)
    attachment = await PrivateNetworkService.attach(env.db, network.id, service.id)
    fake = env.switches[switch.name]
    fake.fail = True

    assert await PrivateNetworkService.detach(env.db, attachment.id) is False
    assert PrivateNetworkDAO.server_has_attachments(env.db, server.id) is True
    assert fake.tagged == {"Ethernet1/5": {network.segment_id}}

    fake.fail = False
    await PrivateNetworkService.reconcile(env.db)
    assert PrivateNetworkDAO.server_has_attachments(env.db, server.id) is False
    assert fake.tagged == {"Ethernet1/5": set()}


@pytest.mark.asyncio
async def test_repair_restores_a_vlan_removed_from_a_port_by_hand(env):
    service, _, switch = _dedicated(env)
    network = await _network(env)
    await PrivateNetworkService.attach(env.db, network.id, service.id)
    env.switches[switch.name].tagged["Ethernet1/5"].clear()

    await PrivateNetworkService.reconcile(env.db)
    assert env.switches[switch.name].tagged == {"Ethernet1/5": {network.segment_id}}


@pytest.mark.asyncio
async def test_service_moved_to_another_server_is_cleaned_off_the_old_port(env):
    service, old_server, old_switch = _dedicated(env)
    network = await _network(env)
    attachment = await PrivateNetworkService.attach(env.db, network.id, service.id)
    new_server, new_switch = _server(env, "dedi-2", ports=("Ethernet1/7",))
    service.bare_metal.server_id = new_server.id
    env.db.commit()

    await PrivateNetworkService.reconcile(env.db)

    env.db.refresh(attachment)
    assert attachment.server_id == new_server.id
    assert env.switches[old_switch.name].tagged == {"Ethernet1/5": set()}
    assert env.switches[new_switch.name].tagged == {"Ethernet1/7": {network.segment_id}}
    assert PrivateNetworkDAO.server_has_attachments(env.db, old_server.id) is False


# --- service end of life ---


@pytest.mark.asyncio
async def test_release_service_detaches_it_from_every_network(env):
    service, server, switch = _dedicated(env)
    for name in ("a", "b"):
        await PrivateNetworkService.attach(env.db, (await _network(env, name)).id, service.id)
    assert len(env.switches[switch.name].tagged["Ethernet1/5"]) == 2

    await PrivateNetworkService.release_service(env.db, service.id)

    assert PrivateNetworkDAO.attachments_for_service(env.db, service.id) == []
    assert env.switches[switch.name].tagged == {"Ethernet1/5": set()}
    assert len(PrivateNetworkDAO.list_networks(env.db)) == 2


@pytest.mark.asyncio
async def test_repair_detaches_terminated_and_deleted_services(env):
    terminated, _, switch = _dedicated(env, "dedi-1")
    deleted, deleted_server, deleted_switch = _dedicated(env, "dedi-2", ports=("Ethernet1/8",))
    network = await _network(env)
    await PrivateNetworkService.attach(env.db, network.id, terminated.id)
    orphan = await PrivateNetworkService.attach(env.db, network.id, deleted.id)
    terminated.status = ServiceStatus.TERMINATED
    orphan.service_id = None
    env.db.commit()

    counts = await PrivateNetworkService.reconcile(env.db)

    assert counts["detached"] == 2 and counts["failed"] == 0
    assert env.switches[switch.name].tagged == {"Ethernet1/5": set()}
    # The port is cleaned through the stored server even though the service link is gone.
    assert env.switches[deleted_switch.name].tagged == {"Ethernet1/8": set()}
    assert PrivateNetworkDAO.server_has_attachments(env.db, deleted_server.id) is False


def test_service_location_follows_the_cluster_or_the_server(env):
    vm = _vm(env)
    dedicated, _, _ = _dedicated(env)
    assert pns.service_location_id(env.db, vm) == env.location.id
    assert pns.service_location_id(env.db, dedicated) == env.location.id

    cluster = ProxmoxInventoryDAO.get_cluster(env.db, vm.vm.proxmox_cluster_id)
    cluster.location_id = None
    env.db.commit()
    assert pns.service_location_id(env.db, vm) is None


def test_pool_range_validation():
    pns.validate_pool_range(2000, 3899)
    for start, end in [(1, 100), (2000, 3968), (3000, 2000)]:
        with pytest.raises(PrivateNetworkError):
            pns.validate_pool_range(start, end)


@pytest.mark.asyncio
async def test_attach_does_not_call_the_hypervisor_for_a_network_being_deleted(env):
    network = await _network(env)
    network.status = PrivateNetworkStatus.DELETING
    env.db.commit()
    with patch.object(env.proxmox, "attach_private_nic", AsyncMock()) as attach:
        with pytest.raises(PrivateNetworkError, match="being deleted"):
            await PrivateNetworkService.attach(env.db, network.id, _vm(env).id)
    attach.assert_not_awaited()


# --- termination and server reuse ---


def _terminate_with_stuck_port(env):
    """A terminated dedicated service whose switch could not be cleaned."""
    import asyncio

    service, server, switch = _dedicated(env)
    env.db.query(NetworkPort).filter(NetworkPort.server_id == server.id).update({"pxe_ip": "10.99.0.5"})
    group = ServerGroup(name="pool")
    group.servers.append(server)
    env.db.add(group)
    env.db.commit()

    async def run():
        network = await _network(env)
        await PrivateNetworkService.attach(env.db, network.id, service.id)
        env.switches[switch.name].fail = True
        service.status = ServiceStatus.TERMINATED
        env.db.commit()
        await PrivateNetworkService.release_service_quietly(env.db, service.id)
        return network

    network = asyncio.run(run())
    return server, switch, group, network


def test_server_is_held_back_until_its_switch_port_is_clean(env):
    import asyncio

    server, switch, group, network = _terminate_with_stuck_port(env)

    assert bare_metal.server_not_ready_reason(env.db, server) == "still attached to a private network"
    with pytest.raises(ProvisioningError) as none_free:
        bare_metal.select_free_server_in_group(env.db, group.id)
    assert none_free.value.code == "no_free_server"
    with pytest.raises(ProvisioningError, match="private network"):
        bare_metal._pin_existing_server(env.db, server.id)
    assert env.switches[switch.name].tagged == {"Ethernet1/5": {network.segment_id}}

    env.switches[switch.name].fail = False
    asyncio.run(PrivateNetworkService.reconcile(env.db))

    assert env.switches[switch.name].tagged == {"Ethernet1/5": set()}
    assert bare_metal.server_not_ready_reason(env.db, server) is None
    assert bare_metal.select_free_server_in_group(env.db, group.id).id == server.id
    assert bare_metal._pin_existing_server(env.db, server.id).id == server.id


def test_admin_terminate_detaches_private_networks(env, client, test_admin_user):
    import asyncio

    service, _, switch = _dedicated(env)

    async def setup():
        network = await _network(env)
        await PrivateNetworkService.attach(env.db, network.id, service.id)
        return network

    network = asyncio.run(setup())
    assert env.switches[switch.name].tagged == {"Ethernet1/5": {network.segment_id}}
    login = client.post("/api/users/login", json={"username": test_admin_user.username, "password": "adminpassword123"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}

    suspended = client.put(f"/api/admin/services/{service.id}/status", json={"status": "suspended"}, headers=headers)
    assert suspended.status_code == 200, suspended.text
    assert env.switches[switch.name].tagged == {"Ethernet1/5": {network.segment_id}}

    terminated = client.put(f"/api/admin/services/{service.id}/status", json={"status": "terminated"}, headers=headers)
    assert terminated.status_code == 200, terminated.text
    assert env.switches[switch.name].tagged == {"Ethernet1/5": set()}
    assert PrivateNetworkDAO.attachments_for_service(env.db, service.id) == []


@pytest.mark.asyncio
async def test_release_quietly_never_raises(env, monkeypatch):
    async def boom(db, service_id):
        raise RuntimeError("database went away")

    monkeypatch.setattr(PrivateNetworkService, "release_service", staticmethod(boom))
    await PrivateNetworkService.release_service_quietly(env.db, 1)
