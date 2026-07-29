"""
VM template ``os_type`` is the provisioning strategy key (model + strategy are one field).

Each allowed value maps to ``billing_os_code``, ``strategy_name``, and default ``strategy_config``
for RackFlow plans (e.g. cloud-init clone vs guest-agent clone).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.services.deployment.registry import get_deployment_strategy_registry


# Keys must match ALLOWED_VM_OS_TYPES in product_catalog (derived from this dict).
INSTALL_TYPE_STRATEGIES: Dict[str, Dict[str, Any]] = {
    "Linux - Cloudinit": {
        "billing_os_code": "install-linux-cloudinit",
        "strategy_name": "cloudinit_clone",
        "strategy_config": {
            "use_cloudinit": True,
            "provision_mode": "clone_then_cloudinit",
            "notes": "Clone from catalog proxmox_template_name; configure cloud-init (drive/ISO per node policy).",
        },
    },
    "Linux - Guest agent": {
        "billing_os_code": "install-linux-guest-agent",
        "strategy_name": "guest_agent",
        "strategy_config": {
            "use_cloudinit": False,
            "provision_mode": "clone_then_guest_agent",
            "notes": "Clone from catalog proxmox_template_name; post-config via QEMU guest agent (no cloud-init ISO).",
        },
    },
    "macOS - Guest agent": {
        "billing_os_code": "install-macos-guest-agent",
        "strategy_name": "macos_guest_agent",
        "strategy_config": {
            "use_cloudinit": False,
            "provision_mode": "clone_then_macos_guest_agent",
            "notes": "Clone macOS template; SMBIOS/network/password via AppleQEMUGuestAgent + OpenCore.",
        },
    },
    "Windows - Guest agent": {
        "billing_os_code": "install-windows-guest-agent",
        "strategy_name": "windows_guest_agent",
        "strategy_config": {
            "use_cloudinit": False,
            "provision_mode": "clone_then_windows_guest_agent",
            "notes": "Clone Windows template; network/password via QEMU guest agent (PowerShell).",
        },
    },
}


def resolve_vm_template_strategy(os_type: str) -> Dict[str, Any]:
    """
    Return keys: billing_os_code, strategy_name, strategy_config, accepts_ssh_key.

    Raises ValueError if os_type is unknown.
    """
    from app.services.ssh_public_keys import strategy_accepts_ssh_key

    spec = INSTALL_TYPE_STRATEGIES.get(os_type)
    if not spec:
        raise ValueError(
            f"Unsupported VM provisioning strategy (os_type) '{os_type}'. "
            f"Allowed: {', '.join(sorted(INSTALL_TYPE_STRATEGIES.keys()))}."
        )
    strategy_name = spec["strategy_name"]
    return {
        "billing_os_code": spec["billing_os_code"],
        "strategy_name": strategy_name,
        "strategy_config": dict(spec["strategy_config"]),
        "accepts_ssh_key": strategy_accepts_ssh_key(strategy_name),
    }


def merge_strategy_options(
    os_type: str,
    template_options: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Merge strategy defaults ← template strategy_options."""
    spec = resolve_vm_template_strategy(os_type)
    strategy_name = spec["strategy_name"]
    registry = get_deployment_strategy_registry()
    strategy = registry.resolve(strategy_name)
    defaults = strategy.default_options() if strategy else {}
    merged = dict(defaults)
    if template_options:
        merged.update(template_options)
    # Keep install-type flags
    for key, value in spec["strategy_config"].items():
        merged.setdefault(key, value)
    return merged


def list_os_type_schemas() -> List[Dict[str, Any]]:
    """Admin UI payload: each os_type with strategy meta and option fields."""
    registry = get_deployment_strategy_registry()
    out: List[Dict[str, Any]] = []
    for os_type, spec in sorted(INSTALL_TYPE_STRATEGIES.items()):
        strategy = registry.resolve(spec["strategy_name"])
        option_schema = []
        actions = []
        if strategy:
            option_schema = [
                {
                    "name": f.name,
                    "label": f.label,
                    "field_type": f.field_type,
                    "default": f.default,
                    "description": f.description,
                    "choices": f.choices,
                }
                for f in strategy.option_schema()
            ]
            actions = [a.to_dict() for a in strategy.actions()]
        from app.services.ssh_public_keys import strategy_accepts_ssh_key

        strategy_name = spec["strategy_name"]
        out.append(
            {
                "os_type": os_type,
                "billing_os_code": spec["billing_os_code"],
                "strategy_name": strategy_name,
                "strategy_config": dict(spec["strategy_config"]),
                "accepts_ssh_key": strategy_accepts_ssh_key(strategy_name),
                "option_schema": option_schema,
                "actions": actions,
            }
        )
    return out


# Backwards-compatible name
resolve_strategy_for_vm_install_type = resolve_vm_template_strategy
