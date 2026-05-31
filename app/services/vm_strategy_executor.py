"""
Execute VM OS strategies against Proxmox (clone template, cloud-init IP, sizing).

Used by admin API; keeps billing/plugin patterns in one place.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict

import httpx
from sqlalchemy.orm import Session

from app.dao.product_catalog_dao import VMTemplateDAO
from app.dao.proxmox_inventory_dao import ProxmoxInventoryDAO
from app.dao.service_dao import ServiceDAO
from app.dao.vm_ip_allocation_dao import VMIPAllocationDAO
from app.models.service import Service, ServiceStatus, ServiceType
from app.models.service_vm import VMGuestState
from app.plugins.registry import get_registry
from app.services.proxmox_placement import cluster_to_proxmox_plugin_config
from app.services.service_resource import vm_placement
from app.services.vm_install_type_strategy import resolve_vm_template_strategy
from app.utils.ipv4_netmask import ipv4_netmask_to_prefixlen

logger = logging.getLogger(__name__)


def resolve_vm_strategy_name_for_service(db: Session, service: Service) -> str:
    cfg = service.config or {}
    plan = cfg.get("vm_plan") or {}
    name = plan.get("strategy_name")
    if name:
        return str(name)
    if service.vm and service.vm.vm_template_id:
        tmpl = VMTemplateDAO.get_by_id(db, service.vm.vm_template_id)
        if tmpl:
            spec = resolve_vm_template_strategy(tmpl.os_type)
            return str(spec["strategy_name"])
    return "stub"


def _merge_provision_config(service: Service, patch: Dict[str, Any]) -> None:
    base = dict(service.config or {})
    prev = dict(base.get("vm_provision") or {})
    prev.update(patch)
    base["vm_provision"] = prev
    service.config = base


async def _find_template_vmid_live(cluster, node_name: str, template_name: str) -> int | None:
    """
    Resolve template VMID directly from Proxmox API at provision time.
    This avoids hard dependency on synced template inventory.
    """
    auth_url = f"{cluster.api_url.rstrip('/')}/api2/json/access/ticket"
    base = f"{cluster.api_url.rstrip('/')}/api2/json"
    async with httpx.AsyncClient(verify=cluster.verify_ssl, timeout=20.0) as client:
        auth_resp = await client.post(
            auth_url,
            data={
                "username": cluster.username,
                "password": cluster.password,
            },
        )
        auth_resp.raise_for_status()
        payload = auth_resp.json().get("data") or {}
        ticket = payload.get("ticket")
        csrf = payload.get("CSRFPreventionToken")
        if not ticket:
            raise ValueError("Failed to authenticate with Proxmox while resolving template")
        headers = {"Cookie": f"PVEAuthCookie={ticket}"}
        if csrf:
            headers["CSRFPreventionToken"] = csrf
        qemu_resp = await client.get(f"{base}/nodes/{node_name}/qemu", headers=headers)
        qemu_resp.raise_for_status()
        qemu_rows = qemu_resp.json().get("data") or []
        for row in qemu_rows:
            if int(row.get("template") or 0) != 1:
                continue
            if str(row.get("name") or "") != template_name:
                continue
            vmid = row.get("vmid")
            if vmid is not None:
                return int(vmid)
    return None


async def provision_vm_service(db: Session, service_id: int) -> Service:
    """
    Clone catalog template to target vmid, apply RAM/CPU, then:

    - ``cloudinit_clone``: Proxmox ``ipconfig0`` from VM IP pool + optional power on.
    - ``guest_agent``: sizing only + power on (no cloud-init network).

    Raises ``ValueError`` for missing prerequisites or unsupported strategy.
    """
    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise ValueError("Service not found")
    if service.service_type != ServiceType.VM:
        raise ValueError("Not a VM service")
    if service.status == ServiceStatus.TERMINATED:
        raise ValueError("Cannot provision a terminated service")

    cid, node_name, target_vmid = vm_placement(service)
    if cid is None or not (node_name or "").strip() or target_vmid is None:
        raise ValueError(
            "VM needs proxmox_cluster_id, proxmox_node_name, and proxmox_vmid before provisioning"
        )
    node_name = node_name.strip()
    if not service.vm or not service.vm.vm_template_id:
        raise ValueError("VM service has no vm_template_id")

    strategy = resolve_vm_strategy_name_for_service(db, service)
    if strategy == "stub":
        raise ValueError(
            "Service has no provisioning strategy (stub). Ensure vm_plan exists or template os_type maps to a strategy."
        )
    if strategy not in ("cloudinit_clone", "guest_agent"):
        raise ValueError(f"Provisioning not implemented for strategy '{strategy}'")

    tmpl = VMTemplateDAO.get_by_id(db, service.vm.vm_template_id)
    if not tmpl:
        raise ValueError("VM template catalog row not found")

    cluster = ProxmoxInventoryDAO.get_cluster(db, cid)
    if not cluster:
        raise ValueError("Proxmox cluster not found")

    template_vmid = ProxmoxInventoryDAO.find_template_vmid_on_node(
        db,
        cluster_id=cid,
        node_name=node_name,
        template_name=tmpl.proxmox_template_name,
    )
    if template_vmid is None:
        # Live fallback: resolve directly from Proxmox at create/provision time.
        template_vmid = await _find_template_vmid_live(cluster, node_name, tmpl.proxmox_template_name)
    if template_vmid is None:
        raise ValueError(
            f"No Proxmox template named '{tmpl.proxmox_template_name}' found on node '{node_name}' in cluster {cid}."
        )

    plugin_config = cluster_to_proxmox_plugin_config(cluster, node_name, int(target_vmid))
    registry = get_registry()
    plugin = registry.get_plugin("proxmox", plugin_config)

    vm_plan = (service.config or {}).get("vm_plan") or {}
    specs: Dict[str, Any] = dict(vm_plan.get("effective_specs") or {})
    memory_mb = int(specs.get("memory_mb", 2048))
    cores = int(specs.get("cores", 2))
    full_clone = bool(specs.get("full_clone", True))

    started = datetime.now(timezone.utc).isoformat()
    _merge_provision_config(
        service,
        {"status": "running", "started_at": started, "strategy": strategy, "step": "clone"},
    )
    if service.vm:
        service.vm.guest_state = VMGuestState.PROVISIONING
        service.vm.guest_last_error = None
    ServiceDAO.update(db, service)

    try:
        clone_out = await plugin.clone_vm_from_template(
            {"vmid": int(template_vmid)},
            {
                "vmid": int(target_vmid),
                "name": (service.name or f"vm-{target_vmid}")[:90],
                "full_clone": full_clone,
            },
        )
        upid = clone_out.get("task")
        logger.info(
            "VM provision clone service=%s template_vmid=%s newid=%s task=%s",
            service_id,
            template_vmid,
            target_vmid,
            upid,
        )
        if upid:
            await plugin.wait_for_proxmox_task(str(upid))

        _merge_provision_config(service, {"step": "configure", "clone_task": upid})
        ServiceDAO.update(db, service)

        vm_ref = {"vmid": int(target_vmid)}
        net_payload: Dict[str, Any] = {"memory_mb": memory_mb, "cores": cores}

        if strategy == "cloudinit_clone":
            alloc = None
            if service.vm.vm_ip_allocation_id:
                alloc = VMIPAllocationDAO.get_by_id(db, service.vm.vm_ip_allocation_id)
            if not alloc:
                raise ValueError("No VM IP pool row linked (service_vm.vm_ip_allocation_id)")
            prefix = ipv4_netmask_to_prefixlen(alloc.subnet_mask)
            gw = (alloc.gateway or "").strip()
            ip = (alloc.ip_address or "").strip()
            if not ip or not gw:
                raise ValueError("VM IP allocation is missing ip_address or gateway")
            net_payload["ipconfig0"] = f"ip={ip}/{prefix},gw={gw}"
            if specs.get("cloudinit_ciuser"):
                net_payload["ciuser"] = str(specs["cloudinit_ciuser"])
            if specs.get("cloudinit_cipassword"):
                net_payload["cipassword"] = str(specs["cloudinit_cipassword"])

        ok = await plugin.configure_vm(vm_ref, net_payload)
        if not ok:
            raise RuntimeError("Proxmox VM config update failed")

        _merge_provision_config(service, {"step": "power_on"})
        ServiceDAO.update(db, service)

        powered = await plugin.power_on()
        if not powered:
            logger.warning("VM provision: power_on returned False for service %s", service_id)

        _merge_provision_config(
            service,
            {
                "status": "success",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "power_on": powered,
            },
        )
        if service.vm:
            service.vm.guest_state = VMGuestState.RUNNING if powered else VMGuestState.STOPPED
            service.vm.guest_last_error = None
        service.status = ServiceStatus.ACTIVE
        ServiceDAO.update(db, service)
        db.refresh(service)
        return service

    except Exception as exc:
        logger.exception("VM provision failed service=%s", service_id)
        _merge_provision_config(
            service,
            {
                "status": "failed",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "error": str(exc),
            },
        )
        if service.vm:
            service.vm.guest_state = VMGuestState.ERROR
            service.vm.guest_last_error = str(exc)
        ServiceDAO.update(db, service)
        raise


def run_provision_vm_service(db: Session, service_id: int) -> Service:
    """Sync entrypoint for FastAPI sync routes."""
    return asyncio.run(provision_vm_service(db, service_id))
