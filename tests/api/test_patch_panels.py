"""Tests for patch panels: admin API, cable runs through panels, path tracing and other-end views."""
import pytest

from app.models.cable_run import CableRun
from app.models.location import Location
from app.models.network_port import NetworkPort
from app.models.network_switch import NetworkSwitch
from app.models.patch_panel import PatchPanel, PatchPanelPort
from app.models.rack import Rack
from app.models.server import Server
from app.models.switch_port import SwitchPort
from app.services.cable_trace_service import CableIndex, switch_port_for_server_port, trace_from_port

PANELS = "/api/patch-panels/"
CABLES = "/api/cable-runs/"


def _login(client, username, password):
    r = client.post("/api/users/login", json={"username": username, "password": password})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture
def admin(client, test_admin_user):
    return _login(client, test_admin_user.username, "adminpassword123")


@pytest.fixture
def site(db_session):
    db = db_session
    dc1 = Location(name="DC1")
    dc2 = Location(name="DC2")
    db.add_all([dc1, dc2])
    db.flush()
    rack = Rack(name="A01", location_id=dc1.id, units=42)
    other_rack = Rack(name="Z01", location_id=dc2.id, units=42)
    db.add_all([rack, other_rack])
    db.flush()
    server = Server(name="hv-01", server_ip="10.0.0.1", location_id=dc1.id, rack_id=rack.id, rack_unit=10,
                    plugin_name="ipmi", plugin_config={})
    switch = NetworkSwitch(name="tor-a", location_id=dc1.id, rack_id=rack.id, rack_unit=42, plugin_name="snmpv3",
                           plugin_config={}, enabled=True)
    db.add_all([server, switch])
    db.flush()
    nic = NetworkPort(server_id=server.id, name="eno1", speed_mbps=10000, mac_address="ac:1f:6b:00:00:01")
    nic2 = NetworkPort(server_id=server.id, name="eno2", speed_mbps=10000)
    sw_port = SwitchPort(switch_id=switch.id, name="Ethernet1/5")
    db.add_all([nic, nic2, sw_port])
    db.commit()
    return {"dc1": dc1, "dc2": dc2, "rack": rack, "other_rack": other_rack, "server": server, "switch": switch,
            "nic": nic, "nic2": nic2, "sw_port": sw_port}


def _panel(client, admin, site, name="PP-A01", **extra):
    body = {"name": name, "location_id": site["dc1"].id, "rack_id": site["rack"].id, "rack_unit": 40,
            "port_count": 24, **extra}
    r = client.post(PANELS, json=body, headers=admin)
    assert r.status_code == 201, r.text
    return r.json()


def _connect(client, admin, a, b, expect=201):
    r = client.post(CABLES, json={"port_a": a, "port_b": b, "cable_type": "Cat6"}, headers=admin)
    assert r.status_code == expect, r.text
    return r.json()


def _two_panels_path(client, admin, site):
    """hv-01 eno1 -> PP-A01 port 3 front; PP-A01 port 3 rear -> PP-B port 7 rear; PP-B port 7 front -> tor-a Ethernet1/5."""
    pa = _panel(client, admin, site)
    pb = _panel(client, admin, site, name="PP-B", rack_unit=38, port_count=12)
    pa3 = pa["ports"][2]["id"]
    pb7 = pb["ports"][6]["id"]
    _connect(client, admin, {"type": "server", "id": site["nic"].id}, {"type": "patch", "id": pa3, "side": "front"})
    _connect(client, admin, {"type": "patch", "id": pa3, "side": "rear"}, {"type": "patch", "id": pb7, "side": "rear"})
    _connect(client, admin, {"type": "patch", "id": pb7, "side": "front"}, {"type": "switch", "id": site["sw_port"].id})
    return pa, pb, pa3, pb7


def test_create_get_list_panel(client, admin, site):
    panel = _panel(client, admin, site, medium="fibre", description="Row A trunk", rack_units=2)
    assert panel["port_count"] == 24
    assert panel["medium"] == "fibre"
    assert [p["number"] for p in panel["ports"]] == list(range(1, 25))
    assert panel["ports"][0]["name"] == "Port 1"
    assert panel["ports"][0]["front"] is None and panel["ports"][0]["rear"] is None

    r = client.get(PANELS, params={"rack_id": site["rack"].id}, headers=admin)
    assert [p["name"] for p in r.json()] == ["PP-A01"]
    assert client.get(PANELS, params={"location_id": site["dc2"].id}, headers=admin).json() == []
    assert client.get(f"{PANELS}{panel['id']}", headers=admin).json()["description"] == "Row A trunk"


@pytest.mark.parametrize("extra, message", [
    ({"port_count": 0}, "port_count"),
    ({"port_count": 1000}, "port_count"),
    ({"rack_unit": 42}, "overlaps"),
    ({"rack_unit": 10}, "overlaps"),
    ({"rack_unit": 42, "rack_units": 2}, "fit within"),
    ({"location_id": 99999}, "Location not found"),
])
def test_create_panel_validation(client, admin, site, extra, message):
    body = {"name": "bad", "location_id": site["dc1"].id, "rack_id": site["rack"].id, "rack_unit": 30,
            "port_count": 24, **extra}
    r = client.post(PANELS, json=body, headers=admin)
    assert r.status_code == 400
    assert message in r.json()["detail"]


def test_create_panel_rejects_rack_in_other_location_and_bad_medium(client, admin, site):
    body = {"name": "x", "location_id": site["dc1"].id, "rack_id": site["other_rack"].id, "port_count": 4}
    assert client.post(PANELS, json=body, headers=admin).status_code == 400
    body = {"name": "x", "location_id": site["dc1"].id, "port_count": 4, "medium": "copper-ish"}
    assert client.post(PANELS, json=body, headers=admin).status_code == 422


def test_duplicate_name_in_location_rejected(client, admin, site):
    _panel(client, admin, site)
    body = {"name": "PP-A01", "location_id": site["dc1"].id, "port_count": 4}
    r = client.post(PANELS, json=body, headers=admin)
    assert r.status_code == 400
    assert "already exists" in r.json()["detail"]
    body["location_id"] = site["dc2"].id
    assert client.post(PANELS, json=body, headers=admin).status_code == 201


def test_rack_overlap_helpers_include_panels(db_session, client, admin, site):
    from app.api.network_switch import _rack_placement_overlaps_switch
    from app.api.server import _rack_placement_overlaps

    _panel(client, admin, site, rack_unit=30, rack_units=2)
    rack_id = site["rack"].id
    assert _rack_placement_overlaps_switch(db_session, rack_id, 31, 1) is True
    assert _rack_placement_overlaps_switch(db_session, rack_id, 32, 1) is False
    assert _rack_placement_overlaps(db_session, rack_id, 29, 2) is True


def test_update_resize_and_label(client, admin, site):
    panel = _panel(client, admin, site)
    pid = panel["id"]
    r = client.put(f"{PANELS}{pid}", json={"port_count": 26, "name": "PP-A01-new"}, headers=admin)
    assert r.status_code == 200, r.text
    assert len(r.json()["ports"]) == 26 and r.json()["name"] == "PP-A01-new"

    r = client.put(f"{PANELS}{pid}/ports/3", json={"label": "A03 to row B"}, headers=admin)
    assert r.json()["ports"][2]["name"] == "A03 to row B"
    assert client.put(f"{PANELS}{pid}/ports/99", json={"label": "x"}, headers=admin).status_code == 404

    port26 = r.json()["ports"][25]["id"]
    _connect(client, admin, {"type": "server", "id": site["nic"].id}, {"type": "patch", "id": port26, "side": "front"})
    r = client.put(f"{PANELS}{pid}", json={"port_count": 24}, headers=admin)
    assert r.status_code == 409
    r = client.put(f"{PANELS}{pid}", json={"port_count": 25, "rack_id": None}, headers=admin)
    assert r.status_code == 409


def test_patch_port_takes_one_cable_per_side(client, admin, site):
    panel = _panel(client, admin, site)
    p1 = panel["ports"][0]["id"]
    _connect(client, admin, {"type": "server", "id": site["nic"].id}, {"type": "patch", "id": p1, "side": "front"})
    r = _connect(client, admin, {"type": "server", "id": site["nic2"].id}, {"type": "patch", "id": p1, "side": "front"},
                 expect=400)
    assert "already connected" in r["detail"]
    r = _connect(client, admin, {"type": "patch", "id": p1, "side": "front"}, {"type": "server", "id": site["nic2"].id},
                 expect=400)
    assert "already connected" in r["detail"]
    _connect(client, admin, {"type": "patch", "id": p1, "side": "rear"}, {"type": "switch", "id": site["sw_port"].id})
    _connect(client, admin, {"type": "patch", "id": p1, "side": "rear"}, {"type": "patch", "id": p1, "side": "front"},
             expect=400)


def test_patch_cable_needs_side_and_existing_port(client, admin, site):
    panel = _panel(client, admin, site)
    p1 = panel["ports"][0]["id"]
    r = _connect(client, admin, {"type": "server", "id": site["nic"].id}, {"type": "patch", "id": p1}, expect=400)
    assert "side" in r["detail"]
    r = _connect(client, admin, {"type": "server", "id": site["nic"].id},
                 {"type": "patch", "id": 999999, "side": "rear"}, expect=404)
    assert "Patch panel port" in r["detail"]
    _connect(client, admin, {"type": "server", "id": site["nic"].id}, {"type": "patch", "id": p1, "side": "middle"},
             expect=422)


def test_dao_rejects_same_side_loop_and_missing_side(db_session, client, admin, site):
    from app.dao.cable_run_dao import CableRunDAO

    panel = _panel(client, admin, site)
    p1 = panel["ports"][0]["id"]
    with pytest.raises(ValueError):
        CableRunDAO.create(db_session, "patch", p1, "patch", p1, port_a_side="front", port_b_side="front")
    with pytest.raises(ValueError):
        CableRunDAO.create(db_session, "patch", p1, "server", site["nic"].id)


@pytest.mark.parametrize(
    "values",
    [
        {"end_a_patch_port_id": "P", "end_a_patch_side": None},
        {"end_a_patch_port_id": "P", "end_a_patch_side": "side"},
        {"end_a_server_port_id": "N", "end_a_patch_side": "front"},
        {"end_a_server_port_id": "N", "end_a_patch_port_id": "P", "end_a_patch_side": "front"},
    ],
    ids=["patch-port-without-side", "invalid-side", "side-without-patch-port", "two-ports-on-one-end"],
)
def test_check_constraint_rejects_bad_patch_ends(db_session, site, values):
    from sqlalchemy.exc import IntegrityError

    panel = PatchPanel(name="PP-CK", location_id=site["dc1"].id, port_count=1)
    db_session.add(panel)
    db_session.flush()
    port = PatchPanelPort(patch_panel_id=panel.id, number=1)
    db_session.add(port)
    db_session.commit()
    ids = {"P": port.id, "N": site["nic2"].id}
    row = {k: ids.get(v, v) if k.endswith("_id") else v for k, v in values.items()}
    db_session.add(CableRun(**row, end_b_switch_port_id=site["sw_port"].id))
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_trace_through_two_panels(client, admin, site, db_session):
    pa, pb, pa3, pb7 = _two_panels_path(client, admin, site)

    path = trace_from_port(db_session, "server", site["nic"].id)
    assert path["complete"] is True
    assert path["far_end"] == ("switch", site["sw_port"].id, None)
    assert [h["to"] for h in path["hops"]] == [
        ("patch", pa3, "front"), ("patch", pb7, "rear"), ("switch", site["sw_port"].id, None),
    ]

    back = trace_from_port(db_session, "switch", site["sw_port"].id)
    assert back["far_end"] == ("server", site["nic"].id, None)
    assert switch_port_for_server_port(db_session, site["nic"].id)[0] == site["sw_port"].id

    index = CableIndex(db_session.query(CableRun).all())
    assert index.trace(("server", site["nic"].id, None))["far_end"] == ("switch", site["sw_port"].id, None)

    r = client.get(f"{CABLES}trace", params={"type": "server", "id": site["nic"].id}, headers=admin)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["complete"] is True
    assert body["far_end"]["device_name"] == "tor-a"
    assert [h["to"]["device_name"] for h in body["hops"]] == ["PP-A01", "PP-B", "tor-a"]
    assert body["hops"][0]["to"]["side"] == "front"
    r = client.get(f"{CABLES}trace", params={"type": "patch", "id": pa3}, headers=admin)
    assert r.status_code == 400
    r = client.get(f"{CABLES}trace", params={"type": "patch", "id": pa3, "side": "rear"}, headers=admin)
    assert r.json()["far_end"]["port_name"] == "Ethernet1/5"


def test_incomplete_path(client, admin, site, db_session):
    panel = _panel(client, admin, site)
    p1 = panel["ports"][0]["id"]
    _connect(client, admin, {"type": "server", "id": site["nic"].id}, {"type": "patch", "id": p1, "side": "front"})
    path = trace_from_port(db_session, "server", site["nic"].id)
    assert path["complete"] is False
    assert path["far_end"] is None
    assert len(path["hops"]) == 1
    assert switch_port_for_server_port(db_session, site["nic"].id)[0] is None


def test_other_end_views_follow_patch_panels(client, admin, site):
    pa, pb, pa3, pb7 = _two_panels_path(client, admin, site)

    server = client.get(f"/api/servers/{site['server'].id}", headers=admin).json()
    eno1 = next(p for p in server["network_ports"] if p["name"] == "eno1")
    cr = eno1["cable_run"]
    assert cr["other_end_type"] == "patch"
    assert cr["other_end_device_name"] == "PP-A01"
    assert cr["other_end_side"] == "front"
    assert [v["patch_panel_name"] for v in cr["via"]] == ["PP-A01", "PP-B"]
    assert cr["reaches_type"] == "switch"
    assert cr["reaches_device_name"] == "tor-a"
    assert cr["reaches_port_name"] == "Ethernet1/5"
    assert cr["path_complete"] is True

    ports = client.get(f"/api/network-switches/{site['switch'].id}/switch-ports", headers=admin)
    assert ports.status_code == 200, ports.text
    eth5 = next(p for p in ports.json()["ports"] if p["name"] == "Ethernet1/5")
    assert eth5["cable_run"]["other_end_device_name"] == "PP-B"
    assert eth5["cable_run"]["reaches_device_name"] == "hv-01"

    detail = client.get(f"{PANELS}{pa['id']}", headers=admin).json()
    port3 = detail["ports"][2]
    assert port3["front"]["other_end"]["device_name"] == "hv-01"
    assert port3["front"]["reaches"]["device_name"] == "hv-01"
    assert port3["rear"]["other_end"]["device_name"] == "PP-B"
    assert port3["rear"]["reaches"]["port_name"] == "Ethernet1/5"
    assert detail["cable_count"] == 2

    listed = client.get(CABLES, headers=admin).json()
    patch_ends = [e for c in listed for e in (c["end_a"], c["end_b"]) if e["type"] == "patch"]
    assert {e["side"] for e in patch_ends} == {"front", "rear"}


def test_delete_panel(client, admin, site, db_session):
    pa, pb, pa3, pb7 = _two_panels_path(client, admin, site)
    r = client.delete(f"{PANELS}{pa['id']}", headers=admin)
    assert r.status_code == 409
    r = client.delete(f"{PANELS}{pa['id']}", params={"force": "true"}, headers=admin)
    assert r.status_code == 204
    db_session.expire_all()
    assert db_session.get(PatchPanel, pa["id"]) is None
    assert db_session.query(PatchPanelPort).filter_by(patch_panel_id=pa["id"]).count() == 0
    assert db_session.query(CableRun).count() == 1
    empty = _panel(client, admin, site, name="empty", rack_unit=20)
    assert client.delete(f"{PANELS}{empty['id']}", headers=admin).status_code == 204


def test_auth(client, site, test_user):
    assert client.get(PANELS).status_code == 401
    user = _login(client, test_user.username, "testpassword123")
    assert client.get(PANELS, headers=user).status_code == 403
    body = {"name": "x", "location_id": site["dc1"].id, "port_count": 4}
    assert client.post(PANELS, json=body, headers=user).status_code == 403
