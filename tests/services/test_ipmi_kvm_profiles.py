"""MegaRAC HTML5 KVM profile packets and registry."""
from __future__ import annotations

import struct

import pytest

from app.services.ipmi_kvm.asrockrack import (
    AsrockRackKvmProfile,
    initial_client_frame,
    initial_client_frame_with_server_ip,
    validate_payload,
    validate_payload_with_server_ip,
)
from app.services.ipmi_kvm.bmc_tls import bmc_ssl_context
from app.services.ipmi_kvm.base import BmcKvmAuth, IpmiKvmUnavailable
from app.services.ipmi_kvm.gigabyte import GigabyteKvmProfile
from app.services.ipmi_kvm.registry import get_profile, list_profiles, normalize_profile_id


def _auth(**kwargs) -> BmcKvmAuth:
    defaults = dict(
        https_base="https://10.16.251.155",
        origin="https://10.16.251.155",
        hostname="10.16.251.155",
        cookie="sess",
        csrf="csrf",
        kvm_token="yxnFUPtd5KnUzqLQ",
        client_ip="192.168.9.119",
        username="admin",
        server_ip="10.16.251.155",
    )
    defaults.update(kwargs)
    return BmcKvmAuth(**defaults)


def test_asrockrack_validate_is_373_bytes_without_server_ip():
    body = validate_payload("tok", "1.2.3.4", "admin")
    assert len(body) == 373
    frame = initial_client_frame("tok", "1.2.3.4", "admin")
    assert len(frame) == 8 + 8 + 373 + 8
    typ, size, status = struct.unpack_from("<HIH", frame, 8)
    assert typ == 0x12
    assert size == 373
    assert status == 1


def test_gigabyte_validate_is_438_bytes_with_server_ip():
    body = validate_payload_with_server_ip("tok", "1.2.3.4", "admin", "10.16.251.155")
    assert len(body) == 438
    assert body[:373] == validate_payload("tok", "1.2.3.4", "admin")
    frame = initial_client_frame_with_server_ip("tok", "1.2.3.4", "admin", "10.16.251.155")
    assert len(frame) == 8 + 8 + 438 + 8
    typ, size, status = struct.unpack_from("<HIH", frame, 8)
    assert typ == 0x12
    assert size == 438
    assert status == 1


def test_hello_frame_differs_by_profile():
    auth = _auth()
    asrock = AsrockRackKvmProfile().hello_frame(auth)
    giga = GigabyteKvmProfile().hello_frame(auth)
    assert len(asrock) == 397
    assert len(giga) == 462
    assert asrock != giga


def test_hello_frame_attempts_order():
    auth = _auth()
    asrock = AsrockRackKvmProfile().hello_frame_attempts(auth)
    giga = GigabyteKvmProfile().hello_frame_attempts(auth)
    assert [len(f) for f in asrock] == [397, 462]
    assert [len(f) for f in giga] == [462, 397]
    assert asrock[0] == AsrockRackKvmProfile().hello_frame(auth)
    assert giga[0] == GigabyteKvmProfile().hello_frame(auth)


def test_registry_lists_gigabyte():
    ids = {p["id"] for p in list_profiles()}
    assert ids == {"asrockrack", "gigabyte", "supermicro"}
    assert isinstance(get_profile("gigabyte"), GigabyteKvmProfile)
    assert normalize_profile_id("gigabyte") == "gigabyte"
    assert normalize_profile_id("") is None
    with pytest.raises(IpmiKvmUnavailable):
        normalize_profile_id("idrac")


def test_ssl_ctx_includes_rsa_aes_gcm():
    ctx = bmc_ssl_context(megarac=True)
    names = {cipher["name"] for cipher in ctx.get_ciphers()}
    assert "AES256-GCM-SHA384" in names
    client = __import__("httpx").AsyncClient(verify=ctx, timeout=1.0)
    assert client is not None
