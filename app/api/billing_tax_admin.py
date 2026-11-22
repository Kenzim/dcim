"""Admin API for tax (docs/billing-phase2-design.md 6.2); every write audited ``billing.tax.*``.

Rules are versioned (design 1.2): a rule whose ``valid_from`` has passed may
have been used by a publish, so it is never edited in place; it is
superseded from a time not in the past. Packs, legal notes and tax ID checks
call the packs worker's services.
"""

from __future__ import annotations

import re
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Any, ClassVar, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import require_admin
from app.core.database import get_db
from app.core.openapi_responses import COMMON_ERROR_RESPONSES
from app.models.commerce_account import BillingAccount, BillingProfile
from app.models.product_catalog import Product
from app.models.tax import (
    JURISDICTION_ANY,
    InvoiceLineTax,
    SellerTaxRegistration,
    TaxClass,
    TaxExemption,
    TaxIdCheck,
    TaxLegalNote,
    TaxRegistrationScheme,
    TaxRule,
    TaxRuleSource,
)
from app.services.audit_service import AuditService
from app.services.billing.rounding import TAX_ROUNDING_RULES
from app.services.tax.rules_repo import MAPPED_RULE_COUNTRIES, TaxRateError, normalise_rate

DbDep = Annotated[Session, Depends(get_db)]
AdminDep = Annotated[dict, Depends(require_admin)]

router = APIRouter(
    prefix="/admin/billing",
    tags=["billing-tax-admin"],
    dependencies=[Depends(require_admin)],
    responses=COMMON_ERROR_RESPONSES,
)

# A time a little in the past still counts as now (a request in flight).
NOW_TOLERANCE = timedelta(seconds=60)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _conflict(code: str, message: str, **extra) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": code, "message": message, **extra})


def _unprocessable(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=422, detail={"code": code, "message": message})


def _audit(db: Session, auth: dict, action: str, resource_type: str, resource_id: Any, metadata: dict) -> None:
    AuditService.log(
        db,
        actor_user_id=auth["user_id"],
        action=f"billing.tax.{action}",
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
        metadata=metadata,
    )


def _json(value: Any) -> Any:
    if is_dataclass(value):
        return _json(asdict(value))
    if isinstance(value, dict):
        return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(v) for v in value]
    if isinstance(value, datetime):
        return _aware(value).isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return getattr(value, "value", value)


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Fields that may be left out but not sent as null: their columns are NOT NULL.
    not_null: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="before")
    @classmethod
    def _refuse_nulls(cls, data: Any) -> Any:
        if isinstance(data, dict):
            nulls = sorted(name for name in cls.not_null if name in data and data[name] is None)
            if nulls:
                raise ValueError(f"{', '.join(nulls)} may not be null")
        return data


# --- classes ---


class ClassCreate(RequestModel):
    code: str = Field(min_length=1, max_length=32, pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1, max_length=128)
    description: Optional[str] = Field(default=None, max_length=512)


class ClassUpdate(RequestModel):
    not_null: ClassVar[frozenset[str]] = frozenset({"name"})

    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    description: Optional[str] = Field(default=None, max_length=512)


def _class_payload(row: TaxClass) -> dict:
    return {"id": row.id, "code": row.code, "name": row.name, "description": row.description, "system": row.system}


@router.get("/tax/classes")
def list_classes(db: DbDep):
    return [_class_payload(row) for row in db.scalars(select(TaxClass).order_by(TaxClass.id))]


@router.post("/tax/classes", status_code=status.HTTP_201_CREATED)
def create_class(body: ClassCreate, auth: AdminDep, db: DbDep):
    if db.scalar(select(TaxClass.id).where(TaxClass.code == body.code)) is not None:
        raise _conflict("tax_class_exists", "A tax class with this code exists")
    row = TaxClass(code=body.code, name=body.name, description=body.description, system=False)
    db.add(row)
    db.flush()
    _audit(db, auth, "class.created", "tax_class", row.id, body.model_dump())
    db.commit()
    return _class_payload(row)


@router.put("/tax/classes/{class_id}")
def update_class(class_id: int, body: ClassUpdate, auth: AdminDep, db: DbDep):
    row = db.get(TaxClass, class_id, with_for_update=True)
    if row is None:
        raise HTTPException(status_code=404, detail="Tax class not found")
    data = body.model_dump(exclude_unset=True)
    for name, value in data.items():
        setattr(row, name, value)
    _audit(db, auth, "class.updated", "tax_class", row.id, data)
    db.commit()
    return _class_payload(row)


@router.delete("/tax/classes/{class_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_class(class_id: int, auth: AdminDep, db: DbDep):
    row = db.get(TaxClass, class_id, with_for_update=True)
    if row is None:
        raise HTTPException(status_code=404, detail="Tax class not found")
    if row.system:
        raise _conflict("tax_class_system", "System tax classes cannot be deleted")
    in_use = db.scalar(select(TaxRule.id).where(TaxRule.tax_class_id == row.id).limit(1)) or db.scalar(
        select(Product.id).where(Product.tax_class_id == row.id).limit(1)
    )
    if in_use:
        raise _conflict("tax_class_in_use", "The tax class is used by a rule or a product")
    _audit(db, auth, "class.deleted", "tax_class", row.id, {"code": row.code})
    db.delete(row)
    db.commit()
    return None


# --- rules ---


RULE_FIELDS = (
    "name", "country", "region", "postcode_pattern", "tax_class_id", "customer_type", "rate_percent",
    "priority", "compound", "compound_order", "jurisdiction", "reverse_charge", "reverse_charge_zone",
    "legal_note_key", "rounding_point", "rounding_rule", "valid_to", "enabled",
)


class RuleFields(RequestModel):
    not_null: ClassVar[frozenset[str]] = frozenset({
        "name", "tax_class_id", "customer_type", "rate_percent", "priority", "compound", "compound_order",
        "jurisdiction", "reverse_charge", "rounding_point", "rounding_rule", "enabled",
    })

    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    country: Optional[str] = Field(default=None, min_length=2, max_length=2)
    region: Optional[str] = Field(default=None, max_length=128)
    postcode_pattern: Optional[str] = Field(default=None, max_length=128)
    tax_class_id: Optional[StrictInt] = Field(default=None, gt=0)
    customer_type: Optional[Literal["any", "business", "consumer"]] = None
    rate_percent: Optional[Decimal] = None
    priority: Optional[StrictInt] = Field(default=None, ge=-1000, le=1000)
    compound: Optional[StrictBool] = None
    compound_order: Optional[StrictInt] = Field(default=None, ge=0, le=100)
    jurisdiction: Optional[str] = Field(default=None, min_length=2, max_length=16)
    reverse_charge: Optional[StrictBool] = None
    reverse_charge_zone: Optional[str] = Field(default=None, max_length=16)
    legal_note_key: Optional[str] = Field(default=None, max_length=64)
    rounding_point: Optional[Literal["line", "invoice_rate"]] = None
    rounding_rule: Optional[str] = Field(default=None, max_length=32)
    valid_to: Optional[datetime] = None
    enabled: Optional[StrictBool] = None

    @field_validator("rate_percent")
    @classmethod
    def _rate(cls, value: Optional[Decimal]) -> Optional[Decimal]:
        if value is None:
            return None
        try:
            return normalise_rate(value)
        except TaxRateError as exc:
            raise ValueError(str(exc)) from None

    @field_validator("rounding_rule")
    @classmethod
    def _rounding(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in TAX_ROUNDING_RULES:
            raise ValueError(f"rounding_rule must be one of {sorted(TAX_ROUNDING_RULES)}")
        return value

    @field_validator("postcode_pattern")
    @classmethod
    def _pattern(cls, value: Optional[str]) -> Optional[str]:
        if value:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"postcode_pattern is not a regular expression: {exc}") from None
        return value or None

    @field_validator("country", "jurisdiction")
    @classmethod
    def _upper(cls, value: Optional[str]) -> Optional[str]:
        return value.upper() if value else value


class RuleCreate(RuleFields):
    name: str = Field(min_length=1, max_length=255)
    tax_class_id: StrictInt = Field(gt=0)
    rate_percent: Decimal
    rounding_rule: str = "round_half_up"
    valid_from: Optional[datetime] = None


class RuleSupersede(RuleFields):
    valid_from: datetime


def _rule_payload(row: TaxRule) -> dict:
    payload = {name: _json(getattr(row, name)) for name in RULE_FIELDS}
    payload.update(
        id=row.id,
        rate_percent=str(normalise_rate(row.rate_percent)),
        valid_from=_json(row.valid_from),
        source=_json(row.source),
        pack_id=row.pack_id,
        pack_version=row.pack_version,
        pack_rule_key=row.pack_rule_key,
        legacy_tax_rate_id=row.legacy_tax_rate_id,
        supersedes_id=row.supersedes_id,
        in_effect=bool(row.valid_from and _aware(row.valid_from) <= _now()),
    )
    return payload


def _check_rule(db: Session, values: dict, valid_from: datetime) -> None:
    """Checks shared by create, update and supersede; ``valid_from`` is the start of the row written."""
    valid_to = values.get("valid_to")
    # A rule holds on [valid_from, valid_to): an end at or before the start leaves it empty.
    if valid_to is not None and _aware(valid_to) <= _aware(valid_from):
        raise _unprocessable("valid_to_not_after_valid_from", "valid_to must be after valid_from")
    # Buyers in Northern Ireland and the Isle of Man are matched as GB, in Monaco as FR, so a rule
    # for XI, IM or MC would never apply.
    country = values.get("country")
    if country in MAPPED_RULE_COUNTRIES:
        names = {"XI": "Northern Ireland", "IM": "The Isle of Man", "MC": "Monaco"}
        target = MAPPED_RULE_COUNTRIES[country]
        raise _unprocessable(
            f"country_{country.lower()}", f"{names[country]} ({country}) is taxed by {target} rules; use country {target}"
        )
    if values.get("tax_class_id") is not None and db.get(TaxClass, values["tax_class_id"]) is None:
        raise _unprocessable("unknown_tax_class", "Unknown tax class")
    jurisdiction = values.get("jurisdiction") or JURISDICTION_ANY
    if jurisdiction not in (JURISDICTION_ANY, "EU_OSS") and not re.fullmatch(r"[A-Z]{2}", jurisdiction):
        raise _unprocessable("invalid_jurisdiction", "jurisdiction is ANY, EU_OSS or a country code")
    if values.get("reverse_charge") and values.get("rate_percent") not in (None, Decimal("0.0000")):
        raise _unprocessable("reverse_charge_rate", "A reverse charge rule has rate 0")


def _not_in_past(value: datetime, now: datetime) -> datetime:
    at = _aware(value)
    if at < now - NOW_TOLERANCE:
        raise _unprocessable("valid_from_in_past", "valid_from may not be in the past")
    return max(at, now)


@router.get("/tax/rules")
def list_rules(
    db: DbDep,
    country: Annotated[Optional[str], Query(max_length=2)] = None,
    jurisdiction: Annotated[Optional[str], Query(max_length=16)] = None,
    current_only: bool = False,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
):
    stmt = select(TaxRule)
    if country:
        stmt = stmt.where(TaxRule.country == country.upper())
    if jurisdiction:
        stmt = stmt.where(TaxRule.jurisdiction == jurisdiction.upper())
    if current_only:
        now = _now()
        stmt = stmt.where(TaxRule.valid_from <= now, (TaxRule.valid_to.is_(None)) | (TaxRule.valid_to > now))
    rows = db.scalars(stmt.order_by(TaxRule.id).offset(offset).limit(limit))
    return [_rule_payload(row) for row in rows]


@router.post("/tax/rules", status_code=status.HTTP_201_CREATED)
def create_rule(body: RuleCreate, auth: AdminDep, db: DbDep):
    now = _now()
    values = body.model_dump(exclude={"valid_from"})
    values = {k: v for k, v in values.items() if v is not None}
    valid_from = _not_in_past(body.valid_from, now) if body.valid_from is not None else now
    _check_rule(db, values, valid_from)
    values.setdefault("jurisdiction", JURISDICTION_ANY)
    row = TaxRule(**values, valid_from=valid_from, source=TaxRuleSource.ADMIN.value)
    db.add(row)
    db.flush()
    _audit(db, auth, "rule.created", "tax_rule", row.id, _json(body.model_dump(exclude_unset=True)))
    db.commit()
    return _rule_payload(row)


@router.get("/tax/rules/{rule_id}")
def get_rule(rule_id: int, db: DbDep):
    row = db.get(TaxRule, rule_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Tax rule not found")
    return _rule_payload(row)


@router.put("/tax/rules/{rule_id}")
def update_rule(rule_id: int, body: RuleFields, auth: AdminDep, db: DbDep):
    """Edit a rule not yet in effect; one in effect answers 409 ``tax_rule_in_use`` (design 1.2)."""
    row = db.get(TaxRule, rule_id, with_for_update=True)
    if row is None:
        raise HTTPException(status_code=404, detail="Tax rule not found")
    if _aware(row.valid_from) <= _now():
        raise _conflict("tax_rule_in_use", "The rule is in effect; supersede it instead")
    if row.legacy_tax_rate_id is not None:
        raise _conflict("tax_rule_mirrored", "A mirrored rule changes through /tax-rates")
    data = body.model_dump(exclude_unset=True)
    _check_rule(db, {**{name: getattr(row, name) for name in RULE_FIELDS}, **data}, row.valid_from)
    for name, value in data.items():
        setattr(row, name, value)
    _audit(db, auth, "rule.updated", "tax_rule", row.id, _json(data))
    db.commit()
    return _rule_payload(row)


@router.post("/tax/rules/{rule_id}/supersede", status_code=status.HTTP_201_CREATED)
def supersede_rule(rule_id: int, body: RuleSupersede, auth: AdminDep, db: DbDep):
    """Close the rule at ``valid_from`` and start a new row with the changed fields (design 1.2)."""
    now = _now()
    at = _not_in_past(body.valid_from, now)
    old = db.get(TaxRule, rule_id, with_for_update=True)
    if old is None:
        raise HTTPException(status_code=404, detail="Tax rule not found")
    if old.legacy_tax_rate_id is not None:
        raise _conflict("tax_rule_mirrored", "A mirrored rule changes through /tax-rates")
    if old.valid_to is not None and _aware(old.valid_to) <= at:
        raise _conflict("tax_rule_closed", "The rule already ends before valid_from")
    if _aware(old.valid_from) >= at:
        raise _conflict("tax_rule_not_started", "valid_from is not after the rule's start; edit it instead")
    changes = body.model_dump(exclude_unset=True, exclude={"valid_from"})
    values = {name: getattr(old, name) for name in RULE_FIELDS}
    values.update(changes)
    values["valid_to"] = changes.get("valid_to", old.valid_to)
    _check_rule(db, values, at)
    old.valid_to = at
    db.flush()
    new = TaxRule(
        **values,
        valid_from=at,
        source=old.source,
        pack_id=old.pack_id,
        pack_version=old.pack_version,
        pack_rule_key=old.pack_rule_key,
        supersedes_id=old.id,
    )
    db.add(new)
    db.flush()
    _audit(
        db, auth, "rule.superseded", "tax_rule", old.id,
        {"new_rule_id": new.id, "valid_from": at.isoformat(), "changes": _json(changes)},
    )
    db.commit()
    return _rule_payload(new)


# --- registrations ---


class RegistrationFields(RequestModel):
    not_null: ClassVar[frozenset[str]] = frozenset(
        {"jurisdiction", "scheme", "tax_number", "country", "valid_from", "enabled"}
    )

    jurisdiction: Optional[str] = Field(default=None, min_length=2, max_length=16)
    scheme: Optional[TaxRegistrationScheme] = None
    tax_number: Optional[str] = Field(default=None, min_length=1, max_length=64)
    country: Optional[str] = Field(default=None, min_length=2, max_length=2)
    valid_from: Optional[datetime] = None
    valid_to: Optional[datetime] = None
    invoice_series_id: Optional[StrictInt] = Field(default=None, gt=0)
    invoice_profile_id: Optional[StrictInt] = Field(default=None, gt=0)
    enabled: Optional[StrictBool] = None

    @field_validator("country", "jurisdiction")
    @classmethod
    def _upper(cls, value: Optional[str]) -> Optional[str]:
        return value.upper() if value else value


class RegistrationCreate(RegistrationFields):
    jurisdiction: str = Field(min_length=2, max_length=16)
    scheme: TaxRegistrationScheme
    tax_number: str = Field(min_length=1, max_length=64)
    country: str = Field(min_length=2, max_length=2)


def _registration_payload(row: SellerTaxRegistration) -> dict:
    return {
        "id": row.id, "jurisdiction": row.jurisdiction, "scheme": _json(row.scheme), "tax_number": row.tax_number,
        "country": row.country, "valid_from": _json(row.valid_from), "valid_to": _json(row.valid_to),
        "invoice_series_id": row.invoice_series_id, "invoice_profile_id": row.invoice_profile_id,
        "enabled": row.enabled,
    }


@router.get("/tax/registrations")
def list_registrations(db: DbDep):
    rows = db.scalars(select(SellerTaxRegistration).order_by(SellerTaxRegistration.id))
    return [_registration_payload(row) for row in rows]


@router.post("/tax/registrations", status_code=status.HTTP_201_CREATED)
def create_registration(body: RegistrationCreate, auth: AdminDep, db: DbDep):
    data = body.model_dump(exclude_unset=True)
    data["scheme"] = body.scheme.value
    data.setdefault("valid_from", _now())
    row = SellerTaxRegistration(**data)
    db.add(row)
    db.flush()
    _audit(db, auth, "registration.created", "seller_tax_registration", row.id, _json(data))
    db.commit()
    return _registration_payload(row)


@router.put("/tax/registrations/{registration_id}")
def update_registration(registration_id: int, body: RegistrationFields, auth: AdminDep, db: DbDep):
    row = db.get(SellerTaxRegistration, registration_id, with_for_update=True)
    if row is None:
        raise HTTPException(status_code=404, detail="Registration not found")
    data = body.model_dump(exclude_unset=True)
    if "scheme" in data and data["scheme"] is not None:
        data["scheme"] = TaxRegistrationScheme(data["scheme"]).value
    for name, value in data.items():
        setattr(row, name, value)
    _audit(db, auth, "registration.updated", "seller_tax_registration", row.id, _json(data))
    db.commit()
    return _registration_payload(row)


# --- exemptions ---


class ExemptionCreate(RequestModel):
    scope: Literal["account", "client_group"] = "account"
    billing_account_id: StrictInt = Field(gt=0)
    reason: str = Field(min_length=1, max_length=255)
    certificate_ref: Optional[str] = Field(default=None, max_length=128)
    valid_from: Optional[datetime] = None
    valid_to: Optional[datetime] = None
    tax_class_ids: list[StrictInt] = Field(default_factory=list, max_length=50)


def _exemption_payload(row: TaxExemption) -> dict:
    return {
        "id": row.id, "scope": _json(row.scope), "billing_account_id": row.billing_account_id, "reason": row.reason,
        "certificate_ref": row.certificate_ref, "valid_from": _json(row.valid_from), "valid_to": _json(row.valid_to),
        "tax_class_ids": list(row.tax_class_ids or []), "created_by_user_id": row.created_by_user_id,
        "revoked_at": _json(row.revoked_at),
    }


@router.get("/tax/exemptions")
def list_exemptions(db: DbDep, billing_account_id: Optional[int] = None, active_only: bool = False):
    stmt = select(TaxExemption)
    if billing_account_id is not None:
        stmt = stmt.where(TaxExemption.billing_account_id == billing_account_id)
    if active_only:
        stmt = stmt.where(TaxExemption.revoked_at.is_(None))
    return [_exemption_payload(row) for row in db.scalars(stmt.order_by(TaxExemption.id))]


@router.post("/tax/exemptions", status_code=status.HTTP_201_CREATED)
def create_exemption(body: ExemptionCreate, auth: AdminDep, db: DbDep):
    if body.scope != "account":
        raise _unprocessable("scope_not_supported", "Only account exemptions are supported")
    if db.get(BillingAccount, body.billing_account_id) is None:
        raise HTTPException(status_code=404, detail="Billing account not found")
    for class_id in body.tax_class_ids:
        if db.get(TaxClass, class_id) is None:
            raise _unprocessable("unknown_tax_class", f"Unknown tax class {class_id}")
    row = TaxExemption(
        scope="account", billing_account_id=body.billing_account_id, reason=body.reason,
        certificate_ref=body.certificate_ref, valid_from=body.valid_from or _now(), valid_to=body.valid_to,
        tax_class_ids=sorted(set(body.tax_class_ids)), created_by_user_id=auth["user_id"],
    )
    db.add(row)
    db.flush()
    AuditService.log(
        db, actor_user_id=auth["user_id"], action="billing.tax_exemption.created", resource_type="tax_exemption",
        resource_id=str(row.id), metadata=_json(body.model_dump()),
    )
    db.commit()
    return _exemption_payload(row)


@router.post("/tax/exemptions/{exemption_id}/revoke")
def revoke_exemption(exemption_id: int, auth: AdminDep, db: DbDep):
    row = db.get(TaxExemption, exemption_id, with_for_update=True)
    if row is None:
        raise HTTPException(status_code=404, detail="Exemption not found")
    if row.revoked_at is not None:
        raise _conflict("already_revoked", "The exemption is already revoked")
    row.revoked_at = _now()
    AuditService.log(
        db, actor_user_id=auth["user_id"], action="billing.tax_exemption.revoked", resource_type="tax_exemption",
        resource_id=str(row.id), metadata={"billing_account_id": row.billing_account_id},
    )
    db.commit()
    return _exemption_payload(row)


class AccountTax(RequestModel):
    tax_exempt: StrictBool
    tax_exempt_reason: Optional[str] = Field(default=None, max_length=255)


@router.put("/accounts/{account_id}/tax")
def update_account_tax(account_id: int, body: AccountTax, auth: AdminDep, db: DbDep):
    account = db.get(BillingAccount, account_id, with_for_update=True)
    if account is None:
        raise HTTPException(status_code=404, detail="Billing account not found")
    if body.tax_exempt and not (body.tax_exempt_reason or "").strip():
        raise _unprocessable("reason_required", "An exempt account needs a reason")
    before = {"tax_exempt": bool(account.tax_exempt), "tax_exempt_reason": account.tax_exempt_reason}
    account.tax_exempt = body.tax_exempt
    account.tax_exempt_reason = body.tax_exempt_reason if body.tax_exempt else None
    _audit(db, auth, "account.updated", "billing_account", account.id, {"from": before, "to": body.model_dump()})
    db.commit()
    return {"billing_account_id": account.id, "tax_exempt": account.tax_exempt,
            "tax_exempt_reason": account.tax_exempt_reason}


# --- packs ---


class PackPreview(RequestModel):
    version: str = Field(min_length=1, max_length=32)
    effective_from: datetime


class PackApply(PackPreview):
    diff_hash: str = Field(min_length=8, max_length=128)


# PackImportError codes that are not 422: an unknown pack is 404, a stale preview is 409.
_PACK_ERROR_STATUS = {"pack_not_found": 404, "pack_changed": 409}
# MariaDB lock wait timeout and deadlock: the rule rows were held by another change.
_LOCK_ERRORS = {1205, 1213}


def _pack_error(exc: Exception) -> Optional[HTTPException]:
    """The answer for a refused preview or apply, or None for an unexpected error."""
    from sqlalchemy.exc import OperationalError

    from app.services.tax.pack_import import PackImportError

    if isinstance(exc, PackImportError):
        status_code = _PACK_ERROR_STATUS.get(exc.code, 422)
        return HTTPException(status_code=status_code, detail={"code": exc.code, "message": exc.message})
    orig = getattr(exc, "orig", None)
    if isinstance(exc, OperationalError) and getattr(orig, "args", (None,))[0] in _LOCK_ERRORS:
        return _conflict("tax_rules_busy", "Another change held the tax rules; nothing was applied. Try again.")
    return None


@router.get("/tax/packs")
def list_packs(db: DbDep):
    from app.services.tax.packs.registry import PackRegistry

    return _json(PackRegistry.list(db))


# A preview and an apply pass effective_from to the importer as given: it is part of the
# diff hash, and the importer refuses a time in the past (a pack never changes a past tax point).


@router.post("/tax/packs/{pack_id}/preview")
def preview_pack(pack_id: str, body: PackPreview, db: DbDep):
    from app.services.tax.pack_import import PackImporter

    try:
        diff = PackImporter.preview(db, pack_id, body.version, _aware(body.effective_from), now=_now())
    except Exception as exc:
        db.rollback()
        mapped = _pack_error(exc)
        if mapped is None:
            raise
        raise mapped from None
    db.rollback()
    return _json(diff)


@router.post("/tax/packs/{pack_id}/apply")
def apply_pack(pack_id: str, body: PackApply, auth: AdminDep, db: DbDep):
    """Apply a previewed diff. The importer writes the only audit entry (with the diff).

    Any error rolls the whole transaction back, audit entry included; nothing is retried here.
    """
    from app.services.tax.pack_import import PackImporter

    try:
        diff = PackImporter.apply(
            db, pack_id, body.version, _aware(body.effective_from),
            diff_hash=body.diff_hash, actor_user_id=auth["user_id"], now=_now(),
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        mapped = _pack_error(exc)
        if mapped is None:
            raise
        raise mapped from None
    return _json(diff)


# --- legal notes ---


class LegalNoteUpdate(RequestModel):
    key: str = Field(min_length=1, max_length=64)
    locale: str = Field(default="en", min_length=2, max_length=16)
    text: str = Field(min_length=1, max_length=1000)


def _note_payload(row: TaxLegalNote) -> dict:
    return {"id": row.id, "key": row.key, "locale": row.locale, "text": row.text,
            "edited_by_admin": row.edited_by_admin}


@router.get("/tax/legal-notes")
def list_legal_notes(db: DbDep):
    rows = db.scalars(select(TaxLegalNote).order_by(TaxLegalNote.key, TaxLegalNote.locale))
    return [_note_payload(row) for row in rows]


@router.put("/tax/legal-notes")
def update_legal_note(body: LegalNoteUpdate, auth: AdminDep, db: DbDep):
    """Edit a note's text; published invoices keep the text frozen at publish (design 4.9)."""
    row = db.execute(
        select(TaxLegalNote).where(TaxLegalNote.key == body.key, TaxLegalNote.locale == body.locale)
        .with_for_update()
    ).scalar_one_or_none()
    before = row.text if row is not None else None
    if row is None:
        row = TaxLegalNote(key=body.key, locale=body.locale, text=body.text, edited_by_admin=True)
        db.add(row)
    else:
        row.text = body.text
        row.edited_by_admin = True
    db.flush()
    _audit(db, auth, "legal_note.updated", "tax_legal_note", row.id,
           {"key": body.key, "locale": body.locale, "from": before, "to": body.text})
    db.commit()
    return _note_payload(row)


# --- tax ID checks ---


def _check_payload(row: TaxIdCheck) -> dict:
    return {
        "id": row.id, "country": row.country, "normalised_id": row.normalised_id, "id_type": row.id_type,
        "validator": row.validator, "status": row.status, "checked_at": _json(row.checked_at),
        "expires_at": _json(row.expires_at), "request_ref": row.request_ref, "error_code": row.error_code,
        "billing_account_id": row.billing_account_id,
    }


@router.get("/tax/id-checks")
def list_id_checks(
    db: DbDep,
    billing_account_id: Optional[int] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
):
    stmt = select(TaxIdCheck)
    if billing_account_id is not None:
        stmt = stmt.where(TaxIdCheck.billing_account_id == billing_account_id)
    rows = db.scalars(stmt.order_by(TaxIdCheck.id.desc()).offset(offset).limit(limit))
    return [_check_payload(row) for row in rows]


class Recheck(RequestModel):
    billing_account_id: StrictInt = Field(gt=0)


@router.post("/tax/id-checks/recheck")
def recheck_tax_id(body: Recheck, auth: AdminDep, db: DbDep):
    from app.services.tax.validators import TaxIdService

    profile = db.execute(
        select(BillingProfile).where(BillingProfile.billing_account_id == body.billing_account_id)
    ).scalar_one_or_none()
    if profile is None or not profile.tax_id:
        raise HTTPException(status_code=404, detail="No tax ID on this billing account")
    check = TaxIdService.check(db, profile, force=True)
    _audit(db, auth, "id_check.recheck", "billing_profile", profile.id,
           {"billing_account_id": body.billing_account_id, "status": _json(getattr(check, "status", None))})
    db.commit()
    return {"billing_account_id": body.billing_account_id, "tax_id_status": profile.tax_id_status,
            "check": _check_payload(check) if isinstance(check, TaxIdCheck) else None}


# --- preview ---


class PreviewBuyer(RequestModel):
    country: Optional[str] = Field(default=None, max_length=2)
    region: Optional[str] = Field(default=None, max_length=128)
    postcode: Optional[str] = Field(default=None, max_length=32)
    is_business: StrictBool = False
    tax_id: Optional[str] = Field(default=None, max_length=64)
    tax_id_type: Optional[str] = Field(default=None, max_length=16)
    tax_id_status: str = Field(default="unchecked", max_length=16)
    tax_id_checked_at: Optional[datetime] = None
    exempt_all: StrictBool = False
    exempt_reason: Optional[str] = Field(default=None, max_length=255)
    exemption_ids: list[StrictInt] = Field(default_factory=list, max_length=50)


class PreviewLine(RequestModel):
    key: str = Field(min_length=1, max_length=64)
    net_cents: StrictInt
    tax_class_code: str = Field(default="standard", max_length=32)
    kind: str = Field(default="product", max_length=32)


class PreviewContext(RequestModel):
    tax_point_at: Optional[datetime] = None
    currency: str = Field(default="GBP", min_length=3, max_length=3)
    prices_include_tax: StrictBool = False
    purpose: str = Field(default="order_charge", max_length=32)
    owner: Literal["retail", "reseller"] = "retail"
    buyer: PreviewBuyer
    lines: list[PreviewLine] = Field(min_length=1, max_length=100)
    evidence: dict[str, Any] = Field(default_factory=dict)


@router.post("/tax/preview")
def preview_tax(body: PreviewContext, db: DbDep):
    """A ``TaxContext`` in JSON answered with the ``TaxResult``; never writes."""
    from app.services.tax.context import BuyerTaxFacts, TaxContext, TaxLineInput
    from app.services.tax.engine import TaxEngine

    buyer = body.buyer
    context = TaxContext(
        tax_point_at=_aware(body.tax_point_at) or _now(),
        currency=body.currency.upper(),
        prices_include_tax=body.prices_include_tax,
        buyer=BuyerTaxFacts(
            country=(buyer.country or "").upper() or None, region=buyer.region, postcode=buyer.postcode,
            is_business=buyer.is_business, tax_id=buyer.tax_id, tax_id_type=buyer.tax_id_type,
            tax_id_status=buyer.tax_id_status, tax_id_check_id=None, exempt_all=buyer.exempt_all,
            exemption_ids=tuple(buyer.exemption_ids), tax_id_checked_at=buyer.tax_id_checked_at,
            exempt_reason=buyer.exempt_reason,
        ),
        lines=tuple(TaxLineInput(**line.model_dump()) for line in body.lines),
        purpose=body.purpose,
        owner=body.owner,
        evidence=dict(body.evidence),
    )
    try:
        result = TaxEngine.calculate(db, context)
    finally:
        db.rollback()
    return {
        "engine": result.engine,
        "scope": result.scope,
        "tax_cents": result.tax_cents,
        "context_hash": result.context_hash,
        "registration_id": result.registration_id,
        "legal_note_keys": list(result.legal_note_keys),
        "lines": _json({key: line for key, line in result.lines.items()}),
        "summary": result.summary_json(),
        "evidence": _json(dict(result.evidence.values)),
    }


# --- used rules (for the switch screen) ---


@router.get("/tax/rules/{rule_id}/usage")
def rule_usage(rule_id: int, db: DbDep):
    """How many invoice tax rows a rule produced (published rows are frozen)."""
    from sqlalchemy import func

    count = int(db.scalar(select(func.count()).where(InvoiceLineTax.tax_rule_id == rule_id)) or 0)
    return {"rule_id": rule_id, "invoice_line_taxes": count}
