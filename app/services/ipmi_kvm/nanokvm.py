"""Sipeed NanoKVM HTML5 console profile.

Video is an MJPEG stream and keyboard/mouse go to ``/api/ws``. That does not
fit the AMI/ATEN hub, so the profile only identifies the server. The bridge
in ``nanokvm_bridge`` opens the console.
"""
from __future__ import annotations

from app.models.server import Server
from app.services.ipmi_kvm.base import BmcKvmAuth, IpmiKvmProfile, IpmiKvmUnavailable


_BRIDGE_ONLY = "NanoKVM console is served by the MJPEG bridge"


class NanoKvmProfile(IpmiKvmProfile):
    id = "nanokvm"
    display_name = "Sipeed NanoKVM"
    packet_mode = "nanokvm"
    decode_worker_path = ""

    async def login(self, server: Server) -> BmcKvmAuth:
        del server
        raise IpmiKvmUnavailable(_BRIDGE_ONLY)

    def open_upstream(self, auth: BmcKvmAuth):
        del auth
        raise IpmiKvmUnavailable(_BRIDGE_ONLY)

    async def handshake(self, upstream, auth: BmcKvmAuth) -> bytes:
        del upstream, auth
        raise IpmiKvmUnavailable(_BRIDGE_ONLY)

    def stop_frame(self) -> bytes:
        return b""
