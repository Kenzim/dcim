"""Destroy + reprovision a VM guest at its reserved Proxmox VMID."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.dao.proxmox_inventory_dao import ProxmoxInventoryDAO
from app.dao.service_dao import ServiceDAO
from app.models.service import Service, ServiceType
from app.models.service_vm import VMGuestState
from app.plugins.base import PowerState
from app.plugins.registry import get_registry
from app.services.proxmox_placement import cluster_to_proxmox_plugin_config
from app.services.service_resource import vm_placement
from app.services.vm_ssh_keys_service import VmSshKeysError, apply_template_change_for_reinstall
from app.services.vm_strategy_executor import provision_vm_service_async

logger = logging.getLogger(__name__)


class VmReinstallError(Exception):
    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


async def reinstall_vm_guest(
    db: Session,
    service: Service,
    *,
    vm_template_id: Optional[int] = None,
    ssh_public_keys: Any = None,
) -> Dict[str, Any]:
    """Destroy the guest (if present) and enqueue reprovision on the same VMID."""
    if service.service_type != ServiceType.VM or not service.vm:
        raise VmReinstallError("Not a VM service", status_code=400)

    if vm_template_id is not None or ssh_public_keys is not None:
        try:
            service = apply_template_change_for_reinstall(
                db,
                service,
                vm_template_id=vm_template_id,
                ssh_public_keys=ssh_public_keys,
            )
        except VmSshKeysError as exc:
            raise VmReinstallError(str(exc), status_code=exc.status_code) from exc

    cid, node, vmid = vm_placement(service)
    if cid is None or not (node and str(node).strip()) or vmid is None:
        raise VmReinstallError("VM placement is not configured", status_code=409)
    cluster = ProxmoxInventoryDAO.get_cluster(db, cid)
    if cluster is None:
        raise VmReinstallError("Unknown Proxmox cluster", status_code=404)
    try:
        plugin_config = cluster_to_proxmox_plugin_config(cluster, str(node).strip(), int(vmid))
    except ValueError as exc:
        raise VmReinstallError(str(exc), status_code=400) from exc
    plugin = get_registry().get_plugin("proxmox", plugin_config)

    try:
        exists = await plugin.vm_exists()
    except Exception:
        exists = False
    if exists:
        try:
            state = await plugin.get_power_state()
        except Exception:
            state = PowerState.UNKNOWN
        if state == PowerState.ON:
            await plugin.power_off(force=False)
            for _ in range(15):
                await asyncio.sleep(1.0)
                try:
                    if await plugin.get_power_state() == PowerState.OFF:
                        break
                except Exception:
                    continue
            try:
                if await plugin.get_power_state() != PowerState.OFF:
                    await plugin.power_off(force=True)
            except Exception:
                await plugin.power_off(force=True)
        try:
            await plugin.delete_vm({"vmid": int(vmid)})
        except Exception as exc:
            logger.warning("reinstall: delete_vm failed for service %s: %s", service.id, exc)

    if service.vm:
        service.vm.guest_state = VMGuestState.PROVISIONING
        service.vm.guest_last_error = None
        ServiceDAO.update(db, service)
    try:
        service, _job = provision_vm_service_async(db, service.id)
    except ValueError as exc:
        raise VmReinstallError(str(exc), status_code=400) from exc
    except Exception as exc:
        raise VmReinstallError(str(exc), status_code=502) from exc

    return {
        "status": "ok",
        "message": "VM reinstall queued",
        "service_id": service.id,
        "proxmox_vmid": int(vmid),
        "vm_template_id": service.vm.vm_template_id if service.vm else None,
    }
