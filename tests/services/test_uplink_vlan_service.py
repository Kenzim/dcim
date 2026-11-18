"""Private networks on Cisco vPC pairs, through the real nxos plugin and a stateful NX-API fake.

Topology: two Nexus switches in vPC domain 1 (peer-link port-channel45), a
dual-homed server on vPC 120 (port-channel120 on both, member port names
differ per switch as they do in production).
"""
import pytest

from app.dao.private_network_dao import PrivateNetworkDAO
from app.dao.service_dao import ServiceDAO
from app.models.cable_run import CableRun
from app.models.location import Location
from app.models.network_port import NetworkPort
from app.models.network_switch import NetworkSwitch
from app.models.private_network import AttachmentState
from app.models.server import Server
from app.models.service import ProvisioningSource, ServiceStatus
from app.models.switch_port import SwitchPort
from app.models.user import User
from app.services.private_network_service import PrivateNetworkService, dry_run_uplink
from tests.nxapi_fake import ALL, FakeNexus, FakeNXAPI

NX = {"nxapi_username": "rackflow", "nxapi_password": "x", "username": "snmp", "security_level": "noAuthNoPriv"}
VLAN = 2000


class Lab:
    def __init__(self, db):
        self.db = db
        self.location = Location(name="DC", description="")
        db.add(self.location)
        db.commit()
        PrivateNetworkDAO.create_pool(
            db, location_id=self.location.id, vlan_start=VLAN, vlan_end=VLAN + 10, bridge="vmbr0",
            max_networks_per_client=5, max_networks_per_service=5,
        )
        self.owner = User(username="owner", email="owner@example.com", is_admin=False)
        self.owner.set_password("secret123")
        db.add(self.owner)
        db.commit()
        self.api = FakeNXAPI()
        self.switch_ports = {}

    def switch(self, name, host, *, plugin="nxos", enabled=True):
        record = NetworkSwitch(name=name, location_id=self.location.id, plugin_name=plugin, enabled=enabled,
                               plugin_config={"hostname": host, **NX})
        self.db.add(record)
        self.db.commit()
        return record, self.api.add(FakeNexus(host))

    def pair(self, peer_link_allowed=(1, 100), *, peer_plugin="nxos"):
        sw1, fake1 = self.switch("25G-1", "10.0.0.1")
        sw2, fake2 = self.switch("25G-2", "10.0.0.2", plugin=peer_plugin)
        for record, fake in ((sw1, fake1), (sw2, fake2)):
            fake.vpc_pair_side(peer_link_allowed=peer_link_allowed)
            record.vpc_domain_id = 1
            record.vpc_peer_link = "port-channel45"
        sw1.vpc_peer_switch_id, sw2.vpc_peer_switch_id = sw2.id, sw1.id
        self.db.commit()
        return (sw1, fake1), (sw2, fake2)

    def switch_port(self, switch, name):
        key = (switch.id, name)
        if key not in self.switch_ports:
            port = SwitchPort(switch_id=switch.id, name=name)
            self.db.add(port)
            self.db.flush()
            self.switch_ports[key] = port
        return self.switch_ports[key]

    def server(self, name, legs, *, lag=None):
        """legs: [(switch, switch port name or None for an uncabled NIC)]; the first NIC is the PXE port."""
        server = Server(name=name, server_ip="10.88.0.1", location_id=self.location.id, plugin_name="ipmi", plugin_config={})
        self.db.add(server)
        self.db.flush()
        for index, (switch, port_name) in enumerate(legs):
            nic = NetworkPort(server_id=server.id, name=f"eth{index}", speed_mbps=25000, pxe_boot=index == 0,
                              lag_group=lag)
            self.db.add(nic)
            self.db.flush()
            if switch is not None and port_name is not None:
                self.db.add(CableRun(end_a_server_port_id=nic.id, end_b_switch_port_id=self.switch_port(switch, port_name).id))
        self.db.commit()
        service = ServiceDAO.create_bare_metal(
            self.db, name=f"{name}-svc", server_id=server.id, owner_user_id=self.owner.id,
            provisioning_source=ProvisioningSource.INTERNAL, status=ServiceStatus.ACTIVE,
        )
        return server, service

    async def network(self, name="net"):
        return await PrivateNetworkService.create_network(
            self.db, owner_user_id=self.owner.id, location_id=self.location.id, name=name
        )


@pytest.fixture
def lab(db_session):
    lab = Lab(db_session)
    with lab.api.active():
        yield lab


def _vpc120(fake1, fake2, members1=("Ethernet1/24",), members2=("Ethernet1/19",)):
    fake1.port_channel(120, list(members1), vpc=120, allowed={100}, native=100)
    fake2.port_channel(120, list(members2), vpc=120, allowed={100}, native=100)


def _batches_for(fake, interface):
    return [b for b in fake.config_batches if f"interface {interface}" in b]


@pytest.mark.asyncio
async def test_dual_homed_server_gets_the_vlan_on_both_peers(lab):
    (sw1, fake1), (sw2, fake2) = lab.pair()
    _vpc120(fake1, fake2)
    _, service = lab.server("hv-19", [(sw1, "Ethernet1/24"), (sw2, "Ethernet1/19")])
    network = await lab.network()

    attachment = await PrivateNetworkService.attach(lab.db, network.id, service.id)

    assert attachment.state == AttachmentState.ACTIVE and attachment.last_error is None
    for fake in (fake1, fake2):
        assert fake.vlans[VLAN] == f"pn-{network.id}"
        assert fake.iface("port-channel45").allowed == {1, 100, VLAN}
        assert fake.iface("port-channel120").allowed == {100, VLAN}
        assert fake.iface("port-channel120").native_vlan == 100
        # Members follow the port-channel and are never configured directly.
        assert not any(b for b in fake.config_batches if "interface Ethernet1/24" in b or "interface Ethernet1/19" in b)
        assert len(_batches_for(fake, "port-channel120")) == 1
        assert ["interface port-channel45", "switchport trunk allowed vlan add 2000"] == _batches_for(
            fake, "port-channel45")[0][1:3]

    # Attaching again changes nothing.
    await PrivateNetworkService._apply(lab.db, attachment)
    assert len(_batches_for(fake1, "port-channel120")) == 1 and len(_batches_for(fake2, "port-channel45")) == 1


@pytest.mark.asyncio
async def test_peer_link_allowing_every_vlan_is_not_changed(lab):
    (sw1, fake1), (sw2, fake2) = lab.pair(peer_link_allowed="all")
    _vpc120(fake1, fake2)
    _, service = lab.server("hv-19", [(sw1, "Ethernet1/24"), (sw2, "Ethernet1/19")])
    network = await lab.network()

    attachment = await PrivateNetworkService.attach(lab.db, network.id, service.id)

    assert attachment.state == AttachmentState.ACTIVE
    for fake in (fake1, fake2):
        assert _batches_for(fake, "port-channel45") == []
        assert fake.iface("port-channel45").allowed >= ALL
        assert VLAN in fake.iface("port-channel120").allowed


@pytest.mark.asyncio
async def test_port_channel_with_several_members_is_configured_once(lab):
    (sw1, fake1), (sw2, fake2) = lab.pair()
    _vpc120(fake1, fake2, members1=("Ethernet1/24", "Ethernet1/25"), members2=("Ethernet1/19", "Ethernet1/20"))
    _, service = lab.server(
        "hv-4link",
        [(sw1, "Ethernet1/24"), (sw1, "Ethernet1/25"), (sw2, "Ethernet1/19"), (sw2, "Ethernet1/20")],
        lag="bond0",
    )
    network = await lab.network()

    attachment = await PrivateNetworkService.attach(lab.db, network.id, service.id)

    assert attachment.state == AttachmentState.ACTIVE
    assert len(_batches_for(fake1, "port-channel120")) == 1
    assert len(_batches_for(fake2, "port-channel120")) == 1
    assert fake1.iface("Ethernet1/25").allowed == {100, VLAN}


@pytest.mark.asyncio
async def test_only_one_leg_cabled_still_configures_the_peer_and_warns(lab):
    (sw1, fake1), (sw2, fake2) = lab.pair()
    _vpc120(fake1, fake2)
    _, service = lab.server("hv-05", [(sw1, "Ethernet1/24")])
    network = await lab.network()

    preview = await dry_run_uplink(lab.db, service.bare_metal.server_id, network_id=network.id)
    assert preview["ok"] and any("No cable run" in w and "25G-2" in w for w in preview["warnings"])

    attachment = await PrivateNetworkService.attach(lab.db, network.id, service.id)

    assert attachment.state == AttachmentState.ACTIVE
    assert attachment.last_error.startswith("Warning:") and "No cable run" in attachment.last_error
    assert VLAN in fake2.iface("port-channel120").allowed and VLAN in fake1.iface("port-channel120").allowed


@pytest.mark.asyncio
async def test_vpc_without_recorded_peer_is_refused(lab):
    (sw1, fake1), (sw2, fake2) = lab.pair()
    _vpc120(fake1, fake2)
    sw1.vpc_peer_switch_id = None
    lab.db.commit()
    _, service = lab.server("hv-19", [(sw1, "Ethernet1/24")])
    network = await lab.network()

    attachment = await PrivateNetworkService.attach(lab.db, network.id, service.id)

    assert attachment.state == AttachmentState.ERROR and "no vPC peer is recorded" in attachment.last_error
    assert fake1.iface("port-channel120").allowed == {100}


@pytest.mark.asyncio
async def test_vpc_whose_peer_is_not_vlan_capable_is_refused(lab):
    (sw1, fake1), (sw2, fake2) = lab.pair(peer_plugin="snmpv3")
    _vpc120(fake1, fake2)
    _, service = lab.server("hv-19", [(sw1, "Ethernet1/24"), (sw2, "Ethernet1/19")])
    network = await lab.network()

    attachment = await PrivateNetworkService.attach(lab.db, network.id, service.id)

    assert attachment.state == AttachmentState.ERROR
    assert "25G-2" in attachment.last_error and "cannot be configured automatically" in attachment.last_error
    assert _batches_for(fake1, "port-channel120") == [] and fake2.config_batches == []


@pytest.mark.asyncio
async def test_failure_on_the_second_peer_rolls_back_the_first(lab):
    (sw1, fake1), (sw2, fake2) = lab.pair()
    _vpc120(fake1, fake2)
    _, service = lab.server("hv-19", [(sw1, "Ethernet1/24"), (sw2, "Ethernet1/19")])
    network = await lab.network()
    # Start from VLANs absent, so creating them is part of the operation too.
    for fake in (fake1, fake2):
        fake.vlans.pop(VLAN)
    fake2.fail_on = lambda cmd, iface: iface == "port-channel120" and cmd.startswith("switchport trunk allowed vlan add")

    attachment = await PrivateNetworkService.attach(lab.db, network.id, service.id)

    assert attachment.state == AttachmentState.ERROR
    assert "25G-2 port-channel120 failed" in attachment.last_error and "rolled back" in attachment.last_error
    for fake in (fake1, fake2):
        assert VLAN not in fake.vlans
        assert fake.iface("port-channel45").allowed == {1, 100}
        assert fake.iface("port-channel120").allowed == {100}


@pytest.mark.asyncio
async def test_rollback_turns_a_converted_access_port_back_into_an_access_port(lab):
    a, fake_a = lab.switch("leaf-a", "10.0.1.1")
    b, fake_b = lab.switch("leaf-b", "10.0.1.2")
    fake_a.access("Ethernet1/6", 100).edge = "edge"
    fake_b.access("Ethernet1/6", 100)
    _, service = lab.server("active-backup", [(a, "Ethernet1/6"), (b, "Ethernet1/6")], lag="bond0")
    network = await lab.network()
    for fake in (fake_a, fake_b):
        fake.vlans.pop(VLAN)
    fake_b.fail_on = lambda cmd, iface: iface == "Ethernet1/6" and cmd == "switchport mode trunk"

    attachment = await PrivateNetworkService.attach(lab.db, network.id, service.id)

    assert attachment.state == AttachmentState.ERROR and "rolled back" in attachment.last_error
    port = fake_a.iface("Ethernet1/6")
    assert (port.mode, port.access_vlan, port.edge) == ("access", 100, "edge")
    assert VLAN not in fake_a.vlans


@pytest.mark.asyncio
async def test_detach_then_delete_removes_in_reverse_order(lab):
    (sw1, fake1), (sw2, fake2) = lab.pair()
    _vpc120(fake1, fake2)
    _, service = lab.server("hv-19", [(sw1, "Ethernet1/24"), (sw2, "Ethernet1/19")])
    network = await lab.network()
    attachment = await PrivateNetworkService.attach(lab.db, network.id, service.id)

    assert await PrivateNetworkService.detach(lab.db, attachment.id) is True
    for fake in (fake1, fake2):
        assert fake.iface("port-channel120").allowed == {100}
        # Other servers on the pair may still use the VLAN, so the peer-link keeps it until the network goes.
        assert VLAN in fake.iface("port-channel45").allowed and VLAN in fake.vlans

    await PrivateNetworkService.delete_network(lab.db, network.id)
    for fake in (fake1, fake2):
        assert fake.iface("port-channel45").allowed == {1, 100}
        assert VLAN not in fake.vlans
        remove_peer = next(i for i, b in enumerate(fake.config_batches) if "switchport trunk allowed vlan remove 2000" in b
                           and "interface port-channel45" in b)
        delete = next(i for i, b in enumerate(fake.config_batches) if "no vlan 2000" in b)
        assert remove_peer < delete


@pytest.mark.asyncio
async def test_dry_run_shows_commands_for_both_switches_without_sending_them(lab):
    (sw1, fake1), (sw2, fake2) = lab.pair()
    _vpc120(fake1, fake2)
    server, _ = lab.server("hv-19", [(sw1, "Ethernet1/24"), (sw2, "Ethernet1/19")])
    before = (list(fake1.config_batches), list(fake2.config_batches))

    preview = await dry_run_uplink(lab.db, server.id, vlan_id=2005)

    assert preview["ok"] and preview["error"] is None
    assert (fake1.config_batches, fake2.config_batches) == before
    target = preview["targets"][0]
    assert target["kind"] == "vpc" and target["vpc_id"] == 120
    assert [(leg["switch"], leg["interface"]) for leg in target["legs"]] == [
        ("25G-1", "port-channel120"), ("25G-2", "port-channel120")]
    by_switch = {entry["switch"]: [cmd for batch in entry["batches"] for cmd in batch] for entry in preview["commands"]}
    for name in ("25G-1", "25G-2"):
        commands = by_switch[name]
        assert "vlan 2005" in commands and "interface port-channel45" in commands
        assert "interface port-channel120" in commands and "switchport trunk allowed vlan add 2005" in commands

    removal = await dry_run_uplink(lab.db, server.id, vlan_id=2005, remove=True)
    assert removal["ok"] and removal["commands"]


@pytest.mark.asyncio
async def test_orphan_member_in_a_bond_is_refused(lab):
    (sw1, fake1), (sw2, fake2) = lab.pair()
    _vpc120(fake1, fake2)
    fake2.access("Ethernet1/30", 100)
    _, service = lab.server("orphan", [(sw1, "Ethernet1/24"), (sw2, "Ethernet1/30")], lag="bond0")
    network = await lab.network()

    attachment = await PrivateNetworkService.attach(lab.db, network.id, service.id)

    assert attachment.state == AttachmentState.ERROR and "orphan or mismatched" in attachment.last_error
    assert fake1.iface("port-channel120").allowed == {100} and fake2.iface("Ethernet1/30").mode == "access"


@pytest.mark.asyncio
async def test_port_channel_with_a_member_cabled_to_another_switch_is_refused(lab):
    (sw1, fake1), (sw2, fake2) = lab.pair()
    _vpc120(fake1, fake2, members1=("Ethernet1/24", "Ethernet1/49"))
    other, _ = lab.switch("core", "10.0.0.9")
    lab.db.add(CableRun(end_a_switch_port_id=lab.switch_port(sw1, "Ethernet1/49").id,
                        end_b_switch_port_id=lab.switch_port(other, "Ethernet1/1").id))
    lab.db.commit()
    _, service = lab.server("hv-19", [(sw1, "Ethernet1/24")])
    network = await lab.network()

    attachment = await PrivateNetworkService.attach(lab.db, network.id, service.id)

    assert attachment.state == AttachmentState.ERROR and "inter-switch links" in attachment.last_error


@pytest.mark.asyncio
async def test_plain_port_channel_and_independent_ports_on_unpaired_switches(lab):
    a, fake_a = lab.switch("leaf-a", "10.0.1.1")
    b, fake_b = lab.switch("leaf-b", "10.0.1.2")
    fake_a.port_channel(7, ["Ethernet1/7", "Ethernet1/8"], access_vlan=100)
    fake_b.access("Ethernet1/6", 100)
    _, lacp = lab.server("lacp", [(a, "Ethernet1/7"), (a, "Ethernet1/8")], lag="bond0")
    _, backup = lab.server("active-backup", [(b, "Ethernet1/6"), (a, "Ethernet1/9")], lag="bond1")
    fake_a.access("Ethernet1/9", 100)
    network = await lab.network()

    assert (await PrivateNetworkService.attach(lab.db, network.id, lacp.id)).state == AttachmentState.ACTIVE
    assert (await PrivateNetworkService.attach(lab.db, network.id, backup.id)).state == AttachmentState.ACTIVE

    assert len(_batches_for(fake_a, "port-channel7")) == 1
    assert fake_a.iface("port-channel7").allowed == {100, VLAN}
    assert fake_b.iface("Ethernet1/6").allowed == {100, VLAN} and fake_a.iface("Ethernet1/9").allowed == {100, VLAN}
