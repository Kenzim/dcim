"""CISCO-VPC-MIB parsing, with rows shaped like a production Nexus 93180YC-EX walk."""
from pysnmp.proto.rfc1902 import Integer, OctetString

from app.plugins.snmpv3 import parse_vpc_tables

IF_NAMES = {369098752: "Ethernet1/24", 369098770: "Ethernet1/19", 409600044: "port-channel45", 409600119: "port-channel120"}


def _rows(domain=1, src=bytes([192, 168, 1, 1]), dst=bytes([192, 168, 1, 2])):
    return [
        ((1, 1, 1, 3, domain), OctetString(dst)),
        ((1, 1, 1, 5, domain), OctetString(src)),
        ((1, 1, 1, 13, domain), OctetString(b"default")),
        ((1, 2, 1, 2, domain), Integer(2)),
        ((2, 1, 1, 2, domain), Integer(2)),
        ((2, 1, 1, 5, domain), OctetString(bytes.fromhex("002304eebe01"))),
        ((4, 1, 1, 2, domain), Integer(409600044)),
        ((4, 2, 1, 3, domain, 120), Integer(409600119)),
        ((4, 2, 1, 4, domain, 120), Integer(3)),
        ((4, 2, 1, 5, domain, 120), Integer(1)),
        ((4, 2, 1, 6, domain, 120), OctetString(b"SUCCESS")),
    ]


def test_parse_vpc_tables_reads_domain_keepalive_peer_link_and_vpcs():
    info = parse_vpc_tables(_rows(), IF_NAMES)
    assert info == {
        "domain_id": 1,
        "role": "primary",
        "keepalive_status": "alive",
        "keepalive_source": "192.168.1.1",
        "keepalive_destination": "192.168.1.2",
        "keepalive_vrf": "default",
        "system_mac": "00:23:04:ee:be:01",
        "peer_link": "port-channel45",
        "vpcs": [
            {
                "vpc_id": 120,
                "interface": "port-channel120",
                "status": "up",
                "consistency": "success",
                "consistency_detail": "SUCCESS",
            }
        ],
    }


def test_parse_vpc_tables_without_a_domain_is_none():
    assert parse_vpc_tables([], IF_NAMES) is None
    assert parse_vpc_tables([((9, 9, 1, 1, 1), Integer(1))], IF_NAMES) is None
