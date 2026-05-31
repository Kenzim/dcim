from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import get_db
from app.dao.service_dao import ServiceDAO
from app.models.service import ServiceStatus, ServiceType
from app.services.service_resource import vm_placement


router = APIRouter(prefix="/services", tags=["services-client"])


class ClientServiceResponse(BaseModel):
    id: int
    name: str
    service_type: Optional[str] = None
    status: str
    external_service_id: Optional[str] = None
    description: Optional[str] = None
    product_code: Optional[str] = None
    os_code: Optional[str] = None
    proxmox_cluster_id: Optional[int] = None
    proxmox_node_name: Optional[str] = None
    proxmox_vmid: Optional[int] = None


def _service_to_client_response(service) -> ClientServiceResponse:
    cid, node, vmid = vm_placement(service)
    return ClientServiceResponse(
        id=service.id,
        name=service.name,
        service_type=service.service_type.value if service.service_type else None,
        status=service.status.value if isinstance(service.status, ServiceStatus) else str(service.status),
        external_service_id=service.external_service_id,
        description=service.description,
        product_code=service.product_code,
        os_code=service.os_code,
        proxmox_cluster_id=cid,
        proxmox_node_name=node,
        proxmox_vmid=vmid,
    )


@router.get("/me", response_model=List[ClientServiceResponse])
async def list_my_services(
    service_type: Optional[str] = None,
    auth: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = auth.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User session required")
    services = ServiceDAO.get_by_owner_user(db, int(user_id))
    if service_type:
        try:
            st = ServiceType(service_type.lower())
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid service_type") from exc
        services = [s for s in services if s.service_type == st]
    return [_service_to_client_response(s) for s in services]
