"""Private network DHCP through the client, member and staff API: owner routes, permissions and views."""

import pytest

from app.models.commerce_audit import UserAuditEvent
from tests.api.test_private_network_sharing_api import (  # noqa: F401  (fixtures)
    ADMIN,
    CLIENT,
    _join,
    _member_attach,
    _network,
    _vm,
    admin,
    alice,
    bob,
    carol,
    proxmox,
    world,
)


def _dhcp(network):
    return f"{CLIENT}/{network['id']}/dhcp"


def test_owner_manages_settings_and_reservations(client, world, alice, db_session):
    network = _network(client, alice, world)
    vm = _vm(db_session, alice.user, world.location, "alice-vm")
    attached = client.post(f"{CLIENT}/{network['id']}/attachments", headers=alice.headers,
                           json={"service_id": vm.id})
    assert attached.status_code == 200, attached.text
    vm_ip = attached.json()["attachments"][0]["ip_address"]
    view = client.get(_dhcp(network), headers=alice.headers)
    assert view.status_code == 200, view.text
    body = view.json()
    assert body["network_id"] == network["id"] and body["available"] is False and body["served"] is False
    assert body["enabled"] is True and body["lease_seconds"] == 86400 and body["lease_seconds_set"] is None
    assert body["automatic"][0]["macs"] == ["bc:24:11:aa:bb:cc"] and body["automatic"][0]["ip"] == vm_ip
    assert body["endpoint_ip"] == "10.0.0.254"

    patched = client.patch(_dhcp(network), headers=alice.headers, json={
        "range_start": "10.0.0.100", "range_end": "10.0.0.150", "gateway": "10.0.0.1", "lease_seconds": 3600,
        "dns_servers": ["1.1.1.1"], "routes": [{"destination": "192.168.50.0/24", "gateway": "10.0.0.1"}],
    })
    assert patched.status_code == 200, patched.text
    assert patched.json()["range_start"] == "10.0.0.100" and patched.json()["lease_seconds_set"] == 3600
    bad = client.patch(_dhcp(network), headers=alice.headers, json={"range_start": vm_ip, "range_end": "10.0.0.9"})
    assert bad.status_code == 409 and vm_ip in bad.json()["detail"]
    assert client.patch(_dhcp(network), headers=alice.headers, json={"mtu": 100}).status_code == 400

    added = client.post(f"{_dhcp(network)}/reservations", headers=alice.headers,
                        json={"mac": "DE:AD:BE:EF:00:01", "ip": "10.0.0.50", "label": "printer"})
    assert added.status_code == 201, added.text
    reservation = added.json()["reservations"][0]
    assert reservation["mac"] == "de:ad:be:ef:00:01" and reservation["label"] == "printer"
    rid = reservation["id"]
    updated = client.patch(f"{_dhcp(network)}/reservations/{rid}", headers=alice.headers, json={"ip": "10.0.0.51"})
    assert updated.status_code == 200 and updated.json()["reservations"][0]["ip"] == "10.0.0.51"
    clash = client.post(f"{_dhcp(network)}/reservations", headers=alice.headers,
                        json={"mac": "de:ad:be:ef:00:02", "ip": "10.0.0.120"})
    assert clash.status_code == 409

    # A new attachment asking for an address inside the range or a reserved one is refused.
    other = _vm(db_session, alice.user, world.location, "alice-vm-2")
    for ip in ("10.0.0.120", "10.0.0.51"):
        refused = client.post(f"{CLIENT}/{network['id']}/attachments", headers=alice.headers,
                              json={"service_id": other.id, "ip_address": ip})
        assert refused.status_code == 409, refused.text

    removed = client.delete(f"{_dhcp(network)}/reservations/{rid}", headers=alice.headers)
    assert removed.status_code == 200 and removed.json()["reservations"] == []
    assert client.delete(f"{_dhcp(network)}/reservations/{rid}", headers=alice.headers).status_code == 404

    actions = [row.action for row in db_session.query(UserAuditEvent).filter(
        UserAuditEvent.action.like("private_network.dhcp%")).order_by(UserAuditEvent.id)]
    assert actions == [
        "private_network.dhcp_update",
        "private_network.dhcp_reservation_add",
        "private_network.dhcp_reservation_update",
        "private_network.dhcp_reservation_remove",
    ]


def _owner_routes(client, network, headers):
    base = f"{CLIENT}/{network['id']}/dhcp"
    return [
        client.get(base, headers=headers),
        client.patch(base, headers=headers, json={"enabled": False}),
        client.post(f"{base}/reservations", headers=headers, json={"mac": "de:ad:be:ef:00:09", "ip": "10.0.0.90"}),
        client.patch(f"{base}/reservations/1", headers=headers, json={"ip": "10.0.0.91"}),
        client.delete(f"{base}/reservations/1", headers=headers),
    ]


def test_member_sees_only_the_member_view(client, world, alice, bob, db_session):
    network = _network(client, alice, world)
    _join(client, alice, bob, network)
    bob_vm = _vm(db_session, bob.user, world.location, "bob-vm")
    assert _member_attach(client, bob, network, bob_vm).status_code == 200
    client.post(f"{_dhcp(network)}/reservations", headers=alice.headers,
                json={"mac": "de:ad:be:ef:00:01", "ip": "10.0.0.50"})
    client.patch(_dhcp(network), headers=alice.headers,
                 json={"gateway": "10.0.0.1", "range_start": "10.0.0.100", "range_end": "10.0.0.110"})

    for response in _owner_routes(client, network, bob.headers):
        assert response.status_code == 404, response.text
        assert "not found" in response.json()["detail"].lower()

    shared = client.get(f"{CLIENT}/shared/{network['id']}", headers=bob.headers)
    assert shared.status_code == 200, shared.text
    block = shared.json()["dhcp"]
    assert set(block) == {"enabled", "served", "lease_seconds", "gateway", "dns_servers", "domain", "mtu", "routes",
                          "ntp_servers"}
    assert block["gateway"] == "10.0.0.1"
    assert "10.0.0.100" not in shared.text and "de:ad:be:ef" not in shared.text

    # The owner sees the member's attachment without its MAC or service.
    automatic = client.get(_dhcp(network), headers=alice.headers).json()["automatic"]
    assert [a["label"] for a in automatic] == ["member attachment"] and automatic[0]["macs"] == []


def test_unrelated_client_gets_not_found_everywhere(client, world, alice, carol):
    network = _network(client, alice, world)
    for response in _owner_routes(client, network, carol.headers):
        assert response.status_code == 404, response.text
    assert client.get(f"{CLIENT}/shared/{network['id']}", headers=carol.headers).status_code == 404


def test_staff_routes_and_network_output(client, world, alice, bob, admin, db_session):
    network = _network(client, alice, world)
    _join(client, alice, bob, network)
    bob_vm = _vm(db_session, bob.user, world.location, "bob-vm")
    assert _member_attach(client, bob, network, bob_vm).status_code == 200
    base = f"{ADMIN}/{network['id']}/dhcp"

    view = client.get(base, headers=admin)
    assert view.status_code == 200, view.text
    assert view.json()["automatic"][0]["macs"] == ["bc:24:11:aa:bb:cc"]
    assert client.patch(base, headers=admin, json={"domain": "lan"}).json()["domain"] == "lan"
    added = client.post(f"{base}/reservations", headers=admin, json={"mac": "de:ad:be:ef:00:01", "ip": "10.0.0.50"})
    assert added.status_code == 201
    rid = added.json()["reservations"][0]["id"]
    assert client.patch(f"{base}/reservations/{rid}", headers=admin, json={"label": "x"}).status_code == 200
    # The same MAC as the member's VM on another address: both dropped, with a staff warning.
    db_session.expire_all()
    clash = client.patch(f"{base}/reservations/{rid}", headers=admin, json={"mac": "bc:24:11:aa:bb:cc"})
    assert clash.status_code == 409
    assert client.delete(f"{base}/reservations/{rid}", headers=admin).status_code == 200

    out = client.get(f"{ADMIN}/{network['id']}", headers=admin).json()
    assert out["presence"] == {"node_id": None, "node_name": None, "since": None, "dhcp_error": None}
    assert out["dhcp_warnings"] == []
    # Clients never see the staff fields.
    owner_out = client.get(f"{CLIENT}/{network['id']}", headers=alice.headers).json()
    assert owner_out.get("presence") is None and owner_out.get("dhcp_warnings") is None

    assert client.get(base, headers=alice.headers).status_code in (401, 403)


@pytest.mark.parametrize("path", ["/dhcp", "/dhcp/reservations/1"])
def test_unknown_network_is_not_found(client, world, alice, path):
    assert client.get(f"{CLIENT}/999999{path}", headers=alice.headers).status_code in (404, 405)
