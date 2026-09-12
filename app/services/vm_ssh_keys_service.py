"""Persist and apply SSH public keys for VM services."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Union

from sqlalchemy.orm import Session

from app.dao.product_catalog_dao import ProductDAO, VMTemplateDAO
from app.dao.service_dao import ServiceDAO
from app.models.service import Service, ServiceType
from app.services.deployment.guest_config import apply_root_authorized_keys
from app.services.proxmox_placement import ProxmoxPlacementError, resolve_proxmox_plugin_for_service
from app.services.ssh_public_keys import (
    SshPublicKeyError,
    parse_ssh_public_keys,
    service_accepts_ssh_key,
    set_ssh_public_keys_on_service,
    ssh_public_keys_from_service_config,
)
from app.services.service_product_snapshot import build_product_snapshot

logger = logging.getLogger(__name__)


class VmSshKeysError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


async def save_and_apply_ssh_public_keys(
    db: Session,
    service: Service,
    keys_input: str | List[str] | None,
    *,
    apply_live: bool = True,
) -> Dict[str, Any]:
    """Validate, store keys, and best-effort apply via guest agent."""
    if service.service_type != ServiceType.VM:
        raise VmSshKeysError("SSH keys are only supported for VM services", 400)
    if not service_accepts_ssh_key(db, service):
        raise VmSshKeysError("Current OS strategy does not accept SSH keys", 409)
    try:
        keys = parse_ssh_public_keys(keys_input)
    except SshPublicKeyError as exc:
        raise VmSshKeysError(str(exc), 400) from exc

    set_ssh_public_keys_on_service(service, keys)
    ServiceDAO.update(db, service)

    applied = False
    apply_error = None
    if apply_live:
        try:
            applied = await _try_apply_authorized_keys(db, service, keys)
        except Exception as exc:
            apply_error = str(exc)
            logger.warning(
                "Live SSH key apply failed for service %s (keys stored): %s",
                service.id,
                exc,
            )

    return {
        "status": "ok",
        "ssh_public_keys": keys,
        "ssh_public_keys_text": "\n".join(keys),
        "has_ssh_public_keys": bool(keys),
        "applied": applied,
        "apply_error": apply_error,
    }


async def _try_apply_authorized_keys(db: Session, service: Service, keys: List[str]) -> bool:
    try:
        plugin, _cid, _node, _vmid = await resolve_proxmox_plugin_for_service(db, service)
    except ProxmoxPlacementError:
        return False
    try:
        ready = await plugin.guest_agent_ready()
    except Exception:
        ready = False
    if not ready:
        return False
    await apply_root_authorized_keys(plugin, keys)
    return True


def _validate_template_for_product(
    db: Session, service: Service, vm_template_id: int, tmpl
) -> None:
    if not service.product_code:
        return
    product = ProductDAO.get_by_code(db, service.product_code)
    if not product:
        return
    allowed = {
        m.vm_template_id
        for m in (product.vm_template_mappings or [])
        if m.vm_template_id is not None
    }
    if allowed and int(vm_template_id) not in allowed:
        raise VmSshKeysError("VM template is not linked to this product", 400)


def _apply_vm_template_snapshot(
    db: Session, service: Service, tmpl, vm_template_id: int
) -> None:
    try:
        snapshot, os_code = build_product_snapshot(
            db,
            service.product_code,
            None,
            ServiceType.VM,
            vm_template_id=int(vm_template_id),
        )
    except ValueError as exc:
        raise VmSshKeysError(str(exc), 400) from exc
    service.product_snapshot = snapshot
    service.os_code = os_code
    cfg = dict(service.config or {})
    cfg["product_snapshot"] = snapshot
    vm_plan = dict(cfg.get("vm_plan") or {})
    if isinstance(snapshot, dict):
        vm_plan["vm_template"] = {
            "id": tmpl.id,
            "name": tmpl.name,
            "os_type": tmpl.os_type,
            "proxmox_template_name": tmpl.proxmox_template_name,
            "strategy_options": tmpl.strategy_options or {},
        }
        from app.services.vm_install_type_strategy import resolve_vm_template_strategy

        try:
            strat = resolve_vm_template_strategy(tmpl.os_type)
            vm_plan["strategy_name"] = strat["strategy_name"]
            vm_plan["strategy_plan"] = {
                "mode": strat["strategy_name"],
                "strategy_config": strat["strategy_config"],
            }
        except ValueError:
            pass
    cfg["vm_plan"] = vm_plan
    service.config = cfg


def _apply_reinstall_ssh_keys(
    db: Session,
    service: Service,
    ssh_public_keys: str | List[str] | None,
    vm_template_id: Optional[int],
) -> None:
    if ssh_public_keys is None:
        return
    if not service_accepts_ssh_key(db, service, target_template_id=vm_template_id):
        return
    try:
        keys = parse_ssh_public_keys(ssh_public_keys)
    except SshPublicKeyError as exc:
        raise VmSshKeysError(str(exc), 400) from exc
    set_ssh_public_keys_on_service(service, keys)


def apply_template_change_for_reinstall(
    db: Session,
    service: Service,
    *,
    vm_template_id: Optional[int] = None,
    ssh_public_keys: str | List[str] | None = None,
) -> Service:
    """Update template / SSH keys on the service before destroy+reprovision."""
    if vm_template_id is not None:
        tmpl = VMTemplateDAO.get_by_id(db, int(vm_template_id))
        if not tmpl or not tmpl.enabled:
            raise VmSshKeysError("VM template not found", 404)
        _validate_template_for_product(db, service, int(vm_template_id), tmpl)
        if service.product_code:
            _apply_vm_template_snapshot(db, service, tmpl, int(vm_template_id))
        if service.vm:
            service.vm.vm_template_id = int(vm_template_id)

    _apply_reinstall_ssh_keys(db, service, ssh_public_keys, vm_template_id)

    ServiceDAO.update(db, service)
    db.refresh(service)
    return service


def stored_keys_for_service(service: Service) -> List[str]:
    return ssh_public_keys_from_service_config(
        service.config if isinstance(service.config, dict) else {}
    )


def list_reinstall_templates_for_service(db: Session, service: Service) -> List[Dict[str, Any]]:
    """Catalog VM templates linked to the service product (for reinstall OS picker)."""
    from app.services.ssh_public_keys import os_type_accepts_ssh_key
    from app.services.vm_install_type_strategy import resolve_vm_template_strategy

    if not service.product_code:
        return []
    product = ProductDAO.get_by_code(db, service.product_code)
    if not product:
        return []
    rows: List[Dict[str, Any]] = []
    for mapping in product.vm_template_mappings or []:
        tmpl = mapping.vm_template
        if tmpl is None or not tmpl.enabled:
            continue
        strategy_name = None
        try:
            strategy_name = resolve_vm_template_strategy(tmpl.os_type).get("strategy_name")
        except ValueError:
            strategy_name = None
        rows.append(
            {
                "id": tmpl.id,
                "name": tmpl.name,
                "os_type": tmpl.os_type,
                "strategy_name": strategy_name,
                "accepts_ssh_key": os_type_accepts_ssh_key(tmpl.os_type),
            }
        )
    rows.sort(key=lambda r: (r.get("name") or "").lower())
    return rows
