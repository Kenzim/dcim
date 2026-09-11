"""IPMI HTML5 KVM packet builders and profile registry."""
import struct

import pytest

from app.services.ipmi_kvm.asrockrack import (
    _IVTP_CONN_COMPLETE,
    _IVTP_RESUME,
    _IVTP_VALIDATE,
    initial_client_frame,
    ivtp,
    validate_payload,
    validate_payload_with_server_ip,
)
from app.services.ipmi_kvm.base import IpmiKvmUnavailable
from app.services.ipmi_kvm.registry import kvm_ready, list_profiles, normalize_profile_id


def _packets(buf: bytes):
    out = []
    offset = 0
    while offset + 8 <= len(buf):
        typ, size, status = struct.unpack_from("<HIH", buf, offset)
        payload = buf[offset + 8 : offset + 8 + size]
        out.append((typ, status, payload))
        offset += 8 + size
    return out


def test_validate_payload_is_373_bytes_without_server_ip():
    body = validate_payload("tok", "10.1.2.3", "admin")
    assert len(body) == 373
    assert body[0] == 0
    # token C-string starts immediately after the flag byte
    assert body[1:5] == b"tok\x00"


def test_initial_client_frame_is_empty_3a_then_validate_then_resume():
    frame = initial_client_frame("tok", "10.1.2.3", "admin")
    packets = _packets(frame)
    assert [p[0] for p in packets] == [_IVTP_CONN_COMPLETE, _IVTP_VALIDATE, _IVTP_RESUME]
    assert packets[0][1] == 1
    assert packets[0][2] == b""
    assert packets[1][1] == 1
    assert len(packets[1][2]) == 373
    assert packets[2][1] == 0
    assert packets[2][2] == b""


def test_validate_payload_with_server_ip_is_438_bytes():
    body = validate_payload_with_server_ip("tok", "10.1.2.3", "admin", "192.168.1.1")
    assert len(body) == 438
    assert body[:373] == validate_payload("tok", "10.1.2.3", "admin")


def test_ivtp_header_little_endian():
    pkt = ivtp(0x12, 1, b"ab")
    typ, size, status = struct.unpack_from("<HIH", pkt)
    assert (typ, size, status) == (0x12, 2, 1)
    assert pkt[8:] == b"ab"


def test_normalize_profile_id_empty_and_aliases_are_off():
    assert normalize_profile_id(None) is None
    assert normalize_profile_id("") is None
    assert normalize_profile_id("none") is None
    assert normalize_profile_id("OFF") is None
    assert normalize_profile_id("disabled") is None


def test_normalize_profile_id_asrockrack():
    assert normalize_profile_id("asrockrack") == "asrockrack"


def test_normalize_profile_id_unknown_raises():
    with pytest.raises(IpmiKvmUnavailable):
        normalize_profile_id("idrac")


def test_list_profiles_includes_asrockrack():
    ids = {p["id"] for p in list_profiles()}
    assert "asrockrack" in ids
    assert "gigabyte" in ids
    assert "supermicro" in ids


def test_kvm_ready_requires_known_profile():
    class _Srv:
        ipmi_kvm_profile = "asrockrack"

    class _Off:
        ipmi_kvm_profile = None

    assert kvm_ready(_Srv()) is True
    assert kvm_ready(_Off()) is False
    assert kvm_ready(None) is False
