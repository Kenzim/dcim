"""Calls to plugin providers of the API 1.1 kinds (docs/billing-phase2-design.md 8.5)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class ProviderUnavailable(Exception):
    """The only exception ``call_provider`` raises; ``reason`` says why."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ProviderRef:
    plugin_id: str
    provider_id: str
    kind: str

    @property
    def ref(self) -> str:
        return f"{self.plugin_id}:{self.provider_id}"


def call_provider(kind: str, provider_ref: str, method: str, *args: Any) -> Any:
    raise NotImplementedError("call_provider is built by the plugin API 1.1 worker")


def providers_of(kind: str) -> list[ProviderRef]:
    """Enabled providers of ``kind`` whose circuit is closed."""
    raise NotImplementedError("providers_of is built by the plugin API 1.1 worker")
