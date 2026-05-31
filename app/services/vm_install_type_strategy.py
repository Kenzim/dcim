"""
VM template ``os_type`` is the provisioning strategy key (model + strategy are one field).

Each allowed value maps to ``billing_os_code``, ``strategy_name``, and default ``strategy_config``
for RackFlow plans (e.g. cloud-init clone vs guest-agent clone).
"""
from __future__ import annotations

from typing import Any, Dict

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
}


def resolve_vm_template_strategy(os_type: str) -> Dict[str, Any]:
    """
    Return keys: billing_os_code, strategy_name, strategy_config.

    Raises ValueError if os_type is unknown.
    """
    spec = INSTALL_TYPE_STRATEGIES.get(os_type)
    if not spec:
        raise ValueError(
            f"Unsupported VM provisioning strategy (os_type) '{os_type}'. "
            f"Allowed: {', '.join(sorted(INSTALL_TYPE_STRATEGIES.keys()))}."
        )
    return {
        "billing_os_code": spec["billing_os_code"],
        "strategy_name": spec["strategy_name"],
        "strategy_config": dict(spec["strategy_config"]),
    }


# Backwards-compatible name
resolve_strategy_for_vm_install_type = resolve_vm_template_strategy
