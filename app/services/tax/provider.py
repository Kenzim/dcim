"""The ``tax_provider`` route into billing (docs/billing-phase2-design.md 8.5 to 8.7).

Only ``TaxEngine.prepare`` calls this, never under a billing lock. A provider
is asked only for a currency its manifest lists (``currencies``, empty for
any) and only when its ``supports`` answers True. Every failure, including a
refusal and an answer that fails validation, becomes ``ProviderResultError``
and the caller falls back to the built-in rules.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import Session

from app.services.billing.rounding import round_half_up, tax_exclusive
from app.services.tax import rules_repo
from app.services.tax.context import TaxContext
from app.services.tax.result import LineTax, LineTaxPart, TaxEvidence, TaxResult

PROVIDER_KIND = "tax_provider"


class ProviderResultError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _request(db: Session, context: TaxContext) -> Any:
    from app.plugin_host.kinds import TaxProviderLine, TaxProviderRequest

    registrations = rules_repo.registrations_valid_at(db, context.tax_point_at)
    buyer = context.buyer
    return TaxProviderRequest(
        tax_point_at=context.tax_point_at,
        currency=context.currency,
        prices_include_tax=bool(context.prices_include_tax),
        seller={
            "registrations": [
                {"jurisdiction": r.jurisdiction, "tax_number": r.tax_number, "country": r.country}
                for r in registrations
            ]
        },
        buyer={
            "country": buyer.country,
            "region": buyer.region,
            "postcode": buyer.postcode,
            "is_business": bool(buyer.is_business),
            "tax_id": buyer.tax_id,
            "tax_id_status": buyer.tax_id_status,
        },
        lines=tuple(
            TaxProviderLine(
                key=line.key,
                net_cents=int(line.net_cents),
                tax_class=line.tax_class_code,
                kind=line.kind,
                product_code=None,
            )
            for line in context.lines
        ),
    )


def _rate(value: Any) -> Decimal:
    try:
        return rules_repo.normalise_rate(value)
    except (rules_repo.TaxRateError, InvalidOperation, ValueError):
        raise ProviderResultError("invalid_rate") from None


def _check_bases(context: TaxContext, line: Any, parts: list) -> None:
    """Each part's base is the line's net: the amount sent (exclusive) or the gross less the line's tax (inclusive)."""
    net = int(line.net_cents)
    if context.prices_include_tax:
        net -= sum(int(part.tax_cents) for _position, part, _rate, _rc in parts)
    if any(int(part.base_cents) != net for _position, part, _rate, _rc in parts):
        raise ProviderResultError("base_does_not_match_line")


def validate_parts(context: TaxContext, answer: Any) -> dict[str, tuple[LineTaxPart, ...]]:
    """Design 8.5: every line key present, no negative amount, rates that reproduce the tax.

    A part's tax must be within one minor unit of ``base * rate``, a reverse
    charge part must carry no tax, and every part's base must be the line's
    net (``_check_bases``), so the parts tax the amount billed.
    """
    lines = getattr(answer, "lines", None)
    if not isinstance(lines, dict):
        raise ProviderResultError("invalid_answer")
    out: dict[str, tuple[LineTaxPart, ...]] = {}
    for line in context.lines:
        if line.key not in lines:
            raise ProviderResultError("missing_line")
        parts = []
        for position, part in enumerate(lines[line.key] or ()):
            base, tax = getattr(part, "base_cents", None), getattr(part, "tax_cents", None)
            if type(base) is not int or type(tax) is not int or base < 0 or tax < 0:
                raise ProviderResultError("negative_or_invalid_amount")
            rate = _rate(getattr(part, "rate_percent", None))
            reverse_charge = bool(getattr(part, "reverse_charge", False))
            if reverse_charge and tax != 0:
                raise ProviderResultError("reverse_charge_with_tax")
            expected = tax_exclusive(base, rules_repo.rate_ppm(rate), round_half_up)
            if abs(expected - tax) > 1:
                raise ProviderResultError("rate_does_not_reproduce_tax")
            parts.append((position, part, rate, reverse_charge))
        _check_bases(context, line, parts)
        out[line.key] = tuple(
            LineTaxPart(
                rule_id=None,
                name=str(part.name)[:255],
                rate_percent=rate,
                jurisdiction=str(part.jurisdiction)[:16],
                base_cents=int(part.base_cents),
                tax_cents=int(part.tax_cents),
                reverse_charge=reverse_charge,
                legal_note_key=(str(part.legal_note)[:64] if getattr(part, "legal_note", None) else None),
            )
            for _position, part, rate, reverse_charge in parts
        )
    return out


def _manifest_currencies(db: Session, provider_ref: str) -> tuple[str, ...]:
    """The ``currencies`` of the provider's manifest entry; empty (any) when it cannot be read.

    A provider that cannot be read is refused by ``call_provider`` with its own reason.
    """
    from app.models.plugin_host import PluginInstallation
    from app.plugin_host import runtime

    plugin_id, _separator, provider_id = (provider_ref or "").partition(":")
    try:
        inst = db.get(PluginInstallation, plugin_id)
        provide = runtime.manifest_of(inst).provider(PROVIDER_KIND, provider_id) if inst is not None else None
    except Exception:  # noqa: BLE001 - a manifest that no longer validates is call_provider's to report
        return ()
    return tuple(str(c).upper() for c in (getattr(provide, "currencies", None) or ()))


def calculate_with_provider(db: Session, context: TaxContext, base: TaxResult, provider_ref: str) -> TaxResult:
    from app.plugin_host.providers import ProviderUnavailable, call_provider

    try:
        request = _request(db, context)
    except ImportError:
        raise ProviderResultError("kinds_unavailable") from None
    currencies = _manifest_currencies(db, provider_ref)
    if currencies and str(context.currency).upper() not in currencies:
        raise ProviderResultError("currency_not_supported")
    try:
        if call_provider(PROVIDER_KIND, provider_ref, "supports", request) is not True:
            raise ProviderResultError("not_supported")
        answer = call_provider(PROVIDER_KIND, provider_ref, "calculate", request)
    except ProviderUnavailable as exc:
        raise ProviderResultError(exc.reason or "unavailable") from None
    except NotImplementedError:
        raise ProviderResultError("provider_host_unavailable") from None
    source = f"provider:{provider_ref}"
    by_line = validate_parts(context, answer)
    lines = {
        key: LineTax(
            key=key,
            parts=tuple(replace(part, source=source) for part in parts),
            tax_cents=sum(p.tax_cents for p in parts),
        )
        for key, parts in by_line.items()
    }
    from app.services.tax.engine import summary_of

    notes = [p.legal_note_key for line in lines.values() for p in line.parts if p.legal_note_key]
    evidence = dict(base.evidence.values)
    evidence["provider"] = provider_ref
    evidence["provider_reference"] = getattr(answer, "reference", None)
    return TaxResult(
        lines=lines,
        summary=summary_of(lines),
        legal_note_keys=tuple(dict.fromkeys(notes)),
        registration_id=base.registration_id,
        evidence=TaxEvidence(evidence),
        engine=source,
        context_hash=base.context_hash,
        scope=base.scope,
    )
