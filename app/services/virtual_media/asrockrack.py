"""ASRockRack AMI MegaRAC Redfish virtual CD."""
from __future__ import annotations

import logging

import httpx

from app.models.server import Server
from app.services.ipmi_kvm.asrockrack import megarac_web_session
from app.services.ipmi_kvm.base import IpmiKvmUnavailable
from app.services.ipmi_kvm.bmc_tls import bmc_httpx_verify
from app.services.virtual_media.base import VirtualMediaProfile, VirtualMediaStatus, VirtualMediaUnavailable
from app.services.virtual_media.redfish import eject_image, insert_image, read_status

logger = logging.getLogger(__name__)


class AsrockRackVirtualMediaProfile(VirtualMediaProfile):
    id = "asrockrack"
    display_name = "ASRockRack (AMI MegaRAC virtual media)"

    def _headers(self, origin: str, cookie: str, csrf: str) -> dict[str, str]:
        return {
            "Origin": origin,
            "X-CSRFTOKEN": csrf,
            "X-Auth-Token": csrf,
            "Cookie": f"QSESSIONID={cookie}",
        }

    async def _client(self, server: Server) -> tuple[httpx.AsyncClient, str]:
        username, password = self.credentials(server)
        origin, _hostname = self.origin_and_host(server)
        try:
            cookie, csrf, _body = await megarac_web_session(origin, username, password)
        except IpmiKvmUnavailable as exc:
            raise VirtualMediaUnavailable(exc.detail) from exc
        client = httpx.AsyncClient(
            verify=bmc_httpx_verify(megarac=True),
            timeout=30.0,
            headers=self._headers(origin, cookie, csrf),
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
