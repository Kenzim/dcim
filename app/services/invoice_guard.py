"""ORM guard that keeps published invoices immutable (docs/billing-phase1-design.md 3.5).

Once an invoice has left ``draft`` its frozen columns may not change and its
lines may not be inserted, updated or deleted. A violation either raises
``FrozenInvoiceError`` (mode ``enforce``) or is logged and recorded as shadow
check ``i4_frozen_write`` (mode ``log``) and allowed to proceed. The mode is the
setting ``billing_invoice_guard_mode``, except that it is always ``enforce``
once the invoice engine is ``v2``; it is only read when a violation is found,
so writes that respect the rules cost nothing extra.

The only escape hatch is ``allow(name, columns, invoice_id)``, used by ``InvoiceService``
for the proforma conversion. Core ``update(Invoice)`` statements bypass these
events; a test keeps them out of every module but the invoice service and the
backfills.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import traceback
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, Optional

from sqlalchemy import event, inspect, select
from sqlalchemy.orm import Session, object_session

from app.models.commerce_invoice_ext import InvoiceLine
from app.models.invoice_adjustment import InvoiceAdjustment
from app.models.reseller import Invoice, InvoiceStatus
from app.models.tax import InvoiceLineTax

logger = logging.getLogger(__name__)

FROZEN_COLUMNS = frozenset(
    {
        "invoice_number",
        "reseller_id",
        "billing_account_id",
        "order_id",
        "purpose",
        "amount_cents",
        "subtotal_cents",
        "discount_cents",
        "tax_cents",
        "prices_include_tax",
        "currency",
        "description",
        "customer_notes",
        "locale",
        "seller_snapshot",
        "buyer_snapshot",
        "model_version",
        "created_at",
        "tax_engine",
        "tax_summary",
        "tax_evidence",
        "legal_notes",
        "fx_rate",
        "fx_period",
        "fx_source",
        "tax_total_gbp_cents",
        "fx_rate_id",
    }
)
# NULL to a value once; a value never changes. ``service_id`` is here because a
# reseller deploy charge is invoiced before the service it pays for exists.
ONCE_SET_COLUMNS = frozenset(
    {
        "service_id",
        "issued_at",
        "series_id",
        "number_period",
        "number_value",
        "number_display",
        "proforma_series_id",
        "proforma_number_period",
        "proforma_number_value",
        "proforma_number_display",
        "tax_point_at",
        "seller_registration_id",
    }
)
# Mirrors of ON DELETE SET NULL: a change to NULL is allowed. ``service_id`` is
# not here: the database clears it when a service is deleted, without the ORM.
NULLABLE_FOREIGN_KEYS = frozenset({"billing_account_id"})
LINE_NULLABLE_FOREIGN_KEYS = frozenset({"service_id", "service_billing_id", "order_item_id"})
GUARDED_COLUMNS = FROZEN_COLUMNS | ONCE_SET_COLUMNS | {"document_type"}
# Columns of ``invoice_adjustments`` frozen from insert (design 4.10).
NOTE_FROZEN_COLUMNS = frozenset(
    {"fx_rate", "fx_period", "fx_source", "tax_total_gbp_cents", "fx_rate_id"}
)

MODES = ("log", "enforce")
PROFORMA_CONVERSION = "proforma_conversion"
_PENDING_KEY = "invoice_guard.pending_records"
_DRAFT = InvoiceStatus.DRAFT.value


class FrozenInvoiceError(RuntimeError):
    """A published invoice's frozen column or line was written."""


_allowance: contextvars.ContextVar[Optional[tuple[str, frozenset, int]]] = contextvars.ContextVar(
    "invoice_guard_allowance", default=None
)
_mode_override: Optional[str] = None


@contextlib.contextmanager
def allow(name: str, columns: Iterable[str], invoice_id: int) -> Iterator[None]:
    """Allow writes to ``columns`` of the published invoice ``invoice_id`` inside the block.

    Only ``proforma_conversion`` exists; the flush that writes the columns must
    happen inside the block. Other invoices flushed in the block stay guarded.
    """
    if name != PROFORMA_CONVERSION:
        raise ValueError(f"unknown invoice guard allowance {name!r}")
    token = _allowance.set((name, frozenset(columns), int(invoice_id)))
    try:
        yield
    finally:
        _allowance.reset(token)


def set_mode_override(mode: Optional[str]) -> Optional[str]:
    """Force the guard mode for this process (tests); returns the previous override."""
    global _mode_override
    if mode is not None and mode not in MODES:
        raise ValueError(f"unknown invoice guard mode {mode!r}")
    previous = _mode_override
    _mode_override = mode
    return previous


def mode_for(db: Session) -> str:
    """The effective guard mode: always ``enforce`` once the invoice engine is ``v2``."""
    from app.services.billing_shadow import billing_engine_mode
    from app.services.settings_store import get_setting

    if _mode_override is not None:
        return _mode_override
    if billing_engine_mode(db, "invoice") == "v2":
        return "enforce"
    mode = get_setting(db, "billing_invoice_guard_mode")
    return mode if mode in MODES else "log"


def _stored_value(connection, key: str) -> Any:
    from app.models.commerce_account import SystemSetting

    table = SystemSetting.__table__
    return connection.execute(select(table.c.value).where(table.c.key == key)).scalar()


def _setting(connection, key: str) -> Any:
    from app.services.settings_store import SCHEMA, STORAGE_PREFIX

    stored = _stored_value(connection, STORAGE_PREFIX + key)
    if isinstance(stored, dict) and "value" in stored:
        return stored["value"]
    return SCHEMA[key].default_for()


def _mode_on(connection) -> str:
    """``mode_for`` read with Core on the flush's own connection (no session inside a flush)."""
    from app.services.billing_shadow import ENGINE_SETTING, SWITCH_RECORD_PREFIX

    if _mode_override is not None:
        return _mode_override
    try:
        if _setting(connection, ENGINE_SETTING["invoice"]) == "v2":
            switch = _stored_value(connection, SWITCH_RECORD_PREFIX + "invoice")
            if isinstance(switch, dict) and switch.get("mode") == "v2":
                return "enforce"
        mode = _setting(connection, "billing_invoice_guard_mode")
        return mode if mode in MODES else "log"
    except Exception:  # noqa: BLE001
        # Only read once a violation is found: refusing that write is the safe side.
        logger.exception("invoice guard: could not read its mode; enforcing")
        return "enforce"


def _plain(value: Any) -> Any:
    value = getattr(value, "value", value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(microsecond=0)
    return value


def _same(left: Any, right: Any) -> bool:
    return _plain(left) == _plain(right)


class _Committed:
    """The invoice (or note) row as stored, read through the flush's connection once, when needed."""

    def __init__(self, connection, invoice_id: Optional[int], table: Any = None):
        self._connection = connection
        self._invoice_id = invoice_id
        self._table = table if table is not None else Invoice.__table__
        self._row: Any = None
        self._loaded = False

    def get(self, name: str) -> Any:
        if not self._loaded:
            self._loaded = True
            if self._invoice_id is not None:
                table = self._table
                self._row = (
                    self._connection.execute(select(table).where(table.c.id == self._invoice_id))
                    .mappings()
                    .first()
                )
        if self._row is None:
            return None
        return self._row.get(name)


def _old_value(state, name: str, committed: _Committed) -> tuple[bool, Any, Any]:
    """``(changed, old, new)`` for one attribute of an invoice being updated."""
    history = state.attrs[name].history
    if not history.added:
        return False, None, None
    new = history.added[0]
    if history.deleted:
        old = history.deleted[0]
    else:
        old = committed.get(state.mapper.columns[name].name)
    return not _same(old, new), old, new


def _old_status(state, committed: _Committed) -> str:
    history = state.attrs["status"].history
    if history.deleted:
        return _plain(history.deleted[0])
    if history.added:
        return _plain(committed.get("status"))
    return _plain(state.obj().status)


def _allowed(
    name: str,
    old: Any,
    new: Any,
    allowance: Optional[tuple[str, frozenset, int]],
    invoice_id: Optional[int],
) -> bool:
    if allowance is not None and name in allowance[1] and invoice_id == allowance[2]:
        if name == "document_type":
            return _plain(old) == "proforma" and _plain(new) == "invoice"
        return True
    if name in ONCE_SET_COLUMNS and old is None:
        return True
    if name in NULLABLE_FOREIGN_KEYS and new is None:
        return True
    return False


# Personal data or free text: logs and shadow rows carry only their size.
_REDACTED_COLUMNS = frozenset({"seller_snapshot", "buyer_snapshot", "description", "customer_notes"})
_MAX_LOGGED_CHARS = 120


def _safe(column: Optional[str], value: Any) -> Any:
    """A value fit for a log line or a shadow row: redacted or cut short."""
    if value is None:
        return None
    if column in _REDACTED_COLUMNS:
        return f"<redacted, {len(str(value))} characters>"
    value = _plain(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (int, float, bool)):
        return value
    text_value = str(value)
    if len(text_value) > _MAX_LOGGED_CHARS:
        return text_value[:_MAX_LOGGED_CHARS] + "..."
    return text_value


def _violation(connection, target: Any, invoice_id: Optional[int], what: str, detail: dict) -> None:
    mode = _mode_on(connection)
    message = f"invoice {invoice_id}: {what} on a published invoice"
    if mode == "enforce":
        raise FrozenInvoiceError(message)
    column = detail.get("column")
    record = {
        "invoice_id": invoice_id,
        "what": what,
        **{key: _safe(column if key in ("old", "new") else None, value) for key, value in detail.items()},
    }
    logger.error(
        "invoice guard (log mode): %s %s\n%s",
        message,
        record,
        "".join(traceback.format_stack(limit=12)),
    )
    session = object_session(target)
    if session is None:
        _record([record])
        return
    transaction = session.get_nested_transaction() or session.get_transaction()
    session.info.setdefault(_PENDING_KEY, []).append((transaction, record))


def _record(records: list[dict]) -> None:
    from app.services.billing_shadow import BillingShadowService

    for record in records:
        invoice_id = record.get("invoice_id")
        if invoice_id is None:
            continue
        BillingShadowService.record(
            "invoice",
            "i4_frozen_write",
            "invoice",
            int(invoice_id),
            {"what": record.get("what"), "column": record.get("column"), "old": record.get("old")},
            {"new": record.get("new")},
            context="invoice_guard",
        )


def _guard_invoice_update(_mapper, connection, target: Invoice) -> None:
    state = inspect(target)
    committed = _Committed(connection, target.id)
    old_status = _old_status(state, committed)
    if old_status == _DRAFT:
        return
    allowance = _allowance.get()
    for name in sorted(GUARDED_COLUMNS):
        changed, old, new = _old_value(state, name, committed)
        if not changed or _allowed(name, old, new, allowance, target.id):
            continue
        _violation(
            connection,
            target,
            target.id,
            f"write to frozen column {name}",
            {"column": name, "old": old, "new": new, "status": old_status},
        )


def _guard_note_update(_mapper, connection, target: InvoiceAdjustment) -> None:
    """A note's sterling VAT is frozen from insert (notes are never drafts)."""
    state = inspect(target)
    committed = _Committed(connection, target.id, InvoiceAdjustment.__table__)
    for name in sorted(NOTE_FROZEN_COLUMNS):
        changed, old, new = _old_value(state, name, committed)
        if not changed:
            continue
        _violation(
            connection,
            target,
            target.invoice_id,
            f"write to frozen note column {name}",
            {"column": name, "old": old, "new": new, "adjustment_id": target.id},
        )


def _guard_invoice_delete(_mapper, connection, target: Invoice) -> None:
    committed = _Committed(connection, target.id)
    status = _plain(committed.get("status")) if target.id is not None else _plain(target.status)
    if status in (None, _DRAFT):
        return
    _violation(connection, target, target.id, "delete", {"status": status})


def _parent_status(connection, invoice_id: Optional[int]) -> Optional[str]:
    if invoice_id is None:
        return None
    table = Invoice.__table__
    return _plain(
        connection.execute(select(table.c.status).where(table.c.id == invoice_id)).scalar()
    )


def _guard_line(action: str):
    def guard(_mapper, connection, target: InvoiceLine) -> None:
        parents = [target.invoice_id]
        if action == "update":
            state = inspect(target)
            changed = [
                attr.key
                for attr in state.attrs
                if attr.history.added and not _same(
                    attr.history.deleted[0] if attr.history.deleted else object(),
                    attr.history.added[0],
                )
            ]
            if changed and all(
                key in LINE_NULLABLE_FOREIGN_KEYS and getattr(target, key) is None
                for key in changed
            ):
                return
            # A line moved between invoices changes both of them.
            history = state.attrs["invoice_id"].history
            if history.deleted and history.deleted[0] is not None:
                parents.insert(0, history.deleted[0])
        for invoice_id in dict.fromkeys(parents):
            status = _parent_status(connection, invoice_id)
            if status in (None, _DRAFT):
                continue
            _violation(
                connection,
                target,
                invoice_id,
                f"invoice line {action}",
                {"column": "invoice_lines", "line_id": target.id, "status": status},
            )

    return guard


def _guard_line_tax(action: str):
    """``invoice_line_taxes`` rows of a published invoice are frozen (design 1.5)."""

    def guard(_mapper, connection, target: InvoiceLineTax) -> None:
        parents = [target.invoice_id]
        if action == "update":
            history = inspect(target).attrs["invoice_id"].history
            if history.deleted and history.deleted[0] is not None:
                parents.insert(0, history.deleted[0])
        for invoice_id in dict.fromkeys(parents):
            status = _parent_status(connection, invoice_id)
            if status in (None, _DRAFT):
                continue
            _violation(
                connection,
                target,
                invoice_id,
                f"invoice line tax {action}",
                {"column": "invoice_line_taxes", "line_tax_id": target.id, "status": status},
            )

    return guard


def _descends_from(transaction, ancestor) -> bool:
    while transaction is not None:
        if transaction is ancestor:
            return True
        transaction = transaction.parent
    return False


def _write_on_root_commit(session: Session) -> None:
    """Write log-mode records once the outermost transaction commits (not a savepoint).

    This runs after the caller's data has committed, so a failed record write
    is logged and dropped: raising would make the caller's ``commit()`` report
    a committed transaction as failed.
    """
    if session.get_nested_transaction() is not None:
        return
    pending = session.info.pop(_PENDING_KEY, None)
    if pending:
        try:
            _record([record for _transaction, record in pending])
        except Exception:  # noqa: BLE001 - never fail a commit that has happened
            logger.exception(
                "invoice guard: %d log-mode record(s) could not be written after commit",
                len(pending),
            )


def _drop_rolled_back(session: Session, previous_transaction) -> None:
    """Forget records of writes undone by a rollback (a savepoint's, or all of them)."""
    pending = session.info.get(_PENDING_KEY)
    if pending:
        session.info[_PENDING_KEY] = [
            item for item in pending if not _descends_from(item[0], previous_transaction)
        ]


def _clear_at_root_end(session: Session, transaction) -> None:
    """A root transaction that ended without a commit (rollback or close) leaves nothing behind."""
    if transaction.parent is None:
        session.info.pop(_PENDING_KEY, None)


_LISTENERS = (
    (Invoice, "before_update", _guard_invoice_update),
    (Invoice, "before_delete", _guard_invoice_delete),
    (InvoiceAdjustment, "before_update", _guard_note_update),
    (InvoiceLine, "before_insert", _guard_line("insert")),
    (InvoiceLine, "before_update", _guard_line("update")),
    (InvoiceLine, "before_delete", _guard_line("delete")),
    (InvoiceLineTax, "before_insert", _guard_line_tax("insert")),
    (InvoiceLineTax, "before_update", _guard_line_tax("update")),
    (InvoiceLineTax, "before_delete", _guard_line_tax("delete")),
)
_registered = False


def register() -> None:
    """Install the listeners once per process."""
    global _registered
    if _registered:
        return
    _registered = True
    for target, name, handler in _LISTENERS:
        event.listen(target, name, handler)
    event.listen(Session, "after_commit", _write_on_root_commit)
    event.listen(Session, "after_soft_rollback", _drop_rolled_back)
    event.listen(Session, "after_transaction_end", _clear_at_root_end)


register()
