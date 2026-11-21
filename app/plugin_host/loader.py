"""Find plugin packages on disk, hash them, and import their entry modules (docs/plugin-system.md sections 3 and 4)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import sys
import threading
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from app.core.config import settings
from app.plugin_host.catalogue import API_VERSION
from app.plugin_host.manifest import Manifest, ManifestError, validate_manifest

logger = logging.getLogger(__name__)

MODULE_ROOT = "rackflow_plugins"
LIFECYCLE_NAMES = ("install", "enable", "disable", "upgrade", "uninstall")
_IGNORED_PARTS = {"__pycache__"}
_import_lock = threading.Lock()


class PluginLoadError(RuntimeError):
    pass


def plugins_root() -> Path:
    return Path(settings.plugins_dir).expanduser().resolve()


def package_hash(path: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(
        p for p in path.rglob("*")
        if p.is_file() and not (_IGNORED_PARTS & set(p.relative_to(path).parts)) and p.suffix != ".pyc"
    )
    for file in files:
        rel = file.relative_to(path).as_posix()
        digest.update(rel.encode("utf-8") + b"\0")
        digest.update(file.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


@dataclass
class Package:
    directory: str
    path: Path
    manifest: Optional[Manifest] = None
    raw: Optional[dict] = None
    errors: list[str] = field(default_factory=list)
    code_hash: Optional[str] = None

    @property
    def valid(self) -> bool:
        return self.manifest is not None and not self.errors


def read_package(path: Path) -> Package:
    package = Package(directory=path.name, path=path)
    manifest_file = path / "plugin.json"
    try:
        raw = json.loads(manifest_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        package.errors.append("plugin.json is missing")
        return package
    except (OSError, ValueError) as exc:
        package.errors.append(f"plugin.json is not valid JSON: {exc}")
        return package
    package.raw = raw if isinstance(raw, dict) else None
    try:
        manifest = validate_manifest(raw)
    except ManifestError as exc:
        package.errors.extend(exc.errors)
        return package
    package.manifest = manifest
    if manifest.id != path.name:
        package.errors.append(f"the directory name must equal the plugin id {manifest.id!r}")
    if not manifest.compatible():
        package.errors.append(
            f"needs Rackflow plugin API {manifest.rackflow_api}; this Rackflow provides {API_VERSION}"
        )
    entry = path / manifest.entry
    if not entry.resolve().is_relative_to(path.resolve()) or not entry.is_file():
        package.errors.append(f"entry module {manifest.entry} is missing")
    try:
        package.code_hash = package_hash(path)
    except OSError as exc:
        package.errors.append(f"cannot read the package: {exc}")
    return package


def discover(root: Optional[Path] = None) -> list[Package]:
    root = root or plugins_root()
    if not root.is_dir():
        return []
    out = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and not child.name.startswith(".") and (child / "plugin.json").exists():
            out.append(read_package(child))
    return out


def find_package(directory: str, root: Optional[Path] = None) -> Package:
    root = root or plugins_root()
    if not directory or "/" in directory or "\\" in directory or directory.startswith("."):
        raise PluginLoadError("Unknown plugin directory")
    path = root / directory
    if not (path / "plugin.json").is_file():
        raise PluginLoadError("Unknown plugin directory")
    return read_package(path)


class Registrar:
    """Collects the handlers a plugin's register(plugin) binds to its manifest names."""

    def __init__(self, plugin_id: str) -> None:
        self.plugin_id = plugin_id
        self.on: dict[str, Callable] = {}
        self.before_hooks: dict[str, Callable] = {}
        self.schedules: dict[str, Callable] = {}
        self.actions: dict[str, Callable] = {}
        self.mcp_tools: dict[str, Callable] = {}
        self.lifecycle_hooks: dict[str, Callable] = {}

    def _binder(self, table: dict, name: str, kind: str) -> Callable[[Callable], Callable]:
        if not isinstance(name, str) or not name:
            raise PluginLoadError(f"{kind} needs a name")

        def bind(fn: Callable) -> Callable:
            if name in table:
                raise PluginLoadError(f"{kind} {name!r} is registered twice")
            if not callable(fn):
                raise PluginLoadError(f"{kind} {name!r} is not callable")
            table[name] = fn
            return fn

        return bind

    def on_event(self, name: str):
        return self._binder(self.on, name, "event handler")

    def before(self, name: str):
        return self._binder(self.before_hooks, name, "before hook")

    def schedule(self, name: str):
        return self._binder(self.schedules, name, "schedule")

    def action(self, name: str):
        return self._binder(self.actions, name, "action")

    def mcp_tool(self, name: str):
        return self._binder(self.mcp_tools, name, "MCP tool")

    def lifecycle(self, name: str):
        if name not in LIFECYCLE_NAMES:
            raise PluginLoadError(f"unknown lifecycle step {name!r}")
        return self._binder(self.lifecycle_hooks, name, "lifecycle handler")


class _PublicRegistrar:
    """What register(plugin) receives: only the decorators, never the tables."""

    def __init__(self, registrar: Registrar) -> None:
        self.on = registrar.on_event
        self.before = registrar.before
        self.schedule = registrar.schedule
        self.action = registrar.action
        self.mcp_tool = registrar.mcp_tool
        self.lifecycle = registrar.lifecycle
        self.id = registrar.plugin_id


@dataclass
class LoadedPlugin:
    manifest: Manifest
    code_hash: str
    module: Any
    handlers: Registrar

    @property
    def id(self) -> str:
        return self.manifest.id


def _check_handlers(manifest: Manifest, reg: Registrar) -> list[str]:
    errors = []
    pairs = (
        ("event handler", set(manifest.events), set(reg.on)),
        ("before hook", set(manifest.hooks), set(reg.before_hooks)),
        ("schedule", {s.id for s in manifest.schedules}, set(reg.schedules)),
        ("action", {a.id for a in manifest.actions}, set(reg.actions)),
        ("MCP tool", {t.id for t in manifest.mcp_tools}, set(reg.mcp_tools)),
    )
    for kind, declared, registered in pairs:
        for name in sorted(declared - registered):
            errors.append(f"{kind} {name!r} is declared but has no handler")
        for name in sorted(registered - declared):
            errors.append(f"{kind} {name!r} has a handler but is not declared in plugin.json")
    return errors


def _purge_modules(plugin_id: str) -> None:
    prefix = f"{MODULE_ROOT}.{plugin_id}"
    for name in [n for n in sys.modules if n == prefix or n.startswith(prefix + ".")]:
        del sys.modules[name]


def import_package(package: Package) -> LoadedPlugin:
    """Import the entry module fresh and collect its handlers. Raises PluginLoadError."""
    if not package.valid:
        raise PluginLoadError("; ".join(package.errors) or "invalid package")
    manifest = package.manifest
    assert manifest is not None
    pkg_name = f"{MODULE_ROOT}.{manifest.id}"
    entry_rel = manifest.entry[:-3].replace("\\", "/").split("/")
    module_name = ".".join([pkg_name, *entry_rel])
    with _import_lock:
        _purge_modules(manifest.id)
        if MODULE_ROOT not in sys.modules:
            root_module = types.ModuleType(MODULE_ROOT)
            root_module.__path__ = []  # type: ignore[attr-defined]
            sys.modules[MODULE_ROOT] = root_module
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(package.path)]  # type: ignore[attr-defined]
        pkg.__package__ = pkg_name
        sys.modules[pkg_name] = pkg
        spec = importlib.util.spec_from_file_location(module_name, package.path / manifest.entry)
        if spec is None or spec.loader is None:
            raise PluginLoadError("cannot import the entry module")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        previous = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001 - any import failure is reported, never raised raw
            _purge_modules(manifest.id)
            raise PluginLoadError(f"importing {manifest.entry} failed: {type(exc).__name__}: {exc}") from exc
        finally:
            sys.dont_write_bytecode = previous
    register = getattr(module, "register", None)
    if not callable(register):
        raise PluginLoadError(f"{manifest.entry} has no register(plugin) function")
    reg = Registrar(manifest.id)
    try:
        register(_PublicRegistrar(reg))
    except PluginLoadError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PluginLoadError(f"register(plugin) failed: {type(exc).__name__}: {exc}") from exc
    errors = _check_handlers(manifest, reg)
    if errors:
        raise PluginLoadError("; ".join(errors))
    return LoadedPlugin(manifest=manifest, code_hash=package.code_hash or "", module=module, handlers=reg)
