"""The registry of deprecated plugin API items (docs/plugins/deprecation-policy.md).

Nothing is deprecated in API 1.0. When an item is deprecated in a minor version it is added
here; ``check_package`` then warns authors whose package still uses it, and the reference
lists it. The tests in ``tests/plugin_host/test_deprecations.py`` check every entry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

KINDS = ("permission", "event", "hook", "manifest_field", "sdk")


@dataclass(frozen=True)
class Deprecation:
    kind: str
    name: str
    since: str
    removal: str
    replacement: str

    def message(self) -> str:
        return (
            f"{self.kind} {self.name} is deprecated since plugin API {self.since} and will be removed in "
            f"API {self.removal}: {self.replacement}"
        )


DEPRECATIONS: tuple[Deprecation, ...] = ()


def used_deprecations(raw_manifest: dict[str, Any], package_dir: Path) -> list[Deprecation]:
    """Deprecated items a package uses, found in its manifest and (for SDK members) its Python files."""
    found: list[Deprecation] = []
    sources = None
    for item in DEPRECATIONS:
        if item.kind == "permission":
            used = item.name in (raw_manifest.get("permissions") or [])
        elif item.kind == "event":
            used = item.name in (raw_manifest.get("events") or [])
        elif item.kind == "hook":
            used = item.name in (raw_manifest.get("hooks") or [])
        elif item.kind == "manifest_field":
            used = item.name.split(".")[-1] in raw_manifest
        else:
            if sources is None:
                sources = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in package_dir.rglob("*.py"))
            used = re.search(rf"\b{re.escape(item.name)}\b", sources) is not None
        if used:
            found.append(item)
    return found
