"""Per-process view of installed plugins: parsed manifests, loaded code and event subscriptions."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.plugin_host import PluginInstallation, PluginStatus
from app.plugin_host.catalogue import EVENTS
from app.plugin_host.common import new_session, write_log
from app.plugin_host.loader import LoadedPlugin, PluginLoadError, _purge_modules, find_package, import_package
from app.plugin_host.manifest import Manifest, validate_manifest

logger = logging.getLogger(__name__)

SUBSCRIPTION_TTL_SECONDS = 10.0

_lock = threading.RLock()
_loaded: dict[str, LoadedPlugin] = {}
_manifests: dict[tuple[str, str], Manifest] = {}


@dataclass
class Subscriptions:
    events: dict[str, list[str]] = field(default_factory=dict)
    hooks: dict[str, list[str]] = field(default_factory=dict)
    loaded_at: float = 0.0


_subs = Subscriptions()


def invalidate(plugin_id: Optional[str] = None) -> None:
    """Forget cached subscriptions (and the loaded code of one plugin, when given)."""
    global _subs
    with _lock:
        _subs = Subscriptions()
        if plugin_id is not None:
            _loaded.pop(plugin_id, None)
            _purge_modules(plugin_id)


def reset_all() -> None:
    global _subs
    with _lock:
        for plugin_id in list(_loaded):
            _purge_modules(plugin_id)
        _loaded.clear()
        _manifests.clear()
        _subs = Subscriptions()


def manifest_of(inst: PluginInstallation) -> Manifest:
    key = (inst.id, inst.code_hash)
    with _lock:
        cached = _manifests.get(key)
    if cached is not None:
        return cached
    manifest = validate_manifest(inst.manifest)
    with _lock:
        _manifests[key] = manifest
    return manifest


def subscriptions(db: Session) -> Subscriptions:
    global _subs
    with _lock:
        current = _subs
    if current.loaded_at and time.monotonic() - current.loaded_at < SUBSCRIPTION_TTL_SECONDS:
        return current
    rows = db.execute(
        select(PluginInstallation.id, PluginInstallation.manifest, PluginInstallation.granted_permissions).where(
            PluginInstallation.status == PluginStatus.ENABLED
        )
    ).all()
    fresh = Subscriptions(loaded_at=time.monotonic())
    for plugin_id, manifest, granted in sorted(rows, key=lambda r: r[0]):
        granted = set(granted or [])
        manifest = manifest or {}
        for name in manifest.get("events") or []:
            spec = EVENTS.get(name)
            if spec and spec.permission in granted:
                fresh.events.setdefault(name, []).append(plugin_id)
        for name in manifest.get("hooks") or []:
            spec = EVENTS.get(name)
            if spec and spec.before and spec.permission in granted and "events.veto" in granted:
                fresh.hooks.setdefault(name, []).append(plugin_id)
    with _lock:
        _subs = fresh
    return fresh


def _set_status(plugin_id: str, status: str, reason: str) -> None:
    db = new_session()
    try:
        inst = db.get(PluginInstallation, plugin_id)
        if inst is not None and inst.status == PluginStatus.ENABLED:
            inst.status = status
            inst.disabled_reason = reason[:500]
            db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.warning("Could not mark plugin %s as %s", plugin_id, status, exc_info=True)
    finally:
        db.close()
    invalidate(plugin_id)
    write_log(plugin_id, "error", reason)


def load_installed(inst: PluginInstallation, *, require_enabled: bool = True) -> Optional[LoadedPlugin]:
    """The plugin's code as approved by the admin, or None when it cannot or must not run."""
    if require_enabled and inst.status != PluginStatus.ENABLED:
        return None
    with _lock:
        cached = _loaded.get(inst.id)
    if cached is not None and cached.code_hash == inst.code_hash and cached.manifest.version == inst.version:
        return cached
    try:
        package = find_package(inst.directory)
    except PluginLoadError:
        package = None
    if package is None or package.manifest is None or package.code_hash != inst.code_hash or package.manifest.version != inst.version:
        reason = "The files on disk differ from the approved version; review and upgrade to run them."
        if require_enabled:
            _set_status(inst.id, PluginStatus.FILES_CHANGED, reason)
        return None
    try:
        loaded = import_package(package)
    except PluginLoadError as exc:
        if require_enabled:
            _set_status(inst.id, PluginStatus.FAILED, f"Loading failed: {exc}")
        return None
    with _lock:
        _loaded[inst.id] = loaded
    return loaded


def enabled_installations(db: Session) -> list[PluginInstallation]:
    return list(
        db.execute(
            select(PluginInstallation)
            .where(PluginInstallation.status == PluginStatus.ENABLED)
            .order_by(PluginInstallation.id)
        ).scalars()
    )
