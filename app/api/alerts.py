"""Staff API for alerts, silences, channels, recipients and alert settings."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import require_admin
from app.core.database import get_db
from app.models.alerting import AlertChannel
from app.services.alerting import admin as svc
from app.services.alerting import catalogue, status as eval_status
from app.services.alerting.catalogue import CATALOGUE, effective_settings

DbDep = Annotated[Session, Depends(get_db)]
AdminDep = Annotated[dict, Depends(require_admin)]

router = APIRouter(prefix="/admin/alerts", tags=["alerts-admin"])


def _fail(exc: Exception) -> HTTPException:
    if isinstance(exc, svc.NotFound):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


class AcknowledgeBody(BaseModel):
    note: Optional[str] = Field(default=None, max_length=512)


class SilenceBody(BaseModel):
    alert_type: Optional[str] = None
    resource_type: Optional[str] = None
    resource_id: Optional[str] = Field(default=None, max_length=128)
    duration_minutes: Optional[int] = Field(default=None, ge=1, le=60 * 24 * 90)
    ends_at: Optional[datetime] = None
    reason: Optional[str] = Field(default=None, max_length=512)


class AlertSilenceBody(BaseModel):
    duration_minutes: int = Field(ge=1, le=60 * 24 * 90)
    reason: Optional[str] = Field(default=None, max_length=512)


class ChannelBody(BaseModel):
    name: str
    kind: str
    enabled: bool = True
    min_severity: str = "warning"
    alert_types: list[str] = Field(default_factory=list)
    send_resolved: bool = True
    addresses: Optional[list[str]] = None
    url: Optional[str] = None
    secret: Optional[str] = None


class ChannelUpdate(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    min_severity: Optional[str] = None
    alert_types: Optional[list[str]] = None
    send_resolved: Optional[bool] = None
    addresses: Optional[list[str]] = None
    url: Optional[str] = None
    secret: Optional[str] = None


class RecipientItem(BaseModel):
    user_id: int
    enabled: bool
    min_severity: str = "warning"


class RecipientsBody(BaseModel):
    recipients: list[RecipientItem]


class TypeSettingBody(BaseModel):
    enabled: Optional[bool] = None
    severity: Optional[str] = None
    params: Optional[dict[str, float]] = None
    reminder_interval_seconds: Optional[int] = None
    hold_down_seconds: Optional[int] = None
    reset: bool = False


class GlobalSettingsBody(BaseModel):
    settings: dict[str, int]


@router.get("")
def list_alerts(
    db: DbDep,
    _: AdminDep,
    state: str = "open",
    severity: Optional[str] = None,
    type: Optional[str] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    acknowledged: Optional[bool] = None,
    q: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    try:
        return svc.list_alerts(
            db,
            state=state,
            severity=severity,
            alert_type=type,
            resource_type=resource_type,
            resource_id=resource_id,
            acknowledged=acknowledged,
            query=q,
            limit=limit,
            offset=offset,
        )
    except svc.AlertError as exc:
        raise _fail(exc)


@router.get("/summary")
def summary(db: DbDep, _: AdminDep) -> dict[str, Any]:
    return svc.summary(db)


@router.post("/evaluate")
def evaluate_now(_: AdminDep) -> dict[str, Any]:
    from app.workers.alerts import evaluate_once

    result = evaluate_once(owner=f"manual-{uuid.uuid4().hex[:8]}")
    if result is None:
        raise HTTPException(status_code=409, detail="An evaluation is already running")
    return {
        "opened": result.opened,
        "reopened": result.reopened,
        "resolved": result.resolved,
        "notices": len(result.notices),
        "failed_types": result.failed_types,
    }


@router.get("/types")
def list_types(db: DbDep, _: AdminDep) -> dict[str, Any]:
    items = []
    for key, eff in effective_settings(db).items():
        spec = CATALOGUE[key]
        if spec.audience != "staff":
            continue
        items.append(
            {
                "type": key,
                "title": spec.title,
                "description": spec.description,
                "enabled": eff.enabled,
                "severity": eff.severity,
                "default_severity": spec.severity,
                "reminder_interval_seconds": eff.reminder_interval_seconds,
                "hold_down_seconds": eff.hold_down_seconds,
                "overridden": eff.overridden,
                "params": [
                    {
                        "key": p.key,
                        "label": p.label,
                        "unit": p.unit,
                        "default": p.default,
                        "value": eff.params[p.key],
                        "min": p.minimum,
                        "max": p.maximum,
                    }
                    for p in spec.params
                ],
            }
        )
    return {"items": items, "global": catalogue.get_global_settings(db), "status": eval_status.read(db)}


@router.put("/types/{alert_type}")
def update_type(alert_type: str, body: TypeSettingBody, db: DbDep, _: AdminDep) -> dict[str, Any]:
    try:
        catalogue.update_type_setting(
            db,
            alert_type,
            enabled=body.enabled,
            severity=body.severity,
            params=body.params,
            reminder_interval_seconds=body.reminder_interval_seconds,
            hold_down_seconds=body.hold_down_seconds,
            reset=body.reset,
        )
        db.commit()
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown alert type")
    except ValueError as exc:
        db.rollback()
        raise _fail(exc)
    return {"ok": True}


@router.get("/settings")
def get_settings(db: DbDep, _: AdminDep) -> dict[str, int]:
    return catalogue.get_global_settings(db)


@router.put("/settings")
def put_settings(body: GlobalSettingsBody, db: DbDep, _: AdminDep) -> dict[str, int]:
    try:
        result = catalogue.update_global_settings(db, body.settings)
        db.commit()
        return result
    except ValueError as exc:
        db.rollback()
        raise _fail(exc)


@router.get("/silences")
def list_silences(db: DbDep, _: AdminDep, include_expired: bool = False) -> dict[str, Any]:
    return {"items": svc.list_silences(db, include_expired=include_expired)}


@router.post("/silences")
def create_silence(body: SilenceBody, db: DbDep, auth: AdminDep) -> dict[str, Any]:
    try:
        row = svc.create_silence(db, user_id=auth.get("user_id"), **body.model_dump())
    except svc.AlertError as exc:
        raise _fail(exc)
    return {"id": row.id}


@router.delete("/silences/{silence_id}")
def delete_silence(silence_id: int, db: DbDep, _: AdminDep) -> dict[str, bool]:
    try:
        svc.delete_silence(db, silence_id)
    except svc.NotFound as exc:
        raise _fail(exc)
    return {"ok": True}


@router.get("/channels")
def list_channels(db: DbDep, _: AdminDep) -> dict[str, Any]:
    rows = db.query(AlertChannel).order_by(AlertChannel.name).all()
    return {"items": [svc.serialise_channel(c) for c in rows]}


@router.post("/channels")
def create_channel(body: ChannelBody, db: DbDep, auth: AdminDep) -> dict[str, Any]:
    try:
        channel = svc.create_channel(db, user_id=auth.get("user_id"), **body.model_dump())
    except svc.AlertError as exc:
        db.rollback()
        raise _fail(exc)
    return svc.serialise_channel(channel)


@router.put("/channels/{channel_id}")
def update_channel(channel_id: int, body: ChannelUpdate, db: DbDep, _: AdminDep) -> dict[str, Any]:
    try:
        channel = svc.update_channel(db, channel_id, **body.model_dump(exclude_unset=True))
    except (svc.AlertError, svc.NotFound) as exc:
        db.rollback()
        raise _fail(exc)
    return svc.serialise_channel(channel)


@router.delete("/channels/{channel_id}")
def delete_channel(channel_id: int, db: DbDep, _: AdminDep) -> dict[str, bool]:
    try:
        svc.delete_channel(db, channel_id)
    except svc.NotFound as exc:
        raise _fail(exc)
    return {"ok": True}


@router.post("/channels/{channel_id}/test")
def test_channel(channel_id: int, db: DbDep, _: AdminDep) -> dict[str, Any]:
    try:
        return svc.send_test_notice(db, channel_id)
    except svc.NotFound as exc:
        raise _fail(exc)


@router.get("/recipients")
def list_recipients(db: DbDep, _: AdminDep) -> dict[str, Any]:
    return {"items": svc.list_recipients(db)}


@router.put("/recipients")
def put_recipients(body: RecipientsBody, db: DbDep, _: AdminDep) -> dict[str, Any]:
    try:
        for item in body.recipients:
            svc.set_recipient(db, item.user_id, enabled=item.enabled, min_severity=item.min_severity)
    except (svc.AlertError, svc.NotFound) as exc:
        db.rollback()
        raise _fail(exc)
    return {"items": svc.list_recipients(db)}


@router.get("/deliveries")
def list_deliveries(
    db: DbDep,
    _: AdminDep,
    status: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    return svc.list_deliveries(db, status_filter=status, limit=limit, offset=offset)


@router.get("/{alert_id}")
def get_alert(alert_id: int, db: DbDep, _: AdminDep) -> dict[str, Any]:
    try:
        return svc.alert_detail(db, alert_id)
    except svc.NotFound as exc:
        raise _fail(exc)


@router.post("/{alert_id}/acknowledge")
def acknowledge(alert_id: int, body: AcknowledgeBody, db: DbDep, auth: AdminDep) -> dict[str, Any]:
    try:
        svc.acknowledge(db, alert_id, auth.get("user_id"), body.note)
        return svc.alert_detail(db, alert_id)
    except (svc.AlertError, svc.NotFound) as exc:
        raise _fail(exc)


@router.post("/{alert_id}/unacknowledge")
def unacknowledge(alert_id: int, db: DbDep, _: AdminDep) -> dict[str, Any]:
    try:
        svc.unacknowledge(db, alert_id)
        return svc.alert_detail(db, alert_id)
    except svc.NotFound as exc:
        raise _fail(exc)


@router.post("/{alert_id}/silence")
def silence_alert(alert_id: int, body: AlertSilenceBody, db: DbDep, auth: AdminDep) -> dict[str, Any]:
    try:
        row = svc.silence_alert(
            db, alert_id, user_id=auth.get("user_id"), duration_minutes=body.duration_minutes, reason=body.reason
        )
    except (svc.AlertError, svc.NotFound) as exc:
        raise _fail(exc)
    return {"id": row.id}
