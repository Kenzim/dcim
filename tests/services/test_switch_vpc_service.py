"""vPC pair detection: keepalive addresses decide the pairing, not the (often reused) domain ID."""
from unittest.mock import AsyncMock, patch

import pytest

from app.models.location import Location
from app.models.network_switch import NetworkSwitch
from app.services import switch_vpc_service
from app.services.switch_vpc_service import detect_vpc_pairs, pair_switches, vpc_summary


def _info(src, dst, domain=1, peer_link="port-channel45"):
    return {"domain_id": domain, "keepalive_source": src, "keepalive_destination": dst, "peer_link": peer_link,
            "role": "primary", "keepalive_status": "alive", "vpcs": [{"vpc_id": 120, "consistency": "success"}]}


def test_pair_switches_uses_keepalive_addresses():
    infos = {
        5: _info("192.168.1.1", "192.168.1.2"),
        6: _info("192.168.1.2", "192.168.1.1"),
        7: _info("192.168.3.1", "192.168.3.2"),
        8: _info("192.168.3.2", "192.168.3.1"),
        3: _info("192.168.2.1", "192.168.2.2"),
    }
    assert pair_switches(infos) == {5: 6, 6: 5, 7: 8, 8: 7, 3: None}


@pytest.mark.asyncio
async def test_detect_vpc_pairs_stores_pairing_and_errors(db_session):
    location = Location(name="DC", description="")
    db_session.add(location)
    db_session.commit()
    names = ["25G-1", "25G-2", "CL-1", "plain", "down"]
    switches = {n: NetworkSwitch(name=n, location_id=location.id, plugin_name="snmpv3", plugin_config={}) for n in names}
    db_session.add_all(switches.values())
    db_session.commit()
    switches["plain"].vpc_domain_id = 9  # stale pairing is cleared when the switch has no vPC
    db_session.commit()

    answers = {
        "25G-1": _info("192.168.1.1", "192.168.1.2"),
        "25G-2": _info("192.168.1.2", "192.168.1.1"),
        "CL-1": _info("192.168.2.1", "192.168.2.2"),
        "plain": None,
        "down": RuntimeError("timeout"),
    }

    async def read(switch):
        answer = answers[switch.name]
        if isinstance(answer, Exception):
            raise answer
        return answer

    with patch.object(switch_vpc_service, "_read", AsyncMock(side_effect=read)):
        report = await detect_vpc_pairs(db_session)

    a, b = switches["25G-1"], switches["25G-2"]
    assert (a.vpc_peer_switch_id, b.vpc_peer_switch_id) == (b.id, a.id)
    assert a.vpc_domain_id == 1 and a.vpc_peer_link == "port-channel45" and a.vpc_error is None
    assert report["pairs"] == [(a.id, b.id)]
    assert switches["CL-1"].vpc_peer_switch_id is None and "no Rackflow switch" in switches["CL-1"].vpc_error
    assert switches["plain"].vpc_domain_id is None and switches["plain"].vpc_detected_at is not None
    assert "timeout" in switches["down"].vpc_error and switches["down"].vpc_detected_at is None
    summary = vpc_summary(db_session, a)
    assert summary["peer_switch"] == "25G-2" and summary["domain_id"] == 1
