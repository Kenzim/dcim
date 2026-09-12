from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session, joinedload
import httpx

from app.core.auth import require_admin
from app.core.database import get_db
from app.dao.proxmox_inventory_dao import ProxmoxInventoryDAO
from app.models.proxmox_inventory import ProxmoxCapacitySnapshot, ProxmoxNode, ProxmoxStorage
from app.services.vm_provisioning_service import VMProvisioningService


from typing import Annotated
DbDep = Annotated[Session, Depends(get_db)]
AdminDep = Annotated[dict, Depends(require_admin)]

router = APIRouter(prefix="/proxmox", tags=["proxmox"])

CLUSTER_NOT_FOUND = "Cluster not found"


class ClusterCreate(BaseModel):
    name: str
    api_url: str
    username: str
    password: str
    # Defaults to True (verify certs) so a management-network MITM can't
    # silently steal Proxmox session cookies/VM console credentials; admins
    # managing clusters with self-signed certs must opt out explicitly.
    verify_ssl: bool = True
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
    auth: AdminDep,
    db: DbDep,
):
    return VMProvisioningService.get_cluster_capacity_summary(db)


@router.get("/backup-storages")
async def list_backup_storages(
    auth: AdminDep,
    db: DbDep,
):
    """Distinct synced storage names suitable for product backup config dropdowns."""
    rows = ProxmoxInventoryDAO.list_distinct_storage_names(db, backup_capable_only=True)
    if not rows:
        # Fall back to all known storages so admins can still pick names before sync tags content.
        rows = ProxmoxInventoryDAO.list_distinct_storage_names(db, backup_capable_only=False)
    return rows


@router.post("/clusters", status_code=status.HTTP_201_CREATED)
async def create_cluster(
    data: ClusterCreate,
    auth: AdminDep,
    db: DbDep,
):
    row = ProxmoxInventoryDAO.create_cluster(db, **data.model_dump())
    return {"id": row.id}


@router.put("/clusters/{cluster_id}")
async def update_cluster(
    cluster_id: int,
    data: ClusterUpdate,
    auth: AdminDep,
    db: DbDep,
):
    row = ProxmoxInventoryDAO.get_cluster(db, cluster_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=CLUSTER_NOT_FOUND)
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
    auth: AdminDep,
    db: DbDep,
):
    cluster = ProxmoxInventoryDAO.get_cluster(db, cluster_id)
    if not cluster:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=CLUSTER_NOT_FOUND)

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
                # Snapshots accumulate every sync; keep only recent history per
                # node so the table (and any full-collection load of it) stays bounded.
                ProxmoxInventoryDAO.prune_capacity_snapshots(db, node_id=node.id)

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
    auth: AdminDep,
    db: DbDep,
):
    cluster = ProxmoxInventoryDAO.get_cluster(db, cluster_id)
    if not cluster:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=CLUSTER_NOT_FOUND)

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


def _capacity_payload(snapshot: Optional[ProxmoxCapacitySnapshot]) -> Optional[dict[str, Any]]:
    if not snapshot:
        return None
    return {
        "cpu_total": snapshot.cpu_total,
        "cpu_used": snapshot.cpu_used,
        "ram_total_bytes": snapshot.ram_total_bytes,
        "ram_used_bytes": snapshot.ram_used_bytes,
        "storage_total_bytes": snapshot.storage_total_bytes,
        "storage_used_bytes": snapshot.storage_used_bytes,
        "overcommit_ratio": snapshot.overcommit_ratio,
        "created_at": snapshot.created_at.isoformat() if snapshot.created_at else None,
    }


def _latest_snapshot(db: Session, node_id: int) -> Optional[ProxmoxCapacitySnapshot]:
    return (
        db.query(ProxmoxCapacitySnapshot)
        .filter(ProxmoxCapacitySnapshot.node_id == node_id)
        .order_by(ProxmoxCapacitySnapshot.created_at.desc(), ProxmoxCapacitySnapshot.id.desc())
        .first()
    )


def _accumulate_totals(totals: dict[str, float], snapshot: ProxmoxCapacitySnapshot) -> None:
    if snapshot.cpu_total is not None:
        totals["cpu_total_cores"] += snapshot.cpu_total
        totals["cpu_used_cores"] += (snapshot.cpu_used or 0) * snapshot.cpu_total
    totals["ram_total_bytes"] += snapshot.ram_total_bytes or 0
    totals["ram_used_bytes"] += snapshot.ram_used_bytes or 0
    totals["storage_total_bytes"] += snapshot.storage_total_bytes or 0
    totals["storage_used_bytes"] += snapshot.storage_used_bytes or 0


def _node_overview_payload(node: ProxmoxNode, snapshot: Optional[ProxmoxCapacitySnapshot]) -> dict[str, Any]:
    return {
        "id": node.id,
        "node_name": node.node_name,
        "enabled": node.enabled,
        "capacity": _capacity_payload(snapshot),
        "template_count": len(node.templates or []),
        "storage_count": len(node.storages or []),
        "templates": [
            {"id": t.id, "vmid": t.vmid, "name": t.name, "storage_name": t.storage_name}
            for t in sorted(node.templates or [], key=lambda t: t.name)
        ],
        "storages": [
            {
                "id": s.id,
                "storage_name": s.storage_name,
                "storage_type": s.storage_type,
                "total_bytes": s.total_bytes,
                "used_bytes": s.used_bytes,
            }
            for s in sorted(node.storages or [], key=lambda s: s.storage_name)
        ],
    }


@router.get("/clusters/{cluster_id}/overview")
async def get_cluster_overview(
    cluster_id: int,
    auth: AdminDep,
    db: DbDep,
):
    """
    Detailed single-cluster view: cluster identity, cluster-wide capacity
    totals (from each node's latest snapshot), and one panel per node with
    its own capacity + templates + storages.
    """
    cluster = ProxmoxInventoryDAO.get_cluster(db, cluster_id)
    if not cluster:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=CLUSTER_NOT_FOUND)

    nodes = (
        db.query(ProxmoxNode)
        .options(joinedload(ProxmoxNode.storages), joinedload(ProxmoxNode.templates))
        .filter(ProxmoxNode.cluster_id == cluster_id)
        .order_by(ProxmoxNode.node_name)
        .all()
    )

    totals = {
        "cpu_total_cores": 0.0,
        "cpu_used_cores": 0.0,
        "ram_total_bytes": 0,
        "ram_used_bytes": 0,
        "storage_total_bytes": 0,
        "storage_used_bytes": 0,
    }
    has_capacity = False
    node_payloads = []
    for node in nodes:
        latest = _latest_snapshot(db, node.id)
        if latest:
            has_capacity = True
            _accumulate_totals(totals, latest)
        node_payloads.append(_node_overview_payload(node, latest))

    return {
        "cluster": {
            "id": cluster.id,
            "name": cluster.name,
            "api_url": cluster.api_url,
            "enabled": cluster.enabled,
            "verify_ssl": cluster.verify_ssl,
            "vmid_min": cluster.vmid_min,
            "vmid_max": cluster.vmid_max,
        },
        "totals": totals if has_capacity else None,
        "template_count": sum(n["template_count"] for n in node_payloads),
        "storage_count": sum(n["storage_count"] for n in node_payloads),
        "nodes": node_payloads,
    }


@router.post("/clusters/{cluster_id}/nodes", status_code=status.HTTP_201_CREATED)
async def upsert_node(
    cluster_id: int,
    data: NodeUpsert,
    auth: AdminDep,
    db: DbDep,
):
    cluster = ProxmoxInventoryDAO.get_cluster(db, cluster_id)
    if not cluster:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=CLUSTER_NOT_FOUND)
    row = ProxmoxInventoryDAO.upsert_node(db, cluster_id=cluster_id, **data.model_dump())
    return {"id": row.id}


@router.post("/nodes/{node_id}/storages", status_code=status.HTTP_201_CREATED)
async def upsert_storage(
    node_id: int,
    data: StorageUpsert,
    auth: AdminDep,
    db: DbDep,
):
    row = ProxmoxInventoryDAO.upsert_storage(db, node_id=node_id, **data.model_dump())
    return {"id": row.id}


@router.post("/nodes/{node_id}/templates", status_code=status.HTTP_201_CREATED)
async def upsert_template(
    node_id: int,
    data: TemplateUpsert,
    auth: AdminDep,
    db: DbDep,
):
    row = ProxmoxInventoryDAO.upsert_template(db, node_id=node_id, **data.model_dump())
    return {"id": row.id}


@router.post("/nodes/{node_id}/capacity", status_code=status.HTTP_201_CREATED)
async def add_capacity_snapshot(
    node_id: int,
    data: CapacityCreate,
    auth: AdminDep,
    db: DbDep,
):
    row = ProxmoxInventoryDAO.add_capacity_snapshot(db, node_id=node_id, **data.model_dump())
    ProxmoxInventoryDAO.prune_capacity_snapshots(db, node_id=node_id)
    db.commit()
    return {"id": row.id}


@router.post("/vm/plan")
async def plan_vm_provisioning(
    data: VMPlanRequest,
    auth: AdminDep,
    db: DbDep,
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
