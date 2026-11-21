"""Admin API for the typed automation and billing settings store."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.core.auth import require_admin
from app.core.database import get_db
from app.core.openapi_responses import COMMON_ERROR_RESPONSES
from app.services import settings_store
from app.services.settings_store import SettingValidationError

DbDep = Annotated[Session, Depends(get_db)]
AdminDep = Annotated[dict, Depends(require_admin)]

router = APIRouter(
    prefix="/admin/settings",
    tags=["settings-admin"],
    dependencies=[Depends(require_admin)],
    responses=COMMON_ERROR_RESPONSES,
)


class SettingUpdateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Any


def _item(key: str, resolved: settings_store.ResolvedSetting) -> dict[str, Any]:
    definition = settings_store.SCHEMA[key]
    group = settings_store.GROUPS[definition.group]
    return {
        "key": key,
        "group": group.key,
        "group_label": group.label,
        "description": definition.description,
        "value_type": definition.value_type,
        "value": resolved.value,
        "default": resolved.default,
        "source": resolved.source,
        "requires_restart": definition.requires_restart,
        "validation": definition.describe_validation(),
    }


def _definition_or_404(key: str) -> None:
    if key not in settings_store.SCHEMA:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown setting")


@router.get("", responses=COMMON_ERROR_RESPONSES)
def list_settings(db: DbDep):
    resolved = settings_store.resolve_all(db)
    return {
        "groups": [
            {"key": group.key, "label": group.label, "description": group.description}
            for group in settings_store.GROUPS.values()
        ],
        "items": [_item(key, resolved[key]) for key in settings_store.SCHEMA],
    }


@router.put("/{key}", responses=COMMON_ERROR_RESPONSES)
def update_setting(key: str, body: SettingUpdateBody, db: DbDep, auth: AdminDep):
    _definition_or_404(key)
    try:
        settings_store.set_setting(db, key, body.value, auth.get("user_id"))
    except SettingValidationError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    db.commit()
    return _item(key, settings_store.resolve_all(db)[key])


@router.delete("/{key}", responses=COMMON_ERROR_RESPONSES)
def reset_setting(key: str, db: DbDep, auth: AdminDep):
    _definition_or_404(key)
    settings_store.reset_setting(db, key, auth.get("user_id"))
    db.commit()
    return _item(key, settings_store.resolve_all(db)[key])
