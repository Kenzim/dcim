from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session
import httpx

from app.core.auth import require_admin
from app.core.database import get_db
from app.dao.proxmox_inventory_dao import ProxmoxInventoryDAO
from app.models.proxmox_inventory import ProxmoxNode, ProxmoxStorage
from app.services.vm_provisioning_service import VMProvisioningService


router = APIRouter(prefix="/proxmox", tags=["proxmox"])


class ClusterCreate(BaseModel):
    name: str
    api_url: str
    username: str
    password: str
    verify_ssl: bool = False
    vmid_min: Optional[int] = None
    vmid_max: Optional[int] = None
    details: dict[str, Any] = Field(default_factory=dict)


class ClusterUpdate(BaseModel):
    name: Optional[str] = None
    api_url: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    verify_ssl: Optional[bool] = None
    enabled: Optional[bool] = None
    vmid_min: Optional[int] = None
    vmid_max: Optional[int] = None
    details: Optional[dict[str, Any]] = None


class NodeUpsert(BaseModel):
    node_name: str
    details: dict[str, Any] = Field(default_factory=dict)


class StorageUpsert(BaseModel):
    storage_name: str
    storage_type: Optional[str] = None
    total_bytes: Optional[int] = None
    used_bytes: Optional[int] = None
    details: dict[str, Any] = Field(default_factory=dict)


class TemplateUpsert(BaseModel):
    vmid: int
    name: str
    storage_name: Optional[str] = None
    details: dict[str, Any] = Field(default_factory=dict)


class CapacityCreate(BaseModel):
    cpu_total: Optional[float] = None
    cpu_used: Optional[float] = None
    ram_total_bytes: Optional[int] = None
    ram_used_bytes: Optional[int] = None
    storage_total_bytes: Optional[int] = None
    storage_used_bytes: Optional[int] = None
    overcommit_ratio: Optional[float] = None


class VMPlanRequest(BaseModel):
    service_id: int
    product_code: str
    os_code: Optional[str] = None
    vm_template_id: Optional[int] = None
    context: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_os_or_template(self):
        if not self.os_code and self.vm_template_id is None:
            raise ValueError("Either os_code or vm_template_id is required")
        return self


@router.get("/clusters")
async def list_clusters(
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return VMProvisioningService.get_cluster_capacity_summary(db)


@router.post("/clusters", status_code=status.HTTP_201_CREATED)
async def create_cluster(
    data: ClusterCreate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    row = ProxmoxInventoryDAO.create_cluster(db, **data.model_dump())
    return {"id": row.id}


@router.put("/clusters/{cluster_id}")
async def update_cluster(
    cluster_id: int,
    data: ClusterUpdate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    row = ProxmoxInventoryDAO.get_cluster(db, cluster_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cluster not found")
    update_data = data.model_dump(exclude_unset=True)
    ProxmoxInventoryDAO.update_cluster(db, row, **update_data)
    return {"status": "ok"}


async def _proxmox_auth(cluster) -> dict[str, str]:
    auth_url = f"{cluster.api_url.rstrip('/')}/api2/json/access/ticket"
    async with httpx.AsyncClient(verify=cluster.verify_ssl, timeout=20.0) as client:
        response = await client.post(
            auth_url,
            data={
                "username": cluster.username,
                "password": cluster.password,
            },
        )
        response.raise_for_status()
        payload = response.json().get("data") or {}
        ticket = payload.get("ticket")
        csrf = payload.get("CSRFPreventionToken")
        if not ticket:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to authenticate with Proxmox")
        return {
            "Cookie": f"PVEAuthCookie={ticket}",
            "CSRFPreventionToken": csrf or "",
        }


@router.post("/clusters/{cluster_id}/sync")
async def sync_cluster_inventory(
    cluster_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    cluster = ProxmoxInventoryDAO.get_cluster(db, cluster_id)
    if not cluster:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cluster not found")

    try:
        headers = await _proxmox_auth(cluster)
        base = f"{cluster.api_url.rstrip('/')}/api2/json"
        async with httpx.AsyncClient(verify=cluster.verify_ssl, timeout=30.0) as client:
            nodes_resp = await client.get(f"{base}/nodes", headers=headers)
            nodes_resp.raise_for_status()
            nodes_data = nodes_resp.json().get("data") or []

            seen_node_names = set()
            for node_info in nodes_data:
                node_name = node_info.get("node")
                if not node_name:
                    continue
                seen_node_names.add(node_name)
                node = ProxmoxInventoryDAO.upsert_node(
                    db,
                    cluster_id=cluster.id,
                    node_name=node_name,
                    details=node_info,
                )

                storages_resp = await client.get(f"{base}/nodes/{node_name}/storage", headers=headers)
                storages_resp.raise_for_status()
                storages_data = storages_resp.json().get("data") or []
                seen_storage_names = set()
                for storage in storages_data:
                    storage_name = storage.get("storage")
                    if not storage_name:
                        continue
                    seen_storage_names.add(storage_name)
                    ProxmoxInventoryDAO.upsert_storage(
                        db=db,
                        node_id=node.id,
                        storage_name=storage_name,
                        storage_type=storage.get("type"),
                        total_bytes=storage.get("total"),
                        used_bytes=storage.get("used"),
                        details=storage,
                    )

                status_resp = await client.get(f"{base}/nodes/{node_name}/status", headers=headers)
                status_resp.raise_for_status()
                status_data = status_resp.json().get("data") or {}
                ProxmoxInventoryDAO.add_capacity_snapshot(
                    db=db,
                    node_id=node.id,
                    cpu_total=status_data.get("cpuinfo", {}).get("cpus"),
                    cpu_used=status_data.get("cpu"),
                    ram_total_bytes=status_data.get("memory", {}).get("total"),
                    ram_used_bytes=status_data.get("memory", {}).get("used"),
                    storage_total_bytes=status_data.get("rootfs", {}).get("total"),
                    storage_used_bytes=status_data.get("rootfs", {}).get("used"),
                    overcommit_ratio=None,
                )

                db.query(ProxmoxStorage).filter(
                    ProxmoxStorage.node_id == node.id,
                    ~ProxmoxStorage.storage_name.in_(list(seen_storage_names) or [""]),
                ).delete(synchronize_session=False)

            stale_nodes = (
                db.query(ProxmoxNode)
                .filter(
                    ProxmoxNode.cluster_id == cluster.id,
                    ~ProxmoxNode.node_name.in_(list(seen_node_names) or [""]),
                )
                .all()
            )
            for stale in stale_nodes:
                db.delete(stale)

            db.commit()
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to sync cluster inventory: {exc}",
        )

    return {"status": "ok"}


@router.get("/clusters/{cluster_id}/inventory")
async def get_cluster_inventory(
    cluster_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    cluster = ProxmoxInventoryDAO.get_cluster(db, cluster_id)
    if not cluster:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cluster not found")

    return {
        "cluster_id": cluster.id,
        "cluster_name": cluster.name,
        "api_url": cluster.api_url,
        "vmid_min": cluster.vmid_min,
        "vmid_max": cluster.vmid_max,
        "nodes": [
            {
                "id": node.id,
                "node_name": node.node_name,
                "enabled": node.enabled,
                "storages": [
                    {
                        "id": storage.id,
                        "storage_name": storage.storage_name,
                        "storage_type": storage.storage_type,
                        "total_bytes": storage.total_bytes,
                        "used_bytes": storage.used_bytes,
                    }
                    for storage in (node.storages or [])
                ],
                "templates": [
                    {
                        "id": template.id,
                        "vmid": template.vmid,
                        "name": template.name,
                        "storage_name": template.storage_name,
                    }
                    for template in (node.templates or [])
                ],
            }
            for node in (cluster.nodes or [])
        ],
    }


@router.post("/clusters/{cluster_id}/nodes", status_code=status.HTTP_201_CREATED)
async def upsert_node(
    cluster_id: int,
    data: NodeUpsert,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    cluster = ProxmoxInventoryDAO.get_cluster(db, cluster_id)
    if not cluster:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cluster not found")
    row = ProxmoxInventoryDAO.upsert_node(db, cluster_id=cluster_id, **data.model_dump())
    return {"id": row.id}


@router.post("/nodes/{node_id}/storages", status_code=status.HTTP_201_CREATED)
async def upsert_storage(
    node_id: int,
    data: StorageUpsert,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    row = ProxmoxInventoryDAO.upsert_storage(db, node_id=node_id, **data.model_dump())
    return {"id": row.id}


@router.post("/nodes/{node_id}/templates", status_code=status.HTTP_201_CREATED)
async def upsert_template(
    node_id: int,
    data: TemplateUpsert,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    row = ProxmoxInventoryDAO.upsert_template(db, node_id=node_id, **data.model_dump())
    return {"id": row.id}


@router.post("/nodes/{node_id}/capacity", status_code=status.HTTP_201_CREATED)
async def add_capacity_snapshot(
    node_id: int,
    data: CapacityCreate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    row = ProxmoxInventoryDAO.add_capacity_snapshot(db, node_id=node_id, **data.model_dump())
    return {"id": row.id}


@router.post("/vm/plan")
async def plan_vm_provisioning(
    data: VMPlanRequest,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        return VMProvisioningService.plan_provisioning(
            db=db,
            service_id=data.service_id,
            product_code=data.product_code,
            os_code=data.os_code,
            vm_template_id=data.vm_template_id,
            context=data.context,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
