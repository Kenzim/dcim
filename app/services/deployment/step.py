"""Deployment step abstraction: precheck outcome + step base class."""
from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.services.deployment.context import DeploymentContext


class DeploymentError(Exception):
    """A hard, non-retryable failure raised from a step precheck/execute.

    Raising this (or returning ``StepOutcome.failed``) fails the job. Transient
    conditions should return ``StepOutcome.wait`` instead so the job is retried.
    """


class StepResult(str, enum.Enum):
    READY = "ready"  # Precheck passed; execute() should run now
    WAIT = "wait"  # Not ready yet; re-run precheck after a backoff
    SKIP = "skip"  # Already satisfied; advance without executing
    FAILED = "failed"  # Broken; fail the job


@dataclass
class StepOutcome:
    """Result of a step precheck."""

    result: StepResult
    message: Optional[str] = None
    # Only meaningful for WAIT: seconds until the worker should re-check.
    retry_after_seconds: Optional[int] = None
    # Optional structured diagnostics persisted on the step row.
    detail: Optional[Dict[str, Any]] = None

    @classmethod
    def ready(cls, message: Optional[str] = None, detail: Optional[Dict[str, Any]] = None) -> "StepOutcome":
        return cls(StepResult.READY, message=message, detail=detail)

    @classmethod
    def wait(
        cls,
        message: Optional[str] = None,
        retry_after_seconds: Optional[int] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> "StepOutcome":
        return cls(StepResult.WAIT, message=message, retry_after_seconds=retry_after_seconds, detail=detail)

    @classmethod
    def skip(cls, message: Optional[str] = None, detail: Optional[Dict[str, Any]] = None) -> "StepOutcome":
        return cls(StepResult.SKIP, message=message, detail=detail)

    @classmethod
    def failed(cls, message: Optional[str] = None, detail: Optional[Dict[str, Any]] = None) -> "StepOutcome":
        return cls(StepResult.FAILED, message=message, detail=detail)


class DeploymentStep(ABC):
    """One unit of work in a deployment strategy.

    Subclasses set ``name`` (stable identifier persisted on the step row) and
    implement ``precheck``. ``execute`` defaults to a no-op so wait-only steps
    (e.g. "wait for guest agent") need only implement ``precheck``.
    """

    #: Stable identifier persisted on the step row (unique within a strategy).
    name: str = ""
    #: Max wall-clock seconds this step may spend WAITing before the runner
    #: fails the job. ``None`` means no timeout.
    timeout_seconds: Optional[int] = None
    #: Default backoff used when a WAIT outcome does not specify one.
    default_retry_after_seconds: int = 10

    def __init__(self, timeout_seconds: Optional[int] = None) -> None:
        if timeout_seconds is not None:
            self.timeout_seconds = timeout_seconds

    @abstractmethod
    async def precheck(self, ctx: "DeploymentContext") -> StepOutcome:
        """Decide whether the step is ready, should wait, skip, or has failed."""
        raise NotImplementedError

    async def execute(self, ctx: "DeploymentContext") -> None:
        """Perform the side effect. Default no-op (for wait-only steps)."""
        return None
