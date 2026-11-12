"""Gigabyte AMI MegaRAC virtual CD over Redfish (BMC pulls an HTTP ISO URL).

Shares the MegaRAC web-session login with ASRockRack but keeps the Redfish
``VirtualMedia.InsertMedia`` path, which ASRockRack firmware does not offer.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

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

    async def set_boot_once(self, server: Server, uefi: bool) -> Optional[bool]:
        """The BIOS consumes the IPMI ``cdrom`` override but still follows its boot
        order (PXE, then disk). Redfish ``BootNext`` on the "AMI Virtual CDROM" UEFI
        entry boots the ISO; ``Cd`` is the fallback when that entry is not listed yet."""
        async with self._session(server) as (client, origin):
            try:
                systems = await client.get(f"{origin}/redfish/v1/Systems")
                members = (systems.json() or {}).get("Members") or [] if systems.status_code < 400 else []
                path = (members[0] or {}).get("@odata.id") if members else "/redfish/v1/Systems/Self"
                system = await client.get(f"{origin}{path}")
                if system.status_code >= 400:
                    return None
                etag = system.headers.get("etag")
                boot = (system.json() or {}).get("Boot") or {}
                virtual_cd = await self._virtual_cd_boot_option(client, origin, boot) if uefi else None
            except (httpx.RequestError, ValueError):
                return None
            targets = boot.get("BootSourceOverrideTarget@Redfish.AllowableValues") or []
            override: dict = {"BootSourceOverrideEnabled": "Once", "BootSourceOverrideMode": "UEFI" if uefi else "Legacy"}
            if virtual_cd and "UefiBootNext" in targets:
                override.update(BootSourceOverrideTarget="UefiBootNext", BootNext=virtual_cd)
            elif "Cd" in targets:
                override["BootSourceOverrideTarget"] = "Cd"
            else:
                return None
            try:
                resp = await client.patch(
                    f"{origin}{path}",
                    json={"Boot": override},
                    headers={"If-Match": etag} if etag else None,
                )
            except httpx.RequestError:
                return False
            return resp.status_code < 400

    @staticmethod
    async def _virtual_cd_boot_option(client: httpx.AsyncClient, origin: str, boot: dict) -> Optional[str]:
        collection = (boot.get("BootOptions") or {}).get("@odata.id")
        if not collection:
            return None
        resp = await client.get(f"{origin}{collection}")
        if resp.status_code >= 400:
            return None
        for member in (resp.json() or {}).get("Members") or []:
            option = await client.get(f"{origin}{member.get('@odata.id')}")
            if option.status_code >= 400:
                continue
            data = option.json() or {}
            if "virtual cdrom" in (data.get("DisplayName") or "").lower():
                return data.get("BootOptionReference") or None
        return None
