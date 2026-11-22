"""Static checks of a plugin package, run by the real host code without a database."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

from app.plugin_host.catalogue import API_VERSION
from app.plugin_host.loader import PluginLoadError, _purge_modules, import_package, read_package
from app.plugin_host.manifest import Manifest, range_allows
from app.plugin_host.testkit.deprecations import used_deprecations


@dataclass
class PackageReport:
    """What the host finds wrong with a package folder, as an admin installing it would see."""

    path: Path
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    manifest: Optional[Manifest] = None

    @property
    def ok(self) -> bool:
        return not self.errors

    def supports(self, api_version: str) -> bool:
        """Whether the manifest's ``rackflow_api`` range admits this plugin API version."""
        return self.manifest is not None and range_allows(self.manifest.rackflow_api, api_version)

    def format(self) -> str:
        lines = [f"{self.path.name}: " + ("valid" if self.ok else "INVALID")]
        lines += [f"  error: {e}" for e in self.errors]
        lines += [f"  warning: {w}" for w in self.warnings]
        return "\n".join(lines)


def _warnings(manifest: Manifest) -> list[str]:
    out = []
    if not manifest.description:
        out.append("description is empty; admins see it when deciding whether to install")
    if not manifest.author:
        out.append("author is empty")
    if not manifest.licence:
        out.append("licence is empty")
    if manifest.rackflow_api.strip() and not any(op in manifest.rackflow_api for op in ("<", "==")):
        out.append("rackflow_api has no upper bound; a future major version may break the plugin")
    return out


def check_package(path: Union[str, Path]) -> PackageReport:
    """Validate the folder the way an install does: manifest, folder name, API range, entry module.

    The entry module is imported and ``register(plugin)`` runs, so a handler missing from
    ``plugin.json`` (or declared there but never registered) is reported too.
    """
    path = Path(path)
    package = read_package(path)
    report = PackageReport(path=path, errors=list(package.errors), manifest=package.manifest)
    if package.manifest is not None:
        report.warnings = _warnings(package.manifest)
        report.warnings += [d.message() for d in used_deprecations(package.raw or {}, path)]
    if package.valid:
        try:
            import_package(package)
        except PluginLoadError as exc:
            report.errors.append(str(exc))
        finally:
            _purge_modules(package.manifest.id)
    return report


def assert_valid_package(path: Union[str, Path]) -> PackageReport:
    """Raise ``AssertionError`` listing every problem; return the report when there is none."""
    report = check_package(path)
    assert report.ok, report.format()
    return report


__all__ = ["API_VERSION", "PackageReport", "assert_valid_package", "check_package"]
