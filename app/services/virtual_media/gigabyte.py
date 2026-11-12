"""Gigabyte AMI MegaRAC virtual CD over Redfish (BMC pulls an HTTP ISO URL).

Shares the MegaRAC web-session login with ASRockRack but keeps the Redfish
``VirtualMedia.InsertMedia`` path, which ASRockRack firmware does not offer.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx

from app.models.server import Server
from app.services.virtual_media.asrockrack import AsrockRackVirtualMediaProfile, bmc_httpx_verify
from app.services.virtual_media.base import VirtualMediaStatus
from app.services.virtual_media.redfish import eject_image, insert_image, read_status, require_inserted


class GigabyteVirtualMediaProfile(AsrockRackVirtualMediaProfile):
    id = "gigabyte"
    display_name = "Gigabyte (AMI MegaRAC virtual media)"
    mounts_cifs = False
    # Redfish InsertMedia refuses HTTP images (task error 403) but mounts nfs:// URLs.
    mounts_nfs = True

    @asynccontextmanager
    async def _session(self, server: Server) -> AsyncIterator[tuple[httpx.AsyncClient, str]]:
        """Redfish rejects the web-session headers (401) but accepts basic auth."""
        username, password = self.credentials(server)
        origin, _hostname = self.origin_and_host(server)
        async with httpx.AsyncClient(
            verify=bmc_httpx_verify(megarac=True),
            timeout=30.0,
            auth=(username, password),
        ) as client:
            yield client, origin

    async def status(self, server: Server) -> VirtualMediaStatus:
        async with self._session(server) as (client, origin):
            return await read_status(client, origin)

    async def insert(self, server: Server, image_url: str) -> VirtualMediaStatus:
        async with self._session(server) as (client, origin):
            return require_inserted(await insert_image(client, origin, image_url))

    async def eject(self, server: Server) -> VirtualMediaStatus:
        async with self._session(server) as (client, origin):
            return await eject_image(client, origin)
