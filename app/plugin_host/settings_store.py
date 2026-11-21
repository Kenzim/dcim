"""Plugin settings with sealed secrets (docs/plugin-system.md section 5)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings as app_settings
from app.core.service_instance_crypto import is_encryption_configured
from app.models.plugin_host import PluginSetting
from app.plugin_host.manifest import Manifest, coerce_value
from app.services.alerting.secrets import seal, unseal

MASK = "\u2022" * 8


class SettingsError(ValueError):
    pass


def _rows(db: Session, plugin_id: str) -> dict[str, PluginSetting]:
    rows = db.execute(select(PluginSetting).where(PluginSetting.plugin_id == plugin_id)).scalars()
    return {row.key: row for row in rows}


def schema_of(manifest: Manifest) -> list[dict]:
    return [f.model_dump(mode="json", by_alias=True) for f in manifest.settings]


def values(db: Session, plugin_id: str, manifest: Manifest) -> dict[str, Any]:
    """Current values with secrets decrypted, for plugin code only."""
    rows = _rows(db, plugin_id)
    out: dict[str, Any] = {}
    for field in manifest.settings:
        row = rows.get(field.key)
        if field.type == "secret":
            out[field.key] = unseal(row.sealed_value) if row is not None else None
        else:
            out[field.key] = row.value if row is not None else field.default
    return out


def api_view(db: Session, plugin_id: str, manifest: Manifest) -> dict:
    rows = _rows(db, plugin_id)
    shown: dict[str, Any] = {}
    secrets_set: dict[str, bool] = {}
    for field in manifest.settings:
        row = rows.get(field.key)
        if field.type == "secret":
            is_set = row is not None and bool(row.sealed_value)
            secrets_set[field.key] = is_set
            shown[field.key] = MASK if is_set else None
        else:
            shown[field.key] = row.value if row is not None else field.default
    return {"schema": schema_of(manifest), "values": shown, "secrets_set": secrets_set}


def apply_defaults(db: Session, plugin_id: str, manifest: Manifest) -> None:
    rows = _rows(db, plugin_id)
    for field in manifest.settings:
        if field.key not in rows and field.type != "secret" and field.default is not None:
            db.add(PluginSetting(plugin_id=plugin_id, key=field.key, value=field.default))
    db.flush()


def update(db: Session, plugin_id: str, manifest: Manifest, incoming: dict[str, Any]) -> list[str]:
    """Validate and store new values. Returns the keys that changed (secret values never appear)."""
    if not isinstance(incoming, dict):
        raise SettingsError("values must be an object")
    fields = manifest.settings_by_key
    unknown = sorted(set(incoming) - set(fields))
    if unknown:
        raise SettingsError(f"Unknown settings: {', '.join(unknown)}")
    rows = _rows(db, plugin_id)
    errors: list[str] = []
    staged: dict[str, tuple[str, Any]] = {}
    for key, raw in incoming.items():
        field = fields[key]
        if field.type == "secret":
            if raw == MASK or raw is None:
                continue
            if raw == "":
                staged[key] = ("clear", None)
                continue
            try:
                coerce_value(field, raw)
            except ValueError as exc:
                errors.append(f"{field.label}: {exc}")
                continue
            if app_settings.require_service_instance_encryption and not is_encryption_configured():
                errors.append(f"{field.label}: set SERVICE_INSTANCE_ENCRYPTION_KEY before saving secrets")
                continue
            staged[key] = ("secret", raw)
            continue
        try:
            value = coerce_value(field, raw if raw != "" or field.type in ("string", "text") else None)
        except ValueError as exc:
            errors.append(f"{field.label}: {exc}")
            continue
        if value in (None, "") and field.required:
            errors.append(f"{field.label} is required")
            continue
        staged[key] = ("value", value)
    if errors:
        raise SettingsError("; ".join(errors))
    changed = []
    for key, (kind, value) in staged.items():
        row = rows.get(key)
        if row is None:
            row = PluginSetting(plugin_id=plugin_id, key=key)
            db.add(row)
        if kind == "secret":
            row.sealed_value = seal(value)
            row.value = None
        elif kind == "clear":
            row.sealed_value = None
            row.value = None
        else:
            if row.value == value:
                continue
            row.value = value
        changed.append(key)
    db.flush()
    return changed
