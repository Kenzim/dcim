"""Strategy-declared runtime actions and option schema fields."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class OptionField:
    """Admin UI field for per-template strategy options."""

    name: str
    label: str
    field_type: str  # string | bool | select | string_list
    default: Any = None
    description: str = ""
    choices: Optional[List[str]] = None


@dataclass(frozen=True)
class StrategyAction:
    """A callable action exposed by a deployment strategy."""

    name: str
    label: str
    description: str = ""
    admin: bool = True
    client_eligible: bool = False
    permission_key: Optional[str] = None
    params_schema: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "admin": self.admin,
            "client_eligible": self.client_eligible,
            "permission_key": self.permission_key,
            "params_schema": self.params_schema,
        }


# Shared default actions — strategies include these unless they opt out.
CHANGE_PASSWORD_ACTION = StrategyAction(
    name="change_password",
    label="Change Password",
    description="Set the guest OS user password via QEMU guest agent.",
    admin=True,
    client_eligible=True,
    permission_key="vm.change_password",
    params_schema={
        "password": {"type": "string", "required": True},
        "username": {"type": "string", "required": False},
    },
)

RESET_NETWORK_ACTION = StrategyAction(
    name="reset_network",
    label="Reset Network",
    description=(
        "Re-apply static IPAM or DHCP networking. "
        "Cloud-init strategies update Proxmox cloud-init and reboot; "
        "guest-agent strategies configure the OS in-place."
    ),
    admin=True,
    client_eligible=True,
    permission_key="vm.reset_network",
    params_schema={},
)

RANDOMIZE_SMBIOS_ACTION = StrategyAction(
    name="randomize_smbios",
    label="Randomize SMBIOS",
    description="Regenerate OpenCore PlatformInfo SMBIOS and reboot the guest.",
    admin=True,
    client_eligible=False,
    params_schema={},
)

DEFAULT_GUEST_ACTIONS = (CHANGE_PASSWORD_ACTION, RESET_NETWORK_ACTION)
