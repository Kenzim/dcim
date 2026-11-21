"""Admin API for document numbering series (docs/billing-phase1-design.md 5.2 and 5.3)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import require_admin
from app.core.database import get_db
from app.core.openapi_responses import COMMON_ERROR_RESPONSES
from app.models.billing_series import DocumentSeries, DocumentType
from app.services.audit_service import AuditService
from app.services.invoice_numbering import (
    NumberingError,
    counter_next_value,
    series_in_use,
    series_max_value,
    set_counter_at_least,
    validate_series_config,
)

DbDep = Annotated[Session, Depends(get_db)]
AdminDep = Annotated[dict, Depends(require_admin)]

router = APIRouter(
    prefix="/admin/billing/series",
    tags=["billing-series-admin"],
    dependencies=[Depends(require_admin)],
    responses=COMMON_ERROR_RESPONSES,
)

DocumentTypeName = Literal["invoice", "proforma", "credit_note", "debit_note"]
NumberSource = Literal["own", "invoice_number"]
ResetPeriod = Literal["never", "yearly"]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SeriesCreate(_Body):
    code: str = Field(min_length=1, max_length=16)
    document_type: DocumentTypeName
    name: str = Field(min_length=1, max_length=255)
    format: str = Field(min_length=1, max_length=64)
    number_padding: StrictInt = Field(default=0, ge=0, le=12)
    number_source: NumberSource = "own"
    gapless: StrictBool = False
    reset_period: ResetPeriod = "never"
    start_value: Optional[StrictInt] = Field(default=None, ge=1)
    enabled: StrictBool = True


class SeriesUpdate(_Body):
    code: Optional[str] = Field(default=None, min_length=1, max_length=16)
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    format: Optional[str] = Field(default=None, min_length=1, max_length=64)
    number_padding: Optional[StrictInt] = Field(default=None, ge=0, le=12)
    number_source: Optional[NumberSource] = None
    gapless: Optional[StrictBool] = None
    reset_period: Optional[ResetPeriod] = None
    start_value: Optional[StrictInt] = Field(default=None, ge=1)
    enabled: Optional[StrictBool] = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _type(series: DocumentSeries) -> str:
    return getattr(series.document_type, "value", series.document_type)


def _serialize(db: Session, series: DocumentSeries) -> dict:
    own = series.number_source == "own"
    return {
        "id": series.id,
        "code": series.code,
        "document_type": _type(series),
        "name": series.name,
        "format": series.format,
        "number_padding": int(series.number_padding or 0),
        "number_source": series.number_source,
        "gapless": bool(series.gapless),
        "reset_period": series.reset_period,
        "default_for": series.default_for,
        "is_default": series.default_for is not None,
        "enabled": bool(series.enabled),
        "in_use": series_in_use(db, series.id),
        "next_value": counter_next_value(db, series, _now()) if own else None,
        "created_at": series.created_at,
        "updated_at": series.updated_at,
    }


def _get_or_404(db: Session, series_id: int) -> DocumentSeries:
    series = db.get(DocumentSeries, series_id)
    if series is None:
        raise HTTPException(status_code=404, detail="Series not found")
    return series


def _validate(**config) -> None:
    try:
        validate_series_config(**config)
    except NumberingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _code_taken(db: Session, code: str, series_id: Optional[int] = None) -> bool:
    stmt = select(DocumentSeries.id).where(DocumentSeries.code == code)
    if series_id is not None:
        stmt = stmt.where(DocumentSeries.id != series_id)
    return db.scalar(stmt) is not None


def _audit(db: Session, auth: dict, action: str, series: DocumentSeries, metadata: dict) -> None:
    AuditService.log(
        db,
        actor_user_id=auth.get("user_id"),
        action=f"billing_series.{action}",
        resource_type="document_series",
        resource_id=str(series.id),
        metadata=metadata,
    )


def _set_counter(db: Session, series: DocumentSeries, value: int) -> None:
    try:
        set_counter_at_least(db, series, value, _now())
    except NumberingError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("")
def list_series(db: DbDep):
    rows = db.execute(select(DocumentSeries).order_by(DocumentSeries.id)).scalars()
    return [_serialize(db, row) for row in rows]


@router.get("/{series_id}")
def get_series(series_id: int, db: DbDep):
    return _serialize(db, _get_or_404(db, series_id))


@router.post("", status_code=201)
def create_series(body: SeriesCreate, auth: AdminDep, db: DbDep):
    _validate(
        code=body.code,
        document_type=body.document_type,
        format=body.format,
        number_padding=body.number_padding,
        number_source=body.number_source,
        gapless=body.gapless,
        reset_period=body.reset_period,
    )
    if body.start_value is not None and body.number_source != "own":
        raise HTTPException(status_code=422, detail="start_value is only for an 'own' series")
    if _code_taken(db, body.code):
        raise HTTPException(status_code=409, detail=f"series code {body.code!r} is taken")
    series = DocumentSeries(
        code=body.code,
        document_type=DocumentType(body.document_type),
        name=body.name,
        format=body.format,
        number_padding=body.number_padding,
        number_source=body.number_source,
        gapless=body.gapless,
        reset_period=body.reset_period,
        enabled=body.enabled,
        default_for=None,
    )
    db.add(series)
    db.flush()
    if body.start_value is not None and body.start_value > 1:
        _set_counter(db, series, body.start_value)
    _audit(db, auth, "create", series, body.model_dump())
    db.commit()
    db.refresh(series)
    return _serialize(db, series)


@router.patch("/{series_id}")
def update_series(series_id: int, body: SeriesUpdate, auth: AdminDep, db: DbDep):
    series = _get_or_404(db, series_id)
    changes = body.model_dump(exclude_unset=True)
    start_value = changes.pop("start_value", None)
    if any(value is None for value in changes.values()):
        raise HTTPException(status_code=422, detail="fields may not be set to null")
    merged = {
        "code": changes.get("code", series.code),
        "document_type": _type(series),
        "format": changes.get("format", series.format),
        "number_padding": changes.get("number_padding", int(series.number_padding or 0)),
        "number_source": changes.get("number_source", series.number_source),
        "gapless": changes.get("gapless", bool(series.gapless)),
        "reset_period": changes.get("reset_period", series.reset_period),
    }
    _validate(**merged)
    used = series_in_use(db, series.id)
    if "code" in changes and changes["code"] != series.code:
        if used:
            raise HTTPException(status_code=409, detail="the code of a series in use cannot change")
        if _code_taken(db, changes["code"], series.id):
            raise HTTPException(status_code=409, detail=f"series code {changes['code']!r} is taken")
    if used and merged["reset_period"] != series.reset_period:
        raise HTTPException(
            status_code=409, detail="the reset period of a series in use cannot change"
        )
    switch_to_own = series.number_source == "invoice_number" and merged["number_source"] == "own"
    if series.number_source == "own" and merged["number_source"] == "invoice_number":
        if used or counter_next_value(db, series, _now()) > 1:
            raise HTTPException(
                status_code=409,
                detail="a series that has issued numbers cannot switch to invoice_number",
            )
    if start_value is not None and merged["number_source"] != "own":
        raise HTTPException(status_code=422, detail="start_value is only for an 'own' series")
    if changes.get("enabled") is False and series.default_for is not None:
        raise HTTPException(
            status_code=409,
            detail="the default series cannot be disabled; make another series default first",
        )
    before = _serialize(db, series)
    for name, value in changes.items():
        setattr(series, name, value)
    db.flush()
    if switch_to_own:
        _set_counter(db, series, max(start_value or 0, series_max_value(db, series.id) + 1))
    elif start_value is not None:
        _set_counter(db, series, start_value)
    _audit(
        db,
        auth,
        "update",
        series,
        {
            "changes": changes,
            "start_value": start_value,
            "before": {key: before[key] for key in changes},
        },
    )
    db.commit()
    db.refresh(series)
    return _serialize(db, series)


@router.post("/{series_id}/make-default")
def make_default(series_id: int, auth: AdminDep, db: DbDep):
    series = _get_or_404(db, series_id)
    if not series.enabled:
        raise HTTPException(status_code=409, detail="a disabled series cannot be the default")
    document_type = _type(series)
    previous = db.scalar(
        select(DocumentSeries).where(DocumentSeries.default_for == document_type)
    )
    if previous is not None and previous.id == series.id:
        return _serialize(db, series)
    if previous is not None:
        previous.default_for = None
        # The unique key on default_for needs the old default cleared first.
        db.flush()
    series.default_for = document_type
    db.flush()
    _audit(
        db,
        auth,
        "make_default",
        series,
        {"document_type": document_type, "previous_id": previous.id if previous else None},
    )
    db.commit()
    db.refresh(series)
    return _serialize(db, series)


@router.delete("/{series_id}", status_code=204)
def delete_series(series_id: int, auth: AdminDep, db: DbDep):
    series = _get_or_404(db, series_id)
    if series.default_for is not None or series_in_use(db, series.id):
        raise HTTPException(
            status_code=409,
            detail="a default series or one in use cannot be deleted; disable it instead",
        )
    _audit(db, auth, "delete", series, {"code": series.code})
    db.delete(series)
    db.commit()
    return None
