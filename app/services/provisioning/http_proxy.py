"""HTTP-proxy service creation (IPAM only, no rack Server)."""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.dao.service_dao import ServiceDAO
from app.models.server_activity import ServerActivityEventType
from app.models.service import Service, ServiceStatus, ServiceType
from app.services.provisioning.errors import ProvisioningError
from app.services.provisioning.request import ProvisionRequest, ProvisioningActor
from app.services.proxy_provisioning import auto_assign_proxy_ips, resolve_proxy_ip_request
from app.services.server_activity_logger import (
    log_server_activity_attempt,
    log_server_activity_success,
)

logger = logging.getLogger(__name__)


def create_http_proxy_service(
    db: Session,
    req: ProvisionRequest,
    actor: ProvisioningActor,
    product_snapshot: dict[str, Any],
) -> Service:
    service = ServiceDAO.create_bare_metal(
        db,
        name=req.name,
        server_id=None,
        owner_user_id=req.owner_user_id,
        external_service_id=req.external_service_id,
        service_type=ServiceType.HTTP_PROXY,
        status=ServiceStatus.PENDING,
        description=req.description,
        config=req.service_config or {},
        product_code=req.product_code,
        product_snapshot=product_snapshot,
        provisioning_source=req.provisioning_source,
    )
    db.refresh(service)
    if req.permission_set_id is not None:
        service.permission_set_id = req.permission_set_id
        ServiceDAO.update(db, service)

    log_server_activity_attempt(
        db,
        service_id=service.id,
        event_type=ServerActivityEventType.SERVICE,
        action="create",
        source=actor.source,
        message=f"Creating proxy service '{service.name}'",
        details={"service_id": service.id, **actor.details},
    )
    ip_req = resolve_proxy_ip_request(
        product_snapshot.get("effective_specs"),
        override_ip_count=req.ip_count,
        override_subnet_id=req.subnet_id,
        override_strategy=req.allocation_strategy,
        override_subnet_group_id=req.subnet_group_id,
    )
    try:
        assignments = auto_assign_proxy_ips(
            db,
            service,
            ip_count=ip_req.ip_count,
            subnet_id=ip_req.subnet_id,
            subnet_group_id=ip_req.subnet_group_id,
            strategy=ip_req.strategy,
            assigned_by=actor.assigned_by,
        )
    except ValueError as exc:
        ServiceDAO.delete(db, service.id)
        raise ProvisioningError("invalid_request", str(exc)) from exc
    except Exception as exc:
        ServiceDAO.delete(db, service.id)
        raise ProvisioningError("internal", str(exc), status_code=500) from exc
    ServiceDAO.update(db, service)
    log_server_activity_success(
        db,
        service_id=service.id,
        event_type=ServerActivityEventType.SERVICE,
        action="create",
        source=actor.source,
        message=(
            f"Created proxy service '{service.name}' "
            f"({len(assignments)}/{ip_req.ip_count} IP(s) assigned)"
        ),
        details={
            "service_id": service.id,
            **actor.details,
            "assigned_ip_count": len(assignments),
            "requested_ip_count": ip_req.ip_count,
        },
    )
    logger.info(
        "Provisioned proxy service '%s' (ID: %s) with %s/%s IP(s)",
        service.name,
        service.id,
        len(assignments),
        ip_req.ip_count,
    )
    return service
