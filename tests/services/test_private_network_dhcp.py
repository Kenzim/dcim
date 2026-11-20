"""Private network DHCP: validation, reservation table, address picking, runner config presences,
presence gating and hand-over, status ingestion and audit."""

from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import settings
from app.dao.storage_dao import StorageDAO
from app.models.commerce_audit import UserAuditEvent
from app.models.location import Location
from app.models.network_port import NetworkPort
from app.models.private_network import (
    AttachmentState,
    NetworkKind,
    PrivateNetwork,
    PrivateNetworkAttachment,
    PrivateNetworkDhcpLease,
    PrivateNetworkDhcpReservation,
)
from app.models.server import Server
from app.models.service import Service, ServiceStatus, ServiceType
from app.models.storage import StorageShare, StorageTenant, VlanMode
from app.models.user import User
from app.services import private_network_dhcp as dhcp
from app.services import storage_ha, storage_location
from app.services.private_network_service import PrivateNetworkError, _pick_ip
from app.services.storage_errors import StorageError
from app.services.storage_service import StorageService, apply_runner_status, build_runner_config


def _now():
    return datetime.now(timezone.utc)


def _z(value):
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


class World:
    def __init__(self, db):
        self.db = db
        self.location = Location(name="London", description="")
        db.add(self.location)
        self.owner = User(username="owner", email="owner@example.com", is_admin=False)
        self.owner.set_password("secret123")
        db.add(self.owner)
        db.commit()
        self.nodes = []
        self.servers = 0

    def node(self, *, version="0.4.0", online=True, vlan_mode=VlanMode.PRECONFIGURED, location=None):
        index = len(self.nodes) + 1
        loc = location or self.location
        server = Server(name=f"stor-srv-{index}-{loc.id}", server_ip=f"10.9.{loc.id}.{index}", location_id=loc.id,
                        plugin_name="ipmi", plugin_config={})
        self.db.add(server)
        self.db.commit()
        node, _key = StorageDAO.create_node(self.db, name=f"stor-{index:02d}", server_id=server.id)
        node.runner_version = version
        node.ceph_ok = True
        node.vlan_mode = vlan_mode
        if online:
            node.last_seen_at = datetime.utcnow()
        self.db.commit()
        self.nodes.append(node)
        return node

    def network(self, vlan, cidr="10.0.0.0/24", kind=NetworkKind.STANDARD, location=None):
        network = PrivateNetwork(name=f"net-{vlan}", owner_user_id=self.owner.id,
                                 location_id=(location or self.location).id, segment_id=vlan, cidr=cidr, kind=kind)
        self.db.add(network)
        self.db.commit()
        return network

    def service(self, service_type=ServiceType.VM, owner=None):
        service = Service(name=f"svc-{service_type.value}", service_type=service_type, status=ServiceStatus.ACTIVE,
                          owner_user_id=(owner or self.owner).id)
        self.db.add(service)
        self.db.commit()
        return service

    def vm(self, network, ip, mac, state=AttachmentState.ACTIVE, owner=None):
        attachment = PrivateNetworkAttachment(network_id=network.id, service_id=self.service(owner=owner).id,
                                              ip_address=ip, mac_address=mac, state=state)
        self.db.add(attachment)
        self.db.commit()
        self.db.refresh(network)
        return attachment

    def dedicated(self, network, ip, ports):
        """ports: [(mac, pxe, lag_group)]"""
        self.servers += 1
        server = Server(name=f"ded-{self.servers}", server_ip=f"10.8.0.{self.servers}", location_id=self.location.id,
                        plugin_name="ipmi", plugin_config={})
        self.db.add(server)
        self.db.flush()
        for index, (mac, pxe, lag) in enumerate(ports):
            self.db.add(NetworkPort(server_id=server.id, name=f"eth{index}", mac_address=mac, pxe_boot=pxe,
                                    lag_group=lag, speed_mbps=25000))
        service = self.service(ServiceType.BARE_METAL)
        attachment = PrivateNetworkAttachment(network_id=network.id, service_id=service.id, server_id=server.id,
                                              ip_address=ip, state=AttachmentState.ACTIVE)
        self.db.add(attachment)
        self.db.commit()
        self.db.refresh(network)
        return attachment

    def tenant(self, network, node, *, current=None, terminated=False):
        service = self.service(ServiceType.NFS_STORAGE)
        tenant = StorageTenant(
            service_id=service.id, node_id=node.id, network_id=network.id, location_id=network.location_id,
            vlan=network.segment_id, network_cidr=network.cidr, endpoint_ip="10.0.0.254", size_bytes=1 << 30,
            current_node_id=current.id if current else None, current_since=_now() if current else None,
            terminated_at=_now() if terminated else None,
            destroy_after=_now() + timedelta(days=3) if terminated else None,
        )
        self.db.add(tenant)
        self.db.commit()
        return tenant

    def reserve(self, network, mac, ip, label=None):
        row = PrivateNetworkDhcpReservation(network_id=network.id, mac=mac, ip=ip, label=label)
        self.db.add(row)
        self.db.commit()
        self.db.refresh(network)
        return row


@pytest.fixture
def world(db_session, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setattr(settings, "service_instance_encryption_key", Fernet.generate_key().decode())
    return World(db_session)


def _presences(world, node):
    return build_runner_config(world.db, node).get("networks")


def _report(node, tenants=None, networks=None):
    body = {
        "runner_version": node.runner_version,
        "node": {"ceph_ok": True},
        "ha": {"lease_ok": True, "fenced": False, "sftp_master": False, "ha_active": True},
        "tenants": tenants or [],
    }
    if networks is not None:
        body["networks"] = networks
    return body


# --- reservation table ------------------------------------------------------------


def test_reservation_table_vm_dedicated_bond_owner_and_conflicts(world):
    network = world.network(3000)
    world.vm(network, "10.0.0.2", "BC:24:11:AA:BB:01")
    world.dedicated(network, "10.0.0.3", [
        ("aa:00:00:00:00:01", True, "bond0"),
        ("aa:00:00:00:00:02", False, "bond0"),
        ("aa:00:00:00:00:03", False, None),
    ])
    world.dedicated(network, "10.0.0.4", [("aa:00:00:00:00:10", False, None)])  # no PXE port: no MAC
    world.vm(network, "10.0.0.5", "bc:24:11:aa:bb:05", state=AttachmentState.DETACHING)
    world.reserve(network, "de:ad:be:ef:00:01", "10.0.0.50")
    # The same MAC on a VM and an owner reservation with another address: dropped, with a warning.
    world.vm(network, "10.0.0.6", "de:ad:be:ef:00:99")
    world.reserve(network, "de:ad:be:ef:00:99", "10.0.0.60")

    table = dhcp.reservation_table(world.db, network)
    assert table.entries == [
        {"mac": "bc:24:11:aa:bb:01", "ip": "10.0.0.2"},
        {"mac": "aa:00:00:00:00:01", "ip": "10.0.0.3"},
        {"mac": "aa:00:00:00:00:02", "ip": "10.0.0.3"},
        {"mac": "de:ad:be:ef:00:01", "ip": "10.0.0.50"},
    ]
    assert table.conflicts == [{"mac": "de:ad:be:ef:00:99", "ips": ["10.0.0.6", "10.0.0.60"]}]
    assert "de:ad:be:ef:00:99" in dhcp.warnings(table)[0]
    assert dhcp.staff_fields(world.db, network)["dhcp_warnings"] == dhcp.warnings(table)


def test_storage_and_proxy_attachments_have_no_reservation(world):
    network = world.network(3000)
    storage = world.service(ServiceType.NFS_STORAGE)
    world.db.add(PrivateNetworkAttachment(network_id=network.id, service_id=storage.id, ip_address="10.0.0.254",
                                          mac_address="02:52:46:00:00:01", state=AttachmentState.ACTIVE))
    world.db.commit()
    world.db.refresh(network)
    assert dhcp.reservation_table(world.db, network).entries == []


def test_detach_removes_the_reservation_from_the_next_config(world):
    node = world.node()
    network = world.network(3000)
    attachment = world.vm(network, "10.0.0.2", "bc:24:11:aa:bb:01")
    assert _presences(world, node)[0]["dhcp"]["reservations"] == [{"mac": "bc:24:11:aa:bb:01", "ip": "10.0.0.2"}]
    attachment.state = AttachmentState.DETACHING
    world.db.commit()
    assert _presences(world, node)[0]["dhcp"]["reservations"] == []
    world.db.delete(attachment)
    world.db.commit()
    world.db.expire_all()
    assert _presences(world, node)[0]["dhcp"]["reservations"] == []


# --- validation (section 4.2) ------------------------------------------------------


@pytest.mark.parametrize("changes, message", [
    ({"range_start": "10.0.0.100"}, "both ends"),
    ({"range_start": "10.0.0.200", "range_end": "10.0.0.100"}, "not be after"),
    ({"range_start": "10.0.1.1", "range_end": "10.0.1.9"}, "usable address inside"),
    ({"range_start": "10.0.0.0", "range_end": "10.0.0.9"}, "usable address inside"),
    ({"range_start": "10.0.0.250", "range_end": "10.0.0.255"}, "usable address inside"),
    ({"range_start": "10.0.0.250", "range_end": "10.0.0.254"}, "storage endpoint"),
    ({"range_start": "10.0.0.2", "range_end": "10.0.0.9"}, "10.0.0.2, which a server"),
    ({"range_start": "10.0.0.40", "range_end": "10.0.0.60"}, "10.0.0.50, which is reserved"),
    ({"range_start": "10.0.0.100", "range_end": "10.0.0.120", "gateway": "10.0.0.110"}, "gateway cannot be inside"),
    ({"range_start": "nope", "range_end": "10.0.0.9"}, "not a valid IPv4"),
    ({"gateway": "10.0.0.254"}, "storage endpoint"),
    ({"gateway": "10.0.0.255"}, "usable address inside"),
    ({"gateway": "192.168.1.1"}, "usable address inside"),
    ({"lease_seconds": 299}, "Lease time"),
    ({"lease_seconds": 604801}, "Lease time"),
    ({"mtu": 575}, "MTU"),
    ({"mtu": 9001}, "MTU"),
    ({"dns_servers": ["1.1.1.1", "8.8.8.8", "9.9.9.9", "8.8.4.4"]}, "At most 3"),
    ({"dns_servers": ["127.0.0.1"]}, "cannot be used"),
    ({"dns_servers": ["224.0.0.1"]}, "cannot be used"),
    ({"dns_servers": ["255.255.255.255"]}, "cannot be used"),
    ({"dns_servers": ["0.0.0.0"]}, "cannot be used"),
    ({"ntp_servers": ["127.0.0.53"]}, "cannot be used"),
    ({"ntp_servers": ["a.b.c.d"]}, "not a valid IPv4"),
    ({"domain": "bad_domain.example"}, "Domain"),
    ({"domain": "-bad.example"}, "Domain"),
    ({"domain": ("a" * 60 + ".") * 5 + "com"}, "Domain"),
    ({"routes": [{"destination": "0.0.0.0/0", "gateway": "10.0.0.1"}]}, "default route"),
    ({"routes": [{"destination": "192.168.50.1/24", "gateway": "10.0.0.1"}]}, "not a valid network"),
    ({"routes": [{"destination": "192.168.50.0/24", "gateway": "10.0.0.254"}]}, "storage endpoint"),
    ({"routes": [{"destination": "192.168.50.0/24", "gateway": "10.0.1.1"}]}, "usable address inside"),
    ({"routes": [{"destination": f"192.168.{i}.0/24", "gateway": "10.0.0.1"} for i in range(17)]}, "At most 16"),
    ({"bogus": 1}, "Unknown DHCP setting"),
])
def test_settings_validation(world, changes, message):
    network = world.network(3000)
    world.vm(network, "10.0.0.2", "bc:24:11:aa:bb:01")
    world.reserve(network, "de:ad:be:ef:00:01", "10.0.0.50")
    with pytest.raises(PrivateNetworkError, match=message):
        dhcp.validate_settings(world.db, network, changes)


def test_range_size_limit_and_valid_settings(world):
    network = world.network(3000, cidr="10.0.0.0/16")
    with pytest.raises(PrivateNetworkError, match="at most 4096"):
        dhcp.validate_settings(world.db, network, {"range_start": "10.0.16.0", "range_end": "10.0.32.0"})
    clean = dhcp.validate_settings(world.db, network, {
        "range_start": "10.0.1.0", "range_end": "10.0.1.255", "gateway": "10.0.0.1", "lease_seconds": 3600,
        "dns_servers": ["1.1.1.1", "1.1.1.1"], "domain": "Example.Internal.", "mtu": 9000,
        "routes": [{"destination": "192.168.50.0/24", "gateway": "10.0.0.1"}], "ntp_servers": ["10.0.0.1"],
    })
    assert clean["dns_servers"] == ["1.1.1.1"] and clean["domain"] == "example.internal"
    assert clean["range_start"] == "10.0.1.0" and clean["lease_seconds"] == 3600


def test_range_may_not_hold_a_restricted_share_address(world):
    node = world.node()
    network = world.network(3000)
    tenant = world.tenant(network, node)
    world.db.add(StorageShare(tenant_id=tenant.id, name="data", quota_bytes=1 << 20, clients=["10.0.0.30"],
                              all_clients=False))
    world.db.commit()
    with pytest.raises(PrivateNetworkError, match="storage share"):
        dhcp.validate_settings(world.db, network, {"range_start": "10.0.0.20", "range_end": "10.0.0.40"})
    with pytest.raises(PrivateNetworkError, match="client of a storage share"):
        dhcp._validate_reservation(world.db, network, "de:ad:be:ef:00:01", "10.0.0.30")


def test_restricted_share_may_not_list_an_address_in_the_range(world):
    node = world.node()
    network = world.network(3000)
    tenant = world.tenant(network, node)
    network.dhcp_range_start, network.dhcp_range_end = "10.0.0.100", "10.0.0.150"
    world.db.commit()
    world.db.refresh(tenant)
    with pytest.raises(StorageError, match="DHCP range"):
        StorageService._validate_clients(tenant, ["10.0.0.120"])
    assert StorageService._validate_clients(tenant, ["10.0.0.99"]) == ["10.0.0.99"]


def test_proxy_access_network_refuses_dhcp(world):
    network = world.network(3000, kind=NetworkKind.PROXY_ACCESS)
    with pytest.raises(PrivateNetworkError, match="proxy access"):
        dhcp.validate_settings(world.db, network, {"enabled": False})
    with pytest.raises(PrivateNetworkError, match="proxy access"):
        dhcp._validate_reservation(world.db, network, "de:ad:be:ef:00:01", "10.0.0.30")


@pytest.mark.parametrize("mac, ip, message", [
    ("de:ad:be:ef:00", "10.0.0.30", "six octets"),
    ("zz:ad:be:ef:00:01", "10.0.0.30", "six octets"),
    ("01:00:5e:00:00:01", "10.0.0.30", "unicast"),
    ("00:00:00:00:00:00", "10.0.0.30", "all zero"),
    ("02:52:46:00:00:01", "10.0.0.30", "kept for storage"),
    ("02:52:4E:00:00:01", "10.0.0.30", "kept for storage"),
    ("bc:24:11:aa:bb:01", "10.0.0.30", "belongs to a server"),
    ("aa:00:00:00:00:02", "10.0.0.31", "belongs to a server"),
    ("de:ad:be:ef:00:01", "10.0.0.31", "already has a reservation"),
    ("de:ad:be:ef:00:02", "10.0.0.50", "already reserved"),
    ("de:ad:be:ef:00:02", "10.0.0.2", "used by a server"),
    ("de:ad:be:ef:00:02", "10.0.0.254", "storage endpoint"),
    ("de:ad:be:ef:00:02", "10.0.0.0", "usable address inside"),
    ("de:ad:be:ef:00:02", "10.0.0.255", "usable address inside"),
    ("de:ad:be:ef:00:02", "10.0.1.5", "usable address inside"),
    ("de:ad:be:ef:00:02", "10.0.0.110", "inside the DHCP range"),
])
def test_reservation_validation(world, mac, ip, message):
    network = world.network(3000)
    world.vm(network, "10.0.0.2", "bc:24:11:aa:bb:01")
    world.dedicated(network, "10.0.0.3", [("aa:00:00:00:00:01", True, "bond0"), ("aa:00:00:00:00:02", False, "bond0")])
    world.reserve(network, "de:ad:be:ef:00:01", "10.0.0.50")
    network.dhcp_range_start, network.dhcp_range_end = "10.0.0.100", "10.0.0.150"
    world.db.commit()
    with pytest.raises(PrivateNetworkError, match=message):
        dhcp._validate_reservation(world.db, network, mac, ip)


def test_mac_formats_are_normalised():
    assert dhcp.parse_mac("DE-AD-BE-EF-00-01") == "de:ad:be:ef:00:01"
    assert dhcp.parse_mac("deadbeef0001") == "de:ad:be:ef:00:01"
    assert dhcp.parse_mac("de:ad:be:ef:00") is None


@pytest.mark.asyncio
async def test_reservation_crud_limits_and_audit(world):
    network = world.network(3000)
    row = await dhcp.add_reservation(world.db, network, "DE:AD:BE:EF:00:01", "10.0.0.50", " printer\x07 ",
                                     actor_user_id=world.owner.id)
    assert (row.mac, row.ip, row.label) == ("de:ad:be:ef:00:01", "10.0.0.50", "printer")
    with pytest.raises(PrivateNetworkError, match="at most 64"):
        await dhcp.add_reservation(world.db, network, "de:ad:be:ef:00:02", "10.0.0.51", "x" * 65,
                                   actor_user_id=world.owner.id)
    row = await dhcp.update_reservation(world.db, network, row.id, {"ip": "10.0.0.51"}, actor_user_id=world.owner.id)
    assert row.ip == "10.0.0.51"
    with pytest.raises(PrivateNetworkError, match="not found"):
        await dhcp.update_reservation(world.db, network, 9999, {"ip": "10.0.0.52"}, actor_user_id=world.owner.id)
    await dhcp.update_settings(world.db, network, {"enabled": False, "mtu": 1400}, actor_user_id=world.owner.id)
    await dhcp.remove_reservation(world.db, network, row.id, actor_user_id=world.owner.id)
    assert network.dhcp_reservations == [] and network.dhcp_enabled is False

    rows = world.db.query(UserAuditEvent).order_by(UserAuditEvent.id).all()
    actions = [r.action for r in rows]
    assert actions == [
        "private_network.dhcp_reservation_add",
        "private_network.dhcp_reservation_update",
        "private_network.dhcp_update",
        "private_network.dhcp_reservation_remove",
    ]
    assert rows[0].event_metadata["mac"] == "de:ad:be:ef:00:01" and rows[0].event_metadata["ip"] == "10.0.0.50"
    assert rows[0].actor_user_id == world.owner.id and rows[0].resource_id == str(network.id)
    assert rows[2].event_metadata["fields"] == ["enabled", "mtu"]
    assert rows[3].event_metadata["ip"] == "10.0.0.51"


@pytest.mark.asyncio
async def test_reservation_limit(world, monkeypatch):
    network = world.network(3000)
    monkeypatch.setattr(dhcp, "MAX_RESERVATIONS", 1)
    await dhcp.add_reservation(world.db, network, "de:ad:be:ef:00:01", "10.0.0.50", actor_user_id=None)
    with pytest.raises(PrivateNetworkError, match="at most 1 reservations"):
        await dhcp.add_reservation(world.db, network, "de:ad:be:ef:00:02", "10.0.0.51", actor_user_id=None)


# --- _pick_ip ---------------------------------------------------------------------


def test_pick_ip_skips_reservations_and_the_range_but_not_the_gateway(world):
    network = world.network(3000, cidr="10.0.0.0/28")
    world.reserve(network, "de:ad:be:ef:00:01", "10.0.0.1")
    network.dhcp_range_start, network.dhcp_range_end = "10.0.0.2", "10.0.0.5"
    network.dhcp_options = {"gateway": "10.0.0.6"}
    world.db.commit()
    assert _pick_ip(network, None) == "10.0.0.6"
    assert _pick_ip(network, None, frozenset({"10.0.0.6"})) == "10.0.0.7"
    with pytest.raises(PrivateNetworkError, match="DHCP range") as caught:
        _pick_ip(network, "10.0.0.3")
    assert caught.value.status_code == 409
    with pytest.raises(PrivateNetworkError, match="reserved") as caught:
        _pick_ip(network, "10.0.0.1")
    assert caught.value.status_code == 409
    assert _pick_ip(network, "10.0.0.9") == "10.0.0.9"


# --- runner config: presences and gating ------------------------------------------


def test_network_without_storage_gets_a_presence(world):
    node = world.node()
    network = world.network(3000)
    world.vm(network, "10.0.0.2", "bc:24:11:aa:bb:01")
    network.dhcp_range_start, network.dhcp_range_end = "10.0.0.100", "10.0.0.150"
    network.dhcp_options = {"gateway": "10.0.0.1", "dns_servers": ["1.1.1.1"], "domain": "lan", "mtu": 1450,
                            "routes": [], "ntp_servers": []}
    expires = _now() + timedelta(hours=1)
    world.db.add(PrivateNetworkDhcpLease(network_id=network.id, mac="aa:bb:cc:00:00:01", ip="10.0.0.101",
                                         expires_at=expires))
    world.db.add(PrivateNetworkDhcpLease(network_id=network.id, mac="aa:bb:cc:00:00:02", ip="10.0.0.102",
                                         expires_at=_now() - timedelta(minutes=1)))
    world.db.commit()
    [entry] = _presences(world, node)
    assert entry == {
        "id": network.id, "vlan": 3000, "network_cidr": "10.0.0.0/24", "endpoint_ip": "10.0.0.254",
        "prefix_len": 24, "preferred_node_id": node.id, "takeover_order": [node.id],
        "dhcp": {
            "enabled": True,
            "lease_seconds": 86400,
            "reservations": [{"mac": "bc:24:11:aa:bb:01", "ip": "10.0.0.2"}],
            "range": {"start": "10.0.0.100", "end": "10.0.0.150"},
            "leases": [{"mac": "aa:bb:cc:00:00:01", "ip": "10.0.0.101", "expires_at": _z(expires)}],
            "options": {"router": "10.0.0.1", "dns_servers": ["1.1.1.1"], "domain": "lan", "mtu": 1450,
                        "routes": [], "ntp_servers": []},
        },
    }


def test_fast_path_networks_are_excluded(world):
    node = world.node()
    world.network(3000, kind=NetworkKind.PROXY_ACCESS)
    access = world.db.query(PrivateNetwork).one()
    assert _presences(world, node) == []
    tenant = world.tenant(access, node, current=node)
    entry = next(t for t in build_runner_config(world.db, node)["tenants"] if t["id"] == tenant.id)
    assert "dhcp" not in entry and "network_id" not in entry
    apply_runner_status(world.db, node, _report(node, networks=[{"id": access.id, "serving": True}]), "10.0.0.1")
    world.db.refresh(access)
    assert access.presence_node_id is None


def test_presence_gating_by_runner_version(world):
    new, old = world.node(), world.node(version="0.3.3")
    network = world.network(3000)
    linked = world.network(3001)
    tenant = world.tenant(linked, new, current=new)
    # A mixed location: the new runner gets an empty networks list and no tenant dhcp.
    config = build_runner_config(world.db, new)
    assert config["networks"] == []
    entry = next(t for t in config["tenants"] if t["id"] == tenant.id)
    assert "dhcp" not in entry and "network_id" not in entry
    # The older runner gets none of the new keys at all.
    old_config = build_runner_config(world.db, old)
    assert "networks" not in old_config
    assert all("dhcp" not in t and "network_id" not in t for t in old_config["tenants"])
    assert dhcp.presence_active(world.db, world.location.id) is False
    # Upgraded (or the old node disabled): presence is active.
    old.enabled = False
    world.db.commit()
    config = build_runner_config(world.db, new)
    assert [n["id"] for n in config["networks"]] == [network.id]
    entry = next(t for t in config["tenants"] if t["id"] == tenant.id)
    assert entry["network_id"] == linked.id and entry["dhcp"]["enabled"] is True


def test_presence_needs_an_enabled_node(world):
    node = world.node()
    node.enabled = False
    world.db.commit()
    assert dhcp.presence_active(world.db, world.location.id) is False
    assert dhcp.presence_active(world.db, None) is False


def test_preferred_node_and_takeover_order(world):
    a, b, c = world.node(), world.node(), world.node()
    world.node(vlan_mode=VlanMode.SWITCH)
    drained = world.node()
    drained.draining = True
    world.db.commit()
    ids = [a.id, b.id, c.id]
    for network_id in range(1, 7):
        order = dhcp.presence_order(network_id, world.nodes)
        preferred = ids[network_id % 3]
        others = [i for i in ids if i != preferred]
        shift = network_id % 2
        assert order == [preferred] + others[shift:] + others[:shift]


def test_ha_on_lists_every_presence_on_every_eligible_node(world):
    a, b = world.node(), world.node()
    switch_node = world.node(vlan_mode=VlanMode.SWITCH)
    network = world.network(3000)
    order = dhcp.presence_order(network.id, world.nodes)
    for node in (a, b):
        [entry] = _presences(world, node)
        assert entry["takeover_order"] == order and entry["preferred_node_id"] == order[0]
    assert _presences(world, switch_node) == []


def test_ha_off_sends_a_presence_only_to_the_preferred_node(world):
    a, b = world.node(), world.node()
    network = world.network(3000)
    row = storage_location.ensure_location(world.db, world.location.id)
    row.ha_enabled = False
    world.db.commit()
    preferred_id = dhcp.presence_order(network.id, world.nodes)[0]
    preferred = a if a.id == preferred_id else b
    other = b if preferred is a else a
    [entry] = _presences(world, preferred)
    assert entry["takeover_order"] == [preferred.id]
    assert _presences(world, other) == []


def test_no_presence_while_storage_is_linked_or_a_tenant_on_the_vlan_has_a_current_node(world):
    node = world.node()
    network = world.network(3000)
    tenant = world.tenant(network, node, current=node)
    assert _presences(world, node) == []
    # Storage terminated but still has a current node: still no presence.
    tenant.terminated_at = _now()
    tenant.destroy_after = _now() + timedelta(days=3)
    world.db.commit()
    assert _presences(world, node) == []
    # A tenant on the same VLAN with a current node (for example a re-linked VLAN) also blocks it.
    tenant.current_node_id = None
    other = world.network(3001)
    stale = world.tenant(other, node, current=node, terminated=True)
    stale.vlan = 3000
    world.db.commit()
    assert [n["id"] for n in _presences(world, node)] == [other.id]
    stale.current_node_id = None
    world.db.commit()
    assert {n["id"] for n in _presences(world, node)} == {network.id, other.id}


# --- status ingestion -------------------------------------------------------------


def test_status_serving_true_false_and_leases(world):
    node, other = world.node(), world.node()
    network = world.network(3000)
    since = _now() - timedelta(minutes=5)
    expires = _now() + timedelta(hours=2)
    leases = [
        {"mac": "AA:BB:CC:00:00:01", "ip": "10.0.0.101", "expires_at": _z(expires), "hostname": "web\x00-1"},
        {"mac": "aa:bb:cc:00:00:02", "ip": "10.0.1.5", "expires_at": _z(expires)},  # outside the network
        {"mac": "aa:bb:cc:00:00:03", "ip": "10.0.0.103", "expires_at": _z(_now() - timedelta(minutes=1))},
    ]
    entry = {"id": network.id, "serving": True, "since": _z(since), "error": "dnsmasq: bad", "leases": leases}
    apply_runner_status(world.db, node, _report(node, networks=[entry]), "10.0.0.1")
    world.db.refresh(network)
    assert network.presence_node_id == node.id and network.dhcp_error == "dnsmasq: bad"
    assert abs((dhcp._aware(network.presence_since) - since).total_seconds()) < 2
    assert [(r.mac, r.ip, r.hostname, r.node_id) for r in network.dhcp_leases] == [
        ("aa:bb:cc:00:00:01", "10.0.0.101", "web-1", node.id)
    ]
    # The next report replaces the leases.
    entry = {"id": network.id, "serving": True, "since": _z(since), "error": None, "leases": [
        {"mac": "aa:bb:cc:00:00:04", "ip": "10.0.0.104", "expires_at": _z(expires)}]}
    apply_runner_status(world.db, node, _report(node, networks=[entry]), "10.0.0.1")
    world.db.refresh(network)
    assert [r.mac for r in network.dhcp_leases] == ["aa:bb:cc:00:00:04"] and network.dhcp_error is None
    # Not serving, from another node: ignored. From the holder: cleared.
    apply_runner_status(world.db, other, _report(other, networks=[{"id": network.id, "serving": False}]), "10.0.0.2")
    world.db.refresh(network)
    assert network.presence_node_id == node.id
    apply_runner_status(world.db, node, _report(node, networks=[{"id": network.id, "serving": False}]), "10.0.0.1")
    world.db.refresh(network)
    assert network.presence_node_id is None and network.presence_since is None and network.dhcp_error is None
    # Nobody serves it: a node that cannot create the presence reports why.
    clash = {"id": network.id, "serving": False, "since": None, "error": "VLAN 3000 is used by tenant 4", "leases": []}
    apply_runner_status(world.db, other, _report(other, networks=[clash]), "10.0.0.2")
    world.db.refresh(network)
    assert network.presence_node_id is None and network.dhcp_error == "VLAN 3000 is used by tenant 4"


def test_presence_cleared_when_the_node_goes_offline_and_leases_pruned(world):
    node = world.node()
    network = world.network(3000)
    entry = {"id": network.id, "serving": True, "leases": [
        {"mac": "aa:bb:cc:00:00:01", "ip": "10.0.0.101", "expires_at": _z(_now() + timedelta(hours=1))}]}
    apply_runner_status(world.db, node, _report(node, networks=[entry]), "10.0.0.1")
    world.db.add(PrivateNetworkDhcpLease(network_id=network.id, mac="aa:bb:cc:00:00:09", ip="10.0.0.109",
                                         expires_at=_now() - timedelta(minutes=1)))
    world.db.commit()
    node.last_seen_at = datetime.utcnow() - timedelta(hours=1)
    world.db.commit()
    storage_ha.reconcile(world.db)
    world.db.refresh(network)
    assert network.presence_node_id is None and network.presence_since is None
    assert [r.mac for r in network.dhcp_leases] == ["aa:bb:cc:00:00:01"]


def test_tenant_dhcp_report_sets_error_and_leases(world):
    node = world.node()
    network = world.network(3000)
    tenant = world.tenant(network, node, current=node)
    expires = _now() + timedelta(hours=1)
    entry = {"id": tenant.id, "state": "ready", "shares": [], "serving": True,
             "dhcp": {"error": "no range", "leases": [{"mac": "aa:bb:cc:00:00:01", "ip": "10.0.0.101",
                                                       "expires_at": _z(expires), "hostname": "db"}]}}
    apply_runner_status(world.db, node, _report(node, tenants=[entry]), "10.0.0.1")
    world.db.refresh(network)
    assert network.dhcp_error == "no range"
    assert [(r.mac, r.hostname) for r in network.dhcp_leases] == [("aa:bb:cc:00:00:01", "db")]
    view = dhcp.owner_view(world.db, network)
    assert view["served"] is True and view["serving_node"] == node.name
    assert view["leases"] == [{"mac": "aa:bb:cc:00:00:01", "ip": "10.0.0.101", "hostname": "db",
                               "expires_at": _z(expires)}]
    presence = dhcp.staff_fields(world.db, network)["presence"]
    assert presence["node_id"] == node.id and presence["dhcp_error"] == "no range"


def test_owner_and_member_views(world):
    node = world.node()
    network = world.network(3000)
    member = User(username="member", email="member@example.com", is_admin=False)
    member.set_password("secret123")
    world.db.add(member)
    world.db.commit()
    mine = world.vm(network, "10.0.0.2", "bc:24:11:aa:bb:01")
    theirs = world.vm(network, "10.0.0.3", "bc:24:11:aa:bb:02", owner=member)
    world.reserve(network, "de:ad:be:ef:00:01", "10.0.0.50", "printer")
    network.presence_node_id = node.id
    network.presence_since = _now()
    world.db.commit()

    view = dhcp.owner_view(world.db, network)
    assert set(view) == {
        "network_id", "available", "served", "serving_node", "enabled", "lease_seconds", "lease_seconds_default",
        "lease_seconds_set", "range_start", "range_end", "gateway", "dns_servers", "domain", "mtu", "routes",
        "ntp_servers", "endpoint_ip", "reservations", "automatic", "leases", "warnings", "error",
    }
    assert view["available"] is True and view["served"] is True and view["serving_node"] == node.name
    assert view["lease_seconds"] == 86400 and view["lease_seconds_set"] is None
    assert view["endpoint_ip"] == "10.0.0.254"
    assert view["reservations"][0]["label"] == "printer"
    automatic = {a["attachment_id"]: a for a in view["automatic"]}
    assert automatic[mine.id]["macs"] == ["bc:24:11:aa:bb:01"]
    assert automatic[theirs.id] == {"attachment_id": theirs.id, "ip": "10.0.0.3", "macs": [],
                                    "label": "member attachment"}
    staff = dhcp.owner_view(world.db, network, staff=True)
    assert {a["attachment_id"]: a for a in staff["automatic"]}[theirs.id]["macs"] == ["bc:24:11:aa:bb:02"]

    member_view = dhcp.member_view(world.db, network)
    assert set(member_view) == {"enabled", "served", "lease_seconds", "gateway", "dns_servers", "domain", "mtu",
                                "routes", "ntp_servers"}
    assert member_view["served"] is True
