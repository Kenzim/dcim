"""Bare-metal service creation: pooled server or explicit server_id."""
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.dao.server_dao import ServerDAO
from app.dao.server_group_dao import ServerGroupDAO
from app.dao.service_dao import ServiceDAO
from app.models.server import Server
from app.models.server_activity import ServerActivityEventType
from app.models.service import Service, ServiceStatus, ServiceType
from app.services.provisioning.errors import ProvisioningError
from app.services.provisioning.request import ProvisionRequest, ProvisioningActor
from app.services.server_activity_logger import (
    log_server_activity_attempt,
    log_server_activity_failure,
    log_server_activity_success,
)

logger = logging.getLogger(__name__)


def select_free_server_in_group(db: Session, group_id: int) -> Server:
    """Pick the lowest-id enabled server in the group that has no live service."""
    group = ServerGroupDAO.get_by_id(db, group_id)
    if not group:
        raise ProvisioningError("not_found", "Server group not found")
    if not group.servers:
        raise ProvisioningError("not_found", "No servers in this server group")

    candidates: list[Server] = []
    for server in group.servers:
        if not server.enabled:
            continue
        services = ServiceDAO.get_by_server(db, server.id)
        has_active = any(s.status != ServiceStatus.TERMINATED for s in services)
        if not has_active:
            candidates.append(server)

    if not candidates:
        raise ProvisioningError("no_free_server", "No free servers available in this group")
    candidates.sort(key=lambda s: s.id)
    return candidates[0]


def determine_template_for_group(
    db: Session,
    group_id: int,
    explicit_template_id: Optional[str],
) -> str:
    group = ServerGroupDAO.get_by_id(db, group_id)
    if not group:
        raise ProvisioningError("not_found", "Server group not found")

    group_name = getattr(group, "name", None) or str(group_id)
    if not (getattr(group, "enable_os_templates", False) or False):
        raise ProvisioningError(
            "invalid_request",
            f"OS templates are not enabled for server group '{group_name}'",
        )

    permitted = [str(tid) for tid in (group.permitted_os_templates or [])]

    if explicit_template_id:
        if permitted and explicit_template_id not in permitted:
            raise ProvisioningError(
                "invalid_request",
                (
                    f"Template '{explicit_template_id}' is not permitted for "
                    f"server group '{group_name}'"
                ),
            )
        return explicit_template_id

    if permitted and len(permitted) == 1:
        return permitted[0]

    permitted_list = ", ".join(permitted) if permitted else "(none)"
    raise ProvisioningError(
        "invalid_request",
        (
            f"OS template must be specified for server group '{group_name}'. "
            f"Permitted templates: {permitted_list}"
        ),
    )


def _queue_template_install(
    db: Session,
    service: Service,
    template_id: str,
    template_parameters: dict | None,
):
    from fastapi import HTTPException

    from app.api.billing import _queue_template_install_for_service

    try:
        return _queue_template_install_for_service(
            db=db,
            service=service,
            template_id=template_id,
            template_parameters=template_parameters,
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        if exc.status_code == 409:
            code = "conflict"
        elif exc.status_code == 404:
            code = "not_found"
        elif exc.status_code == 400:
            code = "invalid_request"
        else:
            code = "internal"
        raise ProvisioningError(code, detail, status_code=exc.status_code) from exc


def queue_group_template_install(
    db: Session,
    *,
    service: Service,
    server: Server,
    actor: ProvisioningActor,
    server_group_id: int,
    service_config: dict,
) -> None:
    explicit_template_id = service_config.get("template_id")
    template_params = service_config.get("template_parameters") or {}
    template_id = determine_template_for_group(db, server_group_id, explicit_template_id)
    log_server_activity_attempt(
        db,
        server_id=server.id,
        event_type=ServerActivityEventType.INSTALL,
        action="queue_template_install",
        source=actor.source,
        message=f"Queueing template install '{template_id}'",
        details={
            "service_id": service.id,
            "template_id": template_id,
            **actor.details,
        },
    )
    try:
        _, installation_task = _queue_template_install(
            db, service, template_id, template_params
        )
    except Exception as exc:
        log_server_activity_failure(
            db,
            server_id=server.id,
            event_type=ServerActivityEventType.INSTALL,
            action="queue_template_install",
            source=actor.source,
            message=f"Failed to queue template install '{template_id}'",
            details={
                "service_id": service.id,
                "template_id": template_id,
                **actor.details,
            },
            error=exc,
        )
        log_server_activity_failure(
            db,
            server_id=server.id,
            event_type=ServerActivityEventType.SERVICE,
            action="create",
            source=actor.source,
            message=f"Service '{service.name}' provisioning failed",
            details={"service_id": service.id, **actor.details},
            error=exc,
        )
        raise
    log_server_activity_success(
        db,
        server_id=server.id,
        event_type=ServerActivityEventType.INSTALL,
        action="queue_template_install",
        source=actor.source,
        message=f"Queued template install '{template_id}'",
        details={
            "service_id": service.id,
            "template_id": template_id,
            "installation_task_id": installation_task.id,
            "boot_task_id": installation_task.boot_task_id,
        },
    )


def _pin_existing_server(db: Session, server_id: int) -> Server:
    server = ServerDAO.get_by_id(db, server_id)
    if not server:
        raise ProvisioningError("not_found", "Server not found")
    existing = ServiceDAO.get_by_server(db, server_id)
    if any(s.status != ServiceStatus.TERMINATED for s in existing):
        raise ProvisioningError("conflict", "Server is already assigned to a service")
    return server


def create_bare_metal_service(
    db: Session,
    req: ProvisionRequest,
    actor: ProvisioningActor,
    product_snapshot: dict[str, Any],
) -> Service:
    if req.server_id is not None:
        server = _pin_existing_server(db, int(req.server_id))
        group_id = req.server_group_id
    elif req.server_group_id is not None:
        server = select_free_server_in_group(db, int(req.server_group_id))
        group_id = int(req.server_group_id)
    else:
        raise ProvisioningError(
            "invalid_request",
            "bare_metal requires service_config.server_group_id or server_id",
        )

    service_config = dict(req.service_config or {})
    if group_id is not None:
        service_config.setdefault("server_group_id", group_id)
    if req.template_id:
        service_config["template_id"] = req.template_id
    if req.template_parameters:
        service_config["template_parameters"] = req.template_parameters

    service = ServiceDAO.create_bare_metal(
        db,
        name=req.name,
        server_id=server.id,
        owner_user_id=req.owner_user_id,
        external_service_id=req.external_service_id,
        service_type=ServiceType.BARE_METAL,
        status=ServiceStatus.PENDING,
        description=req.description,
        config=service_config,
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
        server_id=server.id,
        event_type=ServerActivityEventType.SERVICE,
        action="create",
        source=actor.source,
        message=f"Creating service '{service.name}'",
        details={"service_id": service.id, **actor.details, "server_group_id": group_id},
    )
    if group_id is not None:
        queue_group_template_install(
            db,
            service=service,
            server=server,
            actor=actor,
            server_group_id=group_id,
            service_config=service_config,
        )
    log_server_activity_success(
        db,
        server_id=server.id,
        event_type=ServerActivityEventType.SERVICE,
        action="create",
        source=actor.source,
        message=f"Created service '{service.name}'",
        details={"service_id": service.id, **actor.details, "server_group_id": group_id},
    )
    logger.info(
        "Provisioned bare-metal service '%s' (ID: %s) on server %s (group %s)",
        service.name,
        service.id,
        server.id,
        group_id,
    )
    return service
