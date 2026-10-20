"""Regenerate and push dhcpd.conf when servers or their boot tasks change.

PXE options (next-server/filename and the nonce-bearing iPXE URL) are only
emitted for hosts with a pending boot task, so the config must be refreshed
whenever a task is created, cancelled, or its PXE nonce is consumed.
"""
import asyncio
import logging
import threading

from sqlalchemy.orm import Session

from app.dao import ServerDAO, ServiceInstanceDAO

logger = logging.getLogger(__name__)


async def regenerate_location_dhcp(db: Session, location_id: int) -> bool:
    """Regenerate and push DHCP config for this location if it has a DHCP service instance.

    Returns True when the location has a DHCP instance (whether or not the push succeeded).
    """
    instance = ServiceInstanceDAO.get_by_location_and_type(db, location_id, "dhcp")
    if not instance:
        return False
    try:
        from app.services.dhcp_config_generator import generate_dhcpd_conf
        from app.services.dhcp_config_service import get_dhcp_config_service
        from app.services.runner_client import call_dhcp_runner

        config_svc = get_dhcp_config_service()
        config = config_svc.get_config_by_service_instance(db, instance.id)
        if not config:
            config = config_svc.get_or_create_config_for_service_instance(
                db, instance.id, "/shared/dhcp/dhcpd.conf", "/shared/dhcp/dhcpd.leases"
            )
        result = generate_dhcpd_conf(config, db, location_id=location_id, return_content=True)
        if not result:
            return True
        content, interface_names = result
        headers = {"X-Runner-Interfaces": ",".join(interface_names)} if interface_names else None
        code, _ = await call_dhcp_runner(instance, db, "PUT", "/config", raw_body=content.encode(), extra_headers=headers)
        if code in (200, 201):
            logger.info(f"Regenerated DHCP config for location {location_id}")
        else:
            logger.warning(f"DHCP runner returned {code} pushing config for location {location_id}")
    except Exception as e:
        logger.warning(f"Failed to regenerate DHCP for location {location_id}: {e}")
    return True


async def _regenerate_global_dhcp(db: Session) -> None:
    """Legacy single-site DHCP config (no per-location service instance)."""
    try:
        from app.services.dhcp_config_generator import generate_dhcpd_conf
        from app.services.dhcp_config_service import get_dhcp_config_service
        from app.services.dhcp_service import get_dhcp_service

        config = get_dhcp_config_service().get_config(db)
        if not config:
            return
        generate_dhcpd_conf(config, db)
        dhcp_service = get_dhcp_service(db)
        status_info = await dhcp_service.get_status()
        if status_info.get("running"):
            await dhcp_service.reload()
    except Exception as e:
        logger.warning(f"Failed to regenerate global DHCP config: {e}")


async def sync_dhcp_for_server(db: Session, server_id: int) -> None:
    """Refresh the DHCP config that serves this server. Never raises."""
    try:
        server = ServerDAO.get_by_id(db, server_id)
        location_id = getattr(server, "location_id", None) if server else None
        if location_id and await regenerate_location_dhcp(db, location_id):
            return
        await _regenerate_global_dhcp(db)
    except Exception as e:
        logger.warning(f"DHCP sync for server {server_id} failed: {e}")


async def _sync_in_own_session(server_id: int) -> None:
    from app.core.database import SessionLocal

    db = SessionLocal()
    try:
        await sync_dhcp_for_server(db, server_id)
    finally:
        db.close()


_background_tasks: set = set()


def schedule_dhcp_sync(server_id: int) -> None:
    """Fire-and-forget DHCP refresh for a server, callable from sync or async code.

    Uses its own DB session so the caller's session/transaction is untouched.
    Boot tasks are created well before the server is power-cycled into PXE, so
    the refresh does not need to block the caller.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        task = loop.create_task(_sync_in_own_session(server_id))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        return
    # Sync endpoint (threadpool) or worker without a loop: run in a short-lived thread.
    threading.Thread(
        target=asyncio.run, args=(_sync_in_own_session(server_id),), daemon=True, name=f"dhcp-sync-{server_id}"
    ).start()
