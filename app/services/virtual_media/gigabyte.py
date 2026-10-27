"""Gigabyte AMI MegaRAC virtual CD over Redfish (BMC pulls an HTTP ISO URL).

Shares the MegaRAC web-session login with ASRockRack but keeps the Redfish
``VirtualMedia.InsertMedia`` path, which ASRockRack firmware does not offer.
"""
from __future__ import annotations

from app.models.server import Server
from app.services.virtual_media.asrockrack import AsrockRackVirtualMediaProfile
from app.services.virtual_media.base import VirtualMediaStatus
from app.services.virtual_media.redfish import eject_image, insert_image, read_status, require_inserted


class GigabyteVirtualMediaProfile(AsrockRackVirtualMediaProfile):
    id = "gigabyte"
    display_name = "Gigabyte (AMI MegaRAC virtual media)"
    mounts_cifs = False

    async def status(self, server: Server) -> VirtualMediaStatus:
        async with self._session(server) as (client, origin):
            return await read_status(client, origin)

    async def insert(self, server: Server, image_url: str) -> VirtualMediaStatus:
        async with self._session(server) as (client, origin):
            return require_inserted(await insert_image(client, origin, image_url))

    async def eject(self, server: Server) -> VirtualMediaStatus:
        async with self._session(server) as (client, origin):
            return await eject_image(client, origin)
