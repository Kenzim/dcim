"""Tests for storing polled switch port status (SwitchPortDAO.apply_status and the bandwidth poller)."""
from datetime import datetime, timezone
from types import SimpleNamespace

from app.dao.switch_port_dao import SwitchPortDAO
from app.models.location import Location
from app.models.network_switch import NetworkSwitch
from app.models.switch_bandwidth_sample import SwitchBandwidthSample
from app.models.switch_port import SwitchPort
from scripts.snmp_bandwidth_poller import store_poll_results


def _switch(db):
    loc = Location(name="DC1")
    db.add(loc)
    db.flush()
    sw = NetworkSwitch(name="tor", location_id=loc.id, plugin_name="snmpv3", plugin_config={}, enabled=True)
    db.add(sw)
    db.flush()
    ports = [
        SwitchPort(switch_id=sw.id, name="Ethernet1/1", if_index=101),
        SwitchPort(switch_id=sw.id, name="Ethernet1/2", if_index=102),
        SwitchPort(switch_id=sw.id, name="Ethernet1/3", if_index=None),
        SwitchPort(switch_id=sw.id, name="Ethernet1/4", if_index=104),
    ]
    db.add_all(ports)
    db.commit()
    return sw, ports


def _stats():
    return {
        "Ethernet1/1": {"ifIndex": 101, "name": "Ethernet1/1", "ifAdminStatus": 1, "ifOperStatus": 1},
        "renamed": {"ifIndex": 102, "name": "renamed", "ifAdminStatus": 1, "ifOperStatus": 2},
        "Ethernet1/3": {"ifIndex": 999, "name": "Ethernet1/3", "ifAdminStatus": 2, "ifOperStatus": 0},
        "Ethernet9/9": {"ifIndex": 909, "name": "Ethernet9/9", "ifAdminStatus": 1, "ifOperStatus": 1},
    }


def test_apply_status_matches_by_if_index_then_name(db_session):
    sw, ports = _switch(db_session)
    before = {p.id: p.updated_at for p in ports}
    polled_at = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)

    updated = SwitchPortDAO.apply_status(db_session, sw.id, _stats(), polled_at=polled_at)

    assert updated == 3
    db_session.expire_all()
    p1, p2, p3, p4 = (db_session.get(SwitchPort, p.id) for p in ports)
    assert (p1.admin_status, p1.oper_status) == (1, 1)
    assert (p2.admin_status, p2.oper_status) == (1, 2)
    assert (p3.admin_status, p3.oper_status) == (2, None)
    assert p1.status_updated_at.replace(tzinfo=timezone.utc) == polled_at
    assert p4.status_updated_at is None and p4.oper_status is None
    assert p1.updated_at == before[p1.id]


def test_apply_status_with_no_matching_ports(db_session):
    sw, _ = _switch(db_session)
    assert SwitchPortDAO.apply_status(db_session, sw.id, {"x": {"ifIndex": 5, "name": "x", "ifOperStatus": 1}}) == 0


def test_store_poll_results_writes_samples_and_status(db_session):
    sw, ports = _switch(db_session)
    now = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
    switch = SimpleNamespace(id=sw.id, name=sw.name)

    stored, updated = store_poll_results(db_session, [(switch, _stats())], now)

    assert stored == 4
    assert updated == 3
    assert db_session.query(SwitchBandwidthSample).filter_by(switch_id=sw.id).count() == 4
    db_session.expire_all()
    assert db_session.get(SwitchPort, ports[0].id).oper_status == 1
