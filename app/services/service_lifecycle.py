"""Shared service suspend/unsuspend behavior for APIs and background jobs."""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

from sqlalchemy.orm import Session

import inspect

from app.models.service import Service, ServiceStatus, ServiceType
from app.plugins.base import PowerState
from app.plugins.registry import get_registry
from app.services.proxmox_placement import ProxmoxPlacementError, resolve_proxmox_plugin_for_service
from app.services.service_resource import service_linked_server, vm_placement


logger = logging.getLogger(__name__)


class ServiceLifecycleError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 500) -> None:
        self.status_code = status_code
        super().__init__(message)


class ServiceLifecycle:
    """Lifecycle operations without route/auth coupling or implicit commits."""

    def __init__(
        self,
        plugin_resolver: Optional[Callable[[Session, Service], object]] = None,
    ) -> None:
        self.plugin_resolver = plugin_resolver

    async def _plugin(self, db: Session, service: Service):
        if self.plugin_resolver is not None:
            resolved = self.plugin_resolver(db, service)
            if inspect.isawaitable(resolved):
                resolved = await resolved
            return resolved[0] if isinstance(resolved, tuple) else resolved
        if service.service_type == ServiceType.VM:
            try:
                plugin, _cid, _node, _vmid = await resolve_proxmox_plugin_for_service(db, service)
            except ProxmoxPlacementError as exc:
                raise ServiceLifecycleError(str(exc), status_code=exc.status_code) from exc
            return plugin

        server = service_linked_server(db, service)
        if server is None:
            raise ServiceLifecycleError("Service has no linked server")
        return get_registry().get_plugin(server.plugin_name, server.plugin_config)

    async def ensure_powered_off(self, db: Session, service: Service) -> None:
        """Force power off VM/BM; proxies and unplaced pending VMs are no-ops."""
        if service.service_type == ServiceType.HTTP_PROXY:
            return
        if service.service_type == ServiceType.VM:
            cluster_id, _node, vmid = vm_placement(service)
            if cluster_id is None or vmid is None:
                return

        plugin = await self._plugin(db, service)
        try:
            power_state = await plugin.get_power_state()
        except Exception:
            logger.warning(
                "Could not read power state before suspending service %s",
                service.id,
                exc_info=True,
            )
            power_state = PowerState.UNKNOWN
        if power_state == PowerState.OFF:
            return
        try:
            success = await plugin.power_off(force=True)
        except NotImplementedError as exc:
            raise ServiceLifecycleError(
                "Server plugin does not support power control",
                status_code=400,
            ) from exc
        except Exception as exc:
            raise ServiceLifecycleError(
                "Failed to power off before suspend"
            ) from exc
        if not success:
            raise ServiceLifecycleError("Failed to power off before suspend")

    async def suspend(
        self,
        db: Session,
        service: Service,
        *,
        reason: str,
    ) -> bool:
        if service.status in {ServiceStatus.SUSPENDED, ServiceStatus.TERMINATED}:
            return False
        await self.ensure_powered_off(db, service)
        service.status = ServiceStatus.SUSPENDED
        db.flush()
        logger.info("Suspended service %s (%s)", service.id, reason)
        return True

    async def unsuspend(
        self,
        db: Session,
        service: Service,
        *,
        reason: str,
    ) -> bool:
        await asyncio.sleep(0)
        if service.status != ServiceStatus.SUSPENDED:
            return False
        service.status = ServiceStatus.ACTIVE
        db.flush()
        logger.info("Unsuspended service %s (%s)", service.id, reason)
        return True
