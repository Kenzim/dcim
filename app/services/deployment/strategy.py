"""Deployment strategy abstraction: an ordered list of steps."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Sequence

from app.services.deployment.actions import (
    DEFAULT_GUEST_ACTIONS,
    OptionField,
    StrategyAction,
)
from app.services.deployment.step import DeploymentStep


class DeploymentStrategy(ABC):
    """A named, ordered pipeline of deployment steps.

    Strategies are class-based (not admin-editable JSON). The step list defines
    the deployment plan; the runner walks it one transition at a time.
    """

    #: Stable identifier; matches ``vm_plan.strategy_name`` / template os_type mapping.
    name: str = ""
    #: Bound on crash-reclaim attempts before the job is failed (see runner).
    max_attempts: int = 3
    #: When False, default change_password / reset_network are not offered.
    include_default_actions: bool = True
    #: When False, the runner must not flip guest_state / service.status /
    #: ``config.vm_provision`` (runtime jobs like deferred password apply).
    mutates_provision_lifecycle: bool = True

    @abstractmethod
    def steps(self) -> List[DeploymentStep]:
        """Return a fresh ordered list of step instances for this strategy."""
        raise NotImplementedError

    def step_names(self) -> List[str]:
        return [s.name for s in self.steps()]

    def option_schema(self) -> List[OptionField]:
        """Fields shown on the VM template form for this strategy."""
        return [
            OptionField(
                name="guest_username",
                label="Guest username",
                field_type="string",
                default="client",
                description="OS user for password and guest-agent configuration.",
            ),
            OptionField(
                name="network_mode",
                label="Network mode",
                field_type="select",
                default="static",
                choices=["static", "dhcp"],
                description="static uses Rackflow VM IP allocation; dhcp leaves DHCP.",
            ),
            OptionField(
                name="client_actions",
                label="Client-visible actions",
                field_type="string_list",
                default=["change_password"],
                description="Action names end-users may invoke (also gated by permissions).",
            ),
        ]

    def default_options(self) -> dict:
        return {f.name: f.default for f in self.option_schema()}

    def extra_actions(self) -> Sequence[StrategyAction]:
        """Strategy-specific actions beyond the shared defaults."""
        return ()

    def actions(self) -> List[StrategyAction]:
        out: List[StrategyAction] = []
        if self.include_default_actions:
            out.extend(DEFAULT_GUEST_ACTIONS)
        out.extend(self.extra_actions())
        return out

    def action_by_name(self, name: str) -> StrategyAction | None:
        for action in self.actions():
            if action.name == name:
                return action
        return None
