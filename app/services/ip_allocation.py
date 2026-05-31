from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterable

from app.models.ipam import IPAddress


class AllocationStrategy(ABC):
    name: str = ""

    @abstractmethod
    def order(self, ips: Iterable[IPAddress]) -> list[IPAddress]:
        raise NotImplementedError


class FirstFreeStrategy(AllocationStrategy):
    name = "first_free"

    def order(self, ips: Iterable[IPAddress]) -> list[IPAddress]:
        return sorted(ips, key=lambda i: i.id)


class LeastRecentlyUsedStrategy(AllocationStrategy):
    name = "least_recently_used"

    def order(self, ips: Iterable[IPAddress]) -> list[IPAddress]:
        return sorted(ips, key=lambda i: (i.updated_at or i.created_at, i.id))


class SpreadSubnetsStrategy(AllocationStrategy):
    name = "spread_subnets"

    def order(self, ips: Iterable[IPAddress]) -> list[IPAddress]:
        # Stateless runner chooses deterministic distribution by even/odd IDs.
        return sorted(ips, key=lambda i: (i.id % 2, i.id))


class AllocationStrategyRegistry:
    def __init__(self) -> None:
        self._strategies: dict[str, AllocationStrategy] = {}
        for strategy in (FirstFreeStrategy(), LeastRecentlyUsedStrategy(), SpreadSubnetsStrategy()):
            self.register(strategy)

    def register(self, strategy: AllocationStrategy) -> None:
        self._strategies[strategy.name] = strategy

    def resolve(self, name: str | None) -> AllocationStrategy:
        if name and name in self._strategies:
            return self._strategies[name]
        return self._strategies["first_free"]


_registry = AllocationStrategyRegistry()


def get_allocation_strategy_registry() -> AllocationStrategyRegistry:
    return _registry
