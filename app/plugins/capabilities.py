"""
Capability definitions for server plugins.

Plugins define capabilities with UI schema so the frontend can render
config-driven generic components without hardcoding capability types.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class UIPattern(str, Enum):
    """UI patterns for capability rendering on the dashboard."""
    STATE_AND_ACTIONS = "state_and_actions"
    LIST_AND_ACTIONS = "list_and_actions"
    ORDERED_LIST = "ordered_list"
    ACTIONS_ONLY = "actions_only"


@dataclass
class ActionDef:
    """Definition of an action (button, etc.) within a capability."""
    method: str
    label: str
    variant: str = "primary"
    confirm: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = {"method": self.method, "label": self.label, "variant": self.variant}
        if self.confirm:
            d["confirm"] = self.confirm
        return d


@dataclass
class Capability:
    """
    Capability definition with UI schema for config-driven rendering.

    - id: Unique identifier (e.g. "power_control", "boot_order")
    - display_name: Human-readable title for the UI
    - description: Help text or tooltip
    - optional: If True, user enables per-server; if False, always on with this plugin
    - ui_pattern: Which generic frontend component to use
    - state_action: Method to call for state (e.g. get_power_state); used for state_and_actions
    - actions: List of ActionDef for buttons
    """
    id: str
    display_name: str
    description: str
    optional: bool
    ui_pattern: UIPattern
    state_action: Optional[str] = None
    actions: List[ActionDef] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "id": self.id,
            "display_name": self.display_name,
            "description": self.description,
            "optional": self.optional,
            "ui_pattern": self.ui_pattern.value,
            "actions": [a.to_dict() for a in self.actions],
        }
        if self.state_action:
            d["state_action"] = self.state_action
        return d


def get_effective_capabilities(
    capabilities: List[Capability],
    capability_states: Optional[Dict[str, bool]] = None,
) -> List[Dict[str, Any]]:
    """
    Compute effective capabilities for a server from plugin CAPABILITIES
    and per-server capability state overrides.
    """
    states = capability_states or {}
    result = []
    for cap in capabilities:
        enabled = states.get(cap.id, not cap.optional)
        if enabled:
            result.append(cap.to_dict())
    return result


def server_has_capability(
    capabilities: List[Capability],
    capability_states: Optional[Dict[str, bool]],
    capability_id: str,
) -> bool:
    """Check if a server has a specific capability enabled."""
    states = capability_states or {}
    for cap in capabilities:
        if cap.id == capability_id:
            return states.get(cap.id, not cap.optional)
    return False
