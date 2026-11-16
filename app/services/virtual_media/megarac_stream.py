"""AMI MegaRAC virtual CD streamed by Rackflow (ASRock Rack, Gigabyte, ASUS and MSI).

Instead of asking the BMC to mount an NFS/CIFS share, Rackflow acts as the HTML5
viewer's CD drive over ``wss://<bmc>/cd-server`` and answers the host's reads from
an ISO it holds (``app.services.ipmi_kvm.cd_stream``). It works on BMCs whose stored
remote-media configuration refuses shares (for example X570D4U-2L2T on 3.09).

``insert`` starts the stream, ``eject`` stops it. The one-time boot override is
Redfish ``BootNext`` on the "AMI Virtual CDROM" entry where the BMC offers Redfish
with basic auth (Gigabyte); otherwise the IPMI ``cdrom`` override is used (ASRock Rack).
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

from app.models.server import Server
from app.services.ipmi_kvm.cd_stream import (
    BOOT_SCHEME,
    CdStreamError,
    CdTarget,
    get_manager,
)
from app.services.virtual_media.base import VirtualMediaStatus, VirtualMediaUnavailable
from app.services.virtual_media.gigabyte import GigabyteVirtualMediaProfile

logger = logging.getLogger(__name__)

RESUME_WINDOW_SECONDS = 45.0
RESUME_RETRY_SECONDS = 5.0


def image_filename(image_url: str) -> str:
    parsed = urlparse(image_url or "")
    name = Path(unquote(parsed.path)).name if parsed.path.strip("/") else unquote(parsed.netloc)
    if not name:
        raise VirtualMediaUnavailable("Virtual CD image has no file name")
    return name


class MegaracStreamVirtualMediaProfile(GigabyteVirtualMediaProfile):
    id = "megarac_stream"
    display_name = "AMI MegaRAC virtual CD streamed by Rackflow (ASRock Rack / Gigabyte / ASUS / MSI)"
    mounts_cifs = False
    mounts_nfs = False
    streams_iso = True

    def target(self, server: Server) -> CdTarget:
        username, password = self.credentials(server)
        origin, host = self.origin_and_host(server)
        return CdTarget(origin=origin, host=host, username=username, password=password)

    async def status(self, server: Server) -> VirtualMediaStatus:
        info = get_manager().info(server.id)
        if info is None or not info.active:
            return VirtualMediaStatus(inserted=False, device="CD")
        return VirtualMediaStatus(inserted=True, device="CD", image_name=info.filename)

    async def insert(self, server: Server, image_url: str) -> VirtualMediaStatus:
        filename = image_filename(image_url)
        target = self.target(server)
        try:
            info = await get_manager().start(server.id, target, image_url, filename)
        except CdStreamError as exc:
            raise VirtualMediaUnavailable(exc.detail) from exc
        return VirtualMediaStatus(inserted=info.active, device="CD", image_name=info.filename)

    async def eject(self, server: Server) -> VirtualMediaStatus:
        await get_manager().stop(server.id)
        return VirtualMediaStatus(inserted=False, device="CD")

    async def set_boot_once(self, server: Server, uefi: bool) -> Optional[bool]:
        """Gigabyte boots through Redfish ``BootNext``; ASRock Rack returns None so the
        launcher uses the IPMI ``cdrom`` override."""
        if (getattr(server, "ipmi_kvm_profile", None) or "").strip() == "gigabyte":
            return await super().set_boot_once(server, uefi)
        return None


async def _resume_one(server_id: int, record: dict) -> bool:
    """Restart the stream recorded for ``server_id``. False means try again later."""
    from app.core.database import SessionLocal
    from app.dao.server_dao import ServerDAO

    manager = get_manager()

    def load() -> Optional[CdTarget]:
        db = SessionLocal()
        try:
            server = ServerDAO.get_by_id(db, server_id)
            if server is None or getattr(server, "virtual_media_profile", None) != MegaracStreamVirtualMediaProfile.id:
                return None
            return MegaracStreamVirtualMediaProfile().target(server)
        finally:
            db.close()

    try:
        target = await asyncio.to_thread(load)
    except VirtualMediaUnavailable:
        target = None
    if target is None:
        manager.forget(server_id)
        return True
    if not manager.claim(server_id):
        return False
    try:
        await manager.start(server_id, target, str(record.get("url") or ""), str(record.get("filename") or ""))
    except CdStreamError as exc:
        manager.release_claim(server_id)
        if record.get("url", "").startswith(f"{BOOT_SCHEME}://") and "pending boot" in exc.detail:
            manager.forget(server_id)
            return True
        logger.warning("Could not resume the virtual CD stream for server %s: %s", server_id, exc.detail)
        return True
    logger.info("Resumed the virtual CD stream for server %s (%s)", server_id, record.get("filename"))
    return True


async def resume_streams() -> None:
    """After a restart, reconnect every recorded stream whose previous owner is gone.

    A record whose owner is still alive (another process) cannot be claimed and is
    left alone; it is retried for a short window in case that owner is just exiting.
    """
    manager = get_manager()
    try:
        pending = dict(await asyncio.to_thread(manager.records))
    except Exception:  # noqa: BLE001
        logger.warning("Could not read recorded virtual CD streams", exc_info=True)
        return
    loop = asyncio.get_running_loop()
    deadline = loop.time() + RESUME_WINDOW_SECONDS
    while pending:
        for server_id, record in list(pending.items()):
            try:
                if await _resume_one(server_id, record):
                    pending.pop(server_id, None)
            except Exception:  # noqa: BLE001
                logger.warning("Resuming the virtual CD stream for server %s failed", server_id, exc_info=True)
                pending.pop(server_id, None)
        if not pending or loop.time() >= deadline:
            return
        await asyncio.sleep(RESUME_RETRY_SECONDS)


async def stop_all_streams() -> None:
    """Disconnect on process exit; the records stay so the next process resumes the streams."""
    await get_manager().close_all()
