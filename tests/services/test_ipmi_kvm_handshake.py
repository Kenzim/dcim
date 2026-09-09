"""ASRockRack KVM handshake against a fake BMC WebSocket."""
import pytest

from app.services.ipmi_kvm.asrockrack import (
    AsrockRackKvmProfile,
    _IVTP_ALLOWED,
    _IVTP_CONN_COMPLETE,
    _IVTP_VALIDATED,
    ivtp,
)
from app.services.ipmi_kvm.base import BmcKvmAuth, IpmiKvmUnavailable


class _FakeUpstream:
    def __init__(self, packets):
        self._packets = list(packets)
        self.sent = []

    async def recv(self):
        if not self._packets:
            raise TimeoutError("no more BMC packets")
        return self._packets.pop(0)

    async def send(self, data):
        self.sent.append(data)


def _auth():
    return BmcKvmAuth(
        https_base="https://bmc.example",
        origin="https://bmc.example",
        hostname="bmc.example",
        cookie="qsess",
        csrf="csrf",
        kvm_token="tok",
        client_ip="10.1.2.3",
        username="admin",
    )


@pytest.mark.asyncio
async def test_handshake_sends_empty_3a_validate_resume_and_returns_leftover():
    leftover_video = ivtp(0x19, 0, b"\x00\x00")
    upstream = _FakeUpstream(
        [
            ivtp(_IVTP_ALLOWED, 0),
            ivtp(_IVTP_VALIDATED, 0, bytes([1])) + leftover_video,
        ]
    )
    profile = AsrockRackKvmProfile()
    leftover = await profile.handshake(upstream, _auth())
    assert leftover[:2] == ivtp(_IVTP_VALIDATED, 0, bytes([1]))[:2]
    assert leftover[8] == 1
    assert leftover.endswith(leftover_video)
    assert len(upstream.sent) == 1
    sent = upstream.sent[0]
    assert sent[0:2] == ivtp(_IVTP_CONN_COMPLETE, 1)[0:2]


@pytest.mark.asyncio
async def test_handshake_rejects_invalid_session():
    upstream = _FakeUpstream(
        [
            ivtp(_IVTP_ALLOWED, 0),
            ivtp(_IVTP_VALIDATED, 0, bytes([3])),
        ]
    )
    profile = AsrockRackKvmProfile()
    with pytest.raises(IpmiKvmUnavailable):
        await profile.handshake(upstream, _auth())
