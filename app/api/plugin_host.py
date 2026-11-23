"""HTTP API of the plugin system (docs/plugin-system.md sections 4, 5, 9 and 10).

Admin management lives under ``/admin/plugins``; service render documents and actions
are served to staff, clients and billing integrations from one implementation.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import get_current_user, require_admin
from app.core.billing_auth import get_billing_integration
from app.core.database import get_db
from app.models.billing_integration import BillingIntegration
from app.models.plugin_host import PluginDelivery, PluginInstallation, PluginLog, PluginSchedule
from app.models.service import Service
from app.plugin_host import lifecycle, outbox, runtime, service_links, settings_store, ui
from app.plugin_host.common import audit
from app.plugin_host.loader import discover

DbDep = Annotated[Session, Depends(get_db)]
AdminDep = Annotated[dict, Depends(require_admin)]
UserDep = Annotated[dict, Depends(get_current_user)]
BillingDep = Annotated[BillingIntegration, Depends(get_billing_integration)]

admin_router = APIRouter(prefix="/admin/plugins", tags=["plugins-admin"])
admin_service_router = APIRouter(prefix="/admin/services", tags=["plugins-admin"])
client_router = APIRouter(prefix="/client/services", tags=["client"])
billing_router = APIRouter(prefix="/billing", tags=["billing"])


class InstallBody(BaseModel):
    directory: str = Field(min_length=1, max_length=255)
    accept_permissions: list[str] = Field(default_factory=list)
    enable: bool = False


class UpgradeBody(BaseModel):
    accept_permissions: list[str] = Field(default_factory=list)


class UninstallBody(BaseModel):
    keep_data: bool = False


class SettingsBody(BaseModel):
    values: dict[str, Any]


class ServiceLinkBody(BaseModel):
    service_id: int
    enabled: Optional[bool] = None


class ServiceLinksBody(BaseModel):
    items: list[ServiceLinkBody] = Field(max_length=500)


class AddonRefsBody(BaseModel):
    addon_refs: list[str] = Field(max_length=50)


class ActionBody(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict)
    confirm: bool = False


class AddonBody(BaseModel):
    addon_ref: str
    status: Literal["active", "suspended", "terminated"]


def _admin_id(auth: dict) -> Optional[int]:
    value = auth.get("user_id")
    return int(value) if value else None


def _installation(db: Session, plugin_id: str) -> PluginInstallation:
    inst = db.get(PluginInstallation, plugin_id)
    if inst is None:
        raise HTTPException(status_code=404, detail="Plugin not installed")
    return inst


def _lifecycle_error(exc: lifecycle.LifecycleError) -> HTTPException:
    if isinstance(exc, lifecycle.PermissionsNotAccepted):
        return HTTPException(status_code=409, detail={"code": "permissions_not_accepted", "missing": exc.missing})
    return HTTPException(status_code=400, detail=str(exc))


def _iso(value) -> Optional[str]:
    from app.plugin_host.common import aware

    value = aware(value)
    return value.isoformat() if value else None


def _summary(db: Session, inst: PluginInstallation) -> dict:
    package = next((p for p in discover() if p.directory == inst.directory and not p.reserved), None)
    return lifecycle.summary(db, inst, package)


# ------------------------------------------------------------------ admin management


@admin_router.get("")
def list_plugins(_auth: AdminDep, db: DbDep) -> dict:
    return lifecycle.overview(db)


@admin_router.post("/install")
async def install_plugin(body: InstallBody, auth: AdminDep, db: DbDep) -> dict:
    try:
        inst = await lifecycle.install(
            db, body.directory, accept_permissions=body.accept_permissions, enable=body.enable,
            actor_user_id=_admin_id(auth),
        )
    except lifecycle.LifecycleError as exc:
        raise _lifecycle_error(exc) from exc
    return _summary(db, inst)


@admin_router.get("/{plugin_id}")
def get_plugin(plugin_id: str, _auth: AdminDep, db: DbDep) -> dict:
    return _summary(db, _installation(db, plugin_id))


@admin_router.post("/{plugin_id}/upgrade")
async def upgrade_plugin(plugin_id: str, body: UpgradeBody, auth: AdminDep, db: DbDep) -> dict:
    inst = _installation(db, plugin_id)
    try:
        await lifecycle.upgrade(db, inst, accept_permissions=body.accept_permissions, actor_user_id=_admin_id(auth))
    except lifecycle.LifecycleError as exc:
        raise _lifecycle_error(exc) from exc
    return _summary(db, inst)


@admin_router.post("/{plugin_id}/enable")
async def enable_plugin(plugin_id: str, auth: AdminDep, db: DbDep) -> dict:
    inst = _installation(db, plugin_id)
    try:
        await lifecycle.set_enabled(db, inst, True, actor_user_id=_admin_id(auth))
    except lifecycle.LifecycleError as exc:
        raise _lifecycle_error(exc) from exc
    return _summary(db, inst)


@admin_router.post("/{plugin_id}/disable")
async def disable_plugin(plugin_id: str, auth: AdminDep, db: DbDep) -> dict:
    inst = _installation(db, plugin_id)
    await lifecycle.set_enabled(db, inst, False, actor_user_id=_admin_id(auth))
    return _summary(db, inst)


@admin_router.post("/{plugin_id}/uninstall")
async def uninstall_plugin(plugin_id: str, body: UninstallBody, auth: AdminDep, db: DbDep) -> dict:
    inst = _installation(db, plugin_id)
    await lifecycle.uninstall(db, inst, keep_data=body.keep_data, actor_user_id=_admin_id(auth))
    return {"ok": True}


@admin_router.post("/{plugin_id}/purge-data")
def purge_plugin_data(plugin_id: str, auth: AdminDep, db: DbDep) -> dict:
    """Delete the settings, secrets, state and other rows an uninstall kept of a plugin that is no longer installed."""
    try:
        deleted = lifecycle.purge_data(db, plugin_id, actor_user_id=_admin_id(auth))
    except lifecycle.LifecycleError as exc:
        raise _lifecycle_error(exc) from exc
    return {"ok": True, "deleted": deleted}


@admin_router.post("/{plugin_id}/reset-health")
def reset_health(plugin_id: str, auth: AdminDep, db: DbDep) -> dict:
    from app.plugin_host import health

    inst = _installation(db, plugin_id)
    health.reset(db, inst)
    audit("plugin.health_reset", plugin_id=plugin_id, actor_user_id=_admin_id(auth), resource_type="plugin",
          resource_id=plugin_id, db=db)
    db.commit()
    return _summary(db, inst)


@admin_router.get("/{plugin_id}/settings")
def get_settings(plugin_id: str, _auth: AdminDep, db: DbDep) -> dict:
    inst = _installation(db, plugin_id)
    return settings_store.api_view(db, plugin_id, runtime.manifest_of(inst))


@admin_router.put("/{plugin_id}/settings")
def put_settings(plugin_id: str, body: SettingsBody, auth: AdminDep, db: DbDep) -> dict:
    inst = _installation(db, plugin_id)
    manifest = runtime.manifest_of(inst)
    try:
        changed = settings_store.update(db, plugin_id, manifest, body.values)
    except settings_store.SettingsError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if changed:
        audit("plugin.settings", plugin_id=plugin_id, actor_user_id=_admin_id(auth), resource_type="plugin",
              resource_id=plugin_id, metadata={"changed": changed}, db=db)
    db.commit()
    return settings_store.api_view(db, plugin_id, manifest)


@admin_router.get("/{plugin_id}/logs")
def get_logs(
    plugin_id: str,
    _auth: AdminDep,
    db: DbDep,
    level: Optional[str] = Query(None),
    service_id: Optional[int] = Query(None),
    limit: int = Query(200, ge=1, le=1000),
) -> dict:
    _installation(db, plugin_id)
    query = select(PluginLog).where(PluginLog.plugin_id == plugin_id)
    if level:
        query = query.where(PluginLog.level == level)
    if service_id is not None:
        query = query.where(PluginLog.service_id == service_id)
    rows = db.execute(query.order_by(PluginLog.id.desc()).limit(limit)).scalars()
    return {
        "items": [
            {"id": r.id, "level": r.level, "message": r.message, "service_id": r.service_id, "context": r.context,
             "created_at": _iso(r.created_at)}
            for r in rows
        ]
    }


@admin_router.get("/{plugin_id}/deliveries")
def get_deliveries(
    plugin_id: str,
    _auth: AdminDep,
    db: DbDep,
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(200, ge=1, le=1000),
) -> dict:
    _installation(db, plugin_id)
    query = select(PluginDelivery).where(PluginDelivery.plugin_id == plugin_id)
    if status_filter:
        query = query.where(PluginDelivery.status == status_filter)
    rows = db.execute(query.order_by(PluginDelivery.id.desc()).limit(limit)).scalars()
    return {
        "items": [
            {"id": r.id, "event": r.event, "status": r.status, "attempts": r.attempts, "last_error": r.last_error,
             "service_id": r.service_id, "created_at": _iso(r.created_at), "next_attempt_at": _iso(r.next_attempt_at),
             "delivered_at": _iso(r.delivered_at)}
            for r in rows
        ]
    }


@admin_router.post("/{plugin_id}/deliveries/{delivery_id}/retry")
def retry_delivery(plugin_id: str, delivery_id: int, auth: AdminDep, db: DbDep) -> dict:
    _installation(db, plugin_id)
    if not outbox.retry(db, plugin_id, delivery_id):
        raise HTTPException(status_code=404, detail="No failed or dead delivery with that id")
    audit("plugin.delivery_retry", plugin_id=plugin_id, actor_user_id=_admin_id(auth), resource_type="plugin",
          resource_id=plugin_id, metadata={"delivery_id": delivery_id}, db=db)
    db.commit()
    return {"ok": True}


@admin_router.get("/{plugin_id}/schedules")
def get_schedules(plugin_id: str, _auth: AdminDep, db: DbDep, limit: int = Query(500, ge=1, le=5000)) -> dict:
    _installation(db, plugin_id)
    rows = db.execute(
        select(PluginSchedule).where(PluginSchedule.plugin_id == plugin_id).order_by(PluginSchedule.next_run_at).limit(limit)
    ).scalars()
    return {
        "items": [
            {"schedule": r.schedule, "service_id": r.service_id, "next_run_at": _iso(r.next_run_at),
             "last_run_at": _iso(r.last_run_at), "last_status": r.last_status, "last_error": r.last_error}
            for r in rows
        ]
    }


def _service_type(service: Service) -> str:
    kind = service.service_type
    return kind.value if hasattr(kind, "value") else str(kind)


@admin_router.get("/{plugin_id}/services")
def get_service_links(plugin_id: str, _auth: AdminDep, db: DbDep, limit: int = Query(500, ge=1, le=5000)) -> dict:
    inst = _installation(db, plugin_id)
    manifest = runtime.manifest_of(inst)
    if manifest.services is None:
        return {"items": []}
    from app.models.plugin_host import PluginServiceLink
    from app.models.service import ServiceStatus, ServiceType

    links = {
        link.service_id: link
        for link in db.execute(select(PluginServiceLink).where(PluginServiceLink.plugin_id == plugin_id)).scalars()
    }
    services = db.execute(
        select(Service)
        .where(
            Service.service_type.in_([ServiceType(t) for t in manifest.services.types]),
            Service.status != ServiceStatus.TERMINATED,
        )
        .order_by(Service.id)
        .limit(limit)
    ).scalars()
    items = []
    for service in services:
        link = links.get(service.id)
        items.append(
            {
                "service_id": service.id,
                "service_name": service.name,
                "service_type": _service_type(service),
                "status": service.status.value if hasattr(service.status, "value") else service.status,
                "enabled": link.enabled if link else None,
                "source": link.source if link else None,
                "addon_ref": link.addon_ref if link else None,
                "active": service_links.is_active(db, plugin_id, manifest, service),
            }
        )
    return {"items": items}


@admin_router.put("/{plugin_id}/services")
def put_service_links(plugin_id: str, body: ServiceLinksBody, auth: AdminDep, db: DbDep) -> dict:
    inst = _installation(db, plugin_id)
    manifest = runtime.manifest_of(inst)
    if manifest.services is None:
        raise HTTPException(status_code=400, detail="This plugin has no per-service features")
    for item in body.items:
        service = db.get(Service, item.service_id)
        if service is None or not service_links.applies_to_type(manifest, service):
            raise HTTPException(status_code=400, detail=f"Service {item.service_id} is not a type this plugin applies to")
        service_links.set_admin_link(db, plugin_id, service.id, item.enabled)
    audit("plugin.service_link", plugin_id=plugin_id, actor_user_id=_admin_id(auth), resource_type="plugin",
          resource_id=plugin_id, metadata={"items": [i.model_dump() for i in body.items][:100]}, db=db)
    db.commit()
    return get_service_links(plugin_id, auth, db, limit=500)


@admin_router.put("/{plugin_id}/addon-refs")
def put_addon_refs(plugin_id: str, body: AddonRefsBody, auth: AdminDep, db: DbDep) -> dict:
    inst = _installation(db, plugin_id)
    try:
        refs = service_links.normalise_addon_refs(body.addon_refs)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    inst.addon_refs = refs
    audit("plugin.addon_refs", plugin_id=plugin_id, actor_user_id=_admin_id(auth), resource_type="plugin",
          resource_id=plugin_id, metadata={"addon_refs": refs}, db=db)
    db.commit()
    return _summary(db, inst)


# ------------------------------------------------------------------ service render documents and actions


def _run_request_error(exc: ui.PluginRequestError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


async def _action(db: Session, service: Service, plugin_id: str, action_id: str, body: ActionBody, actor: dict) -> dict:
    try:
        return await ui.run_action(db, service, plugin_id, action_id, params=body.params, confirm=body.confirm,
                                   actor=actor)
    except ui.PluginRequestError as exc:
        raise _run_request_error(exc) from exc


def _service_or_404(db: Session, service_id: int) -> Service:
    service = db.get(Service, service_id)
    if service is None:
        raise HTTPException(status_code=404, detail="Service not found")
    return service


@admin_service_router.get("/{service_id}/plugin-ui")
def admin_plugin_ui(service_id: int, _auth: AdminDep, db: DbDep) -> dict:
    return ui.render(db, _service_or_404(db, service_id), "admin")


@admin_service_router.post("/{service_id}/plugin-actions/{plugin_id}/{action_id}")
async def admin_plugin_action(service_id: int, plugin_id: str, action_id: str, body: ActionBody, auth: AdminDep,
                              db: DbDep) -> dict:
    actor = {"type": "staff", "user_id": _admin_id(auth), "via": "admin"}
    return await _action(db, _service_or_404(db, service_id), plugin_id, action_id, body, actor)


def _client_service(db: Session, auth: dict, service_id: int) -> tuple[Service, dict]:
    from app.api.client import _require_non_admin_client

    _require_non_admin_client(auth)
    user_id = auth.get("user_id")
    if not user_id or auth.get("type") == "api_key":
        raise HTTPException(status_code=403, detail="A user session is required")
    service = db.get(Service, service_id)
    if service is None or service.owner_user_id != int(user_id):
        raise HTTPException(status_code=404, detail="Service not found")
    actor = {"type": "client", "user_id": int(user_id), "via": "portal"}
    if auth.get("impersonated_by"):
        actor["impersonated_by"] = auth.get("impersonated_by")
    return service, actor


@client_router.get("/{service_id}/plugin-ui")
def client_plugin_ui(service_id: int, auth: UserDep, db: DbDep) -> dict:
    service, _actor = _client_service(db, auth, service_id)
    return ui.render(db, service, "client")


@client_router.post("/{service_id}/plugin-actions/{plugin_id}/{action_id}")
async def client_plugin_action(service_id: int, plugin_id: str, action_id: str, body: ActionBody, auth: UserDep,
                               db: DbDep) -> dict:
    service, actor = _client_service(db, auth, service_id)
    return await _action(db, service, plugin_id, action_id, body, actor)


def _billing_service(db: Session, integration: BillingIntegration, service_id: int) -> Service:
    from app.api.billing import _assert_billing_owned_service

    service = _service_or_404(db, service_id)
    _assert_billing_owned_service(service, integration)
    return service


def _billing_actor(service: Service, integration: BillingIntegration, audience: str) -> dict:
    if audience == "client":
        return {"type": "client", "user_id": service.owner_user_id, "via": "billing", "integration_id": integration.id}
    return {"type": "staff", "user_id": None, "via": "billing", "integration_id": integration.id}


@billing_router.get("/services/{service_id}/plugin-ui")
def billing_plugin_ui(
    service_id: int,
    integration: BillingDep,
    db: DbDep,
    audience: Literal["client", "admin"] = Query("client"),
) -> dict:
    return ui.render(db, _billing_service(db, integration, service_id), audience)


@billing_router.post("/services/{service_id}/plugin-actions/{plugin_id}/{action_id}")
async def billing_plugin_action(
    service_id: int,
    plugin_id: str,
    action_id: str,
    body: ActionBody,
    integration: BillingDep,
    db: DbDep,
    audience: Literal["client", "admin"] = Query("client"),
) -> dict:
    service = _billing_service(db, integration, service_id)
    return await _action(db, service, plugin_id, action_id, body, _billing_actor(service, integration, audience))


@billing_router.post("/services/{service_id}/addons", status_code=status.HTTP_200_OK)
def billing_addon_status(service_id: int, body: AddonBody, integration: BillingDep, db: DbDep) -> dict:
    service = _billing_service(db, integration, service_id)
    try:
        affected = service_links.apply_addon(db, service, body.addon_ref, body.status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    for item in affected:
        audit("plugin.service_link", plugin_id=item["plugin_id"], resource_type="service", resource_id=str(service.id),
              metadata={"addon_ref": body.addon_ref, "status": body.status, "enabled": item["enabled"],
                        "changed": item["changed"], "integration_id": integration.id}, db=db)
    db.commit()
    return {"service_id": service.id, "addon_ref": body.addon_ref, "status": body.status, "plugins": affected}
