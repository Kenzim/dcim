"""Registry that resolves deployment strategies by name."""
from __future__ import annotations

from typing import Dict, Optional

from app.services.deployment.strategies import (
    ApplyGuestPasswordStrategy,
    CloudinitCloneStrategy,
    GuestAgentStrategy,
    MacosGuestAgentStrategy,
    RestoreBackupStrategy,
    WindowsGuestAgentStrategy,
)
from app.services.deployment.strategy import DeploymentStrategy


class DeploymentStrategyRegistry:
    def __init__(self) -> None:
        self._strategies: Dict[str, DeploymentStrategy] = {}
        self.register(CloudinitCloneStrategy())
        self.register(GuestAgentStrategy())
        self.register(MacosGuestAgentStrategy())
        self.register(WindowsGuestAgentStrategy())
        self.register(ApplyGuestPasswordStrategy())
        self.register(RestoreBackupStrategy())

    def register(self, strategy: DeploymentStrategy) -> None:
        if not strategy.name:
            raise ValueError("Deployment strategy must define a name")
        self._strategies[strategy.name] = strategy

    def resolve(self, name: Optional[str]) -> Optional[DeploymentStrategy]:
        if not name:
            return None
        return self._strategies.get(name)

    def get(self, name: Optional[str]) -> DeploymentStrategy:
        strategy = self.resolve(name)
        if strategy is None:
            raise ValueError(f"Unknown deployment strategy '{name}'")
        return strategy

    def names(self):
        return sorted(self._strategies.keys())


_registry = DeploymentStrategyRegistry()


def get_deployment_strategy_registry() -> DeploymentStrategyRegistry:
    return _registry
