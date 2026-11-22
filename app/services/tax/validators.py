"""Tax-ID checks (docs/billing-phase2-design.md 7.4).

``TaxIdService.check`` calls validator plugins, so it must never run while a
billing row lock is held. It never raises because of a validator: a timeout,
plugin error, open circuit or invalid answer is the status ``unavailable``, and
no status here ever blocks an invoice or a renewal. The only refusal is the
checkout one of ``ensure_checkout_allowed`` under the ``block_checkout`` policy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.tax import (
    SellerTaxRegistration,
    TaxIdCheck,
    TaxIdStatus,
    TaxIdType,
    TaxRegistrationScheme,
)
from app.plugin_host import providers as plugin_providers
from app.plugin_host.providers import ProviderUnavailable
from app.services.settings_store import get_setting
from app.services.tax import id_formats

logger = logging.getLogger(__name__)

VALIDATOR_KIND = "tax_id_validator"
CORE_FORMAT = "core_format"
INVALID_CACHE = timedelta(days=1)
UNAVAILABLE_CACHE = timedelta(minutes=15)
EVENT = "tax_id.checked"

POLICY_CHARGE_TAX = "charge_tax"
POLICY_BLOCK_CHECKOUT = "block_checkout"

# Shown to the client when the number could not be checked and VAT was charged.
UNVERIFIED_MESSAGE = "We could not verify your VAT number; VAT has been charged. We will retry."

_ONLINE_STATUSES = {TaxIdStatus.VALID.value, TaxIdStatus.INVALID.value, TaxIdStatus.UNAVAILABLE.value}


class TaxIdUnverifiable(Exception):
    """Checkout refused under ``block_checkout``: answer 409 ``tax_id_unverifiable``."""

    code = "tax_id_unverifiable"


# Local copy of the plugin API 1.1 request type (design 8.5), used only while
# ``app.plugin_host.kinds`` does not exist; it is the only place to switch.
try:
    from app.plugin_host.kinds import TaxIdCheckRequest
except ImportError:  # pragma: no cover

    @dataclass(frozen=True)
    class TaxIdCheckRequest:  # type: ignore[no-redef]
        country: str
        tax_id: str
        id_type: str
        requester_country: Optional[str]
        requester_tax_id: Optional[str]


def _call_check_online(db: Session, ref: str, request: Any) -> Any:
    """The one call of ``check_online``, with the setting as an upper bound on the timeout.

    The host uses the lower of this and the provider's manifest ``timeout_seconds`` (design 8.6).
    """
    timeout = int(get_setting(db, "billing_tax_id_check_timeout_seconds"))
    return plugin_providers.call_provider(VALIDATOR_KIND, ref, "check_online", request, timeout_seconds=timeout)


def _now(now: Optional[datetime]) -> datetime:
    value = now or datetime.now(timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _clip(value: Any, length: int) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    return text[:length] if text else None


def configured_validators(db: Session) -> list[str]:
    text = get_setting(db, "billing_tax_id_validators") or ""
    return [ref.strip() for ref in text.split(",") if ref.strip()]


def _supporting_validators(db: Session, country: str, id_type: str) -> list[str]:
    """Configured validators, in the setting's order, that are enabled and support the number."""
    configured = configured_validators(db)
    if not configured:
        return []
    try:
        enabled = {ref.ref for ref in plugin_providers.providers_of(VALIDATOR_KIND)}
    except Exception:  # noqa: BLE001 - the plugin host never breaks a tax-ID check
        logger.exception("Listing tax-ID validators failed")
        return []
    out = []
    for ref in configured:
        if ref not in enabled:
            continue
        try:
            if plugin_providers.call_provider(VALIDATOR_KIND, ref, "supports", country, id_type) is True:
                out.append(ref)
        except ProviderUnavailable:
            continue
        except Exception:  # noqa: BLE001
            logger.exception("Tax-ID validator %s failed in supports()", ref)
    return out


def _requester(db: Session, at: datetime) -> tuple[Optional[str], Optional[str]]:
    """Country (VIES code) and number of the seller's establishment registration, if any."""
    country = (get_setting(db, "billing_tax_seller_country") or "").upper()
    if not country:
        return None, None
    row = db.scalars(
        select(SellerTaxRegistration)
        .where(SellerTaxRegistration.enabled.is_(True))
        .where(SellerTaxRegistration.scheme == TaxRegistrationScheme.DOMESTIC)
        .where(SellerTaxRegistration.country == country)
        .where(SellerTaxRegistration.valid_from <= at)
        .where(or_(SellerTaxRegistration.valid_to.is_(None), SellerTaxRegistration.valid_to > at))
        .order_by(SellerTaxRegistration.id)
    ).first()
    if row is None:
        return None, None
    own = id_formats.normalise(row.tax_number, country)
    if own is None:
        return None, None
    return id_formats.VAT_PREFIX.get(own.country, own.country), own.number


def _expires(db: Session, status: str, checked_at: datetime) -> datetime:
    if status == TaxIdStatus.VALID.value:
        return checked_at + timedelta(days=int(get_setting(db, "billing_tax_id_valid_days")))
    if status == TaxIdStatus.INVALID.value:
        return checked_at + INVALID_CACHE
    return checked_at + UNAVAILABLE_CACHE


def _online(db: Session, ref: str, request: Any) -> dict[str, Any]:
    """Call ``check_online``; every failure becomes an ``unavailable`` answer."""
    try:
        answer = _call_check_online(db, ref, request)
    except ProviderUnavailable as exc:
        return {"status": TaxIdStatus.UNAVAILABLE.value, "error_code": (exc.reason or "PLUGIN_ERROR").upper()}
    except Exception:  # noqa: BLE001 - call_provider should only raise ProviderUnavailable
        logger.exception("Tax-ID validator %s raised", ref)
        return {"status": TaxIdStatus.UNAVAILABLE.value, "error_code": "PLUGIN_ERROR"}
    status = getattr(answer, "status", None)
    if status not in _ONLINE_STATUSES:
        logger.warning("Tax-ID validator %s answered an unknown status %r", ref, status)
        return {"status": TaxIdStatus.UNAVAILABLE.value, "error_code": "INVALID_ANSWER"}
    error_code = getattr(answer, "error_code", None)
    if status == TaxIdStatus.UNAVAILABLE.value and not error_code:
        error_code = "UNKNOWN"
    return {
        "status": status,
        "error_code": error_code,
        "request_ref": getattr(answer, "request_ref", None),
        "registered_name": getattr(answer, "registered_name", None),
        "registered_address": getattr(answer, "registered_address", None),
    }


def _store(
    db: Session,
    profile: Any,
    number: id_formats.NormalisedTaxId,
    validator: str,
    answer: dict[str, Any],
    checked_at: datetime,
    expires_at: datetime,
) -> TaxIdCheck:
    row = TaxIdCheck(
        country=number.country,
        vies_country=number.vies_country,
        normalised_id=number.number[:64],
        id_type=number.id_type,
        validator=validator[:128],
        status=answer["status"],
        checked_at=checked_at,
        expires_at=expires_at,
        request_ref=_clip(answer.get("request_ref"), 64),
        registered_name=_clip(answer.get("registered_name"), 512),
        registered_address=_clip(answer.get("registered_address"), 512),
        error_code=_clip(answer.get("error_code"), 64),
        billing_account_id=getattr(profile, "billing_account_id", None),
    )
    db.add(row)
    db.flush()
    return row


def _set_profile(profile: Any, status: str, checked_at: Optional[datetime], check_id: Optional[int]) -> None:
    profile.tax_id_status = status
    profile.tax_id_checked_at = checked_at
    profile.tax_id_check_id = check_id


def _emit(db: Session, profile: Any, row: TaxIdCheck) -> None:
    try:
        from app.plugin_host.events import emit

        emit(
            db,
            EVENT,
            {
                "billing_account_id": getattr(profile, "billing_account_id", None),
                "country": row.country,
                "status": row.status,
                "validator": row.validator,
                "checked_at": _aware(row.checked_at).isoformat(),
            },
        )
    except Exception:  # noqa: BLE001 - the event never breaks the check
        logger.exception("Emitting %s failed", EVENT)


class TaxIdService:
    @staticmethod
    def normalised(profile: Any) -> Optional[id_formats.NormalisedTaxId]:
        """The profile's tax ID split into country and bare number, or None."""
        if not getattr(profile, "tax_id", None):
            return None
        return id_formats.normalise(profile.tax_id, getattr(profile, "country", None))

    @staticmethod
    def check(
        db: Session,
        profile: Any,
        *,
        force: bool = False,
        now: Optional[datetime] = None,
    ) -> Optional[TaxIdCheck]:
        """Check the profile's tax ID and update its status (flushes, does not commit).

        Returns the check that set the status, or None when no check was made:
        the profile has no tax ID, its country cannot be told, it is of type
        ``other``, or no enabled validator supports it. In those cases the
        status becomes ``unchecked``. Never call this under a billing lock.
        """
        at = _now(now)
        number = TaxIdService.normalised(profile)
        if number is None:
            _set_profile(profile, TaxIdStatus.UNCHECKED.value, None, None)
            return None
        if getattr(profile, "tax_id_type", None) == TaxIdType.OTHER.value:
            number = id_formats.NormalisedTaxId(number.country, number.vies_country, number.number, TaxIdType.OTHER.value)
        elif getattr(profile, "tax_id_type", None) is None:
            profile.tax_id_type = number.id_type

        if number.id_type != TaxIdType.OTHER.value and not id_formats.check_format(number.country, number.number):
            row = _store(
                db,
                profile,
                number,
                CORE_FORMAT,
                {"status": TaxIdStatus.FORMAT_INVALID.value},
                at,
                at,
            )
            _set_profile(profile, row.status, at, row.id)
            _emit(db, profile, row)
            return row

        validators = _supporting_validators(db, number.vies_country or number.country, number.id_type)
        if not validators:
            _set_profile(profile, TaxIdStatus.UNCHECKED.value, None, None)
            return None

        if not force:
            cached = db.scalars(
                select(TaxIdCheck)
                .where(TaxIdCheck.country == number.country)
                .where(TaxIdCheck.normalised_id == number.number)
                .where(TaxIdCheck.validator.in_(validators))
                .where(TaxIdCheck.expires_at > at)
                .order_by(TaxIdCheck.checked_at.desc(), TaxIdCheck.id.desc())
            ).first()
            if cached is not None:
                _set_profile(profile, cached.status, _aware(cached.checked_at), cached.id)
                db.flush()
                return cached

        ref = validators[0]
        requester_country, requester_tax_id = _requester(db, at)
        request = TaxIdCheckRequest(
            country=number.vies_country or number.country,
            tax_id=number.number,
            id_type=number.id_type,
            requester_country=requester_country,
            requester_tax_id=requester_tax_id,
        )
        answer = _online(db, ref, request)
        row = _store(db, profile, number, ref, answer, at, _expires(db, answer["status"], at))
        _set_profile(profile, row.status, at, row.id)
        db.flush()
        _emit(db, profile, row)
        return row

    @staticmethod
    def unavailable_policy(db: Session) -> str:
        return get_setting(db, "billing_tax_id_unavailable_policy") or POLICY_CHARGE_TAX

    @staticmethod
    def checkout_blocked(db: Session, profile: Any) -> bool:
        """True when a checkout by this profile must be refused (fail closed, 7.4).

        Only under ``block_checkout``, only for a business buyer with an EU VAT
        number whose status is ``unavailable``. Renewals and invoices never ask.
        """
        if TaxIdService.unavailable_policy(db) != POLICY_BLOCK_CHECKOUT:
            return False
        if not getattr(profile, "is_business", False):
            return False
        if getattr(profile, "tax_id_status", None) != TaxIdStatus.UNAVAILABLE.value:
            return False
        number = TaxIdService.normalised(profile)
        if number is None:
            return False
        id_type = getattr(profile, "tax_id_type", None) or number.id_type
        return id_type == TaxIdType.EU_VAT.value

    @staticmethod
    def ensure_checkout_allowed(db: Session, profile: Any) -> None:
        """Raise ``TaxIdUnverifiable`` when ``checkout_blocked``."""
        if TaxIdService.checkout_blocked(db, profile):
            raise TaxIdUnverifiable("The VAT number could not be verified; try again later")

    @staticmethod
    def client_message(db: Session, profile: Any) -> Optional[str]:
        """The client-facing note under ``charge_tax`` when the number could not be checked."""
        if getattr(profile, "tax_id_status", None) != TaxIdStatus.UNAVAILABLE.value:
            return None
        if TaxIdService.checkout_blocked(db, profile):
            return None
        return UNVERIFIED_MESSAGE
