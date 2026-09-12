"""SuperMicro Redfish virtual CD (ATEN web session or HTTP Basic)."""
from __future__ import annotations

import logging

import httpx

from app.models.server import Server
from app.services.ipmi_kvm.base import IpmiKvmUnavailable
from app.services.ipmi_kvm.bmc_tls import bmc_httpx_verify
from app.services.ipmi_kvm.supermicro import aten_web_session
from app.services.virtual_media.base import VirtualMediaProfile, VirtualMediaStatus, VirtualMediaUnavailable
from app.services.virtual_media.redfish import eject_image, insert_image, read_status

logger = logging.getLogger(__name__)


class SuperMicroVirtualMediaProfile(VirtualMediaProfile):
    id = "supermicro"
    display_name = "SuperMicro (Redfish virtual media)"

    async def _client(self, server: Server) -> tuple[httpx.AsyncClient, str]:
        username, password = self.credentials(server)
        origin, _hostname = self.origin_and_host(server)
        headers = {
            "Origin": origin,
            "Referer": origin + "/",
            "User-Agent": "Mozilla/5.0",
        }
        try:
            sid = await aten_web_session(origin, username, password)
            headers["Cookie"] = f"SID={sid}"
        except IpmiKvmUnavailable:
            logger.debug("SuperMicro virtual media: SID login failed, trying HTTP Basic")
        client = httpx.AsyncClient(
            verify=bmc_httpx_verify(),
            timeout=30.0,
            headers=headers,
            auth=(username, password),
        )
        return client, origin

    async def status(self, server: Server) -> VirtualMediaStatus:
        client, origin = await self._client(server)
        async with client:
            return await read_status(client, origin)

    async def insert(self, server: Server, image_url: str) -> VirtualMediaStatus:
        client, origin = await self._client(server)
        async with client:
            return await insert_image(client, origin, image_url)

    async def eject(self, server: Server) -> VirtualMediaStatus:
        client, origin = await self._client(server)
        async with client:
            return await eject_image(client, origin)
