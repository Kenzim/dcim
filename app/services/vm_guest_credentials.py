"""Resolve guest OS username/password stored on a VM service.

Passwords typically arrive via WHMCS ``template_parameters.admin_password``
or strategy/cloud-init options written into ``service.config`` at provision
time. Used by the console viewer to offer reveal / copy / type-password
controls — the browser already has console access for that VM via the
session token, so exposing the guest password alongside it is intentional.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from app.models.service import Service


def _dig(cfg: Any, *path: str) -> Any:
    node = cfg
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def get_service_guest_credentials(service: Service) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(username, password)`` for the guest OS, or ``(None, None)``.

    Prefers WHMCS/template password fields, then strategy/cloud-init options
    nested under ``vm_plan`` / ``product_snapshot`` / provision specs.
    """
    cfg: Dict[str, Any] = service.config if isinstance(service.config, dict) else {}
    username: Optional[str] = None
    password: Optional[str] = None

    tpl = cfg.get("template_parameters") or {}
    if isinstance(tpl, dict):
        password = (
            tpl.get("admin_password")
            or tpl.get("guest_password")
            or tpl.get("password")
            or password
        )
        username = (
            tpl.get("admin_username")
            or tpl.get("guest_username")
            or tpl.get("ciuser")
            or username
        )

    for path in (
        ("vm_plan", "strategy_plan", "strategy_config"),
        ("vm_plan", "os_profile", "strategy_config"),
        ("vm_plan", "vm_template", "strategy_options"),
        ("product_snapshot", "os_profile", "strategy_config"),
    ):
        node = _dig(cfg, *path)
        if isinstance(node, dict):
            username = username or node.get("guest_username") or node.get("cloudinit_ciuser")
            password = (
                password
                or node.get("guest_password")
                or node.get("admin_password")
                or node.get("cloudinit_cipassword")
            )

    for key in ("effective_specs", "vm_provision", "vm_provision_result"):
        bucket = cfg.get(key)
        if isinstance(bucket, dict):
            username = username or bucket.get("guest_username") or bucket.get("cloudinit_ciuser")
            password = (
                password
                or bucket.get("guest_password")
                or bucket.get("admin_password")
                or bucket.get("cloudinit_cipassword")
            )

    user_out = str(username).strip() if username else None
    pass_out = str(password) if password else None
    if pass_out is not None and pass_out == "":
        pass_out = None
    return user_out, pass_out


def session_guest_fields(service: Service) -> Dict[str, Any]:
    """Fields to merge into ``VmVncSessionResponse`` for the console toolbar."""
    user, password = get_service_guest_credentials(service)
    return {
        "service_id": service.id,
        "guest_username": user or "",
        "guest_password": password or "",
    }
