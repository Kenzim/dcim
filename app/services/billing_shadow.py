"""Billing shadow comparisons: record differences, store scan results, read engine modes.

In engine mode ``shadow`` billing keeps its legacy answer and also computes the
v2 answer; every difference goes through ``BillingShadowService.record``
(docs/billing-phase1-design.md 6.4). The daily ``scan`` re-checks every credit
ledger chain and a rolling sample of invoices; ``evaluate_switch`` is the
switch criterion of 6.5, and the ``billing_engine_switch:<engine>`` records
hold each engine's switch details and ``shadow_since``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.billing_shadow import BillingShadowMismatch
from app.models.commerce_account import SystemSetting

logger = logging.getLogger(__name__)

# Engines the daily scan, the detectors and the engines page iterate. The tax
# engine (billing phase 2) joins once its scan and switch criteria exist; until
# then it has a mode (``ENGINE_SETTING``) but no scan.
ENGINES = ("invoice", "credit")
SCAN_RESULT_KEY = "billing_shadow_scan"
SWITCH_RECORD_PREFIX = "billing_engine_switch:"
ENGINE_SETTING = {
    "invoice": "billing_invoice_engine",
    "credit": "billing_credit_engine",
    "tax": "billing_tax_engine",
}


def is_reserved_key(key: str) -> bool:
    """``system_settings`` keys only the engine switch and the shadow scan may write."""
    lowered = key.strip().lower()
    return lowered == SCAN_RESULT_KEY or lowered.startswith(SWITCH_RECORD_PREFIX)


_VALUE_LOG_LIMIT = 200


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _default_session() -> Session:
    from app.core import database

    return database.SessionLocal()


def _short(value: Any) -> str:
    text = json.dumps(value, default=str, sort_keys=True)
    return text if len(text) <= _VALUE_LOG_LIMIT else text[: _VALUE_LOG_LIMIT - 3] + "..."


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def billing_engine_mode(db: Session, engine: str) -> str:
    """The effective mode of ``engine`` (``invoice``, ``credit`` or ``tax``): legacy, shadow or v2.

    ``v2`` counts only when the switch record ``billing_engine_switch:<engine>``
    written by the engine switch also says ``v2``; a setting changed to ``v2``
    any other way is treated as ``shadow`` and logged at ERROR.
    """
    from app.services.settings_store import get_setting

    if engine not in ENGINE_SETTING:
        raise ValueError(f"unknown billing engine {engine!r}")
    mode = get_setting(db, ENGINE_SETTING[engine])
    if mode != "v2":
        return mode
    record = db.get(SystemSetting, SWITCH_RECORD_PREFIX + engine)
    if record is not None and isinstance(record.value, dict) and record.value.get("mode") == "v2":
        return "v2"
    logger.error(
        "billing engine %s is set to v2 without a matching switch record; running in shadow",
        engine,
    )
    return "shadow"


class BillingShadowService:
    @staticmethod
    def record(
        engine: str,
        check_key: str,
        subject_type: str,
        subject_id: int,
        legacy_value: Any,
        v2_value: Any,
        context: Optional[str] = None,
        *,
        session_factory: Optional[Callable[[], Session]] = None,
        now: Optional[datetime] = None,
    ) -> None:
        """Record one mismatch on its own short session, committed at once. Never raises.

        A repeat of an open or resolved row increments ``occurrences``, sets
        ``last_seen_at`` and the latest values, and reopens it.
        """
        logger.warning(
            "billing shadow mismatch engine=%s check=%s %s=%s legacy=%s v2=%s context=%s",
            engine,
            check_key,
            subject_type,
            subject_id,
            _short(legacy_value),
            _short(v2_value),
            context,
        )
        factory = session_factory or _default_session
        seen = now or _utcnow()
        try:
            session = factory()
        except Exception:  # noqa: BLE001 - recording must never break billing
            logger.exception("billing shadow: could not open a session")
            return
        try:
            for _attempt in range(2):
                try:
                    BillingShadowService._upsert(
                        session,
                        engine=engine,
                        check_key=check_key,
                        subject_type=subject_type,
                        subject_id=int(subject_id),
                        legacy_value=_jsonable(legacy_value),
                        v2_value=_jsonable(v2_value),
                        context=context[:128] if context else None,
                        seen=seen,
                    )
                    session.commit()
                    return
                except IntegrityError:
                    # Another process inserted the same key first; update it instead.
                    session.rollback()
            logger.error(
                "billing shadow: could not record %s/%s for %s %s",
                engine,
                check_key,
                subject_type,
                subject_id,
            )
        except Exception:  # noqa: BLE001 - recording must never break billing
            session.rollback()
            logger.exception("billing shadow: recording %s/%s failed", engine, check_key)
        finally:
            session.close()

    @staticmethod
    def _upsert(
        session: Session,
        *,
        engine: str,
        check_key: str,
        subject_type: str,
        subject_id: int,
        legacy_value: Any,
        v2_value: Any,
        context: Optional[str],
        seen: datetime,
    ) -> None:
        row = session.execute(
            select(BillingShadowMismatch)
            .where(
                BillingShadowMismatch.engine == engine,
                BillingShadowMismatch.check_key == check_key,
                BillingShadowMismatch.subject_type == subject_type,
                BillingShadowMismatch.subject_id == subject_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if row is None:
            session.add(
                BillingShadowMismatch(
                    engine=engine,
                    check_key=check_key,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    legacy_value=legacy_value,
                    v2_value=v2_value,
                    context=context,
                    occurrences=1,
                    first_seen_at=seen,
                    last_seen_at=seen,
                )
            )
        else:
            row.occurrences = int(row.occurrences or 0) + 1
            row.last_seen_at = seen
            row.legacy_value = legacy_value
            row.v2_value = v2_value
            row.context = context
            row.resolved_at = None
            row.resolved_by_user_id = None
            row.resolution_note = None
        session.flush()

    @staticmethod
    def store_scan_result(
        db: Session,
        engine: str,
        *,
        started_at: datetime,
        finished_at: datetime,
        subjects_checked: int,
        mismatches_found: int,
    ) -> dict:
        """Write one engine's latest scan result into ``system_settings`` key ``billing_shadow_scan``.

        The caller commits. Returns the whole stored value.
        """
        if engine not in ENGINES:
            raise ValueError(f"unknown billing engine {engine!r}")
        row = db.get(SystemSetting, SCAN_RESULT_KEY, with_for_update=True)
        value = dict(row.value) if row is not None and isinstance(row.value, dict) else {}
        engines = dict(value.get("engine") or {})
        engines[engine] = {
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "subjects_checked": int(subjects_checked),
            "mismatches_found": int(mismatches_found),
        }
        value["engine"] = engines
        if row is None:
            db.add(SystemSetting(key=SCAN_RESULT_KEY, value=value))
        else:
            row.value = value
        db.flush()
        return value

    @staticmethod
    def last_scan(db: Session, engine: str) -> Optional[dict]:
        row = db.get(SystemSetting, SCAN_RESULT_KEY)
        if row is None or not isinstance(row.value, dict):
            return None
        return (row.value.get("engine") or {}).get(engine)

    @staticmethod
    def scan(
        db: Session,
        now: datetime,
        *,
        dry_run: bool = False,
        scheduled: bool = False,
        session_factory: Optional[Callable[[], Session]] = None,
    ) -> dict:
        """Daily comparison of both engines: the ``c2_*`` credit checks and ``i2_scan``.

        Scans each engine whose mode is ``shadow`` or ``v2``. Unless ``dry_run``,
        every finding is recorded through ``record`` and each engine's result is
        stored (latest result plus a capped history); the caller commits.
        ``scheduled`` marks the daily job's run, the only scan that may start an
        engine's ``shadow_since`` clock. A dry run records and writes nothing.
        """
        started = _aware(now)
        result: dict[str, Any] = {"dry_run": dry_run, "engines": {}}
        for engine in ENGINES:
            mode = billing_engine_mode(db, engine)
            if mode == "legacy":
                result["engines"][engine] = {"skipped": "legacy"}
                continue
            if engine == "credit":
                checked, findings = _credit_scan(db, mode)
            else:
                outcome = _invoice_scan(db, started)
                if outcome is None:
                    result["engines"][engine] = {"skipped": "balance_not_implemented"}
                    continue
                checked, findings = outcome
            blocking = [f for f in findings if f["check_key"] not in EXPECTED_DIFFERENCE_KEYS]
            expected = [f for f in findings if f["check_key"] in EXPECTED_DIFFERENCE_KEYS]
            summary = {
                "mode": mode,
                "subjects_checked": checked,
                "mismatches_found": len(blocking),
                "expected_differences_found": len(expected),
                "findings": blocking[:_FINDINGS_RETURNED],
                "expected_differences": expected[:_FINDINGS_RETURNED],
            }
            result["engines"][engine] = summary
            if dry_run:
                continue
            for finding in findings:
                BillingShadowService.record(
                    engine, now=started, session_factory=session_factory, **finding
                )
            finished = _utcnow()
            BillingShadowService.store_scan_result(
                db,
                engine,
                started_at=started,
                finished_at=finished,
                subjects_checked=checked,
                mismatches_found=len(blocking),
            )
            _append_history(
                db,
                engine,
                {
                    "started_at": started.isoformat(),
                    "finished_at": finished.isoformat(),
                    "subjects_checked": checked,
                    "mismatches_found": len(blocking),
                    "expected_differences_found": len(expected),
                    "scheduled": bool(scheduled),
                    "complete": True,
                },
            )
            if scheduled and mode == "shadow":
                set_shadow_since(db, engine, at=started, by="scheduled_shadow_scan")
        return result

    @staticmethod
    def scan_history(db: Session, engine: str) -> list[dict]:
        """The engine's stored scan results, oldest first.

        Each entry records ``scheduled`` (the daily job's run) and ``complete``
        (the scan checked every subject). Entries written before those fields
        existed count as neither: the scan then stored admin and scheduled runs
        alike.
        """
        row = db.get(SystemSetting, SCAN_RESULT_KEY)
        if row is None or not isinstance(row.value, dict):
            return []
        return list((row.value.get("history") or {}).get(engine) or [])


HISTORY_LIMIT = 90

# A blocking credit finding: a balance with no ledger entries behind it.
CACHE_WITHOUT_ENTRIES = "c2_cache_without_entries"
# Check keys for differences the two engines are known to produce. They are
# recorded and listed, but do not count against the switch criterion.
EXPECTED_PARTIAL_REFUND = "i2_expected_partial_refund"
EXPECTED_DIFFERENCE_KEYS = frozenset({EXPECTED_PARTIAL_REFUND})
_SCAN_BATCH = 1000
_FINDINGS_RETURNED = 200
SCAN_FRESH_HOURS = 26


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _parse_time(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return _aware(value)
    if not value:
        return None
    try:
        return _aware(datetime.fromisoformat(str(value)))
    except ValueError:
        return None


def _append_history(db: Session, engine: str, entry: dict) -> None:
    """Append one scan result to the engine's history, keeping ``HISTORY_LIMIT`` entries.

    When the history is full the oldest on-demand entry goes first, which can
    be the new entry itself (``last_scan`` still holds it), so admin scans
    never push out the scheduled scans the switch criterion counts.
    """
    row = db.get(SystemSetting, SCAN_RESULT_KEY, with_for_update=True)
    value = dict(row.value) if row is not None and isinstance(row.value, dict) else {}
    history = dict(value.get("history") or {})
    entries = list(history.get(engine) or []) + [entry]
    while len(entries) > HISTORY_LIMIT:
        on_demand = next(
            (index for index, old in enumerate(entries) if old.get("scheduled") is not True), None
        )
        entries.pop(on_demand if on_demand is not None else 0)
    history[engine] = entries
    value["history"] = history
    if row is None:
        db.add(SystemSetting(key=SCAN_RESULT_KEY, value=value))
    else:
        row.value = value
    db.flush()


def _finding(check_key: str, subject_type: str, subject_id: int, legacy: Any, v2: Any, context: str) -> dict:
    return {
        "check_key": check_key,
        "subject_type": subject_type,
        "subject_id": int(subject_id),
        "legacy_value": legacy,
        "v2_value": v2,
        "context": context,
    }


def unlinked_entry_counts(db: Session) -> dict[int, int]:
    """Reseller id to the number of its ledger entries with no billing account."""
    from app.models.reseller import CreditLedgerEntry

    rows = db.execute(
        select(CreditLedgerEntry.reseller_id, func.count(CreditLedgerEntry.id))
        .where(
            CreditLedgerEntry.reseller_id.is_not(None),
            CreditLedgerEntry.billing_account_id.is_(None),
        )
        .group_by(CreditLedgerEntry.reseller_id)
    ).all()
    return {int(reseller_id): int(count) for reseller_id, count in rows}


def reseller_cache_differences(db: Session) -> list[tuple[int, int, int]]:
    """Reseller accounts whose cache differs from the reseller cache: (account, reseller cache, account cache)."""
    from app.models.commerce_account import BillingAccount, BillingAccountType
    from app.models.reseller import Reseller

    rows = db.execute(
        select(BillingAccount.id, Reseller.cached_balance_cents, BillingAccount.credit_balance_cents)
        .join(Reseller, Reseller.id == BillingAccount.reseller_id)
        .where(
            BillingAccount.account_type == BillingAccountType.RESELLER,
            func.coalesce(BillingAccount.credit_balance_cents, 0)
            != func.coalesce(Reseller.cached_balance_cents, 0),
        )
        .order_by(BillingAccount.id)
    ).all()
    return [(int(a), int(r or 0), int(c or 0)) for a, r, c in rows]


def caches_without_entries(db: Session) -> list[tuple[str, int, int, int]]:
    """Balances no ledger entry explains: (subject type, subject id, legacy value, v2 value).

    A reseller with no entries (by ``reseller_id`` or through its account)
    and a non-zero reseller or account cache is reported as subject
    ``reseller`` with the reseller cache and the account cache (0 without an
    account). A retail account with no entries and a non-zero cache is
    reported as subject ``billing_account`` with 0 and its cache. Every ledger
    write moves a cache and the entry sum by the same amount, so no ledger
    write can make such a balance agree with its entries.
    """
    from app.models.commerce_account import BillingAccount, BillingAccountType
    from app.models.reseller import CreditLedgerEntry, Reseller

    account = BillingAccount.__table__.alias("reseller_account")
    reseller_entries = select(CreditLedgerEntry.id).where(
        or_(
            CreditLedgerEntry.reseller_id == Reseller.id,
            and_(account.c.id.is_not(None), CreditLedgerEntry.billing_account_id == account.c.id),
        )
    )
    resellers = db.execute(
        select(Reseller.id, Reseller.cached_balance_cents, account.c.credit_balance_cents)
        .outerjoin(
            account,
            and_(
                account.c.reseller_id == Reseller.id,
                account.c.account_type == BillingAccountType.RESELLER,
            ),
        )
        .where(
            or_(
                func.coalesce(Reseller.cached_balance_cents, 0) != 0,
                func.coalesce(account.c.credit_balance_cents, 0) != 0,
            ),
            ~reseller_entries.exists(),
        )
        .order_by(Reseller.id)
    ).all()
    retail = db.execute(
        select(BillingAccount.id, BillingAccount.credit_balance_cents)
        .where(
            BillingAccount.account_type == BillingAccountType.CLIENT,
            func.coalesce(BillingAccount.credit_balance_cents, 0) != 0,
            ~select(CreditLedgerEntry.id)
            .where(CreditLedgerEntry.billing_account_id == BillingAccount.id)
            .exists(),
        )
        .order_by(BillingAccount.id)
    ).all()
    return [("reseller", int(rid), int(r or 0), int(a or 0)) for rid, r, a in resellers] + [
        ("billing_account", int(aid), 0, int(c or 0)) for aid, c in retail
    ]


def resellers_without_account(db: Session) -> list[int]:
    from app.models.commerce_account import BillingAccount, BillingAccountType
    from app.models.reseller import Reseller

    linked = select(BillingAccount.reseller_id).where(
        BillingAccount.account_type == BillingAccountType.RESELLER,
        BillingAccount.reseller_id.is_not(None),
    )
    return [
        int(rid)
        for rid in db.scalars(select(Reseller.id).where(Reseller.id.not_in(linked)).order_by(Reseller.id))
    ]


def chain_breaks(db: Session, account_ids: Optional[list[int]] = None) -> dict[int, tuple[int, int, int]]:
    """First broken entry per account: account id to (entry id, expected, actual balance_after).

    An entry is broken when its ``balance_after_cents`` is not the previous
    entry's (by id, same account) plus its own amount; the first entry must
    equal its own amount.
    """
    from app.models.reseller import CreditLedgerEntry

    previous = func.lag(CreditLedgerEntry.balance_after_cents).over(
        partition_by=CreditLedgerEntry.billing_account_id, order_by=CreditLedgerEntry.id
    )
    inner = select(
        CreditLedgerEntry.id.label("id"),
        CreditLedgerEntry.billing_account_id.label("account_id"),
        CreditLedgerEntry.amount_cents.label("amount"),
        CreditLedgerEntry.balance_after_cents.label("after"),
        previous.label("prev"),
    ).where(CreditLedgerEntry.billing_account_id.is_not(None))
    if account_ids is not None:
        inner = inner.where(CreditLedgerEntry.billing_account_id.in_(account_ids))
    sub = inner.subquery()
    expected = func.coalesce(sub.c.prev, 0) + sub.c.amount
    rows = db.execute(
        select(sub.c.account_id, sub.c.id, expected.label("expected"), sub.c.after)
        .where(sub.c.after != expected)
        .order_by(sub.c.account_id, sub.c.id)
    ).all()
    breaks: dict[int, tuple[int, int, int]] = {}
    for account_id, entry_id, want, actual in rows:
        breaks.setdefault(int(account_id), (int(entry_id), int(want), int(actual)))
    return breaks


def account_chain_state(db: Session, account_ids: list[int]) -> dict[int, dict]:
    """Per account with entries: caches, last entry's balance_after and the entry sum."""
    from app.models.commerce_account import BillingAccount, BillingAccountType
    from app.models.reseller import CreditLedgerEntry, Reseller

    sums = (
        select(
            CreditLedgerEntry.billing_account_id.label("account_id"),
            func.sum(CreditLedgerEntry.amount_cents).label("total"),
            func.max(CreditLedgerEntry.id).label("last_id"),
        )
        .where(CreditLedgerEntry.billing_account_id.in_(account_ids))
        .group_by(CreditLedgerEntry.billing_account_id)
        .subquery()
    )
    last = CreditLedgerEntry.__table__.alias("last_entry")
    rows = db.execute(
        select(
            BillingAccount.id,
            BillingAccount.account_type,
            BillingAccount.credit_balance_cents,
            Reseller.cached_balance_cents,
            sums.c.total,
            last.c.balance_after_cents,
        )
        .join(sums, sums.c.account_id == BillingAccount.id)
        .join(last, last.c.id == sums.c.last_id)
        .outerjoin(Reseller, Reseller.id == BillingAccount.reseller_id)
    ).all()
    state = {}
    for account_id, account_type, account_cache, reseller_cache, total, last_after in rows:
        is_reseller = account_type == BillingAccountType.RESELLER and reseller_cache is not None
        state[int(account_id)] = {
            "reseller": is_reseller,
            "account_cache": int(account_cache or 0),
            "reseller_cache": int(reseller_cache or 0) if is_reseller else None,
            "sum": int(total or 0),
            "last_balance_after": int(last_after or 0),
        }
    return state


def authoritative_cache(state: dict, mode: str) -> int:
    if state["reseller"] and mode != "v2":
        return state["reseller_cache"]
    return state["account_cache"]


def _credit_scan(db: Session, mode: str) -> tuple[int, list[dict]]:
    from app.models.reseller import CreditLedgerEntry

    findings: list[dict] = []
    checked = 0
    after_id = 0
    while True:
        batch = [
            int(value)
            for value in db.scalars(
                select(CreditLedgerEntry.billing_account_id)
                .where(CreditLedgerEntry.billing_account_id > after_id)
                .group_by(CreditLedgerEntry.billing_account_id)
                .order_by(CreditLedgerEntry.billing_account_id)
                .limit(_SCAN_BATCH)
            )
        ]
        if not batch:
            break
        after_id = batch[-1]
        checked += len(batch)
        states = account_chain_state(db, batch)
        breaks = chain_breaks(db, batch)
        for account_id in batch:
            state = states.get(account_id)
            if state is None:
                continue
            cache = authoritative_cache(state, mode)
            if cache != state["last_balance_after"]:
                findings.append(_finding("c2_cache_vs_chain", "billing_account", account_id,
                                         cache, state["last_balance_after"], "cache vs last entry"))
            if cache != state["sum"]:
                findings.append(_finding("c2_cache_vs_sum", "billing_account", account_id,
                                         cache, state["sum"], "cache vs entry sum"))
            if account_id in breaks:
                entry_id, expected, actual = breaks[account_id]
                findings.append(_finding("c2_chain_continuity", "billing_account", account_id,
                                         expected, actual, f"ledger_entry:{entry_id}"))
    unexplained = caches_without_entries(db)
    unexplained_resellers = {sid for kind, sid, _, _ in unexplained if kind == "reseller"}
    for subject_type, subject_id, legacy_value, v2_value in unexplained:
        findings.append(_finding(CACHE_WITHOUT_ENTRIES, subject_type, subject_id, legacy_value, v2_value,
                                 "cache_without_entries: non-zero balance with no ledger entries"))
    from app.models.commerce_account import BillingAccount

    for account_id, reseller_cache, account_cache in reseller_cache_differences(db):
        # A reseller with no entries is reported once, above, with both caches.
        if db.scalar(select(BillingAccount.reseller_id).where(BillingAccount.id == account_id)) in unexplained_resellers:
            continue
        findings.append(_finding("c2_mirror", "billing_account", account_id,
                                 reseller_cache, account_cache, "reseller cache vs account cache"))
    for reseller_id, count in unlinked_entry_counts(db).items():
        findings.append(_finding("c2_unlinked", "reseller", reseller_id, 0, count,
                                 "entries without billing_account_id"))
    return checked, findings


def _invoice_scan(db: Session, now: datetime) -> Optional[tuple[int, list[dict]]]:
    from app.models.reseller import Invoice, InvoiceStatus
    from app.services.invoice_service import InvoiceService

    window = now - timedelta(hours=48)
    selected = and_(
        Invoice.status.not_in([InvoiceStatus.DRAFT, InvoiceStatus.VOID]),
        or_(Invoice.updated_at >= window, Invoice.id % 7 == now.weekday()),
    )
    findings: list[dict] = []
    checked = 0
    after_id = 0
    while True:
        invoices = list(
            db.scalars(
                select(Invoice)
                .where(selected, Invoice.id > after_id)
                .order_by(Invoice.id)
                .limit(_SCAN_BATCH)
            )
        )
        if not invoices:
            break
        after_id = invoices[-1].id
        for invoice in invoices:
            try:
                balance = InvoiceService.balance(db, invoice)
            except NotImplementedError:
                logger.info("billing shadow scan: InvoiceService.balance is not implemented; invoice scan skipped")
                return None
            checked += 1
            allocated = InvoiceService.allocated_cents(db, invoice.id)
            legacy = {
                "remaining_cents": max(0, int(invoice.amount_cents) - allocated),
                "paid": allocated >= int(invoice.amount_cents),
            }
            v2 = {"remaining_cents": int(balance.remaining_cents), "paid": balance.remaining_cents == 0}
            if legacy == v2:
                continue
            context = f"status:{invoice.status.value}"
            explained = _partial_refund_difference(db, invoice.id)
            legacy_due = int(invoice.amount_cents) - allocated
            if explained is not None and int(balance.due_cents) - legacy_due == explained:
                findings.append(_finding(EXPECTED_PARTIAL_REFUND, "invoice", invoice.id, legacy, v2,
                                         f"{context};refund_difference_cents:{explained}"))
            else:
                findings.append(_finding("i2_scan", "invoice", invoice.id, legacy, v2, context))
    return checked, findings


def _partial_refund_difference(db: Session, invoice_id: int) -> Optional[int]:
    """How much partial refunds alone move v2's amount due away from legacy's, or None.

    Legacy counts the full amount of a ``succeeded`` payment and nothing of a
    ``partially_refunded`` one; v2 counts ``amount - refunded_cents`` of both.
    Returns the sum of that per-payment difference over the invoice's payments
    with a partial refund, or None when it has none. A fully refunded payment
    counts 0 in both models, so it adds nothing.
    """
    from app.models.reseller import Payment, PaymentStatus

    rows = db.execute(
        select(Payment.status, Payment.amount_cents, Payment.refunded_cents).where(
            Payment.invoice_id == invoice_id,
            or_(
                Payment.status == PaymentStatus.PARTIALLY_REFUNDED,
                and_(Payment.status == PaymentStatus.SUCCEEDED, Payment.refunded_cents > 0),
            ),
        )
    ).all()
    if not rows:
        return None
    difference = 0
    for status, amount, refunded in rows:
        legacy_counted = int(amount) if status == PaymentStatus.SUCCEEDED else 0
        v2_counted = int(amount) - int(refunded or 0)
        difference += legacy_counted - v2_counted
    return difference


# --- Engine switch records and the switch criterion (design 6.5) ---


def switch_record(db: Session, engine: str, *, for_update: bool = False) -> dict:
    row = db.get(SystemSetting, SWITCH_RECORD_PREFIX + engine, with_for_update=for_update)
    return dict(row.value) if row is not None and isinstance(row.value, dict) else {}


def write_switch_record(db: Session, engine: str, value: dict) -> None:
    row = db.get(SystemSetting, SWITCH_RECORD_PREFIX + engine)
    if row is None:
        db.add(SystemSetting(key=SWITCH_RECORD_PREFIX + engine, value=value))
    else:
        row.value = value
    db.flush()


def shadow_since(db: Session, engine: str) -> Optional[datetime]:
    return _parse_time(switch_record(db, engine).get("shadow_since"))


def set_shadow_since(db: Session, engine: str, *, at: datetime, by: str, reset: bool = False) -> bool:
    """Start the engine's quiet-period clock. Never moves an existing value back.

    Without ``reset`` an existing value is kept; with it (the engine was just
    recorded as entering shadow) the clock restarts at ``at`` if that is later.
    Returns whether the value changed.
    """
    record = switch_record(db, engine, for_update=True)
    current = _parse_time(record.get("shadow_since"))
    at = _aware(at)
    if current is not None and (not reset or at <= current):
        return False
    record["shadow_since"] = at.isoformat()
    record["shadow_since_set_by"] = by
    write_switch_record(db, engine, record)
    return True


def clear_shadow_since(db: Session, engine: str) -> None:
    record = switch_record(db, engine, for_update=True)
    record.pop("shadow_since", None)
    record.pop("shadow_since_set_by", None)
    write_switch_record(db, engine, record)


def open_mismatch_counts(db: Session, engine: str, *, expected: bool = False) -> dict[str, int]:
    """Open rows per check key: blocking mismatches, or with ``expected`` the expected differences."""
    keys = BillingShadowMismatch.check_key.in_(EXPECTED_DIFFERENCE_KEYS)
    rows = db.execute(
        select(BillingShadowMismatch.check_key, func.count(BillingShadowMismatch.id))
        .where(
            BillingShadowMismatch.engine == engine,
            BillingShadowMismatch.resolved_at.is_(None),
            keys if expected else ~keys,
        )
        .group_by(BillingShadowMismatch.check_key)
    ).all()
    return {key: int(count) for key, count in rows}


def _daily_scan_coverage(history: list[dict], quiet_start: Optional[datetime], now: datetime) -> dict:
    """Which UTC days of the quiet period have a complete, clean, scheduled scan (design 6.5 item 4).

    A scan counts when it is scheduled, complete, found no blocking mismatch
    and started at or after ``quiet_start``. ``days_missing`` lists every whole
    UTC day strictly between the day of ``quiet_start`` and today that has no
    such scan; the partial first day and today may be covered but need not be.
    Only the last ``HISTORY_LIMIT`` days are checked, the most the capped
    history can hold at one scheduled scan a day.
    """
    counted = []
    for entry in history:
        started = _parse_time(entry.get("started_at"))
        finished = _parse_time(entry.get("finished_at"))
        if quiet_start is None or started is None or finished is None or started < quiet_start:
            continue
        if entry.get("scheduled") is not True or entry.get("complete") is not True:
            continue
        if int(entry.get("mismatches_found", 1)) != 0:
            continue
        counted.append((started, finished))
    days = sorted({started.astimezone(timezone.utc).date() for started, _ in counted})
    missing = []
    if quiet_start is not None:
        today = now.astimezone(timezone.utc).date()
        day = max(
            quiet_start.astimezone(timezone.utc).date() + timedelta(days=1),
            today - timedelta(days=HISTORY_LIMIT - 1),
        )
        covered = set(days)
        while day < today:
            if day not in covered:
                missing.append(day.isoformat())
            day += timedelta(days=1)
    return {
        "scans": len(counted),
        "days_covered": [day.isoformat() for day in days],
        "days_missing": missing,
        "latest_scheduled_finished_at": max((finished for _, finished in counted), default=None),
    }


def _item(name: str, ok: bool, **details: Any) -> dict:
    return {"item": name, "ok": bool(ok), **_jsonable(details)}


def evaluate_switch(
    db: Session, engine: str, now: datetime, *, confirm_overpayments_to_reseller_cache: bool = False
) -> list[dict]:
    """Each item of the switch criterion for moving ``engine`` to v2, with its values. Never writes."""
    from app.services.settings_store import get_setting

    now = _aware(now)
    min_days = int(get_setting(db, "billing_shadow_min_days"))
    mode = billing_engine_mode(db, engine)
    items = [_item("mode_is_shadow", mode == "shadow", mode=mode)]
    if engine == "credit":
        unlinked = sum(unlinked_entry_counts(db).values())
        items.append(_item("no_unlinked_entries", unlinked == 0, unlinked_entries=unlinked))
        missing = resellers_without_account(db)
        items.append(_item("every_reseller_has_account", not missing, resellers_without_account=missing[:50]))
        differing = reseller_cache_differences(db)
        items.append(_item("reseller_caches_match", not differing,
                           accounts_differing=[account for account, _, _ in differing[:50]]))
    else:
        credit_mode = billing_engine_mode(db, "credit")
        items.append(_item("credit_engine_v2_or_confirmed",
                           credit_mode == "v2" or confirm_overpayments_to_reseller_cache,
                           credit_mode=credit_mode,
                           confirmed=confirm_overpayments_to_reseller_cache))
        # The migration chain is linear, so revision 9 (zero totals) is applied
        # whenever this code runs.
        items.append(_item("zero_totals_migration_applied", True))
    since = shadow_since(db, engine)
    last_seen = _parse_time(
        db.scalar(
            select(func.max(BillingShadowMismatch.last_seen_at)).where(
                BillingShadowMismatch.engine == engine,
                BillingShadowMismatch.check_key.not_in(EXPECTED_DIFFERENCE_KEYS),
            )
        )
    )
    quiet_start = max([moment for moment in (since, last_seen) if moment is not None], default=None)
    quiet_days = (now - quiet_start).total_seconds() / 86400 if quiet_start is not None else None
    items.append(_item(
        "quiet_period",
        since is not None and quiet_days is not None and quiet_days >= min_days,
        shadow_since=since, last_mismatch_seen_at=last_seen, quiet_days=quiet_days, required_days=min_days,
    ))
    coverage = _daily_scan_coverage(BillingShadowService.scan_history(db, engine), quiet_start, now)
    latest = BillingShadowService.last_scan(db, engine)
    latest_finished = coverage["latest_scheduled_finished_at"]
    fresh = latest_finished is not None and now - latest_finished < timedelta(hours=SCAN_FRESH_HOURS)
    latest_clean = latest is not None and int(latest.get("mismatches_found", 1)) == 0
    items.append(_item(
        "daily_scans",
        quiet_start is not None
        and len(coverage["days_covered"]) >= min_days
        and not coverage["days_missing"]
        and fresh
        and latest_clean,
        scans_in_quiet_period=coverage["scans"], days_covered=len(coverage["days_covered"]),
        required_days=min_days, days_missing=coverage["days_missing"][:50],
        latest_scheduled_finished_at=latest_finished,
        latest_mismatches=latest.get("mismatches_found") if latest else None,
    ))
    open_counts = open_mismatch_counts(db, engine)
    items.append(_item("no_open_mismatches", not open_counts, open_mismatches=open_counts))
    return items
