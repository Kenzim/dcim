"""Tax shadow checks (docs/billing-phase2-design.md section 5).

Each check is recorded through ``BillingShadowService.record("tax", ...)``,
which commits on its own short session and never raises.
"""

from __future__ import annotations

from typing import Any, Optional

from app.services.tax.result import TaxResult

ENGINE = "tax"
T1_CHECKOUT = "t1_checkout"
T1_RULE_DIVERGENCE = "t1_rule_divergence"
T2_UNCOVERED = "t2_uncovered"
T3_PROFORMA_RATE_CHANGED = "t3_proforma_rate_changed"
T4_NOTE_TAX = "t4_note_tax"
T5_CONTEXT_CHANGED = "t5_context_changed"
T5_PROVIDER_FALLBACK = "t5_provider_fallback"
T6_SCAN = "t6_scan"

# Checks recorded and listed that do not count against the switch criterion (design 5).
NON_BLOCKING_KEYS = frozenset(
    {T1_RULE_DIVERGENCE, T2_UNCOVERED, T3_PROFORMA_RATE_CHANGED, T5_CONTEXT_CHANGED, T5_PROVIDER_FALLBACK}
)
# Checks that count against the switch criterion.
BLOCKING_KEYS = frozenset({T1_CHECKOUT, T4_NOTE_TAX, T6_SCAN})


def _record(check_key: str, subject_type: str, subject_id: int, legacy: Any, v2: Any, context: Optional[str]) -> None:
    from app.services.billing_shadow import BillingShadowService

    BillingShadowService.record(ENGINE, check_key, subject_type, int(subject_id), legacy, v2, context)


def result_values(result: TaxResult) -> dict:
    return {
        "tax_cents": result.tax_cents,
        "engine": result.engine,
        "scope": result.scope,
        "summary": result.summary_json(),
    }


def compare_checkout(legacy_tax_cents: int, result: TaxResult, *, subject_type: str, subject_id: int, context: str) -> Optional[str]:
    """``t1_checkout`` or ``t1_rule_divergence`` when the engine's total differs from ``_tax_cents``.

    A difference the engine explains (a rule not mirrored from ``tax_rates``,
    an exemption or a reverse charge) is a divergence; the row lists which.
    Returns the check key recorded, or None when the totals agree.
    """
    engine_tax = result.tax_cents
    if int(legacy_tax_cents) == engine_tax:
        return None
    explained = list(result.evidence.values.get("explained_by") or [])
    key = T1_RULE_DIVERGENCE if explained else T1_CHECKOUT
    values = result_values(result)
    if explained:
        values["explained_by"] = explained
    _record(key, subject_type, subject_id, {"tax_cents": int(legacy_tax_cents)}, values, context)
    return key


def record_legacy_raised(subject_type: str, subject_id: int, error: str, result: Optional[TaxResult]) -> None:
    """``t1_checkout`` when ``_tax_cents`` raised: its own outcome, not a plain mismatch."""
    v2 = result_values(result) if result is not None else {"tax_cents": None}
    _record(T1_CHECKOUT, subject_type, subject_id, {"raised": error}, v2, "legacy_raised")


def record_uncovered(invoice_id: int, purpose: str, result: TaxResult) -> None:
    """``t2_uncovered``: a publish legacy never taxed that the engine would tax."""
    if result.tax_cents == 0 and not any(line.parts for line in result.lines.values()):
        return
    values = result_values(result)
    values["purpose"] = purpose
    _record(T2_UNCOVERED, "invoice", invoice_id, {"tax_cents": 0, "purpose": purpose}, values, purpose)


def record_proforma_rate_changed(invoice_id: int, published: dict, at_payment: dict) -> None:
    _record(T3_PROFORMA_RATE_CHANGED, "invoice", invoice_id, published, at_payment, "convert_proforma")


def record_note_tax(adjustment_id: int, phase1: dict, rows: dict) -> None:
    _record(T4_NOTE_TAX, "invoice_adjustment", adjustment_id, phase1, rows, "credit_note_by_lines")


def record_context_changed(invoice_id: int, given: TaxResult, used: TaxResult, where: str) -> None:
    _record(
        T5_CONTEXT_CHANGED,
        "invoice",
        invoice_id,
        {"context_hash": given.context_hash, "engine": given.engine, "tax_cents": given.tax_cents},
        {"context_hash": used.context_hash, "engine": used.engine, "tax_cents": used.tax_cents},
        where,
    )


def record_provider_fallback(provider_ref: str, reason: str) -> None:
    _record(T5_PROVIDER_FALLBACK, "tax_provider", 0, {"provider": provider_ref}, {"reason": reason}, reason)


def record_scan_finding(invoice_id: int, legacy: Any, v2: Any, context: str) -> None:
    _record(T6_SCAN, "invoice", invoice_id, legacy, v2, context)
