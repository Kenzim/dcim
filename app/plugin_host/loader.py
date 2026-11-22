"""Find plugin packages on disk, hash them, and import their entry modules (docs/plugin-system.md sections 3 and 4)."""

from __future__ import annotations

import hashlib
import importlib
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
from app.plugin_host import kinds
from app.plugin_host.catalogue import SUPPORTED_API_VERSIONS
from app.plugin_host.manifest import Manifest, ManifestError, validate_manifest

logger = logging.getLogger(__name__)

MODULE_ROOT = "rackflow_plugins"
LIFECYCLE_NAMES = ("install", "enable", "disable", "upgrade", "uninstall")
_IGNORED_PARTS = {"__pycache__"}
_import_lock = threading.Lock()


class PluginLoadError(RuntimeError):
    pass


def _install_sdk_modules() -> None:
    """Make ``rackflow_plugin.kinds`` importable by plugin code (API 1.1)."""
    if "rackflow_plugin.kinds" in sys.modules:
        return
    package = types.ModuleType("rackflow_plugin")
    package.__path__ = []  # type: ignore[attr-defined]
    package.kinds = kinds  # type: ignore[attr-defined]
    sys.modules.setdefault("rackflow_plugin", package)
    sys.modules["rackflow_plugin.kinds"] = kinds


_install_sdk_modules()


def plugins_root() -> Path:
    return Path(settings.plugins_dir).expanduser().resolve()


def bundled_root() -> Path:
    """The read-only folder of first-party packages; a relative setting counts from the repository root."""
    path = Path(settings.bundled_plugins_dir).expanduser()
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[2] / path
    return path.resolve()


def package_roots() -> list[tuple[Path, bool]]:
    """Every folder plugins are read from, with whether it is the bundled one."""
    roots = [(plugins_root(), False)]
    bundled = bundled_root()
    if bundled != roots[0][0]:
        roots.append((bundled, True))
    return roots


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
    bundled: bool = False

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
            f"needs Rackflow plugin API {manifest.rackflow_api}; this Rackflow provides "
            f"{', '.join(SUPPORTED_API_VERSIONS)}"
        )
    entry = path / manifest.entry
    if not entry.resolve().is_relative_to(path.resolve()) or not entry.is_file():
        package.errors.append(f"entry module {manifest.entry} is missing")
    package.errors.extend(_check_pack_files(path, manifest))
    try:
        package.code_hash = package_hash(path)
    except OSError as exc:
        package.errors.append(f"cannot read the package: {exc}")
    return package


def _check_pack_files(path: Path, manifest: Manifest) -> list[str]:
    """The ``pack.file`` of every ``tax_rule_pack`` provider must exist in the package and be a valid pack."""
    errors = []
    for provide in manifest.provides:
        if provide.pack is None:
            continue
        pack_file = path / provide.pack.file
        if not pack_file.resolve().is_relative_to(path.resolve()) or not pack_file.is_file():
            errors.append(f"pack file {provide.pack.file} of provider {provide.id} is missing")
            continue
        too_large = f"pack file {provide.pack.file} is larger than 1 MB"
        try:
            if pack_file.stat().st_size > kinds.MAX_PACK_BYTES:
                errors.append(too_large)
                continue
            with pack_file.open("rb") as handle:
                content = handle.read(kinds.MAX_PACK_BYTES + 1)  # the file may have grown since stat
            if len(content) > kinds.MAX_PACK_BYTES:
                errors.append(too_large)
                continue
            data = json.loads(content.decode("utf-8"))
        except (OSError, ValueError, RecursionError) as exc:
            errors.append(f"pack file {provide.pack.file} is not valid JSON: {exc}")
            continue
        errors.extend(_pack_schema_errors(provide.pack.file, data))
    return errors


def _pack_schema_errors(name: str, data: Any) -> list[str]:
    """Validate a pack with the tax pack format (``app.services.tax.packs.schema``)."""
    # Imported here: the schema imports app.models, whose package init loads
    # app.plugin_host.listeners and through it this module.
    from app.services.tax.packs.schema import PackFormatError, validate_pack

    if not isinstance(data, dict):
        return [f"pack file {name} must hold a JSON object"]
    try:
        validate_pack(data)
    except PackFormatError as exc:
        return [f"pack file {name} does not pass the pack schema: {str(exc)[:300]}"]
    return []


def _package_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return [
        child for child in sorted(root.iterdir())
        if child.is_dir() and not child.name.startswith(".") and (child / "plugin.json").exists()
    ]


def _scan(root: Path, bundled: bool) -> list[Package]:
    out = []
    for child in _package_dirs(root):
        package = read_package(child)
        package.bundled = bundled
        out.append(package)
    return out


def _raw_id(raw: Any) -> Optional[str]:
    value = raw.get("id") if isinstance(raw, dict) else None
    return value if isinstance(value, str) and value else None


def _claims(directory: str, raw: Any) -> set[str]:
    """The plugin ids a package folder stands for: its manifest id, and its folder name."""
    return {directory} | ({_raw_id(raw)} - {None})  # type: ignore[operator]


def _manifest_id_at(path: Path) -> Optional[str]:
    try:
        return _raw_id(json.loads((path / "plugin.json").read_text(encoding="utf-8")))
    except (OSError, ValueError, RecursionError):
        return None


def _conflict_message(plugin_id: str) -> str:
    return (
        f"the plugin {plugin_id!r} exists in both the plugins folder and the bundled plugins folder; "
        "remove one copy before it can be installed or run"
    )


def discover(root: Optional[Path] = None) -> list[Package]:
    """Packages of ``root``, or of the plugins folder and then the bundled folder when no root is given."""
    if root is not None:
        return _scan(root, False)
    out: list[Package] = []
    for folder, bundled in package_roots():
        out.extend(_scan(folder, bundled))
    claims = {id(p): _claims(p.directory, p.raw) for p in out}
    for package in out:
        mine = claims[id(package)]
        for other in out:
            shared = mine & claims[id(other)]
            if other.bundled != package.bundled and shared:
                package.errors.append(_conflict_message(_raw_id(package.raw) or sorted(shared)[0]))
                break
    return out


def find_package(directory: str, root: Optional[Path] = None) -> Package:
    if not directory or "/" in directory or "\\" in directory or directory.startswith("."):
        raise PluginLoadError("Unknown plugin directory")
    roots = [(root, False)] if root is not None else package_roots()
    found = [(base / directory, bundled) for base, bundled in roots if (base / directory / "plugin.json").is_file()]
    if not found:
        raise PluginLoadError("Unknown plugin directory")
    package = read_package(found[0][0])
    package.bundled = found[0][1]
    mine = _claims(directory, package.raw)
    for base, bundled in roots:
        if bundled == package.bundled:
            continue
        if any(child.name in mine or _manifest_id_at(child) in mine for child in _package_dirs(base)):
            package.errors.append(_conflict_message(_raw_id(package.raw) or directory))
            break
    return package


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
        self.provider_factories: dict[tuple[str, str], Callable] = {}
        # Filled by import_package: one instance per factory, created once per load.
        self.providers: dict[tuple[str, str], Any] = {}

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

    def provider(self, kind: str, provider_id: str) -> Callable[[Callable], Callable]:
        if kind not in kinds.KINDS:
            raise PluginLoadError(f"unknown provider kind {kind!r}")
        if not isinstance(provider_id, str) or not provider_id:
            raise PluginLoadError("provider needs an id")
        key = (kind, provider_id)

        def bind(factory: Callable) -> Callable:
            if key in self.provider_factories:
                raise PluginLoadError(f"provider {kind}:{provider_id} is registered twice")
            if not callable(factory):
                raise PluginLoadError(f"provider {kind}:{provider_id} is not a class or a factory function")
            self.provider_factories[key] = factory
            return factory

        return bind


class _PublicRegistrar:
    """What register(plugin) receives: only the decorators, never the tables."""

    def __init__(self, registrar: Registrar) -> None:
        self.on = registrar.on_event
        self.before = registrar.before
        self.schedule = registrar.schedule
        self.action = registrar.action
        self.mcp_tool = registrar.mcp_tool
        self.lifecycle = registrar.lifecycle
        self.provider = registrar.provider
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
    declared = {(p.kind, p.id) for p in manifest.provides}
    registered = set(reg.provider_factories)
    for kind, provider_id in sorted(declared - registered):
        errors.append(f"provider {kind}:{provider_id} is declared but has no @plugin.provider")
    for kind, provider_id in sorted(registered - declared):
        errors.append(f"provider {kind}:{provider_id} is registered but not declared in provides")
    return errors


def _build_providers(manifest: Manifest, reg: Registrar) -> list[str]:
    """Create each provider once and check it against the protocol of its kind."""
    errors = []
    for (kind, provider_id), factory in sorted(reg.provider_factories.items()):
        try:
            obj = factory()
        except Exception as exc:  # noqa: BLE001 - a constructor failure is a load error
            errors.append(f"provider {kind}:{provider_id} could not be created: {type(exc).__name__}: {exc}")
            continue
        problems = kinds.check_provider(kind, obj)
        errors.extend(f"provider {kind}:{provider_id}: {problem}" for problem in problems)
        if not problems:
            reg.providers[(kind, provider_id)] = obj
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
    if not errors:
        errors = _build_providers(manifest, reg)
    if errors:
        raise PluginLoadError("; ".join(errors))
    return LoadedPlugin(manifest=manifest, code_hash=package.code_hash or "", module=module, handlers=reg)
