"""Install, enable, disable, upgrade and uninstall (docs/plugin-system.md section 4)."""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.plugin_host import (
    PluginDelivery,
    PluginInstallation,
    PluginLog,
    PluginSchedule,
    PluginServiceLink,
    PluginSetting,
    PluginState,
    PluginStatus,
)
from app.plugin_host import health, outbox, runtime, settings_store
from app.plugin_host.catalogue import describe_permissions
from app.plugin_host.common import audit, aware
from app.plugin_host.loader import Package, PluginLoadError, discover, find_package, import_package
from app.plugin_host.manifest import Manifest, parse_semver

logger = logging.getLogger(__name__)

LIFECYCLE_TIMEOUT = 30.0
DISABLE_TIMEOUT = 10.0


class LifecycleError(ValueError):
    pass


class PermissionsNotAccepted(LifecycleError):
    def __init__(self, missing: list[str]) -> None:
        super().__init__("permissions_not_accepted")
        self.missing = sorted(missing)


def _load_check(package: Package) -> None:
    if not package.valid:
        raise LifecycleError("; ".join(package.errors) or "The package is not valid")
    try:
        import_package(package)
    except PluginLoadError as exc:
        raise LifecycleError(str(exc)) from exc
    finally:
        runtime.invalidate(package.manifest.id if package.manifest else None)


async def _run_step(inst: PluginInstallation, step: str, info: dict, *, timeout: float = LIFECYCLE_TIMEOUT):
    from app.plugin_host.invoke import invoke

    return await invoke(
        inst, "lifecycle", step, (info,), timeout=timeout, require_enabled=False, respect_circuit=False,
        count_failures=False, optional=True,
    )


def _purge(db: Session, plugin_id: str, *, keep_data: bool) -> None:
    for model in (PluginDelivery, PluginSchedule, PluginLog):
        db.execute(delete(model).where(model.plugin_id == plugin_id).execution_options(synchronize_session=False))
    if not keep_data:
        for model in (PluginSetting, PluginState, PluginServiceLink):
            db.execute(delete(model).where(model.plugin_id == plugin_id).execution_options(synchronize_session=False))


async def install(
    db: Session,
    directory: str,
    *,
    accept_permissions: list[str],
    enable: bool = False,
    actor_user_id: Optional[int] = None,
) -> PluginInstallation:
    try:
        package = find_package(directory, for_install=True)
    except PluginLoadError as exc:
        raise LifecycleError(str(exc)) from exc
    if package.reserved:
        raise LifecycleError("; ".join(package.errors))
    if package.manifest is None:
        raise LifecycleError("; ".join(package.errors) or "The package is not valid")
    manifest = package.manifest
    if db.get(PluginInstallation, manifest.id) is not None:
        raise LifecycleError("This plugin is already installed; use upgrade")
    missing = set(manifest.permissions) - set(accept_permissions or [])
    if missing:
        raise PermissionsNotAccepted(list(missing))
    _load_check(package)
    inst = PluginInstallation(
        id=manifest.id,
        name=manifest.name,
        version=manifest.version,
        manifest=package.raw,
        directory=package.directory,
        code_hash=package.code_hash,
        effective_api=manifest.effective_api(),
        status=PluginStatus.DISABLED,
        granted_permissions=sorted(manifest.permissions),
        addon_refs=[],
        consecutive_failures=0,
        total_failures=0,
        circuit_opens=0,
    )
    db.add(inst)
    db.flush()
    settings_store.apply_defaults(db, manifest.id, manifest)
    db.commit()
    runtime.invalidate(manifest.id)
    outcome = await _run_step(inst, "install", {"version": manifest.version})
    if not outcome.ok:
        _purge(db, manifest.id, keep_data=False)
        db.delete(inst)
        db.commit()
        runtime.invalidate(manifest.id)
        raise LifecycleError(f"The plugin's install step failed: {outcome.refused or outcome.error}")
    audit("plugin.install", plugin_id=manifest.id, actor_user_id=actor_user_id, resource_type="plugin",
          resource_id=manifest.id, metadata={"version": manifest.version, "permissions": sorted(manifest.permissions)},
          db=db)
    db.commit()
    if enable:
        await set_enabled(db, inst, True, actor_user_id=actor_user_id)
    return inst


async def set_enabled(db: Session, inst: PluginInstallation, enabled: bool, *, actor_user_id: Optional[int] = None) -> None:
    if enabled:
        if inst.status == PluginStatus.FILES_CHANGED:
            raise LifecycleError("The files on disk changed; review and upgrade before enabling")
        if inst.status == PluginStatus.ENABLED:
            return
        if runtime.load_installed(inst, require_enabled=False) is None:
            raise LifecycleError(
                "The plugin cannot be loaded: its files are missing, changed or broken; review and upgrade"
            )
        outcome = await _run_step(inst, "enable", {})
        if not outcome.ok:
            raise LifecycleError(f"The plugin's enable step failed: {outcome.refused or outcome.error}")
        inst.status = PluginStatus.ENABLED
        inst.disabled_reason = None
        health.reset(db, inst)
        action = "plugin.enable"
    else:
        if inst.status == PluginStatus.ENABLED:
            await _run_step(inst, "disable", {}, timeout=DISABLE_TIMEOUT)
        if inst.status in (PluginStatus.ENABLED, PluginStatus.FAILED):
            inst.status = PluginStatus.DISABLED
        action = "plugin.disable"
    audit(action, plugin_id=inst.id, actor_user_id=actor_user_id, resource_type="plugin", resource_id=inst.id, db=db)
    db.commit()
    runtime.invalidate(inst.id)


async def upgrade(
    db: Session, inst: PluginInstallation, *, accept_permissions: list[str], actor_user_id: Optional[int] = None
) -> PluginInstallation:
    try:
        package = find_package(inst.directory)
    except PluginLoadError as exc:
        raise LifecycleError(str(exc)) from exc
    if package.reserved:
        raise LifecycleError("; ".join(package.errors))
    if package.manifest is None:
        raise LifecycleError("; ".join(package.errors) or "The package is not valid")
    manifest = package.manifest
    if manifest.id != inst.id:
        raise LifecycleError("The package on disk has a different plugin id")
    old, new = parse_semver(inst.version), parse_semver(manifest.version)
    if new < old:
        raise LifecycleError("Downgrades are not supported")
    if new == old and package.code_hash == inst.code_hash:
        raise LifecycleError("This version is already installed")
    added = set(manifest.permissions) - set(inst.granted_permissions or [])
    missing = added - set(accept_permissions or [])
    if missing:
        raise PermissionsNotAccepted(list(missing))
    _load_check(package)
    from_version = inst.version
    was_running = inst.status in (PluginStatus.ENABLED, PluginStatus.FILES_CHANGED)
    if inst.status == PluginStatus.ENABLED:
        await _run_step(inst, "disable", {}, timeout=DISABLE_TIMEOUT)
    inst.name = manifest.name
    inst.version = manifest.version
    inst.manifest = package.raw
    inst.code_hash = package.code_hash
    inst.effective_api = manifest.effective_api()
    inst.granted_permissions = sorted(manifest.permissions)
    inst.status = PluginStatus.DISABLED
    inst.disabled_reason = None
    health.reset(db, inst)
    settings_store.apply_defaults(db, inst.id, manifest)
    db.commit()
    runtime.invalidate(inst.id)
    outcome = await _run_step(inst, "upgrade", {"from_version": from_version, "to_version": manifest.version})
    if not outcome.ok:
        inst.status = PluginStatus.FAILED
        inst.disabled_reason = f"The upgrade step failed: {outcome.refused or outcome.error}"[:500]
    elif was_running:
        enable_outcome = await _run_step(inst, "enable", {})
        if enable_outcome.ok:
            inst.status = PluginStatus.ENABLED
        else:
            inst.status = PluginStatus.FAILED
            inst.disabled_reason = f"The enable step failed: {enable_outcome.refused or enable_outcome.error}"[:500]
    audit("plugin.upgrade", plugin_id=inst.id, actor_user_id=actor_user_id, resource_type="plugin", resource_id=inst.id,
          metadata={"from_version": from_version, "to_version": manifest.version, "added_permissions": sorted(added)},
          db=db)
    db.commit()
    runtime.invalidate(inst.id)
    return inst


async def uninstall(db: Session, inst: PluginInstallation, *, keep_data: bool = False, actor_user_id: Optional[int] = None) -> None:
    plugin_id = inst.id
    if inst.status == PluginStatus.ENABLED:
        await _run_step(inst, "disable", {}, timeout=DISABLE_TIMEOUT)
    inst.status = PluginStatus.DISABLED
    db.commit()
    runtime.invalidate(plugin_id)
    await _run_step(inst, "uninstall", {"keep_data": keep_data}, timeout=DISABLE_TIMEOUT)
    _purge(db, plugin_id, keep_data=keep_data)
    db.delete(inst)
    audit("plugin.uninstall", plugin_id=plugin_id, actor_user_id=actor_user_id, resource_type="plugin",
          resource_id=plugin_id, metadata={"keep_data": keep_data}, db=db)
    db.commit()
    runtime.invalidate(plugin_id)


# ------------------------------------------------------------------ views for the admin API


def _iso(value) -> Optional[str]:
    value = aware(value)
    return value.isoformat() if value else None


def available_view(package: Package, installed: Optional[PluginInstallation]) -> dict:
    manifest = package.manifest
    raw = package.raw or {}
    return {
        "directory": package.directory,
        "id": manifest.id if manifest else raw.get("id"),
        "name": manifest.name if manifest else raw.get("name") or package.directory,
        "version": manifest.version if manifest else raw.get("version"),
        "description": manifest.description if manifest else raw.get("description") or "",
        "author": manifest.author if manifest else raw.get("author"),
        "permissions": describe_permissions(manifest.permissions if manifest else []),
        "valid": package.valid,
        "errors": list(package.errors),
        "installed": installed is not None,
        "installed_version": installed.version if installed else None,
        "bundled": package.bundled,
    }


def _upgrade_view(inst: PluginInstallation, package: Optional[Package]) -> Optional[dict]:
    if package is None or package.manifest is None:
        return None
    try:
        newer = parse_semver(package.manifest.version) > parse_semver(inst.version)
    except ValueError:
        return None
    changed = package.manifest.version == inst.version and package.code_hash != inst.code_hash
    if not newer and not changed:
        return None
    granted = set(inst.granted_permissions or [])
    wanted = set(package.manifest.permissions)
    return {
        "version": package.manifest.version,
        "added_permissions": describe_permissions(wanted - granted),
        "removed_permissions": describe_permissions(granted - wanted),
        "error": "; ".join(package.errors) or None,
    }


def summary(db: Session, inst: PluginInstallation, package: Optional[Package] = None) -> dict:
    try:
        manifest: Optional[Manifest] = runtime.manifest_of(inst)
    except Exception:  # noqa: BLE001
        manifest = None
    counts = outbox.counts(db, inst.id)
    return {
        "id": inst.id,
        "name": inst.name,
        "version": inst.version,
        "effective_api": inst.effective_api,
        "bundled": bool(package and package.bundled),
        "description": manifest.description if manifest else "",
        "author": manifest.author if manifest else None,
        "homepage": manifest.homepage if manifest else None,
        "licence": manifest.licence if manifest else None,
        "status": inst.status,
        "disabled_reason": inst.disabled_reason,
        "permissions": describe_permissions(inst.granted_permissions),
        "health": {
            "state": health.state_of(inst),
            "consecutive_failures": inst.consecutive_failures or 0,
            "total_failures": inst.total_failures or 0,
            "last_error": inst.last_error,
            "last_error_at": _iso(inst.last_error_at),
            "last_success_at": _iso(inst.last_success_at),
            "circuit_open_until": _iso(inst.circuit_open_until) if health.circuit_open(inst) else None,
            "queued_deliveries": counts["queued"],
            "dead_deliveries": counts["dead"],
        },
        "upgrade": _upgrade_view(inst, package),
        "services": manifest.services.model_dump(mode="json") if manifest and manifest.services else None,
        "addon_refs": list(inst.addon_refs or []),
        "settings_schema": settings_store.schema_of(manifest) if manifest else [],
        "actions": [{"id": a.id, "label": a.label, "audience": a.audience} for a in (manifest.actions if manifest else [])],
        "schedules": [
            {"id": s.id, "every": s.every, "scope": s.scope} for s in (manifest.schedules if manifest else [])
        ],
        "events": list(manifest.events) if manifest else [],
        "hooks": list(manifest.hooks) if manifest else [],
        "mcp_tools": [
            {"id": t.id, "description": t.description, "audience": t.audience, "scope": t.scope}
            for t in (manifest.mcp_tools if manifest else [])
        ],
        "provides": [
            {"kind": p.kind, "id": p.id, "label": p.label, "ref": f"{inst.id}:{p.id}"}
            for p in (manifest.provides if manifest else [])
        ],
        "installed_at": _iso(inst.installed_at),
        "updated_at": _iso(inst.updated_at),
    }


def overview(db: Session) -> dict:
    from app.plugin_host.catalogue import API_VERSION
    from app.plugin_host.loader import bundled_root, plugins_root

    installed = list(db.execute(select(PluginInstallation).order_by(PluginInstallation.id)).scalars())
    by_id = {inst.id: inst for inst in installed}
    packages = discover()
    package_by_id = {p.manifest.id: p for p in packages if p.manifest is not None and not p.reserved}
    package_by_dir = {p.directory: p for p in packages if not p.reserved}
    return {
        "api_version": API_VERSION,
        "plugins_dir": str(plugins_root()),
        "bundled_plugins_dir": str(bundled_root()),
        "installed": [summary(db, inst, package_by_dir.get(inst.directory)) for inst in installed],
        "available": [
            available_view(p, by_id.get(p.manifest.id) if p.manifest and not p.reserved else None)
            for p in packages
            if p.manifest is None or p.reserved or p.manifest.id not in by_id or p is package_by_id.get(p.manifest.id)
        ],
    }
