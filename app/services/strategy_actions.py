"""Resolve and execute strategy-declared runtime actions for VM services."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from sqlalchemy.orm import Session

from app.dao.product_catalog_dao import VMTemplateDAO
from app.dao.service_dao import ServiceDAO
from app.models.service import Service, ServiceType
from app.services.client_permission_resolver import resolve_client_permissions
from app.services.deployment.guest_config import (
    apply_cloudinit_network_reset,
    apply_opencore_smbios,
    configure_linux_network,
    configure_macos_network,
    configure_windows_network,
    old_guest_passwords_from_ctx,
    reboot_guest_and_wait_agent,
    set_guest_password,
    strategy_options_from_ctx,
)
from app.services.deployment.registry import get_deployment_strategy_registry
from app.services.proxmox_placement import ProxmoxPlacementError, resolve_proxmox_plugin_for_service
from app.services.vm_install_type_strategy import merge_strategy_options
from app.services.vm_strategy_executor import (
    enqueue_apply_guest_password_job,
    resolve_vm_strategy_name_for_service,
)

logger = logging.getLogger(__name__)

Audience = Literal["admin", "client"]

# Stay well under WHMCS CURLOPT_TIMEOUT (45s for change_password).
_SYNC_PASSWORD_WAIT_SECONDS = 8.0
_SYNC_PASSWORD_POLL_INTERVAL = 1.0


class StrategyActionError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _strategy_for_service(db: Session, service: Service):
    name = resolve_vm_strategy_name_for_service(db, service)
    if not name or name == "stub":
        raise StrategyActionError("Service has no deployment strategy", 409)
    strategy = get_deployment_strategy_registry().resolve(name)
    if not strategy:
        raise StrategyActionError(f"Unknown strategy '{name}'", 409)
    return strategy


def _template_options(db: Session, service: Service) -> Dict[str, Any]:
    vm = service.vm
    if vm and vm.vm_template_id:
        tmpl = VMTemplateDAO.get_by_id(db, vm.vm_template_id)
        if tmpl:
            return merge_strategy_options(tmpl.os_type, tmpl.strategy_options or {})
    return strategy_options_from_ctx(_ActionCtx(db, service))


def _action_visible_for_audience(
    action,
    audience: Audience,
    *,
    client_allow: set,
    perms: dict,
) -> bool:
    if audience == "admin":
        return bool(action.admin)
    if not action.client_eligible or action.name not in client_allow:
        return False
    if action.permission_key and not perms.get(action.permission_key, False):
        return False
    return True


def list_actions(db: Session, service: Service, audience: Audience) -> List[Dict[str, Any]]:
    if service.service_type != ServiceType.VM:
        return []
    try:
        strategy = _strategy_for_service(db, service)
    except StrategyActionError:
        return []
    opts = _template_options(db, service)
    client_allow = set(opts.get("client_actions") or [])
    perms = resolve_client_permissions(db, service) if audience == "client" else {}
    out: List[Dict[str, Any]] = []
    for action in strategy.actions():
        if not _action_visible_for_audience(
            action, audience, client_allow=client_allow, perms=perms
        ):
            continue
        item = action.to_dict()
        item["enabled"] = True
        item["audience"] = audience
        out.append(item)
    return out


async def _plugin_for_service(db: Session, service: Service):
    try:
        plugin, _cid, _node, _vmid = await resolve_proxmox_plugin_for_service(db, service)
    except ProxmoxPlacementError as exc:
        raise StrategyActionError(str(exc), exc.status_code) from exc
    return plugin


class _ActionCtx:
    """Minimal context compatible with strategy_options_from_ctx / get_ip_allocation."""

    def __init__(self, db: Session, service: Service):
        self.db = db
        self.service = service
        self.logger = logger

    @property
    def vm(self):
        return self.service.vm

    def get_specs(self) -> Dict[str, Any]:
        return ((self.service.config or {}).get("vm_plan") or {}).get("effective_specs") or {}

    def get_ip_allocation(self):
        from app.dao.vm_ip_allocation_dao import VMIPAllocationDAO

        vm = self.vm
        if vm and vm.vm_ip_allocation_id:
            return VMIPAllocationDAO.get_by_id(self.db, vm.vm_ip_allocation_id)
        return None

    async def get_plugin(self):
        return await _plugin_for_service(self.db, self.service)


def _set_guest_password_apply(service: Service, **fields: Any) -> None:
    cfg = dict(service.config or {})
    summary = dict(cfg.get("guest_password_apply") or {})
    summary.update(fields)
    summary["updated_at"] = datetime.now(timezone.utc).isoformat()
    cfg["guest_password_apply"] = summary
    service.config = cfg


def _persist_desired_guest_password(service: Service, password: str) -> list[str]:
    """Write desired password to template_parameters; return prior-password candidates."""
    cfg = dict(service.config or {})
    tpl = dict(cfg.get("template_parameters") or {})
    previous_candidates: list[str] = []
    for key in ("admin_password", "guest_password", "previous_admin_password"):
        value = tpl.get(key)
        if value and str(value) != str(password) and str(value) not in previous_candidates:
            previous_candidates.append(str(value))
    if previous_candidates:
        tpl["previous_admin_password"] = previous_candidates[0]
    tpl["admin_password"] = str(password)
    tpl["guest_password"] = str(password)
    cfg["template_parameters"] = tpl
    service.config = cfg
    return previous_candidates


async def _try_sync_guest_password(
    plugin,
    username: str,
    password: str,
    old_passwords: list,
) -> bool:
    """Return True if the guest password was applied within the short sync window."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _SYNC_PASSWORD_WAIT_SECONDS
    while True:
        try:
            ready = await plugin.guest_agent_ready()
        except Exception as exc:
            logger.debug("guest_agent_ready during sync password wait: %s", exc)
            ready = False
        if ready:
            try:
                await set_guest_password(
                    plugin,
                    username,
                    password,
                    old_passwords=old_passwords,
                )
                return True
            except RuntimeError:
                # Password logic / Secure Token rejection — not deferrable.
                raise
            except Exception as exc:
                logger.warning(
                    "Sync guest password apply failed with agent up; deferring: %s",
                    exc,
                )
                return False
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(_SYNC_PASSWORD_POLL_INTERVAL)


async def _run_change_password(
    db: Session,
    service: Service,
    ctx: _ActionCtx,
    plugin,
    params: Dict[str, Any],
    opts: Dict[str, Any],
) -> Dict[str, Any]:
    username = str(params.get("username") or opts.get("guest_username") or "client")
    password = params.get("password")
    if not password:
        raise StrategyActionError("password is required", 400)

    previous = _persist_desired_guest_password(service, str(password))
    _set_guest_password_apply(
        service, status="pending", username=username, error=None, job_id=None
    )
    ServiceDAO.update(db, service)

    old_passwords = list(previous)
    for candidate in old_guest_passwords_from_ctx(ctx, include_stored=False):
        if candidate not in old_passwords and candidate != str(password):
            old_passwords.append(candidate)

    try:
        applied = await _try_sync_guest_password(plugin, username, str(password), old_passwords)
    except RuntimeError as exc:
        _set_guest_password_apply(service, status="failed", error=str(exc))
        ServiceDAO.update(db, service)
        raise StrategyActionError(str(exc), 502) from exc

    if applied:
        _set_guest_password_apply(
            service, status="applied", username=username, error=None, job_id=None
        )
        ServiceDAO.update(db, service)
        return {
            "status": "ok",
            "action": "change_password",
            "username": username,
            "applied": True,
            "pending": False,
        }

    try:
        job = enqueue_apply_guest_password_job(db, service)
    except ValueError as exc:
        raise StrategyActionError(str(exc), 409) from exc

    _set_guest_password_apply(
        service,
        status="pending",
        username=username,
        error=None,
        job_id=job.id if job else None,
        deferred=True,
    )
    ServiceDAO.update(db, service)
    return {
        "status": "ok",
        "action": "change_password",
        "username": username,
        "applied": False,
        "pending": True,
        "job_id": job.id if job else None,
    }


async def _run_reset_network(
    strategy,
    plugin,
    ctx: _ActionCtx,
    opts: Dict[str, Any],
) -> Dict[str, Any]:
    mode = str(opts.get("network_mode") or "static").lower()
    alloc = ctx.get_ip_allocation()
    if strategy.name == "cloudinit_clone":
        if mode == "static" and not alloc:
            raise StrategyActionError("Static network requires a linked VM IP allocation", 409)
        await apply_cloudinit_network_reset(plugin, alloc, mode=mode)
        return {
            "status": "ok",
            "action": "reset_network",
            "network_mode": mode,
            "via": "cloudinit",
        }
    if strategy.name == "macos_guest_agent":
        await configure_macos_network(plugin, mode=mode, alloc=alloc)
    elif strategy.name == "windows_guest_agent":
        await configure_windows_network(plugin, mode=mode, alloc=alloc)
    else:
        await configure_linux_network(plugin, mode=mode, alloc=alloc)
    return {"status": "ok", "action": "reset_network", "network_mode": mode}


async def _run_randomize_smbios(
    strategy,
    plugin,
    ctx: _ActionCtx,
    opts: Dict[str, Any],
) -> Dict[str, Any]:
    if strategy.name != "macos_guest_agent":
        raise StrategyActionError("randomize_smbios is only supported for macOS guests", 400)
    from app.services.vm_identity_stamp import stamp_vm_identity

    sm = await apply_opencore_smbios(
        plugin,
        model=str(opts.get("smbios_model") or "iMacPro1,1"),
        oc_disk=str(opts.get("opencore_disk") or "disk1s1"),
        cfg_path=str(opts.get("opencore_config") or "/Volumes/OPENCORE/EFI/OC/config.plist"),
    )
    await stamp_vm_identity(ctx.db, ctx.service, plugin)
    await reboot_guest_and_wait_agent(plugin, max_wait=600.0)
    return {"status": "ok", "action": "randomize_smbios", "smbios": sm}


async def run_action(
    db: Session,
    service: Service,
    action_name: str,
    params: Optional[Dict[str, Any]],
    audience: Audience,
) -> Dict[str, Any]:
    if service.service_type != ServiceType.VM:
        raise StrategyActionError("Strategy actions are only available for VM services", 400)
    strategy = _strategy_for_service(db, service)
    action = strategy.action_by_name(action_name)
    if not action:
        raise StrategyActionError(f"Unknown action '{action_name}'", 404)

    available = {a["name"] for a in list_actions(db, service, audience)}
    if action_name not in available:
        raise StrategyActionError(f"Action '{action_name}' is not available", 403)

    params = params or {}
    ctx = _ActionCtx(db, service)
    opts = strategy_options_from_ctx(ctx)
    plugin = await _async_plugin(ctx)

    if action_name == "change_password":
        return await _run_change_password(db, service, ctx, plugin, params, opts)
    if action_name == "reset_network":
        return await _run_reset_network(strategy, plugin, ctx, opts)
    if action_name == "randomize_smbios":
        return await _run_randomize_smbios(strategy, plugin, ctx, opts)
    raise StrategyActionError(f"Action '{action_name}' has no handler", 501)


async def _async_plugin(ctx: _ActionCtx):
    return await ctx.get_plugin()
