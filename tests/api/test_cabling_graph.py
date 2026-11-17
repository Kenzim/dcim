"""Tests for the admin cabling graph endpoint (GET /api/admin/cabling/graph)."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event

from app.models.cable_run import CableRun
from app.models.location import Location
from app.models.network_port import NetworkPort
from app.models.network_switch import NetworkSwitch
from app.models.rack import Rack
from app.models.server import Server
from app.models.switch_port import SwitchPort

URL = "/api/admin/cabling/graph"
SECRET = "sup3r-s3cret-value"


def _login(client, username, password):
    r = client.post("/api/users/login", json={"username": username, "password": password})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture
def admin_headers(client, test_admin_user):
    return _login(client, test_admin_user.username, "adminpassword123")


def _server(db, name, location, rack=None, unit=None, **extra):
    s = Server(
        name=name,
        server_ip="10.0.0.1",
        location_id=location.id,
        rack_id=rack.id if rack else None,
        rack_unit=unit,
        plugin_name="ipmi",
        plugin_config={"hostname": "10.9.9.9", "username": "ADMIN", "password": SECRET},
        **extra,
    )
    db.add(s)
    db.flush()
    return s


def _nic(db, server, name, speed=10000, mac=None):
    p = NetworkPort(server_id=server.id, name=name, mac_address=mac, speed_mbps=speed)
    db.add(p)
    db.flush()
    return p


def _switch(db, name, location, rack=None, unit=None):
    sw = NetworkSwitch(
        name=name,
        location_id=location.id,
        rack_id=rack.id if rack else None,
        rack_unit=unit,
        plugin_name="snmpv3",
        plugin_config={"hostname": "10.9.9.1", "auth_password": SECRET, "privacy_password": SECRET},
        enabled=True,
        model="Nexus 3064",
    )
    db.add(sw)
    db.flush()
    return sw


def _swport(db, switch, name, speed=10000, oper=None, admin=None, polled_at="now"):
    if polled_at == "now":
        polled_at = datetime.now(timezone.utc) if (oper is not None or admin is not None) else None
    p = SwitchPort(
        switch_id=switch.id, name=name, speed_mbps=speed, oper_status=oper, admin_status=admin,
        status_updated_at=polled_at,
    )
    db.add(p)
    db.flush()
    return p


def _cable(db, server_port, switch_port, **extra):
    c = CableRun(end_a_server_port_id=server_port.id, end_b_switch_port_id=switch_port.id, **extra)
    db.add(c)
    db.flush()
    return c


@pytest.fixture
def topology(db_session):
    """Two locations. DC1 has rack A (switch + 2 servers) and rack B (1 server cabled to rack A's switch)."""
    db = db_session
    dc1 = Location(name="DC1")
    dc2 = Location(name="DC2")
    db.add_all([dc1, dc2])
    db.flush()
    rack_a = Rack(name="A01", location_id=dc1.id, row=1, row_position=1, units=42)
    rack_b = Rack(name="B01", location_id=dc1.id, row=2, row_position=1, units=42)
    rack_c = Rack(name="C01", location_id=dc2.id, units=42)
    db.add_all([rack_a, rack_b, rack_c])
    db.flush()

    sw = _switch(db, "tor-a", dc1, rack_a, 42)
    sw_p1 = _swport(db, sw, "Ethernet1/1", oper=1, admin=1)
    sw_p2 = _swport(db, sw, "Ethernet1/2", oper=2, admin=1)
    sw_p3 = _swport(db, sw, "Ethernet1/3")
    _swport(db, sw, "Ethernet1/4")

    web = _server(db, "web-01", dc1, rack_a, 10, locked=True)
    web_eno1 = _nic(db, web, "eno1", mac="aa:bb:cc:00:00:01")
    _nic(db, web, "eno2", mac="aa:bb:cc:00:00:02")
    db_srv = _server(db, "db-01", dc1, rack_a, 12, enabled=False)
    db_eno1 = _nic(db, db_srv, "eno1", speed=25000)
    far = _server(db, "far-01", dc1, rack_b, 5)
    far_eno1 = _nic(db, far, "eno1", speed=1000)

    other = _server(db, "other-01", dc2, rack_c, 1)
    _nic(db, other, "eno1")

    c1 = _cable(db, web_eno1, sw_p1, cable_type="DAC", speed_mbps=10000, description="web uplink")
    c2 = _cable(db, db_eno1, sw_p2)
    c3 = _cable(db, far_eno1, sw_p3, cable_type="Cat6")
    db.commit()
    return {
        "dc1": dc1, "dc2": dc2, "rack_a": rack_a, "rack_b": rack_b, "rack_c": rack_c,
        "switch": sw, "web": web, "db": db_srv, "far": far, "other": other,
        "c1": c1, "c2": c2, "c3": c3,
    }


def test_requires_authentication(client):
    assert client.get(URL).status_code == 401


def test_requires_admin(client, test_user):
    headers = _login(client, test_user.username, "testpassword123")
    assert client.get(URL, headers=headers).status_code == 403


def test_empty_graph(client, admin_headers):
    r = client.get(URL, headers=admin_headers)
    assert r.status_code == 200
    data = r.json()
    assert data.pop("generated_at")
    assert data.pop("status_stale_after_seconds") == 900
    assert data == {
        "filters": {"location_id": None, "rack_id": None},
        "locations": [],
        "racks": [],
        "nodes": [],
        "ports": [],
        "edges": [],
    }


def test_full_graph_shape(client, admin_headers, topology):
    r = client.get(URL, headers=admin_headers)
    assert r.status_code == 200
    data = r.json()

    nodes = {n["id"]: n for n in data["nodes"]}
    assert set(nodes) == {
        f"switch:{topology['switch'].id}",
        f"server:{topology['web'].id}",
        f"server:{topology['db'].id}",
        f"server:{topology['far'].id}",
        f"server:{topology['other'].id}",
    }
    assert all(n["in_scope"] for n in nodes.values())

    sw = nodes[f"switch:{topology['switch'].id}"]
    assert sw["kind"] == "switch"
    assert sw["model"] == "Nexus 3064"
    assert sw["rack_id"] == topology["rack_a"].id
    assert sw["rack_unit"] == 42
    assert sw["port_count"] == 4

    web = nodes[f"server:{topology['web'].id}"]
    assert web["locked"] is True and web["enabled"] is True
    assert web["port_count"] == 2
    assert nodes[f"server:{topology['db'].id}"]["enabled"] is False

    ports = {p["id"]: p for p in data["ports"]}
    web_ports = [p for p in ports.values() if p["device"] == web["id"]]
    assert sorted(p["name"] for p in web_ports) == ["eno1", "eno2"]
    eno1 = next(p for p in web_ports if p["name"] == "eno1")
    assert eno1["mac_address"] == "aa:bb:cc:00:00:01"
    assert eno1["cable_id"] == topology["c1"].id
    eno2 = next(p for p in web_ports if p["name"] == "eno2")
    assert eno2["cable_id"] is None

    switch_port_names = sorted(p["name"] for p in ports.values() if p["kind"] == "switch")
    assert switch_port_names == ["Ethernet1/1", "Ethernet1/2", "Ethernet1/3"]

    edges = {e["cable_id"]: e for e in data["edges"]}
    assert set(edges) == {topology["c1"].id, topology["c2"].id, topology["c3"].id}
    e1 = edges[topology["c1"].id]
    assert e1["source"] == web["id"] and e1["target"] == sw["id"]
    assert ports[e1["source_port"]]["name"] == "eno1"
    assert ports[e1["target_port"]]["name"] == "Ethernet1/1"
    assert ports[e1["target_port"]]["oper_status"] == "up"
    assert e1["cable_type"] == "DAC"
    assert e1["description"] == "web uplink"
    assert e1["link_status"] == "up"
    assert e1["effective_speed_mbps"] == 10000

    e2 = edges[topology["c2"].id]
    assert e2["link_status"] == "down"
    assert e2["speed_mbps"] is None
    assert e2["effective_speed_mbps"] == 10000

    e3 = edges[topology["c3"].id]
    assert e3["link_status"] == "unknown"
    assert e3["effective_speed_mbps"] == 1000

    assert {loc["name"] for loc in data["locations"]} == {"DC1", "DC2"}
    assert {rk["name"] for rk in data["racks"]} == {"A01", "B01", "C01"}


def test_link_status_fresh_stale_and_never_polled(db_session, topology):
    from app.services.cabling_graph_service import build_cabling_graph

    edges = lambda g: {e["cable_id"]: e for e in g["edges"]}  # noqa: E731
    ports = lambda g: {p["id"]: p for p in g["ports"]}  # noqa: E731

    graph = build_cabling_graph(db_session)
    e1 = edges(graph)[topology["c1"].id]
    assert e1["link_status"] == "up"
    assert ports(graph)[e1["target_port"]]["status_fresh"] is True
    assert ports(graph)[e1["target_port"]]["status_updated_at"] is not None
    assert edges(graph)[topology["c3"].id]["link_status"] == "unknown"

    later = datetime.now(timezone.utc) + timedelta(minutes=16)
    graph = build_cabling_graph(db_session, now=later)
    assert edges(graph)[topology["c1"].id]["link_status"] == "stale"
    assert edges(graph)[topology["c2"].id]["link_status"] == "stale"
    assert ports(graph)[e1["target_port"]]["status_fresh"] is False
    assert edges(graph)[topology["c3"].id]["link_status"] == "unknown"


def test_server_port_link_state_exposed(db_session, topology):
    from app.services.cabling_graph_service import build_cabling_graph

    checked = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    port = db_session.query(NetworkPort).filter_by(server_id=topology["web"].id, name="eno1").one()
    port.link_up = True
    port.link_checked_at = checked
    db_session.commit()

    graph = build_cabling_graph(db_session)
    by_id = {p["id"]: p for p in graph["ports"]}
    eno1 = by_id[f"server-port:{port.id}"]
    assert eno1["link_up"] is True
    assert eno1["link_checked_at"] == checked
    eno2 = next(p for p in graph["ports"] if p["device"] == f"server:{topology['web'].id}" and p["name"] == "eno2")
    assert eno2["link_up"] is None


def test_filter_by_location(client, admin_headers, topology):
    r = client.get(URL, params={"location_id": topology["dc2"].id}, headers=admin_headers)
    assert r.status_code == 200
    data = r.json()
    assert data["filters"] == {"location_id": topology["dc2"].id, "rack_id": None}
    assert [n["name"] for n in data["nodes"]] == ["other-01"]
    assert data["edges"] == []
    assert [loc["name"] for loc in data["locations"]] == ["DC2"]
    assert [rk["name"] for rk in data["racks"]] == ["C01"]


def test_filter_by_rack_includes_cabled_peers_out_of_scope(client, admin_headers, topology):
    r = client.get(URL, params={"rack_id": topology["rack_b"].id}, headers=admin_headers)
    assert r.status_code == 200
    data = r.json()
    nodes = {n["name"]: n for n in data["nodes"]}
    assert set(nodes) == {"far-01", "tor-a"}
    assert nodes["far-01"]["in_scope"] is True
    assert nodes["tor-a"]["in_scope"] is False
    assert [e["cable_id"] for e in data["edges"]] == [topology["c3"].id]
    switch_ports = [p["name"] for p in data["ports"] if p["kind"] == "switch"]
    assert switch_ports == ["Ethernet1/3"]
    assert {rk["name"] for rk in data["racks"]} == {"A01", "B01"}


def test_filter_by_rack_with_switch(client, admin_headers, topology):
    r = client.get(URL, params={"rack_id": topology["rack_a"].id}, headers=admin_headers)
    data = r.json()
    nodes = {n["name"]: n for n in data["nodes"]}
    assert set(nodes) == {"tor-a", "web-01", "db-01", "far-01"}
    assert nodes["far-01"]["in_scope"] is False
    assert len(data["edges"]) == 3
    far_ports = [p for p in data["ports"] if p["device"] == nodes["far-01"]["id"]]
    assert [p["name"] for p in far_ports] == ["eno1"]


def test_unknown_filters_return_empty(client, admin_headers, topology):
    r = client.get(URL, params={"location_id": 99999}, headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["nodes"] == []


def test_invalid_filter_rejected(client, admin_headers):
    assert client.get(URL, params={"rack_id": 0}, headers=admin_headers).status_code == 422
    assert client.get(URL, params={"rack_id": "abc"}, headers=admin_headers).status_code == 422


def test_no_secrets_in_response(client, admin_headers, topology):
    r = client.get(URL, headers=admin_headers)
    assert r.status_code == 200
    body = r.text
    assert SECRET not in body
    assert "plugin_config" not in body
    assert "10.9.9.9" not in body
    assert "password" not in body.lower()
    assert "bmc" not in body.lower()
    for node in json.loads(body)["nodes"]:
        assert "plugin_name" not in node


def _count_queries(db_session, fn):
    engine = db_session.get_bind()
    statements = []

    def before(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", before)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", before)
    return len(statements)


def test_query_count_does_not_grow_with_devices(client, admin_headers, db_session, topology):
    def fetch():
        assert client.get(URL, headers=admin_headers).status_code == 200

    fetch()
    baseline = _count_queries(db_session, fetch)

    sw = topology["switch"]
    for i in range(10):
        s = _server(db_session, f"bulk-{i}", topology["dc1"], topology["rack_b"], 20 + i)
        nic = _nic(db_session, s, "eno1")
        _cable(db_session, nic, _swport(db_session, sw, f"Ethernet2/{i}", oper=1))
    db_session.commit()

    assert _count_queries(db_session, fetch) == baseline
