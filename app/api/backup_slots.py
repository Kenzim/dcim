"""Backup slot API: clients manage their own slots; staff manage any client's."""

from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import get_current_user, require_admin
from app.core.client_permissions import PermissionKey
from app.core.database import get_db
from app.dao.service_dao import ServiceDAO
from app.dao.user_dao import UserDAO
from app.models.service import Service, ServiceStatus, ServiceType
from app.services import backup_slot_service as slots
from app.services.backup_slot_service import BackupSlotError
from app.services.client_permission_resolver import require_client_permission
from app.services.provisioning import (
    ProvisioningActor,
    ProvisioningError,
    ProvisioningService,
    ProvisionRequest,
    infer_provisioning_source,
)
from app.services.retained_restore_service import restore_targets

DbDep = Annotated[Session, Depends(get_db)]
UserDep = Annotated[dict, Depends(get_current_user)]
AdminDep = Annotated[dict, Depends(require_admin)]

client_router = APIRouter(prefix="/client/backup-slots", tags=["client"])
admin_router = APIRouter(prefix="/admin/backup-slots", tags=["admin-backup-slots"])

SLOT_NOT_FOUND = "Backup slot not found"
_SERVICE_NOT_FOUND = "Service not found"


class SlotCreate(BaseModel):
    owner_user_id: int
    name: str = Field(..., min_length=1, max_length=255)
    size_gb: int = Field(..., ge=1)
    product_code: Optional[str] = None
    description: Optional[str] = None


class ResizeBody(BaseModel):
    size_gb: int = Field(..., ge=1)


class RetainBody(BaseModel):
    source_service_id: int
    volid: str = Field(..., min_length=1)
    name: Optional[str] = Field(None, max_length=255)
    replace: bool = False


class RestoreBody(BaseModel):
    target_service_id: int
    confirm_data_loss: bool = False
    start: bool = True


def raise_slot_error(exc: Exception) -> HTTPException:
    if isinstance(exc, BackupSlotError):
        return HTTPException(status_code=exc.status_code, detail=exc.message)
    raise exc


def client_user_id(auth: dict) -> int:
    if auth.get("type") == "api_key" or auth.get("is_admin") or auth.get("is_reseller"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Backup slots are managed from a client account")
    user_id = auth.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    return int(user_id)


def owned_slot_service(db: Session, user_id: int, service_id: int, permission: str) -> Service:
    """A live backup slot service the client owns; anyone else's is reported as missing."""
    service = ServiceDAO.get_by_id(db, service_id)
    if (
        service is None
        or service.owner_user_id != user_id
        or service.service_type != ServiceType.BACKUP_SLOT
        or service.status == ServiceStatus.TERMINATED
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=SLOT_NOT_FOUND)
    require_client_permission(db, service, permission)
    return service


def owned_vm_service(db: Session, user_id: int, service_id: int) -> Service:
    service = ServiceDAO.get_by_id(db, service_id)
    if service is None or service.owner_user_id != user_id or service.service_type != ServiceType.VM:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_SERVICE_NOT_FOUND)
    require_client_permission(db, service, PermissionKey.VM_BACKUPS)
    return service


def slot_detail(db: Session, service: Service) -> dict:
    slot = slots.require_slot(db, service)
    out = slots.slot_summary(service, slot)
    if slot.retained is not None:
        out["restore_targets"] = restore_targets(db, slot.retained)
    return out


async def do_retain(db: Session, slot_service: Service, source: Service, body: RetainBody) -> dict:
    try:
        retained = await slots.retain_from_vm(
            db, slot_service, source, volid=body.volid, name=body.name, replace=body.replace
        )
    except BackupSlotError as exc:
        raise raise_slot_error(exc) from exc
    return {"retained": retained, "slot": slot_detail(db, slot_service)}


async def do_release(db: Session, slot_service: Service) -> dict:
    try:
        out = await slots.release_from_slot(db, slot_service)
    except BackupSlotError as exc:
        raise raise_slot_error(exc) from exc
    return {**out, "slot": slot_detail(db, slot_service)}


async def do_restore(db: Session, slot_service: Service, target: Service, body: RestoreBody, *, staff: bool) -> dict:
    try:
        return await slots.restore_from_slot(
            db, slot_service, target, start=body.start, confirm_data_loss=body.confirm_data_loss, staff=staff
        )
    except BackupSlotError as exc:
        raise raise_slot_error(exc) from exc
    except Exception as exc:
        from app.api.vm_backup_routes import map_backup_error

        raise map_backup_error(exc) from exc


# --- client ---


@client_router.get("")
async def client_list_slots(auth: UserDep, db: DbDep):
    """Every slot the client holds, with what each retains, whether or not they have any VMs."""
    return {"slots": slots.list_slot_summaries(db, client_user_id(auth))}


@client_router.get("/{service_id}")
async def client_get_slot(service_id: int, auth: UserDep, db: DbDep):
    service = owned_slot_service(db, client_user_id(auth), service_id, PermissionKey.SERVICE_VIEW)
    return slot_detail(db, service)


@client_router.post("/{service_id}/retain")
async def client_retain(service_id: int, body: RetainBody, auth: UserDep, db: DbDep):
    user_id = client_user_id(auth)
    slot_service = owned_slot_service(db, user_id, service_id, PermissionKey.BACKUP_SLOT_MANAGE)
    source = owned_vm_service(db, user_id, body.source_service_id)
    return await do_retain(db, slot_service, source, body)


@client_router.post("/{service_id}/release")
async def client_release(service_id: int, auth: UserDep, db: DbDep):
    slot_service = owned_slot_service(db, client_user_id(auth), service_id, PermissionKey.BACKUP_SLOT_MANAGE)
    return await do_release(db, slot_service)


@client_router.post("/{service_id}/restore")
async def client_restore(service_id: int, body: RestoreBody, auth: UserDep, db: DbDep):
    user_id = client_user_id(auth)
    slot_service = owned_slot_service(db, user_id, service_id, PermissionKey.BACKUP_SLOT_MANAGE)
    target = owned_vm_service(db, user_id, body.target_service_id)
    return await do_restore(db, slot_service, target, body, staff=False)


# --- staff ---


def admin_slot_service(db: Session, service_id: int) -> Service:
    service = ServiceDAO.get_by_id(db, service_id)
    if service is None or service.service_type != ServiceType.BACKUP_SLOT:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=SLOT_NOT_FOUND)
    return service


@admin_router.post("", status_code=status.HTTP_201_CREATED)
async def admin_create_slot(data: SlotCreate, auth: AdminDep, db: DbDep):
    if UserDAO.get_by_id(db, data.owner_user_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    actor = ProvisioningActor(
        kind="admin", actor_id=int(auth.get("user_id") or 0), name=str(auth.get("username") or "admin"),
        source="admin_api",
    )
    try:
        req = ProvisionRequest(
            name=data.name,
            service_type=ServiceType.BACKUP_SLOT,
            owner_user_id=data.owner_user_id,
            product_code=data.product_code,
            description=data.description,
            provisioning_source=infer_provisioning_source(db, data.owner_user_id),
            size_gb=data.size_gb,
        )
        service = ProvisioningService.create(db, req, actor)
    except ProvisioningError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.message) from exc
    return slot_detail(db, service)


@admin_router.get("/{service_id}")
async def admin_get_slot(service_id: int, auth: AdminDep, db: DbDep):
    return slot_detail(db, admin_slot_service(db, service_id))


@admin_router.post("/{service_id}/resize")
async def admin_resize_slot(service_id: int, body: ResizeBody, auth: AdminDep, db: DbDep):
    service = admin_slot_service(db, service_id)
    try:
        slots.resize(db, service, body.size_gb)
    except BackupSlotError as exc:
        raise raise_slot_error(exc) from exc
    return slot_detail(db, service)


@admin_router.post("/{service_id}/release")
async def admin_release(service_id: int, auth: AdminDep, db: DbDep):
    return await do_release(db, admin_slot_service(db, service_id))
