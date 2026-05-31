from ipaddress import ip_address
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import require_admin
from app.core.database import get_db
from app.dao.proxmox_inventory_dao import ProxmoxInventoryDAO
from app.dao.vm_ip_allocation_dao import VMIPAllocationDAO


router = APIRouter(prefix="/vm-ip-allocations", tags=["vm-ip-allocations"])


class VMIPCreate(BaseModel):
    ip_address: str
    subnet_mask: str
    gateway: str
    bridge_name: Optional[str] = None
    cluster_ids: list[int] = Field(default_factory=list)
    enabled: bool = True


class VMIPBulkCreate(BaseModel):
    start_ip: str
    end_ip: str
    subnet_mask: str
    gateway: str
    bridge_name: Optional[str] = None
    cluster_ids: list[int] = Field(default_factory=list)
    enabled: bool = True


class VMIPUpdate(BaseModel):
    subnet_mask: Optional[str] = None
    gateway: Optional[str] = None
    bridge_name: Optional[str] = None
    cluster_ids: Optional[list[int]] = None
    enabled: Optional[bool] = None


class VMIPBulkUpdate(BaseModel):
    ids: list[int]
    subnet_mask: Optional[str] = None
    gateway: Optional[str] = None
    bridge_name: Optional[str] = None
    cluster_ids: Optional[list[int]] = None
    enabled: Optional[bool] = None


def _serialize(row):
    svc = row.assigned_service
    return {
        "id": row.id,
        "ip_address": row.ip_address,
        "subnet_mask": row.subnet_mask,
        "gateway": row.gateway,
        "bridge_name": row.bridge_name,
        "enabled": row.enabled,
        "assigned_service_id": row.assigned_service_id,
        "assigned_service_name": svc.name if svc else None,
        "assigned_service_type": svc.service_type.value if svc and svc.service_type else None,
        "cluster_ids": [cluster.id for cluster in row.clusters],
        "clusters": [{"id": cluster.id, "name": cluster.name} for cluster in row.clusters],
    }


def _validate_ip(value: str, field_name: str) -> None:
    try:
        ip_address(value)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid {field_name}: '{value}'",
        )


def _validate_clusters(db: Session, cluster_ids: list[int]) -> None:
    if not cluster_ids:
        return
    existing = {cluster.id for cluster in ProxmoxInventoryDAO.list_clusters(db)}
    missing = sorted(set(cluster_ids) - existing)
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown cluster_ids: {missing}",
        )


@router.get("")
async def list_vm_ip_allocations(
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    rows = VMIPAllocationDAO.list_all(db)
    return [_serialize(row) for row in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_vm_ip_allocation(
    data: VMIPCreate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _validate_ip(data.ip_address, "ip_address")
    _validate_ip(data.gateway, "gateway")
    _validate_clusters(db, data.cluster_ids)
    if VMIPAllocationDAO.get_by_ip(db, data.ip_address):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="IP allocation already exists")
    row = VMIPAllocationDAO.create(
        db=db,
        ip_address_value=data.ip_address,
        subnet_mask=data.subnet_mask,
        gateway=data.gateway,
        bridge_name=data.bridge_name,
        cluster_ids=data.cluster_ids,
        enabled=data.enabled,
    )
    db.commit()
    return {"id": row.id}


@router.post("/bulk", status_code=status.HTTP_201_CREATED)
async def create_vm_ip_allocations_bulk(
    data: VMIPBulkCreate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _validate_ip(data.start_ip, "start_ip")
    _validate_ip(data.end_ip, "end_ip")
    _validate_ip(data.gateway, "gateway")
    _validate_clusters(db, data.cluster_ids)

    ips = VMIPAllocationDAO.iter_ips_in_range(data.start_ip, data.end_ip)
    if len(ips) > 65536:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Range too large (>65536 IPs)")

    created = 0
    skipped_existing = 0
    for ip_value in ips:
        if VMIPAllocationDAO.get_by_ip(db, ip_value):
            skipped_existing += 1
            continue
        VMIPAllocationDAO.create(
            db=db,
            ip_address_value=ip_value,
            subnet_mask=data.subnet_mask,
            gateway=data.gateway,
            bridge_name=data.bridge_name,
            cluster_ids=data.cluster_ids,
            enabled=data.enabled,
        )
        created += 1
    db.commit()
    return {"created": created, "skipped_existing": skipped_existing}


@router.put("/bulk")
async def bulk_update_vm_ip_allocations(
    data: VMIPBulkUpdate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if not data.ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No ids provided")
    if data.gateway is not None:
        _validate_ip(data.gateway, "gateway")
    if data.cluster_ids is not None:
        _validate_clusters(db, data.cluster_ids)

    updated = 0
    for allocation_id in data.ids:
        row = VMIPAllocationDAO.get_by_id(db, allocation_id)
        if not row:
            continue
        VMIPAllocationDAO.update(
            db=db,
            row=row,
            subnet_mask=data.subnet_mask,
            gateway=data.gateway,
            bridge_name=data.bridge_name,
            cluster_ids=data.cluster_ids,
            enabled=data.enabled,
        )
        updated += 1
    db.commit()
    return {"updated": updated}


@router.put("/{allocation_id}")
async def update_vm_ip_allocation(
    allocation_id: int,
    data: VMIPUpdate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    row = VMIPAllocationDAO.get_by_id(db, allocation_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="VM IP allocation not found")
    if data.gateway is not None:
        _validate_ip(data.gateway, "gateway")
    if data.cluster_ids is not None:
        _validate_clusters(db, data.cluster_ids)
    VMIPAllocationDAO.update(
        db=db,
        row=row,
        subnet_mask=data.subnet_mask,
        gateway=data.gateway,
        bridge_name=data.bridge_name,
        cluster_ids=data.cluster_ids,
        enabled=data.enabled,
    )
    db.commit()
    return {"status": "ok"}


@router.delete("/{allocation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_vm_ip_allocation(
    allocation_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    row = VMIPAllocationDAO.get_by_id(db, allocation_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="VM IP allocation not found")
    VMIPAllocationDAO.delete(db, row)
    db.commit()
    return None
