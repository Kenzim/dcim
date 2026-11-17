"""
Tests for PDUs, power feeds, server power inputs, power aggregation and guarded outlet control.
"""
import pytest

from app.dao import ServerDAO
from app.models.location import Location
from app.models.rack import Rack
from app.models.server_power_input import ServerPowerInput
from app.plugins.pdu.base import PduCapability, PduPlugin
from app.plugins.pdu.registry import get_pdu_registry


class FakePdu(PduPlugin):
    """Metered, switched 4-outlet PDU on two banks; records outlet actions instead of switching."""

    PLUGIN_NAME = "fake_test_pdu"
    PLUGIN_VERSION = "0"
    SUPPORTED_CAPABILITIES = [PduCapability.METERING, PduCapability.OUTLET_METERING, PduCapability.OUTLET_CONTROL]
    CONFIG_TEMPLATE = {
        "type": "object",
        "properties": {
            "hostname": {"type": "string"},
            "community": {"type": "string", "format": "password"},
        },
    }
    actions: list = []
    outlet_count = 4
    inlet_count = 1

    async def test_connection(self):
        return {"success": True, "message": "ok", "details": {}}

    async def get_pdu_info(self):
        return {
            "model": "FAKE-1",
            "serial_number": "SN1",
            "inlet_count": FakePdu.inlet_count,
            "outlet_count": FakePdu.outlet_count,
            "outlets": [
                {"position": p, "name": f"Outlet {p}", "bank": "A" if p <= 2 else "B"}
                for p in range(1, FakePdu.outlet_count + 1)
            ],
            "outlet_metering": True,
            "outlet_switching": True,
        }

    async def get_readings(self):
        return {
            "inlets": [{"position": 1, "voltage_v": 208.0, "current_a": 10.0, "power_w": 2000.0}],
            "banks": [{"name": "A", "current_a": 4.0, "rated_current_a": 16}],
            "outlets": [
                {"position": 1, "current_a": 1.5, "power_w": 300.0, "state": "on"},
                {"position": 2, "current_a": 2.5, "power_w": 500.0, "state": "on"},
                {"position": 3, "current_a": 0.0, "power_w": 0.0, "state": "off"},
            ],
        }

    async def set_outlet_state(self, position, action):
        FakePdu.actions.append((position, action))
        return {"position": position, "action": action}


class FakeAts(PduPlugin):
    """Two-input ATS that only reports unit totals and which input is active."""

    PLUGIN_NAME = "fake_test_ats"
    PLUGIN_VERSION = "0"
    SUPPORTED_CAPABILITIES = [PduCapability.METERING]
    CONFIG_TEMPLATE = {"type": "object", "properties": {}}

    async def test_connection(self):
        return {"success": True, "message": "ok", "details": {}}

    async def get_pdu_info(self):
        return {"inlet_count": 2, "outlet_count": 2, "outlet_metering": False, "outlet_switching": False}

    async def get_readings(self):
        return {
            "inlets": [
                {"position": 1, "voltage_v": 207.0, "active": False},
                {"position": 2, "voltage_v": 208.0, "active": True},
            ],
            "totals": {"current_a": 8.0},
        }


@pytest.fixture(autouse=True)
def fake_plugins():
    registry = get_pdu_registry()
    registry.register(FakePdu)
    registry.register(FakeAts)
    FakePdu.actions = []
    FakePdu.outlet_count = 4
    yield
    registry._plugins.pop(FakePdu.PLUGIN_NAME, None)
    registry._plugins.pop(FakeAts.PLUGIN_NAME, None)


@pytest.fixture
def auth(client, test_admin_user):
    r = client.post("/api/users/login", json={"username": test_admin_user.username, "password": "adminpassword123"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture
def rack(db_session):
    loc = Location(name="DC1", description="")
    db_session.add(loc)
    db_session.commit()
    r = Rack(location_id=loc.id, name="Rack A", units=42)
    db_session.add(r)
    db_session.commit()
    db_session.refresh(r)
    return r


@pytest.fixture
def server(db_session, rack):
    return ServerDAO.create(
        db_session,
        name="srv-1",
        server_ip="10.0.0.1",
        plugin_name="ipmi",
        plugin_config={},
        location_id=rack.location_id,
        rack_id=rack.id,
    )


def _create_pdu(client, auth, rack, **overrides):
    body = {
        "name": "pdu-a",
        "location_id": rack.location_id,
        "rack_id": rack.id,
        "plugin_name": FakePdu.PLUGIN_NAME,
        "plugin_config": {"hostname": "10.0.0.50", "community": "s3cret"},
    }
    body.update(overrides)
    r = client.post("/api/pdus/", json=body, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()


def _create_feed(client, auth, rack, name="A1", **overrides):
    r = client.post("/api/power-feeds/", json={"rack_id": rack.id, "name": name, **overrides}, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()


def test_new_server_gets_one_power_input(db_session, server):
    inputs = db_session.query(ServerPowerInput).filter_by(server_id=server.id).all()
    assert [i.name for i in inputs] == ["PSU1"]


def test_power_feed_capacity_and_unique_name(client, auth, rack):
    feed = _create_feed(client, auth, rack, voltage=208, amperage=30)
    assert feed["capacity_w"] == 6240.0
    assert feed["usable_w"] == 4992.0
    assert feed["usable_current_a"] == 24.0
    assert feed["current_a"] is None

    r = client.post("/api/power-feeds/", json={"rack_id": rack.id, "name": "A1"}, headers=auth)
    assert r.status_code == 400

    three = _create_feed(client, auth, rack, name="C1", voltage=208, amperage=30, phase="three")
    assert three["capacity_w"] == pytest.approx(10808.0, abs=0.1)


def test_create_pdu_generates_ports_and_masks_secrets(client, auth, rack):
    pdu = _create_pdu(client, auth, rack)
    assert pdu["model"] == "FAKE-1"
    assert [i["name"] for i in pdu["inlets"]] == ["Input"]
    assert [o["position"] for o in pdu["outlets"]] == [1, 2, 3, 4]
    assert [o["bank"] for o in pdu["outlets"]] == ["A", "A", "B", "B"]
    assert pdu["outlet_metering"] is True and pdu["outlet_switching"] is True
    assert pdu["outlet_control_enabled"] is False
    assert pdu["plugin_config"]["community"] == "********"

    # Sending the mask back keeps the stored secret
    r = client.put(
        f"/api/pdus/{pdu['id']}",
        json={"plugin_config": {"hostname": "10.0.0.51", "community": "********"}},
        headers=auth,
    )
    assert r.status_code == 200
    from app.dao.pdu_dao import PduDAO
    from tests.conftest import TestingSessionLocal

    db = TestingSessionLocal()
    stored = PduDAO.get_by_id(db, pdu["id"]).plugin_config
    db.close()
    assert stored == {"hostname": "10.0.0.51", "community": "s3cret"}


def test_manual_pdu_with_counts(client, auth, rack):
    r = client.post(
        "/api/pdus/",
        json={
            "name": "ats-1",
            "location_id": rack.location_id,
            "plugin_name": "manual",
            "plugin_config": {"outlet_count": 16, "inlet_count": 2},
        },
        headers=auth,
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert [i["name"] for i in data["inlets"]] == ["Input A", "Input B"]
    assert len(data["outlets"]) == 16


def test_rack_mounted_pdu_blocks_overlapping_placement(client, auth, rack, db_session):
    _create_pdu(client, auth, rack, rack_unit=10, rack_units=2)
    r = client.post(
        "/api/pdus/",
        json={
            "name": "pdu-b",
            "location_id": rack.location_id,
            "rack_id": rack.id,
            "rack_unit": 11,
            "rack_units": 1,
            "plugin_name": "manual",
        },
        headers=auth,
    )
    assert r.status_code == 400
    from app.dao.pdu_dao import pdu_overlaps_range

    assert pdu_overlaps_range(db_session, rack.id, 11, 11) is True
    assert pdu_overlaps_range(db_session, rack.id, 12, 20) is False


def test_link_feed_outlet_and_aggregate(client, auth, rack, server):
    feed = _create_feed(client, auth, rack)
    pdu = _create_pdu(client, auth, rack)
    inlet_id = pdu["inlets"][0]["id"]
    outlet1, outlet2 = pdu["outlets"][0], pdu["outlets"][1]

    r = client.put(f"/api/pdus/{pdu['id']}/inlets/{inlet_id}", json={"power_feed_id": feed["id"]}, headers=auth)
    assert r.status_code == 200
    assert r.json()["inlets"][0]["power_feed_name"] == "A1"

    power = client.get(f"/api/servers/{server.id}/power-inputs", headers=auth).json()
    psu1 = power["inputs"][0]
    r = client.put(
        f"/api/servers/{server.id}/power-inputs/{psu1['id']}", json={"pdu_outlet_id": outlet1["id"]}, headers=auth
    )
    assert r.status_code == 200
    r = client.post(
        f"/api/servers/{server.id}/power-inputs",
        json={"name": "PSU2", "pdu_outlet_id": outlet2["id"]},
        headers=auth,
    )
    assert r.status_code == 201

    # The same outlet cannot feed two inputs
    r = client.post(
        f"/api/servers/{server.id}/power-inputs", json={"name": "PSU3", "pdu_outlet_id": outlet1["id"]}, headers=auth
    )
    assert r.status_code == 409

    r = client.post(f"/api/pdus/{pdu['id']}/poll", headers=auth)
    assert r.status_code == 200 and r.json()["success"] is True

    power = client.get(f"/api/servers/{server.id}/power-inputs", headers=auth).json()
    assert power["metered"] is True and power["fully_metered"] is False  # PSU3 was not created; 2 of 2
    assert power["total_power_w"] == 800.0
    assert power["total_current_a"] == 4.0
    assert power["inputs"][0]["feeds"][0]["name"] == "A1"

    rack_power = client.get(f"/api/racks/{rack.id}/power", headers=auth).json()
    assert rack_power["total_power_w"] == 2000.0
    assert rack_power["feeds"][0]["current_a"] == 10.0
    assert rack_power["feeds"][0]["utilisation_percent"] == pytest.approx(33.3, abs=0.1)
    assert rack_power["feeds"][0]["over_limit"] is False
    assert rack_power["pdus"][0]["banks"][0]["name"] == "A"

    detail = client.get(f"/api/pdus/{pdu['id']}", headers=auth).json()
    assert detail["outlets"][0]["server_power_input"]["server_name"] == "srv-1"
    assert detail["outlets"][2]["state"] == "off"


def test_fully_metered_flag(client, auth, rack, server):
    pdu = _create_pdu(client, auth, rack)
    power = client.get(f"/api/servers/{server.id}/power-inputs", headers=auth).json()
    client.put(
        f"/api/servers/{server.id}/power-inputs/{power['inputs'][0]['id']}",
        json={"pdu_outlet_id": pdu["outlets"][0]["id"]},
        headers=auth,
    )
    power = client.get(f"/api/servers/{server.id}/power-inputs", headers=auth).json()
    assert power["fully_metered"] is True


def test_ats_load_goes_on_active_inlet(client, auth, rack):
    feed_a = _create_feed(client, auth, rack, name="A1")
    feed_b = _create_feed(client, auth, rack, name="B1")
    r = client.post(
        "/api/pdus/",
        json={"name": "ats", "location_id": rack.location_id, "rack_id": rack.id, "plugin_name": FakeAts.PLUGIN_NAME},
        headers=auth,
    )
    pdu = r.json()
    assert len(pdu["inlets"]) == 2
    for inlet, feed in zip(pdu["inlets"], (feed_a, feed_b)):
        client.put(f"/api/pdus/{pdu['id']}/inlets/{inlet['id']}", json={"power_feed_id": feed["id"]}, headers=auth)
    client.post(f"/api/pdus/{pdu['id']}/poll", headers=auth)

    feeds = {f["name"]: f for f in client.get(f"/api/racks/{rack.id}/power", headers=auth).json()["feeds"]}
    assert feeds["A1"]["current_a"] == 0.0
    assert feeds["B1"]["current_a"] == 8.0
    assert feeds["B1"]["power_w"] == 1664.0  # estimated from 208 V x 8 A


def test_outlet_control_guards(client, auth, rack, server):
    pdu = _create_pdu(client, auth, rack)
    outlet = pdu["outlets"][0]
    url = f"/api/pdus/{pdu['id']}/outlets/{outlet['id']}/action"

    r = client.post(url, json={"action": "off", "confirm": True}, headers=auth)
    assert r.status_code == 403
    assert "not enabled" in r.json()["detail"]

    client.put(f"/api/pdus/{pdu['id']}", json={"outlet_control_enabled": True}, headers=auth)

    r = client.post(url, json={"action": "off"}, headers=auth)
    assert r.status_code == 400

    r = client.post(url, json={"action": "explode", "confirm": True}, headers=auth)
    assert r.status_code == 400

    client.put(f"/api/pdus/{pdu['id']}/outlets/{outlet['id']}", json={"locked": True}, headers=auth)
    r = client.post(url, json={"action": "off", "confirm": True}, headers=auth)
    assert r.status_code == 403
    assert FakePdu.actions == []

    client.put(f"/api/pdus/{pdu['id']}/outlets/{outlet['id']}", json={"locked": False}, headers=auth)
    power = client.get(f"/api/servers/{server.id}/power-inputs", headers=auth).json()
    spi_id = power["inputs"][0]["id"]
    client.put(f"/api/servers/{server.id}/power-inputs/{spi_id}", json={"pdu_outlet_id": outlet["id"]}, headers=auth)
    r = client.post(
        f"/api/servers/{server.id}/power-inputs/{spi_id}/outlet-action",
        json={"action": "cycle", "confirm": True},
        headers=auth,
    )
    assert r.status_code == 200, r.text
    assert FakePdu.actions == [(1, "cycle")]

    activity = client.get(f"/api/servers/{server.id}/activity", headers=auth).json()
    assert any(a["action"] == "pdu_outlet_cycle" for a in activity)


def test_regenerate_keeps_linked_outlets(client, auth, rack, server):
    pdu = _create_pdu(client, auth, rack)
    outlet4 = pdu["outlets"][3]
    power = client.get(f"/api/servers/{server.id}/power-inputs", headers=auth).json()
    client.put(
        f"/api/servers/{server.id}/power-inputs/{power['inputs'][0]['id']}",
        json={"pdu_outlet_id": outlet4["id"]},
        headers=auth,
    )
    FakePdu.outlet_count = 2
    r = client.post(f"/api/pdus/{pdu['id']}/regenerate-ports", headers=auth)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["deleted"] == 1 and data["kept_linked"] == 1
    assert [o["position"] for o in data["pdu"]["outlets"]] == [1, 2, 4]


def test_delete_pdu_unlinks_server_inputs(client, auth, rack, server, db_session):
    pdu = _create_pdu(client, auth, rack)
    power = client.get(f"/api/servers/{server.id}/power-inputs", headers=auth).json()
    spi_id = power["inputs"][0]["id"]
    client.put(
        f"/api/servers/{server.id}/power-inputs/{spi_id}", json={"pdu_outlet_id": pdu["outlets"][0]["id"]}, headers=auth
    )
    r = client.delete(f"/api/pdus/{pdu['id']}", headers=auth)
    assert r.status_code == 204
    power = client.get(f"/api/servers/{server.id}/power-inputs", headers=auth).json()
    assert power["inputs"][0]["outlet"] is None


def test_delete_server_removes_power_inputs(db_session, server):
    ServerDAO.delete(db_session, server.id)
    assert db_session.query(ServerPowerInput).count() == 0
