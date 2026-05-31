from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class VMProvisionRequest:
    service_id: int
    product_code: str
    os_code: str
    specs: Dict[str, Any]
    context: Dict[str, Any]


@dataclass
class VMProvisionPlan:
    strategy_name: str
    payload: Dict[str, Any]


class VMOSStrategy(ABC):
    name: str = ""

    @abstractmethod
    def build_plan(self, request: VMProvisionRequest, strategy_config: Optional[Dict[str, Any]] = None) -> VMProvisionPlan:
        raise NotImplementedError


class StubVMOSStrategy(VMOSStrategy):
    name = "stub"

    def build_plan(self, request: VMProvisionRequest, strategy_config: Optional[Dict[str, Any]] = None) -> VMProvisionPlan:
        return VMProvisionPlan(
            strategy_name=self.name,
            payload={
                "service_id": request.service_id,
                "product_code": request.product_code,
                "os_code": request.os_code,
                "specs": request.specs,
                "strategy_config": strategy_config or {},
            },
        )


class CloudinitCloneVMOSStrategy(VMOSStrategy):
    """Clone template then apply cloud-init (ISO/drive); payload for runners / future Proxmox automation."""

    name = "cloudinit_clone"

    def build_plan(self, request: VMProvisionRequest, strategy_config: Optional[Dict[str, Any]] = None) -> VMProvisionPlan:
        cfg = strategy_config or {}
        return VMProvisionPlan(
            strategy_name=self.name,
            payload={
                "mode": "cloudinit_clone",
                "service_id": request.service_id,
                "product_code": request.product_code,
                "os_code": request.os_code,
                "specs": request.specs,
                "context": request.context,
                "strategy_config": cfg,
                "network": {
                    "proxmox": "ipconfig0",
                    "source": "vm_ip_allocation",
                    "description": "Static IP/gateway applied via Proxmox QEMU cloud-init (ipconfig0).",
                },
            },
        )


class GuestAgentVMOSStrategy(VMOSStrategy):
    """Clone template; configure via QEMU guest agent without cloud-init ISO."""

    name = "guest_agent"

    def build_plan(self, request: VMProvisionRequest, strategy_config: Optional[Dict[str, Any]] = None) -> VMProvisionPlan:
        cfg = strategy_config or {}
        return VMProvisionPlan(
            strategy_name=self.name,
            payload={
                "mode": "guest_agent",
                "service_id": request.service_id,
                "product_code": request.product_code,
                "os_code": request.os_code,
                "specs": request.specs,
                "context": request.context,
                "strategy_config": cfg,
            },
        )


class VMOSStrategyRegistry:
    def __init__(self) -> None:
        self._strategies: dict[str, VMOSStrategy] = {}
        self.register(StubVMOSStrategy())
        self.register(CloudinitCloneVMOSStrategy())
        self.register(GuestAgentVMOSStrategy())

    def register(self, strategy: VMOSStrategy) -> None:
        self._strategies[strategy.name] = strategy

    def resolve(self, strategy_name: Optional[str]) -> VMOSStrategy:
        if strategy_name and strategy_name in self._strategies:
            return self._strategies[strategy_name]
        return self._strategies["stub"]


_registry = VMOSStrategyRegistry()


def get_vm_os_strategy_registry() -> VMOSStrategyRegistry:
    return _registry
