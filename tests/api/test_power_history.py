"""Power history API: the per-scope endpoints, failed polls and the ATS's inactive input."""
import pytest

from app.models.pdu_sample import PduSample
from app.plugins.pdu.base import PduCapability, PduPlugin
from app.plugins.pdu.registry import get_pdu_registry
from tests.api.test_pdus import (  # noqa: F401  (fixtures and helpers)
    FakeAts,
    FakePdu,
    _create_feed,
    _create_pdu,
    auth,
    fake_plugins,
    rack,
    server,
)


class FailingPdu(PduPlugin):
    PLUGIN_NAME = "fake_failing_pdu"
    PLUGIN_VERSION = "0"
    SUPPORTED_CAPABILITIES = [PduCapability.METERING]
    CONFIG_TEMPLATE = {"type": "object", "properties": {}}

    async def test_connection(self):
        return {"success": True, "message": "ok", "details": {}}

    async def get_pdu_info(self):
        return {"inlet_count": 1, "outlet_count": 0, "outlet_metering": False, "outlet_switching": False}

    async def get_readings(self):
        raise RuntimeError("SNMP timeout")


@pytest.fixture(autouse=True)
def failing_plugin():
    registry = get_pdu_registry()
    registry.register(FailingPdu)
    yield
    registry._plugins.pop(FailingPdu.PLUGIN_NAME, None)


def _poll(client, auth, pdu_id):
    r = client.post(f"/api/pdus/{pdu_id}/poll", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def test_pdu_history_after_a_poll(client, auth, rack):
    pdu = _create_pdu(client, auth, rack)
    assert _poll(client, auth, pdu["id"])["success"] is True

    r = client.get(f"/api/pdus/{pdu['id']}/history?hours=24", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scope"] == "pdu" and body["resolution"] == "raw" and body["hours"] == 24
    assert body["metrics"]["power_w"][0]["value"] == 2000.0
    assert body["inlets"][0]["metrics"]["power_w"][0]["value"] == 2000.0
    bank = body["banks"][0]
    assert bank["name"] == "A" and bank["rated_current_a"] == 16.0
    assert bank["metrics"]["current_a"][0]["value"] == 4.0
    assert body["energy"]["method"] == "integrated" and body["energy"]["kwh"] > 0


def test_history_range_picks_the_resolution(client, auth, rack):
    pdu = _create_pdu(client, auth, rack)
    _poll(client, auth, pdu["id"])
    seen = {
        hours: client.get(f"/api/pdus/{pdu['id']}/history?hours={hours}", headers=auth).json()["resolution"]
        for hours in (6, 24, 168, 24 * 90, 24 * 365)
    }
    assert seen == {6: "raw", 24: "raw", 168: "5m", 24 * 90: "1h", 24 * 365: "1h"}
    assert client.get(f"/api/pdus/{pdu['id']}/history?hours=0", headers=auth).status_code == 422
    clamped = client.get(f"/api/pdus/{pdu['id']}/history?hours={24 * 400}", headers=auth).json()
    assert clamped["hours"] == 396 * 24


def test_failed_poll_writes_no_history(client, auth, rack, db_session):
    pdu = client.post(
        "/api/pdus/",
        json={
            "name": "bad", "location_id": rack.location_id, "rack_id": rack.id,
            "plugin_name": FailingPdu.PLUGIN_NAME, "inlet_count": 1,
        },
        headers=auth,
    ).json()
    result = _poll(client, auth, pdu["id"])
    assert result["success"] is False
    assert db_session.query(PduSample).count() == 0
    body = client.get(f"/api/pdus/{pdu['id']}/history", headers=auth).json()
    assert body["metrics"]["power_w"] == [] and body["energy"] == {"kwh": None, "method": None}


def test_ats_inactive_input_is_recorded_as_zero(client, auth, rack):
    feed_a = _create_feed(client, auth, rack, name="A1")
    feed_b = _create_feed(client, auth, rack, name="B1")
    pdu = client.post(
        "/api/pdus/",
        json={"name": "ats", "location_id": rack.location_id, "rack_id": rack.id, "plugin_name": FakeAts.PLUGIN_NAME},
        headers=auth,
    ).json()
    for inlet, feed in zip(pdu["inlets"], (feed_a, feed_b)):
        client.put(f"/api/pdus/{pdu['id']}/inlets/{inlet['id']}", json={"power_feed_id": feed["id"]}, headers=auth)
    _poll(client, auth, pdu["id"])
    inactive, active = pdu["inlets"]

    idle = client.get(f"/api/pdus/{pdu['id']}/inlets/{inactive['id']}/history", headers=auth).json()
    assert idle["metrics"]["power_w"][0]["value"] == 0.0
    busy = client.get(f"/api/pdus/{pdu['id']}/inlets/{active['id']}/history", headers=auth).json()
    assert busy["metrics"]["power_w"][0]["value"] == 1664.0

    feed_hist = {f["name"]: client.get(f"/api/power-feeds/{f['id']}/history", headers=auth).json() for f in (feed_a, feed_b)}
    assert feed_hist["A1"]["metrics"]["power_w"][0]["value"] == 0.0
    assert feed_hist["B1"]["metrics"]["power_w"][0]["value"] == 1664.0
    assert feed_hist["B1"]["limits"]["usable_current_a"] == 24.0
    assert feed_hist["B1"]["inlets"][0]["pdu_name"] == "ats"

    wrong_pdu = client.get(f"/api/pdus/{pdu['id'] + 99}/inlets/{active['id']}/history", headers=auth)
    assert wrong_pdu.status_code == 404


def test_rack_location_and_overall_history(client, auth, rack):
    pdu = _create_pdu(client, auth, rack)
    _poll(client, auth, pdu["id"])
    rack_hist = client.get(f"/api/racks/{rack.id}/power-history", headers=auth).json()
    assert rack_hist["scope"] == "rack" and rack_hist["metrics"]["power_w"][0]["value"] == 2000.0
    assert rack_hist["current_power_w"] == 2000.0
    loc_hist = client.get(f"/api/power-history?location_id={rack.location_id}", headers=auth).json()
    assert loc_hist["scope"] == "location" and loc_hist["pdu_count"] == 1
    overall = client.get("/api/power-history?hours=168", headers=auth).json()
    assert overall["scope"] == "all" and overall["resolution"] == "5m"
    assert overall["metrics"]["power_w"][0]["value"] == 2000.0
    assert client.get("/api/power-history?location_id=9999", headers=auth).status_code == 404
    assert client.get("/api/racks/9999/power-history", headers=auth).status_code == 404


def test_server_history_from_metered_outlets(client, auth, rack, server):
    pdu = _create_pdu(client, auth, rack)
    inputs = client.get(f"/api/servers/{server.id}/power-inputs", headers=auth).json()["inputs"]

    none_linked = client.get(f"/api/servers/{server.id}/power-history", headers=auth).json()
    assert none_linked["available"] is False and "outlet" in none_linked["reason"]
    assert none_linked["metrics"]["power_w"] == []

    client.put(
        f"/api/servers/{server.id}/power-inputs/{inputs[0]['id']}",
        json={"pdu_outlet_id": pdu["outlets"][1]["id"]},
        headers=auth,
    )
    client.post(f"/api/servers/{server.id}/power-inputs", json={"name": "PSU2"}, headers=auth)
    _poll(client, auth, pdu["id"])

    body = client.get(f"/api/servers/{server.id}/power-history", headers=auth).json()
    assert body["available"] is True and body["complete"] is False
    assert "part of the server" in body["reason"]
    assert body["metrics"]["power_w"][0]["value"] == 500.0
    assert [i["status"] for i in body["inputs"]] == ["metered", "unlinked"]
    assert body["energy"]["method"] == "integrated"


def test_server_on_an_unmetered_pdu_says_so(client, auth, rack, server):
    pdu = _create_pdu(client, auth, rack, outlet_metering=False)
    inputs = client.get(f"/api/servers/{server.id}/power-inputs", headers=auth).json()["inputs"]
    client.put(
        f"/api/servers/{server.id}/power-inputs/{inputs[0]['id']}",
        json={"pdu_outlet_id": pdu["outlets"][0]["id"]},
        headers=auth,
    )
    body = client.get(f"/api/servers/{server.id}/power-history", headers=auth).json()
    assert body["available"] is False
    assert body["inputs"][0]["status"] == "unmetered" and "does not meter" in body["inputs"][0]["reason"]


def test_history_needs_an_admin(client, rack):
    assert client.get("/api/power-history").status_code in (401, 403)
