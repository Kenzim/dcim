"""Start a pending boot task: virtual-media ISO boot when possible, PXE otherwise.

Virtual-media flow (no persistent boot-order changes):
  stamp per-boot iPXE ISO -> stage on the location media runner (HTTP, or SMB for
  Supermicro X9) or serve from the app -> BMC mounts it -> one-time boot to CD ->
  power cycle. iPXE then gets its IP from DHCP and fetches /pxe with the task nonce
  from the same per-subnet origin PXE uses (or VMEDIA_BOOT_BASE_URL). The ISO is
  ejected and deleted once /pxe has served the script.

PXE flow: nothing to do here; dhcpd advertises the boot path for pending PXE tasks
(see app/services/dhcp_sync.py) and the operator/customer reboots the server.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.dao import BootTaskDAO, ServerDAO
from app.models.boot_task import BootTask, BootTaskStatus
from app.models.server import Server
from app.models.server_activity import ServerActivityEventType
from app.services.server_activity_logger import (
    log_server_activity_failure,
    log_server_activity_success,
)
from app.services.virtual_media.base import VirtualMediaUnavailable
from app.services.virtual_media.boot_iso import boot_iso_filename, is_boot_iso_name, vmedia_boot_available
from app.services.virtual_media.image_token import (
    image_url_for_token,
    mint_image_token,
    mounted_filename,
    revoke_server_tokens,
)
from app.services.virtual_media.registry import profile_for_server, virtual_media_ready

logger = logging.getLogger(__name__)

RUNNER_STAGE_TIMEOUT_SECONDS = 90.0
_POLL_INTERVAL_SECONDS = 1.0


class BootLaunchError(Exception):
    """Launching the boot failed; ``detail`` is safe to show to admins."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def vmedia_capable(server: Optional[Server]) -> bool:
    return virtual_media_ready(server) and vmedia_boot_available()


def _media_runner(db: Session, server: Server):
    from app.dao.runner_dao import RunnerDAO
    from app.services.runners.hub import get_hub

    location_id = getattr(server, "location_id", None)
    if not location_id:
        return None
    try:
        runner = RunnerDAO.get_by_location_and_capability(db, int(location_id), "media")
    except Exception:  # noqa: BLE001
        return None
    if runner is None or not get_hub().is_connected(runner.id):
        return None
    return runner


async def _wait_for_runner_job(runner_id: int, job_id: str) -> None:
    from app.services.runners.cache import cached_state

    loop = asyncio.get_running_loop()
    deadline = loop.time() + RUNNER_STAGE_TIMEOUT_SECONDS
    while loop.time() < deadline:
        jobs = (cached_state(runner_id) or {}).get("jobs") or []
        job = next((j for j in jobs if isinstance(j, dict) and j.get("id") == job_id), None)
        if job and job.get("status") == "done":
            return
        if job and job.get("status") == "error":
            raise BootLaunchError(f"Media runner could not fetch the boot ISO: {job.get('error') or 'unknown error'}")
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
    raise BootLaunchError("Timed out waiting for the media runner to fetch the boot ISO")


async def _stage_boot_iso(db: Session, server: Server, profile, filename: str) -> str:
    """Return the URL the BMC should mount for this server's boot ISO."""
    from app.services.runners.cache import cached_state
    from app.services.runners.hub import get_hub
    from app.services.runners.media_urls import (
        build_cifs_url,
        media_http_iso_url,
        public_http_base_from_state,
    )

    app_url = image_url_for_token(mint_image_token(server.id, filename), filename)
    runner = _media_runner(db, server)
    if runner is None:
        return app_url

    hub = get_hub()
    result = await hub.rpc(runner.id, "media.download", {"url": app_url, "filename": filename}, timeout=15.0)
    job_id = (result or {}).get("job_id") if isinstance(result, dict) else None
    if not job_id:
        raise BootLaunchError("Media runner did not accept the boot ISO download")
    await _wait_for_runner_job(runner.id, job_id)

    if getattr(profile, "mounts_cifs", False):
        creds = await hub.rpc(
            runner.id,
            "media.mint_smb_credentials",
            {"filename": filename, "ttl": int(settings.virtual_media_token_ttl_seconds or 14400)},
            timeout=15.0,
        )
        if not isinstance(creds, dict):
            raise BootLaunchError("Media runner returned invalid SMB credentials")
        return build_cifs_url(
            str(creds.get("host") or ""),
            str(creds.get("share") or "isos"),
            filename,
            str(creds.get("user") or ""),
            str(creds.get("password") or ""),
        )
    base = public_http_base_from_state(cached_state(runner.id) or runner.state)
    if not base:
        raise BootLaunchError("Media runner is online but has no public HTTP base for virtual media")
    return media_http_iso_url(base, filename)


async def release_boot_media(db: Session, server: Server) -> None:
    """Eject and delete this server's per-boot ISO, if one is mounted. Never raises."""
    filename = mounted_filename(server.id)
    if not is_boot_iso_name(filename):
        return
    try:
        await profile_for_server(server).eject(server)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ejecting boot ISO for server %s failed: %s", server.id, exc)
    runner = _media_runner(db, server)
    if runner is not None:
        from app.services.runners.hub import get_hub

        try:
            await get_hub().rpc(runner.id, "media.delete", {"filename": filename}, timeout=15.0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Deleting boot ISO %s on runner %s failed: %s", filename, runner.id, exc)
    revoke_server_tokens(server.id)


async def _power_cycle(plugin) -> str:
    from app.plugins.base import PowerState

    try:
        state = await plugin.get_power_state()
    except Exception:  # noqa: BLE001
        state = PowerState.UNKNOWN
    if state == PowerState.OFF:
        ok = await plugin.power_on()
        action = "power_on"
    else:
        ok = await plugin.power_reset()
        action = "power_reset"
    if not ok:
        raise BootLaunchError(f"BMC refused {action}")
    return action


async def _launch_vmedia(db: Session, server: Server, boot_task: BootTask) -> dict:
    from app.models.server import BootMode
    from app.plugins.registry import get_registry

    profile = profile_for_server(server)
    filename = boot_iso_filename()
    url = await _stage_boot_iso(db, server, profile, filename)
    await profile.insert(server, url)

    plugin = get_registry().get_plugin(server.plugin_name, server.plugin_config)
    uefi = getattr(server, "pxe_boot_mode", None) == BootMode.UEFI
    if not await plugin.set_next_boot_device("cdrom", persistent=False, uefi=uefi):
        raise BootLaunchError("BMC refused the one-time boot-to-CD override")
    action = await _power_cycle(plugin)
    return {"filename": filename, "uefi": uefi, "power_action": action}


async def launch_boot_task(
    db: Session,
    server: Server,
    boot_task: BootTask,
    *,
    source: str,
    service_id: Optional[int] = None,
) -> str:
    """Start ``boot_task``. Returns the boot method used ("vmedia" or "pxe").

    If the vmedia launch fails, the boot media is released and the task stays
    pending as a PXE task (dhcpd advertises it again), so the install still runs
    on the server's next network boot.
    """
    if boot_task.status != BootTaskStatus.PENDING or not vmedia_capable(server):
        return "pxe"

    boot_task.boot_method = "vmedia"
    db.commit()
    BootTaskDAO._schedule_dhcp_sync(server.id)  # drop PXE options for this host
    activity_server_id = server.id if service_id is None else None
    try:
        details = await _launch_vmedia(db, server, boot_task)
    except Exception as exc:  # noqa: BLE001
        detail = exc.detail if isinstance(exc, (BootLaunchError, VirtualMediaUnavailable)) else str(exc)
        logger.warning(
            "Virtual-media boot for task %s on server %s failed, falling back to PXE: %s",
            boot_task.id, server.id, detail,
        )
        await release_boot_media(db, server)
        boot_task.boot_method = "pxe"
        db.commit()
        BootTaskDAO._schedule_dhcp_sync(server.id)  # re-advertise PXE options for this host
        log_server_activity_failure(
            db,
            server_id=activity_server_id,
            service_id=service_id,
            event_type=ServerActivityEventType.INSTALL,
            action="vmedia_boot",
            source=source,
            message="Virtual-media boot failed; falling back to PXE on the next network boot",
            error=exc,
            details={"boot_task_id": boot_task.id, "fallback": "pxe", "detail": detail},
        )
        return "pxe"
    log_server_activity_success(
        db,
        server_id=activity_server_id,
        service_id=service_id,
        event_type=ServerActivityEventType.INSTALL,
        action="vmedia_boot",
        source=source,
        message="Mounted boot ISO, set one-time boot to CD and power-cycled",
        details={"boot_task_id": boot_task.id, **details},
    )
    return "vmedia"


async def _launch_in_own_session(server_id: int, boot_task_id: int, source: str, service_id: Optional[int]) -> None:
    from app.core.database import SessionLocal

    db = SessionLocal()
    try:
        server = ServerDAO.get_by_id(db, server_id)
        task = BootTaskDAO.get_by_id(db, boot_task_id)
        if server is None or task is None:
            return
        await launch_boot_task(db, server, task, source=source, service_id=service_id)
    except BootLaunchError:
        pass  # already logged and recorded on the task
    except Exception as exc:  # noqa: BLE001
        logger.warning("Launching boot task %s failed: %s", boot_task_id, exc)
    finally:
        db.close()


_background_tasks: set = set()


def schedule_launch(server_id: int, boot_task_id: int, *, source: str, service_id: Optional[int] = None) -> None:
    """Fire-and-forget ``launch_boot_task`` for sync callers (own DB session)."""
    coro = _launch_in_own_session(server_id, boot_task_id, source, service_id)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        task = loop.create_task(coro)
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        return
    threading.Thread(target=asyncio.run, args=(coro,), daemon=True, name=f"boot-launch-{boot_task_id}").start()


async def _release_in_own_session(server_id: int) -> None:
    from app.core.database import SessionLocal

    db = SessionLocal()
    try:
        server = ServerDAO.get_by_id(db, server_id)
        if server is not None:
            await release_boot_media(db, server)
    finally:
        db.close()


def schedule_release(server_id: int) -> None:
    """Fire-and-forget ``release_boot_media`` (after /pxe serves, on cancel/complete)."""
    coro = _release_in_own_session(server_id)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        task = loop.create_task(coro)
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        return
    threading.Thread(target=asyncio.run, args=(coro,), daemon=True, name=f"boot-release-{server_id}").start()
