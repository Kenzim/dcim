"""Service lifecycle: VM, bare metal, HTTP proxy, backups, jobs."""

from __future__ import annotations

from typing import Optional

from app.api.services_admin import (
    ServiceStatusUpdateBody,
    VmPowerActionBody,
    admin_reinstall_vm_guest,
    admin_restore_vm_backup,
    admin_vm_power_action,
    update_service_status,
)
from app.api.vm_backup_routes import BackupMutateBody
from app.dao.service_dao import ServiceDAO
from app.dao.vm_deployment_job_dao import VMDeploymentJobDAO
from app.mcp.instance import mcp
from app.mcp.runtime import run_tool
from app.mcp.serialize import service_row
from app.models.service import ServiceStatus, ServiceType
from app.services.provisioning import (
    ProvisionRequest,
    ProvisioningActor,
    ProvisioningError,
    ProvisioningService,
    infer_provisioning_source,
)
from app.services.vm_backup_service import list_service_backups_and_jobs

_MSG_SERVICE_NOT_FOUND = "Service not found"


def _auth(ctx):
    return ctx.as_admin_auth()


def _mcp_actor() -> ProvisioningActor:
    return ProvisioningActor(kind="mcp", actor_id=0, name="mcp", source="mcp")


def _create(db, req: ProvisionRequest):
    try:
        return ProvisioningService.create(db, req, _mcp_actor())
    except ProvisioningError as exc:
        raise ValueError(exc.message) from exc


@mcp.tool()
async def list_services(
    service_type: Optional[str] = None, status: Optional[str] = None, limit: int = 100
) -> dict:
    """List VM, bare-metal, and HTTP-proxy services."""

    def work(db, ctx):
        cap = max(1, min(int(limit or 100), 200))
        status_enum = ServiceStatus(status) if status else None
        rows = ServiceDAO.get_all(db, skip=0, limit=500, status=status_enum)
        if service_type:
            wanted = ServiceType(service_type)
            rows = [s for s in rows if s.service_type == wanted]
        return {"services": [service_row(db, s) for s in rows[:cap]]}

    return await run_tool(
        "list_services",
        "read",
        work,
        args={"service_type": service_type, "status": status, "limit": limit},
    )


@mcp.tool()
async def get_service(service_id: int) -> dict:
    """Get a service (no guest passwords or proxy credentials)."""

    def work(db, ctx):
        row = ServiceDAO.get_by_id(db, service_id)
        if not row:
            raise ValueError(_MSG_SERVICE_NOT_FOUND)
        return service_row(db, row)

    return await run_tool("get_service", "read", work, args={"service_id": service_id})


@mcp.tool()
async def list_service_backups(service_id: int) -> dict:
    """List VM backups and in-flight backup jobs."""

    async def work(db, ctx):
        row = ServiceDAO.get_by_id(db, service_id)
        if not row:
            raise ValueError(_MSG_SERVICE_NOT_FOUND)
        items, jobs = await list_service_backups_and_jobs(db, row)
        return {"backups": items, "jobs": jobs}

    return await run_tool("list_service_backups", "read", work, args={"service_id": service_id})


@mcp.tool()
async def list_deployment_jobs(service_id: int, limit: int = 20) -> dict:
    """List VM deployment jobs for a service."""

    def work(db, ctx):
        if not ServiceDAO.get_by_id(db, service_id):
            raise ValueError(_MSG_SERVICE_NOT_FOUND)
        cap = max(1, min(int(limit or 20), 50))
        jobs = VMDeploymentJobDAO.list_by_service(db, service_id, limit=cap)
        return {
            "jobs": [
                {
                    "id": j.id,
                    "status": j.status.value if j.status else None,
                    "strategy_name": j.strategy_name,
                    "attempt": j.attempt,
                    "error_message": j.error_message,
                    "created_at": j.created_at.isoformat() if j.created_at else None,
                }
                for j in jobs
            ]
        }

    return await run_tool(
        "list_deployment_jobs", "read", work, args={"service_id": service_id, "limit": limit}
    )


@mcp.tool()
async def provision_vm(
    name: str,
    product_code: str,
    vm_template_id: int,
    owner_user_id: Optional[int] = None,
    proxmox_cluster_id: Optional[int] = None,
    proxmox_node_name: Optional[str] = None,
    proxmox_vmid: Optional[int] = None,
    auto_provision: bool = True,
    description: Optional[str] = None,
) -> dict:
    """Create a VM service and optionally enqueue provisioning."""

    def work(db, ctx):
        req = ProvisionRequest(
            name=name,
            service_type=ServiceType.VM,
            owner_user_id=owner_user_id,
            product_code=product_code,
            description=description,
            provisioning_source=infer_provisioning_source(db, owner_user_id),
            auto_provision=auto_provision,
            vm_template_id=vm_template_id,
            proxmox_cluster_id=proxmox_cluster_id,
            proxmox_node_name=proxmox_node_name,
            proxmox_vmid=proxmox_vmid,
        )
        service = _create(db, req)
        return service_row(db, service)

    return await run_tool(
        "provision_vm",
        "write",
        work,
        args={
            "name": name,
            "product_code": product_code,
            "vm_template_id": vm_template_id,
            "owner_user_id": owner_user_id,
            "proxmox_cluster_id": proxmox_cluster_id,
        },
    )


@mcp.tool()
async def provision_http_proxy(
    name: str,
    product_code: Optional[str] = None,
    owner_user_id: Optional[int] = None,
    ip_count: Optional[int] = None,
    subnet_id: Optional[int] = None,
    description: Optional[str] = None,
) -> dict:
    """Create an HTTP-proxy service and auto-assign IPAM addresses."""

    def work(db, ctx):
        req = ProvisionRequest(
            name=name,
            service_type=ServiceType.HTTP_PROXY,
            owner_user_id=owner_user_id,
            product_code=product_code,
            description=description,
            provisioning_source=infer_provisioning_source(db, owner_user_id),
            ip_count=ip_count,
            subnet_id=subnet_id,
        )
        service = _create(db, req)
        return service_row(db, service)

    return await run_tool(
        "provision_http_proxy",
        "write",
        work,
        args={"name": name, "product_code": product_code, "owner_user_id": owner_user_id},
    )


@mcp.tool()
async def provision_bare_metal(
    name: str,
    server_id: Optional[int] = None,
    server_group_id: Optional[int] = None,
    owner_user_id: Optional[int] = None,
    product_code: Optional[str] = None,
    template_id: Optional[str] = None,
    description: Optional[str] = None,
) -> dict:
    """Create a bare-metal service on an existing rack server or a server group."""

    def work(db, ctx):
        req = ProvisionRequest(
            name=name,
            service_type=ServiceType.BARE_METAL,
            owner_user_id=owner_user_id,
            product_code=product_code,
            description=description,
            provisioning_source=infer_provisioning_source(db, owner_user_id),
            server_id=server_id,
            server_group_id=server_group_id,
            template_id=template_id,
        )
        service = _create(db, req)
        return service_row(db, service)

    return await run_tool(
        "provision_bare_metal",
        "write",
        work,
        args={"name": name, "server_id": server_id, "owner_user_id": owner_user_id},
    )


@mcp.tool()
async def service_power(service_id: int, action: str, confirm: bool = False) -> dict:
    """Power a VM service (off/reset need confirm=true) or its linked BM server."""
    action_norm = (action or "").strip().lower()
    destructive = action_norm in {"off", "reset", "reboot"}

    async def work(db, ctx):
        service = ServiceDAO.get_by_id(db, service_id)
        if not service:
            raise ValueError(_MSG_SERVICE_NOT_FOUND)
        if service.service_type == ServiceType.VM:
            body = VmPowerActionBody(action=action_norm)
            await admin_vm_power_action(service_id, body, _auth(ctx), db)
            db.refresh(service)
            return service_row(db, service)
        from app.mcp.tools.power import server_power
        from app.services.service_resource import service_linked_server

        server = service_linked_server(db, service)
        if not server:
            raise ValueError("Service has no linked server")
        return await server_power(server.id, action_norm, confirm=confirm)

    return await run_tool(
        "service_power",
        "destructive" if destructive else "write",
        work,
        destructive=destructive,
        confirm=confirm,
        args={"service_id": service_id, "action": action_norm, "confirm": confirm},
    )


@mcp.tool()
async def service_set_status(service_id: int, status: str, confirm: bool = False) -> dict:
    """Set service status: active, suspended, terminated, pending. Terminate is destructive."""
    destructive = (status or "").strip().lower() == "terminated"

    async def work(db, ctx):
        body = ServiceStatusUpdateBody(status=status)
        await update_service_status(service_id, body, _auth(ctx), db)
        row = ServiceDAO.get_by_id(db, service_id)
        return service_row(db, row)

    return await run_tool(
        "service_set_status",
        "destructive" if destructive else "write",
        work,
        destructive=destructive,
        confirm=confirm,
        args={"service_id": service_id, "status": status, "confirm": confirm},
    )


@mcp.tool()
async def terminate_service(service_id: int, confirm: bool = False) -> dict:
    """Terminate a service (releases IPs; does not impersonate or dump keys)."""
    return await service_set_status(service_id, "terminated", confirm=confirm)


@mcp.tool()
async def restore_backup(
    service_id: int,
    volid: str,
    storage: Optional[str] = None,
    start: bool = True,
    confirm: bool = False,
) -> dict:
    """Restore a VM backup onto the service VMID (enqueued)."""

    async def work(db, ctx):
        body = BackupMutateBody(volid=volid, storage=storage, wait=False, start=start)
        return await admin_restore_vm_backup(service_id, body, _auth(ctx), db)

    return await run_tool(
        "restore_backup",
        "destructive",
        work,
        destructive=True,
        confirm=confirm,
        args={"service_id": service_id, "volid": volid, "confirm": confirm},
    )


@mcp.tool()
async def reinstall_vm(
    service_id: int, vm_template_id: Optional[int] = None, confirm: bool = False
) -> dict:
    """Destroy and reprovision a VM guest at the reserved VMID."""

    async def work(db, ctx):
        from app.api.services_admin import VmReinstallBody

        body = VmReinstallBody(vm_template_id=vm_template_id) if vm_template_id else None
        await admin_reinstall_vm_guest(service_id, body, _auth(ctx), db)
        row = ServiceDAO.get_by_id(db, service_id)
        return service_row(db, row)

    return await run_tool(
        "reinstall_vm",
        "destructive",
        work,
        destructive=True,
        confirm=confirm,
        args={"service_id": service_id, "vm_template_id": vm_template_id, "confirm": confirm},
    )
