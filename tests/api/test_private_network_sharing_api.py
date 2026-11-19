"""Private network sharing through the client API: invitations, members, owner-only management and lifecycle."""
from datetime import datetime, timedelta, timezone

import pytest

from app.dao.private_network_dao import PrivateNetworkDAO
from app.dao.proxmox_inventory_dao import ProxmoxInventoryDAO
from app.dao.service_dao import ServiceDAO
from app.models.cable_run import CableRun
from app.models.location import Location
from app.models.network_port import NetworkPort
from app.models.network_switch import NetworkSwitch
from app.models.private_network import (
    InvitationStatus,
    NetworkKind,
    PrivateNetwork,
    PrivateNetworkAttachment,
    PrivateNetworkInvitation,
    PrivateNetworkMember,
    PrivateNetworkStatus,
    SegmentationType,
)
from app.models.reseller import Reseller
from app.models.server import Server
from app.models.service import ProvisioningSource, ServiceStatus
from app.models.switch_port import SwitchPort
from app.models.user import User
from app.services import private_network_service as pns
from app.services import private_network_sharing as sharing

ADMIN = "/api/admin/private-networks"
STORAGE_ADMIN = "/api/admin/storage"
STORAGE = "/api/client/storage"
CLIENT = "/api/client/private-networks"


class FakeSwitch:
    def __init__(self):
        self.tagged = {}

    async def create_vlan(self, vlan_id, name):
        return None

    async def delete_vlan(self, vlan_id, name=None):
        return None

    async def get_port_vlans(self, port):
        tagged = self.tagged.get(port, set())
        return {
            "interface": port,
            "mode": "trunk" if tagged else "access",
            "native_vlan": 100,
            "allowed_all": False,
            "allowed_vlans": sorted(tagged | {100}),
        }

    async def add_port_vlan(self, port, vlan_id):
        self.tagged.setdefault(port, set()).add(vlan_id)

    async def remove_port_vlan(self, port, vlan_id):
        self.tagged.get(port, set()).discard(vlan_id)


class FakeProxmox:
    def __init__(self):
        self.nics = set()
        self.fail = False

    async def attach_private_nic(self, bridge, vlan_tag, *, ip_cidr=None, mac=None, vmid=None):
        self.nics.add((vmid, vlan_tag))
        return {"changed": True, "index": 1, "mac": "BC:24:11:AA:BB:CC"}

    async def detach_private_nic(self, bridge, vlan_tag, *, vmid=None):
        if self.fail:
            raise RuntimeError("hypervisor unreachable")
        self.nics.discard((vmid, vlan_tag))
        return {"changed": True, "removed": [1]}


@pytest.fixture
def proxmox(monkeypatch):
    fake = FakeProxmox()

    async def resolve(db, service):
        return fake, None, "epyc", service.vm.proxmox_vmid

    monkeypatch.setattr(pns, "resolve_proxmox_plugin_for_service", resolve)
    return fake


@pytest.fixture
def world(db_session, monkeypatch, mock_redis, proxmox):
    import app.core.rate_limit as rate_limit_module

    monkeypatch.setattr(rate_limit_module, "redis_client", mock_redis)
    switches = {}
    monkeypatch.setattr(pns, "_switch_plugin", lambda switch: switches.setdefault(switch.name, FakeSwitch()))
    location = Location(name="London", description="")
    db_session.add(location)
    db_session.commit()
    pool = PrivateNetworkDAO.create_pool(
        db_session,
        location_id=location.id,
        vlan_start=2000,
        vlan_end=2009,
        bridge="vmbr0",
        max_networks_per_client=2,
        max_networks_per_service=4,
    )
    switch = NetworkSwitch(name="tor-0", location_id=location.id, plugin_name="nxos", plugin_config={})
    server = Server(
        name="store-0", server_ip="10.88.0.1", location_id=location.id, plugin_name="ipmi", plugin_config={}
    )
    db_session.add_all([switch, server])
    db_session.flush()
    port = NetworkPort(server_id=server.id, name="eth0", speed_mbps=25000, pxe_boot=True)
    switch_port = SwitchPort(switch_id=switch.id, name="Ethernet1/1")
    db_session.add_all([port, switch_port])
    db_session.flush()
    db_session.add(CableRun(end_a_server_port_id=port.id, end_b_switch_port_id=switch_port.id))
    db_session.commit()

    class World:
        pass

    w = World()
    w.db, w.location, w.pool, w.server, w.proxmox = db_session, location, pool, server, proxmox
    return w


def _login(client, username, password):
    response = client.post("/api/users/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _user(db, username, reseller_id=None):
    user = User(username=username, email=f"{username}@example.com", is_admin=False, reseller_id=reseller_id)
    user.set_password("secret123")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


class Person:
    def __init__(self, client, db, name, reseller_id=None):
        self.user = _user(db, name, reseller_id)
        self.headers = _login(client, name, "secret123")
        self.id = self.user.id


@pytest.fixture
def admin(client, test_admin_user):
    return _login(client, test_admin_user.username, "adminpassword123")


@pytest.fixture
def alice(client, db_session):
    return Person(client, db_session, "alice")


@pytest.fixture
def bob(client, db_session):
    return Person(client, db_session, "bob")


@pytest.fixture
def carol(client, db_session):
    return Person(client, db_session, "carol")


_vm_count = 0


def _vm(db, owner, location, name):
    global _vm_count
    _vm_count += 1
    cluster = ProxmoxInventoryDAO.create_cluster(
        db,
        name=f"{name}-cluster",
        api_url="https://pve.example:8006",
        username="root@pam",
        password="x",
        location_id=location.id,
    )
    return ServiceDAO.create_vm(
        db,
        name=name,
        owner_user_id=owner.id,
        provisioning_source=ProvisioningSource.INTERNAL,
        status=ServiceStatus.ACTIVE,
        proxmox_cluster_id=cluster.id,
        proxmox_node_name="epyc",
        proxmox_vmid=100 + _vm_count,
    )


def _network(client, person, world, name="backend"):
    response = client.post(CLIENT, headers=person.headers, json={"name": name, "location_id": world.location.id})
    assert response.status_code == 201, response.text
    return response.json()


def _invite(client, owner, network, who):
    return client.post(f"{CLIENT}/{network['id']}/invitations", headers=owner.headers, json={"invitee": who})


def _join(client, owner, member, network, who=None):
    invited = _invite(client, owner, network, who or member.user.email)
    assert invited.status_code == 201, invited.text
    mine = client.get(f"{CLIENT}/invitations", headers=member.headers).json()
    assert len(mine) == 1, mine
    accepted = client.post(f"{CLIENT}/invitations/{mine[0]['id']}/accept", headers=member.headers)
    assert accepted.status_code == 200, accepted.text
    return accepted.json()


def _member_attach(client, member, network, vm, ip=None):
    body = {"service_id": vm.id}
    if ip:
        body["ip_address"] = ip
    return client.post(f"{CLIENT}/shared/{network['id']}/attachments", headers=member.headers, json=body)


# --- the happy path ---


def test_invite_accept_attach_and_leave(client, world, alice, bob, db_session):
    network = _network(client, alice, world)
    alice_vm = _vm(db_session, alice.user, world.location, "alice-vm")
    bob_vm = _vm(db_session, bob.user, world.location, "bob-vm")

    invited = _invite(client, alice, network, "BOB@example.com")
    assert invited.status_code == 201 and invited.json()["invitee"] == "bob@example.com"
    incoming = client.get(f"{CLIENT}/invitations", headers=bob.headers).json()
    assert incoming[0]["network_name"] == "backend" and incoming[0]["owner"] == "alice"
    assert "cidr" not in incoming[0]

    joined = client.post(f"{CLIENT}/invitations/{incoming[0]['id']}/accept", headers=bob.headers).json()
    assert joined["role"] == "member" and joined["owner"] == "alice"
    assert "vlan_id" not in joined and "owner_user_id" not in joined and "switch_synced" not in joined
    assert client.get(f"{CLIENT}/invitations", headers=bob.headers).json() == []

    assert client.post(
        f"{CLIENT}/{network['id']}/attachments", headers=alice.headers, json={"service_id": alice_vm.id}
    ).status_code == 200
    attached = _member_attach(client, bob, network, bob_vm)
    assert attached.status_code == 200, attached.text
    mine = attached.json()["attachments"]
    assert [a["service_name"] for a in mine] == ["bob-vm"] and mine[0]["ip_address"] == "10.0.0.2"
    assert (bob_vm.vm.proxmox_vmid, 2000) in world.proxmox.nics

    # The owner sees the member and the address, never the member's service.
    owner_view = client.get(f"{CLIENT}/{network['id']}", headers=alice.headers).json()
    assert [a["service_name"] for a in owner_view["attachments"]] == ["alice-vm"]
    member = owner_view["members"][0]
    assert member["username"] == "bob" and member["servers"] == [
        {"ip_address": "10.0.0.2", "kind": "vm", "state": "active"}
    ]
    assert "bob-vm" not in str(owner_view) and "BC:24:11" not in str(owner_view["members"])
    assert owner_view["invitations"] == [] and owner_view["member_limit"] == 5

    # The member's own network list stays empty: the shared network is not theirs.
    assert client.get(CLIENT, headers=bob.headers).json() == []
    assert [n["id"] for n in client.get(f"{CLIENT}/shared", headers=bob.headers).json()] == [network["id"]]

    detached = client.delete(
        f"{CLIENT}/shared/{network['id']}/attachments/{mine[0]['id']}", headers=bob.headers
    )
    assert detached.status_code == 200 and detached.json()["attachments"] == []
    assert (bob_vm.vm.proxmox_vmid, 2000) not in world.proxmox.nics

    assert _member_attach(client, bob, network, bob_vm).status_code == 200
    assert client.post(f"{CLIENT}/shared/{network['id']}/leave", headers=bob.headers).status_code == 204
    assert world.proxmox.nics == {(alice_vm.vm.proxmox_vmid, 2000)}
    assert client.get(f"{CLIENT}/shared/{network['id']}", headers=bob.headers).status_code == 404
    assert client.get(f"{CLIENT}/shared", headers=bob.headers).json() == []
    db_session.expire_all()
    assert PrivateNetworkDAO.count_members(db_session, network["id"]) == 0


def test_decline_and_cancel(client, world, alice, bob, db_session):
    network = _network(client, alice, world)
    invited = _invite(client, alice, network, "bob").json()
    assert client.post(f"{CLIENT}/invitations/{invited['id']}/decline", headers=bob.headers).status_code == 204
    assert client.get(f"{CLIENT}/invitations", headers=bob.headers).json() == []
    assert client.post(f"{CLIENT}/invitations/{invited['id']}/accept", headers=bob.headers).status_code == 404

    second = _invite(client, alice, network, "bob").json()
    listed = client.get(f"{CLIENT}/{network['id']}", headers=alice.headers).json()
    assert [i["id"] for i in listed["invitations"]] == [second["id"]]
    assert client.delete(f"{CLIENT}/{network['id']}/invitations/{second['id']}", headers=alice.headers).status_code == 204
    assert client.get(f"{CLIENT}/invitations", headers=bob.headers).json() == []
    assert client.post(f"{CLIENT}/invitations/{second['id']}/accept", headers=bob.headers).status_code == 404
    assert client.delete(f"{CLIENT}/{network['id']}/invitations/{second['id']}", headers=alice.headers).status_code == 404


def test_expired_invitation_cannot_be_accepted(client, world, alice, bob, db_session):
    network = _network(client, alice, world)
    invited = _invite(client, alice, network, "bob").json()
    row = db_session.get(PrivateNetworkInvitation, invited["id"])
    row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db_session.commit()
    assert client.get(f"{CLIENT}/invitations", headers=bob.headers).json() == []
    assert client.post(f"{CLIENT}/invitations/{invited['id']}/accept", headers=bob.headers).status_code == 404
    db_session.expire_all()
    assert db_session.get(PrivateNetworkInvitation, invited["id"]).status == InvitationStatus.EXPIRED


# --- who can be invited ---


def test_invitation_never_reveals_whether_an_account_exists(client, world, alice, bob, carol, db_session):
    network = _network(client, alice, world)
    real = _invite(client, alice, network, "bob@example.com")
    missing = _invite(client, alice, network, "nobody@example.com")
    assert real.status_code == missing.status_code == 201
    assert set(real.json()) == set(missing.json())
    # Only the real account can ever see or answer its invitation.
    assert len(client.get(f"{CLIENT}/invitations", headers=bob.headers).json()) == 1
    assert client.get(f"{CLIENT}/invitations", headers=carol.headers).json() == []
    assert client.post(f"{CLIENT}/invitations/{missing.json()['id']}/accept", headers=carol.headers).status_code == 404
    assert client.post(f"{CLIENT}/invitations/{real.json()['id']}/accept", headers=carol.headers).status_code == 404
    # Inviting the owner, staff or a reseller looks the same as inviting nobody.
    assert _invite(client, alice, network, "alice").status_code == 201
    assert PrivateNetworkDAO.count_pending_invitations(db_session, network["id"]) == 3
    # Asking again gives the same invitation back.
    assert _invite(client, alice, network, "Bob@Example.com").json()["id"] == real.json()["id"]


def test_sharing_does_not_cross_a_reseller_boundary(client, world, alice, db_session):
    reseller_user = _user(db_session, "reseller-owner")
    db_session.add_all([Reseller(user_id=reseller_user.id)])
    db_session.commit()
    reseller = db_session.query(Reseller).first()
    outsider = Person(client, db_session, "dave", reseller_id=reseller.id)
    network = _network(client, alice, world)

    assert _invite(client, alice, network, "dave").status_code == 201
    assert client.get(f"{CLIENT}/invitations", headers=outsider.headers).json() == []

    # A client of the reseller cannot share into a direct client either, and an existing member
    # whose account moves into a reseller is removed by the repair loop.
    bob = Person(client, db_session, "bob")
    _join(client, alice, bob, network)
    user = db_session.get(User, bob.id)
    user.reseller_id = reseller.id
    db_session.commit()
    import asyncio

    asyncio.run(sharing.reconcile_sharing(db_session))
    db_session.expire_all()
    assert PrivateNetworkDAO.count_members(db_session, network["id"]) == 0


def test_staff_and_reseller_accounts_are_never_members(client, world, alice, admin, db_session, test_admin_user):
    network = _network(client, alice, world)
    _invite(client, alice, network, test_admin_user.username)
    assert client.get(f"{CLIENT}/invitations", headers=admin).status_code == 403
    assert PrivateNetworkDAO.pending_invitations_for_user(db_session, test_admin_user.id) == []


def test_member_limit_counts_pending_invitations(client, world, alice, db_session):
    world.pool.max_members_per_network = 2
    db_session.commit()
    network = _network(client, alice, world)
    assert _invite(client, alice, network, "a@example.com").status_code == 201
    assert _invite(client, alice, network, "b@example.com").status_code == 201
    assert _invite(client, alice, network, "c@example.com").status_code == 409
    world.pool.max_members_per_network = 0
    db_session.commit()
    assert _invite(client, alice, network, "d@example.com").status_code == 409


def test_owned_network_limit_ignores_networks_joined(client, world, alice, bob, carol, db_session):
    first = _network(client, alice, world, "one")
    second = _network(client, carol, world, "two")
    _join(client, alice, bob, first)
    _join(client, carol, bob, second)
    assert _network(client, bob, world, "mine")["name"] == "mine"
    assert _network(client, bob, world, "mine2")["name"] == "mine2"
    third = client.post(CLIENT, headers=bob.headers, json={"name": "x", "location_id": world.location.id})
    assert third.status_code == 409


def test_proxy_access_networks_cannot_be_shared(client, world, alice, bob, db_session):
    network = PrivateNetwork(
        name="Proxy fast path",
        owner_user_id=alice.id,
        location_id=world.location.id,
        segmentation_type=SegmentationType.VLAN,
        segment_id=2005,
        cidr="100.80.0.0/16",
        kind=NetworkKind.PROXY_ACCESS,
        status=PrivateNetworkStatus.ACTIVE,
    )
    db_session.add(network)
    db_session.commit()
    assert _invite(client, alice, {"id": network.id}, "bob").status_code == 409
    db_session.add(PrivateNetworkMember(network_id=network.id, user_id=bob.id))
    db_session.commit()
    assert client.get(f"{CLIENT}/shared/{network.id}", headers=bob.headers).status_code == 404
    assert client.get(f"{CLIENT}/shared", headers=bob.headers).json() == []


# --- owner-only actions are refused to members and strangers ---


@pytest.mark.parametrize("who", ["member", "stranger"])
def test_owner_actions_are_refused_to_members_and_strangers(client, world, alice, bob, carol, db_session, who):
    network = _network(client, alice, world)
    alice_vm = _vm(db_session, alice.user, world.location, "alice-vm")
    bob_vm = _vm(db_session, bob.user, world.location, "bob-vm")
    _join(client, alice, bob, network)
    own = client.post(
        f"{CLIENT}/{network['id']}/attachments", headers=alice.headers, json={"service_id": alice_vm.id}
    ).json()["attachments"][0]
    assert _member_attach(client, bob, network, bob_vm).status_code == 200
    pending = _invite(client, alice, network, "pending@example.com").json()

    actor = bob if who == "member" else carol
    base = f"{CLIENT}/{network['id']}"
    mine = _vm(db_session, actor.user, world.location, "actor-vm")
    attempts = [
        ("get", base, None),
        ("delete", base, None),
        ("post", f"{base}/attachments", {"service_id": mine.id}),
        ("delete", f"{base}/attachments/{own['id']}", None),
        ("post", f"{base}/invitations", {"invitee": "carol"}),
        ("delete", f"{base}/invitations/{pending['id']}", None),
        ("delete", f"{base}/members/{bob.id}", None),
    ]
    for method, url, body in attempts:
        kwargs = {"headers": actor.headers}
        if body is not None:
            kwargs["json"] = body
        assert getattr(client, method)(url, **kwargs).status_code == 404, (who, method, url)
    # None of it changed anything.
    db_session.expire_all()
    assert PrivateNetworkDAO.get_network(db_session, network["id"]) is not None
    assert PrivateNetworkDAO.count_members(db_session, network["id"]) == 1
    assert PrivateNetworkDAO.count_pending_invitations(db_session, network["id"]) == 1
    assert len(PrivateNetworkDAO.get_network(db_session, network["id"]).attachments) == 2


def test_owner_cannot_attach_or_detach_a_members_server(client, world, alice, bob, db_session):
    network = _network(client, alice, world)
    bob_vm = _vm(db_session, bob.user, world.location, "bob-vm")
    carol_vm = _vm(db_session, _user(db_session, "carol"), world.location, "carol-vm")
    _join(client, alice, bob, network)
    attached = _member_attach(client, bob, network, bob_vm).json()["attachments"][0]
    # Alice cannot attach a service she does not own, member or not, nor detach the member's attachment.
    for vm in (bob_vm, carol_vm):
        response = client.post(
            f"{CLIENT}/{network['id']}/attachments", headers=alice.headers, json={"service_id": vm.id}
        )
        assert response.status_code == 404
    assert client.delete(
        f"{CLIENT}/{network['id']}/attachments/{attached['id']}", headers=alice.headers
    ).status_code == 404
    assert (bob_vm.vm.proxmox_vmid, 2000) in world.proxmox.nics


def test_member_cannot_attach_someone_elses_service_or_use_other_locations(client, world, alice, bob, carol, db_session):
    network = _network(client, alice, world)
    alice_vm = _vm(db_session, alice.user, world.location, "alice-vm")
    carol_vm = _vm(db_session, carol.user, world.location, "carol-vm")
    _join(client, alice, bob, network)
    assert _member_attach(client, bob, network, alice_vm).status_code == 404
    assert _member_attach(client, bob, network, carol_vm).status_code == 404
    elsewhere = Location(name="Paris", description="")
    db_session.add(elsewhere)
    db_session.commit()
    far_vm = _vm(db_session, bob.user, elsewhere, "far-vm")
    assert _member_attach(client, bob, network, far_vm).status_code == 409


@pytest.mark.parametrize("who", ["stranger", "owner"])
def test_member_actions_are_refused_to_strangers_and_the_owner(client, world, alice, bob, carol, db_session, who):
    network = _network(client, alice, world)
    bob_vm = _vm(db_session, bob.user, world.location, "bob-vm")
    _join(client, alice, bob, network)
    attached = _member_attach(client, bob, network, bob_vm).json()["attachments"][0]
    actor = carol if who == "stranger" else alice
    mine = _vm(db_session, actor.user, world.location, "actor-vm")
    base = f"{CLIENT}/shared/{network['id']}"
    attempts = [
        ("get", base, None),
        ("get", f"{base}/storage", None),
        ("post", f"{base}/attachments", {"service_id": mine.id}),
        ("delete", f"{base}/attachments/{attached['id']}", None),
        ("post", f"{base}/leave", None),
    ]
    for method, url, body in attempts:
        kwargs = {"headers": actor.headers}
        if body is not None:
            kwargs["json"] = body
        assert getattr(client, method)(url, **kwargs).status_code == 404, (who, method, url)
    assert client.get(f"{CLIENT}/shared", headers=actor.headers).json() == []
    assert (bob_vm.vm.proxmox_vmid, 2000) in world.proxmox.nics


def test_anonymous_staff_and_api_key_callers_are_refused(client, world, alice, admin):
    network = _network(client, alice, world)
    for url in (
        f"{CLIENT}/invitations",
        f"{CLIENT}/shared",
        f"{CLIENT}/shared/{network['id']}",
    ):
        assert client.get(url).status_code in (401, 403), url
        assert client.get(url, headers=admin).status_code == 403, url
    assert client.post(f"{CLIENT}/{network['id']}/invitations", json={"invitee": "bob"}).status_code in (401, 403)
    assert client.post(
        f"{CLIENT}/{network['id']}/invitations", headers=admin, json={"invitee": "bob"}
    ).status_code == 403


# --- removal and lifecycle ---


def test_removing_a_member_ends_access_at_once(client, world, alice, bob, db_session):
    network = _network(client, alice, world)
    bob_vm = _vm(db_session, bob.user, world.location, "bob-vm")
    _join(client, alice, bob, network)
    attached = _member_attach(client, bob, network, bob_vm).json()["attachments"][0]
    assert world.proxmox.nics

    removed = client.delete(f"{CLIENT}/{network['id']}/members/{bob.id}", headers=alice.headers)
    assert removed.status_code == 204
    assert world.proxmox.nics == set()
    db_session.expire_all()
    assert PrivateNetworkDAO.get_network(db_session, network["id"]).attachments == []
    assert client.get(f"{CLIENT}/shared/{network['id']}", headers=bob.headers).status_code == 404
    assert client.get(f"{CLIENT}/shared/{network['id']}/storage", headers=bob.headers).status_code == 404
    assert _member_attach(client, bob, network, bob_vm).status_code == 404
    assert client.delete(
        f"{CLIENT}/shared/{network['id']}/attachments/{attached['id']}", headers=bob.headers
    ).status_code == 404
    assert client.delete(f"{CLIENT}/{network['id']}/members/{bob.id}", headers=alice.headers).status_code == 404
    # The address is free for the owner again.
    alice_vm = _vm(db_session, alice.user, world.location, "alice-vm")
    again = client.post(
        f"{CLIENT}/{network['id']}/attachments", headers=alice.headers, json={"service_id": alice_vm.id}
    )
    assert again.json()["attachments"][0]["ip_address"] == "10.0.0.1"


def test_removal_with_an_unreachable_hypervisor_still_ends_access(client, world, alice, bob, db_session):
    import asyncio

    network = _network(client, alice, world)
    bob_vm = _vm(db_session, bob.user, world.location, "bob-vm")
    _join(client, alice, bob, network)
    _member_attach(client, bob, network, bob_vm)

    world.proxmox.fail = True
    assert client.delete(f"{CLIENT}/{network['id']}/members/{bob.id}", headers=alice.headers).status_code == 204
    # Access is gone even though the NIC is still there; the repair loop retries the detach.
    assert client.get(f"{CLIENT}/shared/{network['id']}", headers=bob.headers).status_code == 404
    db_session.expire_all()
    leftover = PrivateNetworkDAO.get_network(db_session, network["id"]).attachments
    assert [a.state.value for a in leftover] == ["detaching"]
    world.proxmox.fail = False
    asyncio.run(pns.PrivateNetworkService.reconcile(db_session))
    db_session.expire_all()
    assert PrivateNetworkDAO.get_network(db_session, network["id"]).attachments == []
    assert world.proxmox.nics == set()


def test_staff_can_see_and_remove_members(client, world, alice, bob, admin, db_session):
    network = _network(client, alice, world)
    bob_vm = _vm(db_session, bob.user, world.location, "bob-vm")
    _join(client, alice, bob, network)
    _member_attach(client, bob, network, bob_vm)
    seen = client.get(f"{ADMIN}/{network['id']}", headers=admin).json()
    assert seen["members"][0]["username"] == "bob" and seen["members"][0]["servers"][0]["kind"] == "vm"
    # Staff see every attachment, the member's included.
    assert [a["service_name"] for a in seen["attachments"]] == ["bob-vm"]
    assert client.delete(f"{ADMIN}/{network['id']}/members/{bob.id}", headers=alice.headers).status_code == 403
    assert client.delete(f"{ADMIN}/{network['id']}/members/{bob.id}", headers=admin).status_code == 204
    assert world.proxmox.nics == set()
    assert client.delete(f"{ADMIN}/{network['id']}/members/{bob.id}", headers=admin).status_code == 404


def test_deleting_the_network_removes_members_and_their_servers(client, world, alice, bob, db_session):
    network = _network(client, alice, world)
    bob_vm = _vm(db_session, bob.user, world.location, "bob-vm")
    _join(client, alice, bob, network)
    _member_attach(client, bob, network, bob_vm)
    _invite(client, alice, network, "pending@example.com")

    assert client.delete(f"{CLIENT}/{network['id']}", headers=alice.headers).status_code == 204
    assert world.proxmox.nics == set()
    assert client.get(f"{CLIENT}/shared", headers=bob.headers).json() == []
    db_session.expire_all()
    assert PrivateNetworkDAO.get_network(db_session, network["id"]) is None
    assert db_session.query(PrivateNetworkMember).count() == 0
    assert db_session.query(PrivateNetworkAttachment).count() == 0


def test_owner_delete_still_needs_own_servers_detached(client, world, alice, bob, db_session):
    network = _network(client, alice, world)
    alice_vm = _vm(db_session, alice.user, world.location, "alice-vm")
    _join(client, alice, bob, network)
    client.post(f"{CLIENT}/{network['id']}/attachments", headers=alice.headers, json={"service_id": alice_vm.id})
    assert client.delete(f"{CLIENT}/{network['id']}", headers=alice.headers).status_code == 409
    assert client.get(f"{CLIENT}/shared/{network['id']}", headers=bob.headers).status_code == 200


def test_members_are_removed_after_the_owner_has_no_live_service(client, world, alice, bob, db_session):
    import asyncio

    network = _network(client, alice, world)
    bob_vm = _vm(db_session, bob.user, world.location, "bob-vm")
    _join(client, alice, bob, network)
    _member_attach(client, bob, network, bob_vm)
    # Alice has no service at all: the first pass starts the grace period.
    counts = asyncio.run(sharing.reconcile_sharing(db_session))
    db_session.expire_all()
    row = PrivateNetworkDAO.get_network(db_session, network["id"])
    assert counts["removed"] == 0 and row.owner_gone_since is not None
    assert client.get(f"{CLIENT}/shared/{network['id']}", headers=bob.headers).status_code == 200

    # A live service of the owner's clears it.
    alice_vm = _vm(db_session, alice.user, world.location, "alice-vm")
    asyncio.run(sharing.reconcile_sharing(db_session))
    db_session.expire_all()
    assert PrivateNetworkDAO.get_network(db_session, network["id"]).owner_gone_since is None

    alice_vm.status = ServiceStatus.TERMINATED
    db_session.commit()
    asyncio.run(sharing.reconcile_sharing(db_session))
    row = PrivateNetworkDAO.get_network(db_session, network["id"])
    row.owner_gone_since = datetime.now(timezone.utc) - timedelta(days=31)
    db_session.commit()
    counts = asyncio.run(sharing.reconcile_sharing(db_session))
    assert counts["removed"] == 1
    db_session.expire_all()
    assert world.proxmox.nics == set()
    assert client.get(f"{CLIENT}/shared/{network['id']}", headers=bob.headers).status_code == 404
    assert PrivateNetworkDAO.get_network(db_session, network["id"]) is not None


def test_a_network_whose_owner_is_deleted_is_wound_up(client, world, alice, bob, db_session):
    import asyncio

    network = _network(client, alice, world)
    bob_vm = _vm(db_session, bob.user, world.location, "bob-vm")
    _join(client, alice, bob, network)
    _member_attach(client, bob, network, bob_vm)
    row = PrivateNetworkDAO.get_network(db_session, network["id"])
    row.owner_user_id = None
    db_session.commit()
    asyncio.run(sharing.reconcile_sharing(db_session))
    db_session.expire_all()
    assert world.proxmox.nics == set()
    assert PrivateNetworkDAO.get_network(db_session, network["id"]) is None
    assert client.get(f"{CLIENT}/shared", headers=bob.headers).json() == []


def test_terminated_member_service_keeps_the_membership(client, world, alice, bob, db_session):
    import asyncio

    network = _network(client, alice, world)
    bob_vm = _vm(db_session, bob.user, world.location, "bob-vm")
    _join(client, alice, bob, network)
    _member_attach(client, bob, network, bob_vm)
    bob_vm.status = ServiceStatus.TERMINATED
    db_session.commit()
    asyncio.run(pns.PrivateNetworkService.reconcile(db_session))
    db_session.expire_all()
    assert PrivateNetworkDAO.get_network(db_session, network["id"]).attachments == []
    assert client.get(f"{CLIENT}/shared/{network['id']}", headers=bob.headers).status_code == 200


def test_repair_detaches_a_service_that_changed_to_a_stranger(client, world, alice, bob, carol, db_session):
    import asyncio

    network = _network(client, alice, world)
    bob_vm = _vm(db_session, bob.user, world.location, "bob-vm")
    _join(client, alice, bob, network)
    _member_attach(client, bob, network, bob_vm)
    bob_vm.owner_user_id = carol.id
    db_session.commit()
    asyncio.run(pns.PrivateNetworkService.reconcile(db_session))
    db_session.expire_all()
    assert PrivateNetworkDAO.get_network(db_session, network["id"]).attachments == []
    assert world.proxmox.nics == set()


# --- storage ---


def _storage_for(client, admin, owner, network_id):
    node = client.post(f"{STORAGE_ADMIN}/nodes", headers=admin, json={"server_id": owner[1].id})
    assert node.status_code == 201, node.text
    response = client.post(
        f"{STORAGE_ADMIN}/services",
        headers=admin,
        json={"owner_user_id": owner[0].id, "name": "alice-nfs", "size_gb": 100, "network_id": network_id},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_members_see_open_shares_only_and_cannot_manage_storage(client, world, alice, bob, carol, admin, db_session):
    network = _network(client, alice, world)
    _join(client, alice, bob, network)
    assert client.get(f"{CLIENT}/shared/{network['id']}/storage", headers=bob.headers).json() == {
        "linked": False, "endpoint_ip": None, "status": None, "shares": []
    }
    storage = _storage_for(client, admin, (alice.user, world.server), network["id"])
    sid = storage["service_id"]
    for body in (
        {"name": "open", "quota_gb": 10},
        {"name": "closed", "quota_gb": 10, "clients": ["10.0.0.50"]},
    ):
        assert client.post(f"{STORAGE}/{sid}/shares", headers=alice.headers, json=body).status_code == 201

    view = client.get(f"{CLIENT}/shared/{network['id']}/storage", headers=bob.headers).json()
    assert view["linked"] is True and view["endpoint_ip"] == "10.0.0.254"
    assert [s["name"] for s in view["shares"]] == ["open"]
    assert view["shares"][0]["mount_command"] == "mount -t nfs4 10.0.0.254:/open /mnt/open"
    assert "quota_gb" not in view["shares"][0] and "clients" not in view["shares"][0]
    assert client.get(f"{CLIENT}/shared/{network['id']}/storage", headers=carol.headers).status_code == 404

    # The storage itself stays the owner's: members and strangers get "not found" on every storage route.
    share_id = client.get(f"{STORAGE}/{sid}", headers=alice.headers).json()["shares"][0]["id"]
    for actor in (bob, carol):
        for method, url, body in (
            ("get", f"{STORAGE}/{sid}", None),
            ("get", f"{STORAGE}/{sid}/networks", None),
            ("post", f"{STORAGE}/{sid}/shares", {"name": "evil", "quota_gb": 1}),
            ("patch", f"{STORAGE}/{sid}/shares/{share_id}", {"quota_gb": 1}),
            ("delete", f"{STORAGE}/{sid}/shares/{share_id}", None),
            ("post", f"{STORAGE}/{sid}/sftp-users", {"name": "x"}),
            ("post", f"{STORAGE}/{sid}/link", {"network_id": network["id"]}),
        ):
            kwargs = {"headers": actor.headers}
            if body is not None:
                kwargs["json"] = body
            assert getattr(client, method)(url, **kwargs).status_code == 404, (actor.user.username, method, url)
    assert client.get(STORAGE, headers=bob.headers).json() == []
    # Members cannot delete a network that holds the owner's storage either.
    assert client.delete(f"{CLIENT}/{network['id']}", headers=bob.headers).status_code == 404
    assert client.delete(f"{CLIENT}/{network['id']}", headers=alice.headers).status_code == 409


def test_member_is_not_given_an_address_a_restricted_share_lists(client, world, alice, bob, admin, db_session):
    network = _network(client, alice, world)
    bob_vm = _vm(db_session, bob.user, world.location, "bob-vm")
    _join(client, alice, bob, network)
    storage = _storage_for(client, admin, (alice.user, world.server), network["id"])
    created = client.post(
        f"{STORAGE}/{storage['service_id']}/shares",
        headers=alice.headers,
        json={"name": "closed", "quota_gb": 10, "clients": ["10.0.0.1", "10.0.0.2"]},
    )
    assert created.status_code == 201, created.text
    taken = _member_attach(client, bob, network, bob_vm, ip="10.0.0.1")
    assert taken.status_code in (400, 409)
    auto = _member_attach(client, bob, network, bob_vm)
    assert auto.status_code == 200 and auto.json()["attachments"][0]["ip_address"] == "10.0.0.3"


def test_removed_members_address_leaves_restricted_shares(client, world, alice, bob, admin, db_session):
    network = _network(client, alice, world)
    bob_vm = _vm(db_session, bob.user, world.location, "bob-vm")
    _join(client, alice, bob, network)
    storage = _storage_for(client, admin, (alice.user, world.server), network["id"])
    ip = _member_attach(client, bob, network, bob_vm).json()["attachments"][0]["ip_address"]
    created = client.post(
        f"{STORAGE}/{storage['service_id']}/shares",
        headers=alice.headers,
        json={"name": "closed", "quota_gb": 10, "clients": [ip, "10.0.0.50"]},
    )
    assert created.status_code == 201, created.text
    assert client.delete(f"{CLIENT}/{network['id']}/members/{bob.id}", headers=alice.headers).status_code == 204
    shown = client.get(f"{STORAGE}/{storage['service_id']}", headers=alice.headers).json()
    assert shown["shares"][0]["clients"] == ["10.0.0.50"]
