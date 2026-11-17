"""Tests for LLDP collection: LLDP-MIB parsing, SNMP walk, snapshot storage and the admin refresh endpoint."""
import asyncio
from datetime import datetime, timezone

import pytest
from pysnmp.proto.rfc1902 import Integer32, OctetString
from pysnmp.proto.rfc1905 import EndOfMibView

from app.dao.switch_lldp_neighbor_dao import SwitchLldpNeighborDAO
from app.models.location import Location
from app.models.network_switch import NetworkSwitch
from app.models.switch_lldp_neighbor import SwitchLldpNeighbor
from app.models.switch_port import SwitchPort
from app.plugins import snmpv3
from app.plugins.snmpv3 import LLDP_REM_TABLE, parse_lldp_tables

MAC = bytes.fromhex("ac1f6b0a0b0c")
CHASSIS = bytes.fromhex("0050569a0001")


def _loc_rows():
    return [
        ((2, 501), Integer32(5)), ((3, 501), OctetString(b"Ethernet1/1")), ((4, 501), OctetString(b"server uplink")),
        ((2, 502), Integer32(7)), ((3, 502), OctetString(b"502")), ((4, 502), OctetString(b"Ethernet1/2")),
        ((2, 503), Integer32(7)), ((3, 503), OctetString(b"x")), ((4, 503), OctetString(b"")),
    ]


def _rem_rows():
    rows = []

    def add(port_num, rem_index, **cols):
        for column, value in cols.items():
            rows.append(((int(column[1:]), 0, port_num, rem_index), value))

    add(501, 1, c4=Integer32(4), c5=OctetString(CHASSIS), c6=Integer32(3), c7=OctetString(MAC),
        c8=OctetString(b"eno1"), c9=OctetString(b"hv-01.example"), c10=OctetString(b"Debian 12"))
    add(502, 7, c4=Integer32(4), c5=OctetString(bytes.fromhex("001122334455")), c6=Integer32(5),
        c7=OctetString(b"Ethernet49"), c9=OctetString(b"core-01"))
    add(503, 2, c4=Integer32(7), c5=OctetString(b"\x01\xff\x00"), c6=Integer32(7), c7=OctetString(b"port 3"))
    rows.append(((99, 0, 503, 2), OctetString(b"ignored column")))
    rows.append(((5, 0, 503), OctetString(b"malformed index")))
    return rows


def test_parse_lldp_tables():
    neighbours = parse_lldp_tables(_loc_rows(), _rem_rows())
    assert len(neighbours) == 3
    n1, n2, n3 = neighbours

    assert n1["local_port_num"] == 501
    assert n1["local_port_id"] == "Ethernet1/1"
    assert n1["local_port_desc"] == "server uplink"
    assert n1["chassis_id_subtype"] == 4 and n1["chassis_id"] == "00:50:56:9a:00:01"
    assert n1["port_id_subtype"] == 3 and n1["port_id"] == "ac:1f:6b:0a:0b:0c"
    assert n1["port_desc"] == "eno1"
    assert n1["sys_name"] == "hv-01.example"
    assert n1["sys_desc"] == "Debian 12"

    assert n2["local_port_id"] == "502" and n2["local_port_desc"] == "Ethernet1/2"
    assert n2["remote_index"] == 7
    assert n2["port_id"] == "Ethernet49"
    assert n2["sys_name"] == "core-01"
    assert n2["port_desc"] is None

    assert n3["chassis_id"] == "01:ff:00"
    assert n3["local_port_desc"] is None


def test_parse_lldp_tables_without_local_table():
    neighbours = parse_lldp_tables([], _rem_rows())
    assert neighbours[0]["local_port_id"] is None
    assert neighbours[0]["local_port_num"] == 501


class _Oid:
    def __init__(self, text):
        self.text = text

    def __str__(self):
        return self.text


def _plugin():
    return snmpv3.SNMPv3Plugin({
        "hostname": "192.0.2.10", "username": "monitoruser", "auth_password": "authpassword1",
        "privacy_password": "privpassword1", "security_level": "authPriv",
    })


def test_walk_stops_when_leaving_subtree(monkeypatch):
    responses = [
        [(_Oid(f"{LLDP_REM_TABLE}.9.0.501.1"), OctetString(b"a"))],
        [(_Oid(f"{LLDP_REM_TABLE}.9.0.502.1"), OctetString(b"b"))],
        [(_Oid("1.0.8802.1.1.2.1.4.2.1.3.0.501.1"), OctetString(b"next table"))],
        [(_Oid(f"{LLDP_REM_TABLE}.9.0.503.1"), OctetString(b"never reached"))],
    ]
    calls = []

    async def fake_next_cmd(*args, **kwargs):
        calls.append(args)
        return None, None, None, responses[len(calls) - 1]

    async def fake_transport(self):
        return object()

    monkeypatch.setattr(snmpv3, "next_cmd", fake_next_cmd)
    monkeypatch.setattr(snmpv3.SNMPv3Plugin, "_get_transport", fake_transport)
    rows = asyncio.run(_plugin()._walk(object(), LLDP_REM_TABLE))
    assert [r[0] for r in rows] == [(9, 0, 501, 1), (9, 0, 502, 1)]
    assert len(calls) == 3


def test_walk_stops_at_end_of_mib_view(monkeypatch):
    responses = [
        [(_Oid(f"{LLDP_REM_TABLE}.9.0.501.1"), OctetString(b"a"))],
        [(_Oid(f"{LLDP_REM_TABLE}.9.0.501.1"), EndOfMibView())],
    ]
    calls = []

    async def fake_next_cmd(*args, **kwargs):
        calls.append(args)
        return None, None, None, responses[len(calls) - 1]

    async def fake_transport(self):
        return object()

    monkeypatch.setattr(snmpv3, "next_cmd", fake_next_cmd)
    monkeypatch.setattr(snmpv3.SNMPv3Plugin, "_get_transport", fake_transport)
    rows = asyncio.run(_plugin()._walk(object(), LLDP_REM_TABLE))
    assert len(rows) == 1
    assert len(calls) == 2


def test_walk_raises_on_error(monkeypatch):
    async def fake_next_cmd(*args, **kwargs):
        return "requestTimedOut", None, None, []

    async def fake_transport(self):
        return object()

    monkeypatch.setattr(snmpv3, "next_cmd", fake_next_cmd)
    monkeypatch.setattr(snmpv3.SNMPv3Plugin, "_get_transport", fake_transport)
    with pytest.raises(Exception, match="requestTimedOut"):
        asyncio.run(_plugin()._walk(object(), LLDP_REM_TABLE))


def test_snmpv3_declares_lldp_support():
    from app.plugins.switch_registry import get_switch_registry

    assert get_switch_registry().supports_lldp("snmpv3") is True
    assert get_switch_registry().supports_lldp("does-not-exist") is False


@pytest.fixture
def switch(db_session):
    loc = Location(name="DC1")
    db_session.add(loc)
    db_session.flush()
    sw = NetworkSwitch(name="tor-a", location_id=loc.id, plugin_name="snmpv3", plugin_config={}, enabled=True)
    db_session.add(sw)
    db_session.flush()
    db_session.add_all([
        SwitchPort(switch_id=sw.id, name="Ethernet1/1", if_index=501),
        SwitchPort(switch_id=sw.id, name="Ethernet1/2", if_index=None),
        SwitchPort(switch_id=sw.id, name="Ethernet1/3", if_index=503),
    ])
    db_session.commit()
    return sw


def _ports(db, sw):
    return {p.name: p.id for p in db.query(SwitchPort).filter_by(switch_id=sw.id)}


def test_replace_for_switch_resolves_ports_and_replaces_snapshot(db_session, switch):
    neighbours = parse_lldp_tables(_loc_rows(), _rem_rows())
    collected = datetime(2026, 11, 17, 9, 0, tzinfo=timezone.utc)
    SwitchLldpNeighborDAO.replace_for_switch(db_session, switch.id, neighbours, collected_at=collected)

    ports = _ports(db_session, switch)
    rows = {r.local_port_num: r for r in db_session.query(SwitchLldpNeighbor).filter_by(switch_id=switch.id)}
    assert rows[501].switch_port_id == ports["Ethernet1/1"]
    assert rows[502].switch_port_id == ports["Ethernet1/2"]
    assert rows[503].switch_port_id == ports["Ethernet1/3"]
    assert rows[501].port_id == "ac:1f:6b:0a:0b:0c"
    assert rows[501].local_port_label == "Ethernet1/1"
    db_session.refresh(switch)
    assert switch.lldp_collected_at.replace(tzinfo=timezone.utc) == collected
    assert switch.lldp_error is None

    SwitchLldpNeighborDAO.replace_for_switch(db_session, switch.id, neighbours[:1])
    assert db_session.query(SwitchLldpNeighbor).filter_by(switch_id=switch.id).count() == 1


def test_replace_only_touches_one_switch(db_session, switch):
    other = NetworkSwitch(name="tor-b", location_id=switch.location_id, plugin_name="snmpv3", plugin_config={})
    db_session.add(other)
    db_session.commit()
    neighbours = parse_lldp_tables(_loc_rows(), _rem_rows())
    SwitchLldpNeighborDAO.replace_for_switch(db_session, switch.id, neighbours)
    SwitchLldpNeighborDAO.replace_for_switch(db_session, other.id, neighbours[:2])
    SwitchLldpNeighborDAO.replace_for_switch(db_session, switch.id, [])
    assert db_session.query(SwitchLldpNeighbor).filter_by(switch_id=switch.id).count() == 0
    assert db_session.query(SwitchLldpNeighbor).filter_by(switch_id=other.id).count() == 2
    assert {r.switch_port_id for r in db_session.query(SwitchLldpNeighbor).filter_by(switch_id=other.id)} == {None}


def test_record_error_keeps_snapshot(db_session, switch):
    SwitchLldpNeighborDAO.replace_for_switch(db_session, switch.id, parse_lldp_tables(_loc_rows(), _rem_rows()))
    SwitchLldpNeighborDAO.record_error(db_session, switch.id, "timeout")
    db_session.refresh(switch)
    assert switch.lldp_error == "timeout"
    assert switch.lldp_collected_at is not None
    assert db_session.query(SwitchLldpNeighbor).filter_by(switch_id=switch.id).count() == 3


class _FakePlugin:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    async def get_lldp_neighbors(self):
        if self.error:
            raise Exception(self.error)
        return self.result


def _admin(client, test_admin_user):
    r = client.post("/api/users/login", json={"username": test_admin_user.username, "password": "adminpassword123"})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _use_plugin(monkeypatch, plugin):
    from app.plugins.switch_registry import get_switch_registry

    monkeypatch.setattr(get_switch_registry(), "get_plugin", lambda name, config: plugin)


def test_refresh_endpoint(client, db_session, switch, test_admin_user, monkeypatch):
    _use_plugin(monkeypatch, _FakePlugin(result=parse_lldp_tables(_loc_rows(), _rem_rows())))
    r = client.post(f"/api/admin/cabling/lldp/switches/{switch.id}/refresh", headers=_admin(client, test_admin_user))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["switch_id"] == switch.id
    assert body["neighbours"] == 3
    assert body["matched_ports"] == 3
    assert body["collected_at"]


def test_refresh_endpoint_plugin_error_keeps_snapshot(client, db_session, switch, test_admin_user, monkeypatch):
    SwitchLldpNeighborDAO.replace_for_switch(db_session, switch.id, parse_lldp_tables(_loc_rows(), _rem_rows()))
    _use_plugin(monkeypatch, _FakePlugin(error="requestTimedOut"))
    r = client.post(f"/api/admin/cabling/lldp/switches/{switch.id}/refresh", headers=_admin(client, test_admin_user))
    assert r.status_code == 502
    assert "requestTimedOut" in r.json()["detail"]
    db_session.expire_all()
    assert db_session.query(SwitchLldpNeighbor).filter_by(switch_id=switch.id).count() == 3
    assert db_session.get(NetworkSwitch, switch.id).lldp_error == "requestTimedOut"


def test_refresh_endpoint_errors(client, db_session, switch, test_admin_user, test_user):
    headers = _admin(client, test_admin_user)
    assert client.post("/api/admin/cabling/lldp/switches/99999/refresh", headers=headers).status_code == 404
    switch.plugin_name = "no-such-plugin"
    db_session.commit()
    assert client.post(f"/api/admin/cabling/lldp/switches/{switch.id}/refresh", headers=headers).status_code == 400
    client.cookies.clear()
    assert client.post(f"/api/admin/cabling/lldp/switches/{switch.id}/refresh").status_code == 401
    r = client.post("/api/users/login", json={"username": test_user.username, "password": "testpassword123"})
    user_headers = {"Authorization": f"Bearer {r.json()['token']}"}
    assert client.post(f"/api/admin/cabling/lldp/switches/{switch.id}/refresh", headers=user_headers).status_code == 403
