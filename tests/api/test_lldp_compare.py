"""Tests for the LLDP comparison (GET /api/admin/cabling/lldp and app.services.lldp_compare_service)."""
from datetime import datetime, timezone

import pytest
from sqlalchemy import event

from app.models.cable_run import CableRun
from app.models.location import Location
from app.models.network_port import NetworkPort
from app.models.network_switch import NetworkSwitch
from app.models.server import Server
from app.models.switch_lldp_neighbor import SwitchLldpNeighbor
from app.models.switch_port import SwitchPort
from app.services.lldp_compare_service import compare_lldp, normalise_mac

URL = "/api/admin/cabling/lldp"
COLLECTED = datetime(2026, 11, 17, 9, 0, tzinfo=timezone.utc)


def test_normalise_mac():
    assert normalise_mac("AC:1F:6B:0A:0B:0C") == "ac:1f:6b:0a:0b:0c"
    assert normalise_mac("ac-1f-6b-0a-0b-0c") == "ac:1f:6b:0a:0b:0c"
    assert normalise_mac("ac1f.6b0a.0b0c") == "ac:1f:6b:0a:0b:0c"
    assert normalise_mac("ac1f6b0a0b0c") == "ac:1f:6b:0a:0b:0c"
    assert normalise_mac("Ethernet1/1") is None
    assert normalise_mac("eno1") is None
    assert normalise_mac(None) is None


def _switch(db, name, loc, collected=True, plugin="snmpv3"):
    sw = NetworkSwitch(
        name=name, location_id=loc.id, plugin_name=plugin, plugin_config={"auth_password": "s3cret"},
        enabled=True, lldp_collected_at=COLLECTED if collected else None,
    )
    db.add(sw)
    db.flush()
    return sw


def _swport(db, sw, name):
    p = SwitchPort(switch_id=sw.id, name=name)
    db.add(p)
    db.flush()
    return p


def _server(db, name, loc):
    s = Server(name=name, server_ip="10.0.0.1", location_id=loc.id, plugin_name="ipmi", plugin_config={"password": "s3cret"})
    db.add(s)
    db.flush()
    return s


def _nic(db, server, name, mac):
    p = NetworkPort(server_id=server.id, name=name, mac_address=mac, speed_mbps=10000)
    db.add(p)
    db.flush()
    return p


def _lldp(db, sw, port, **fields):
    n = SwitchLldpNeighbor(switch_id=sw.id, switch_port_id=port.id if port else None, collected_at=COLLECTED, **fields)
    db.add(n)
    db.flush()
    return n


@pytest.fixture
def setup(db_session):
    db = db_session
    dc1 = Location(name="DC1")
    dc2 = Location(name="DC2")
    db.add_all([dc1, dc2])
    db.flush()
    tor = _switch(db, "tor-a", dc1)
    core = _switch(db, "core-01", dc1)
    never = _switch(db, "tor-new", dc1, collected=False)
    far = _switch(db, "tor-ams", dc2)

    p = {name: _swport(db, tor, name) for name in [f"Ethernet1/{i}" for i in range(1, 10)]}
    core_p = {name: _swport(db, core, name) for name in ["Ethernet49", "Ethernet50"]}
    never_p1 = _swport(db, never, "Ethernet1/1")
    far_p1 = _swport(db, far, "Ethernet1/1")

    hv1 = _server(db, "hv-01", dc1)
    hv1_eno1 = _nic(db, hv1, "eno1", "AC:1F:6B:00:00:01")
    hv1_eno2 = _nic(db, hv1, "eno2", "ac:1f:6b:00:00:02")
    hv2 = _server(db, "hv-02", dc1)
    hv2_eno1 = _nic(db, hv2, "eno1", "ac:1f:6b:00:00:21")
    db1 = _server(db, "db-01", dc1)
    db1_eno1 = _nic(db, db1, "eno1", "ac:1f:6b:00:00:31")
    web = _server(db, "web-01", dc1)
    web_eno1 = _nic(db, web, "eno1", "ac:1f:6b:00:00:41")
    ams = _server(db, "ams-01", dc2)
    ams_eno1 = _nic(db, ams, "eno1", "ac:1f:6b:00:00:51")

    def cable(a, b):
        kw = {}
        kw["end_a_server_port_id" if isinstance(a, NetworkPort) else "end_a_switch_port_id"] = a.id
        kw["end_b_server_port_id" if isinstance(b, NetworkPort) else "end_b_switch_port_id"] = b.id
        c = CableRun(**kw)
        db.add(c)
        db.flush()
        return c

    cables = {
        "match_mac": cable(hv1_eno1, p["Ethernet1/1"]),
        "mismatch_port": cable(hv1_eno2, p["Ethernet1/2"]),
        "mismatch_device": cable(db1_eno1, p["Ethernet1/3"]),
        "no_lldp": cable(web_eno1, p["Ethernet1/4"]),
        "uplink": cable(p["Ethernet1/9"], core_p["Ethernet49"]),
        "never": cable(hv2_eno1, never_p1),
        "far": cable(ams_eno1, far_p1),
    }

    _lldp(db, tor, p["Ethernet1/1"], port_id_subtype=3, port_id="ac:1f:6b:00:00:01", sys_name="hv-01")
    _lldp(db, tor, p["Ethernet1/2"], port_id_subtype=3, port_id="ac:1f:6b:00:00:01", sys_name="hv-01")
    _lldp(db, tor, p["Ethernet1/3"], chassis_id_subtype=4, chassis_id="ac-1f-6b-00-00-21", port_id_subtype=5,
          port_id="eno1", sys_name="hv-02.example.net")
    _lldp(db, tor, p["Ethernet1/5"], port_id_subtype=7, port_id="eno1", sys_name="web-01.example.net")
    _lldp(db, tor, p["Ethernet1/6"], chassis_id="00:11:22:33:44:55", port_id="Gi0/1", sys_name="unknown-router")
    _lldp(db, tor, p["Ethernet1/9"], chassis_id="00:aa:bb:cc:dd:ee", port_id="Ethernet49", port_desc="to tor-a",
          sys_name="core-01.example.net")
    _lldp(db, tor, None, local_port_num=77, local_port_label="Ethernet1/77", port_id="x", sys_name="mystery")
    _lldp(db, core, core_p["Ethernet49"], port_id="Ethernet1/9", sys_name="tor-a")
    _lldp(db, core, core_p["Ethernet50"], port_id="Ethernet1/8", sys_name="tor-a")
    _lldp(db, never, never_p1, port_id="ac:1f:6b:00:00:99", sys_name="ghost")
    _lldp(db, far, far_p1, port_id_subtype=3, port_id="ac:1f:6b:00:00:51", sys_name="ams-01")
    db.commit()
    return {
        "dc1": dc1, "dc2": dc2, "tor": tor, "core": core, "never": never, "far": far, "p": p, "core_p": core_p,
        "cables": cables, "hv1": hv1, "hv2": hv2, "web": web, "db1": db1,
    }


def _by_port(result):
    return {(r["switch_id"], r["switch_port_name"]): r for r in result["results"]}


def test_compare_statuses(db_session, setup):
    result = compare_lldp(db_session)
    rows = _by_port(result)
    tor = setup["tor"].id
    cables = setup["cables"]

    r = rows[(tor, "Ethernet1/1")]
    assert r["status"] == "match"
    assert r["cable_id"] == cables["match_mac"].id
    assert r["observed"]["resolved_by"] == "port_mac"
    assert r["observed"]["resolved"]["port_name"] == "eno1"
    assert r["recorded"]["device_name"] == "hv-01"

    r = rows[(tor, "Ethernet1/2")]
    assert r["status"] == "mismatch"
    assert r["recorded"]["port_name"] == "eno2"
    assert r["observed"]["resolved"]["port_name"] == "eno1"

    r = rows[(tor, "Ethernet1/3")]
    assert r["status"] == "mismatch"
    assert r["recorded"]["device_name"] == "db-01"
    assert r["observed"]["resolved"]["device_name"] == "hv-02"
    assert r["observed"]["resolved_by"] == "chassis_mac"
    assert r["observed"]["resolved"]["port_name"] == "eno1"

    r = rows[(tor, "Ethernet1/4")]
    assert r["status"] == "no_lldp"
    assert r["observed"] is None
    assert r["recorded"]["device_name"] == "web-01"

    r = rows[(tor, "Ethernet1/5")]
    assert r["status"] == "missing_cable"
    assert r["cable_id"] is None
    assert r["observed"]["resolved_by"] == "system_name"
    assert r["observed"]["resolved"]["device_name"] == "web-01"
    assert r["observed"]["resolved"]["port_name"] == "eno1"

    r = rows[(tor, "Ethernet1/6")]
    assert r["status"] == "unknown_neighbour"
    assert r["observed"]["resolved"] is None
    assert r["observed"]["sys_name"] == "unknown-router"

    r = rows[(tor, "Ethernet1/9")]
    assert r["status"] == "match"
    assert r["observed"]["resolved"]["kind"] == "switch"
    assert r["observed"]["resolved"]["port_name"] == "Ethernet49"

    core = setup["core"].id
    assert rows[(core, "Ethernet49")]["status"] == "match"
    assert rows[(core, "Ethernet50")]["status"] == "missing_cable"
    assert rows[(core, "Ethernet50")]["observed"]["resolved"]["port_name"] == "Ethernet1/8"

    unmapped = [r for r in result["results"] if r["status"] == "unmapped_local_port"]
    assert len(unmapped) == 1
    assert unmapped[0]["local_port_label"] == "Ethernet1/77"
    assert unmapped[0]["switch_port"] is None

    assert (tor, "Ethernet1/7") not in rows
    assert all(r["switch_id"] != setup["never"].id for r in result["results"])

    summary = result["summary"]
    assert summary["match"] == 4
    assert summary["mismatch"] == 2
    assert summary["missing_cable"] == 2
    assert summary["unknown_neighbour"] == 1
    assert summary["no_lldp"] == 1
    assert summary["unmapped_local_port"] == 1

    switches = {s["name"]: s for s in result["switches"]}
    assert switches["tor-new"]["lldp_collected_at"] is None
    assert switches["tor-new"]["neighbour_count"] == 1
    assert switches["tor-a"]["supports_lldp"] is True
    assert switches["tor-a"]["neighbour_count"] == 7


def test_device_level_match_when_port_unknown(db_session, setup):
    tor = setup["tor"]
    port = setup["p"]["Ethernet1/4"]
    _lldp(db_session, tor, port, chassis_id="11:22:33:44:55:66", port_id="some-port-label", sys_name="web-01")
    db_session.commit()
    r = _by_port(compare_lldp(db_session))[(tor.id, "Ethernet1/4")]
    assert r["status"] == "match"
    assert r["observed"]["resolved"]["port"] is None
    assert r["observed"]["resolved"]["device_name"] == "web-01"


def test_shared_mac_resolution(db_session, setup):
    tor, p, dc1 = setup["tor"], setup["p"], setup["dc1"]
    bond = _server(db_session, "bond-01", dc1)
    b1 = _nic(db_session, bond, "ens1f0", "ac:1f:6b:77:00:01")
    b2 = _nic(db_session, bond, "ens1f1", "ac:1f:6b:77:00:01")
    other = _server(db_session, "clone-01", dc1)
    _nic(db_session, other, "eno1", "ac:1f:6b:77:00:99")
    clone2 = _server(db_session, "clone-02", dc1)
    _nic(db_session, clone2, "eno1", "ac:1f:6b:77:00:99")
    db_session.add(CableRun(end_a_server_port_id=b1.id, end_b_switch_port_id=p["Ethernet1/7"].id))
    db_session.add(CableRun(end_a_server_port_id=b2.id, end_b_switch_port_id=p["Ethernet1/8"].id))
    _lldp(db_session, tor, p["Ethernet1/7"], port_id="ac:1f:6b:77:00:01", port_desc="ens1f0")
    _lldp(db_session, tor, p["Ethernet1/8"], port_id="ac:1f:6b:77:00:01")
    port = _swport(db_session, tor, "Ethernet3/1")
    _lldp(db_session, tor, port, port_id="ac:1f:6b:77:00:99")
    db_session.commit()

    rows = _by_port(compare_lldp(db_session))
    named = rows[(tor.id, "Ethernet1/7")]
    assert named["status"] == "match"
    assert named["observed"]["resolved"]["port_name"] == "ens1f0"
    unnamed = rows[(tor.id, "Ethernet1/8")]
    assert unnamed["status"] == "match"
    assert unnamed["observed"]["resolved"]["port"] is None
    assert unnamed["observed"]["resolved"]["device_name"] == "bond-01"
    assert rows[(tor.id, "Ethernet3/1")]["status"] == "unknown_neighbour"


def test_filter_by_location(db_session, setup):
    result = compare_lldp(db_session, location_id=setup["dc2"].id)
    assert [s["name"] for s in result["switches"]] == ["tor-ams"]
    assert [r["status"] for r in result["results"]] == ["match"]


def _login(client, username, password):
    r = client.post("/api/users/login", json={"username": username, "password": password})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_endpoint_shape_and_auth(client, db_session, setup, test_admin_user, test_user):
    r = client.get(URL, headers=_login(client, test_admin_user.username, "adminpassword123"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"filters", "generated_at", "switches", "results", "summary"}
    assert body["summary"]["mismatch"] == 2
    assert "s3cret" not in r.text
    assert "plugin_config" not in r.text

    r = client.get(URL, params={"rack_id": 0}, headers=_login(client, test_admin_user.username, "adminpassword123"))
    assert r.status_code == 422
    r = client.get(URL, headers=_login(client, test_user.username, "testpassword123"))
    assert r.status_code == 403
    client.cookies.clear()
    assert client.get(URL).status_code == 401


def _count_queries(db_session, fn):
    engine = db_session.get_bind()
    count = 0

    def before(*args):
        nonlocal count
        count += 1

    event.listen(engine, "before_cursor_execute", before)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", before)
    return count


def test_query_count_does_not_grow(db_session, setup):
    baseline = _count_queries(db_session, lambda: compare_lldp(db_session))
    tor = setup["tor"]
    for i in range(10):
        s = _server(db_session, f"bulk-{i}", setup["dc1"])
        nic = _nic(db_session, s, "eno1", f"ac:1f:6b:10:00:{i:02x}")
        port = _swport(db_session, tor, f"Ethernet2/{i}")
        db_session.add(CableRun(end_a_server_port_id=nic.id, end_b_switch_port_id=port.id))
        _lldp(db_session, tor, port, port_id=f"ac:1f:6b:10:00:{i:02x}", sys_name=f"bulk-{i}")
    db_session.commit()
    assert _count_queries(db_session, lambda: compare_lldp(db_session)) == baseline
