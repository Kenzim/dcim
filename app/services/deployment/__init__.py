"""VM deployment strategy/step framework.

A ``DeploymentStrategy`` declares an ordered list of ``DeploymentStep`` objects.
Each step exposes a ``precheck`` that decides whether it can run now (``ready``),
must wait (``wait``), is already satisfied (``skip``), or is broken (``failed``),
followed by an ``execute`` that performs the actual side effect.

The runner (``runner.py``) advances one step transition at a time and persists
state to ``vm_deployment_jobs`` / ``vm_deployment_job_steps`` so any step may wait
across ticks and the whole job survives a worker restart.
"""

from app.services.deployment.step import (
    DeploymentStep,
    StepResult,
    StepOutcome,
    DeploymentError,
)
from app.services.deployment.strategy import DeploymentStrategy
from app.services.deployment.context import DeploymentContext
from app.services.deployment.registry import (
    get_deployment_strategy_registry,
    DeploymentStrategyRegistry,
)

__all__ = [
    "DeploymentStep",
    "StepResult",
    "StepOutcome",
    "DeploymentError",
    "DeploymentStrategy",
    "DeploymentContext",
    "DeploymentStrategyRegistry",
    "get_deployment_strategy_registry",
]
