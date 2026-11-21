"""ORM guard that keeps published invoices immutable (docs/billing-phase1-design.md 3.5).

Once an invoice has left ``draft`` its frozen columns may not change and its
lines may not be inserted, updated or deleted. A violation either raises
``FrozenInvoiceError`` (mode ``enforce``) or is logged and recorded as shadow
check ``i4_frozen_write`` (mode ``log``) and allowed to proceed. The mode is the
setting ``billing_invoice_guard_mode``, except that it is always ``enforce``
once the invoice engine is ``v2``; it is only read when a violation is found,
so writes that respect the rules cost nothing extra.

The only escape hatch is ``allow(name, columns)``, used by ``InvoiceService``
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
from app.models.reseller import Invoice, InvoiceStatus

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
    }
)
# Mirrors of ON DELETE SET NULL: a change to NULL is allowed.
NULLABLE_FOREIGN_KEYS = frozenset({"service_id", "billing_account_id"})
LINE_NULLABLE_FOREIGN_KEYS = frozenset({"service_id", "service_billing_id", "order_item_id"})
GUARDED_COLUMNS = FROZEN_COLUMNS | ONCE_SET_COLUMNS | {"document_type"}

MODES = ("log", "enforce")
PROFORMA_CONVERSION = "proforma_conversion"
_PENDING_KEY = "invoice_guard.pending_records"
_DRAFT = InvoiceStatus.DRAFT.value


class FrozenInvoiceError(RuntimeError):
    """A published invoice's frozen column or line was written."""


_allowance: contextvars.ContextVar[Optional[tuple[str, frozenset]]] = contextvars.ContextVar(
    "invoice_guard_allowance", default=None
)
_mode_override: Optional[str] = None


@contextlib.contextmanager
def allow(name: str, columns: Iterable[str]) -> Iterator[None]:
    """Allow writes to ``columns`` of published invoices inside the block.

    Only ``proforma_conversion`` exists; the flush that writes the columns must
    happen inside the block.
    """
    if name != PROFORMA_CONVERSION:
        raise ValueError(f"unknown invoice guard allowance {name!r}")
    token = _allowance.set((name, frozenset(columns)))
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
    except Exception:  # noqa: BLE001 - an unreadable setting must not block billing
        logger.exception("invoice guard: could not read its mode; logging only")
        return "log"


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
    """The invoice row as stored, read through the flush's connection once, when needed."""

    def __init__(self, connection, invoice_id: Optional[int]):
        self._connection = connection
        self._invoice_id = invoice_id
        self._row: Any = None
        self._loaded = False

    def get(self, name: str) -> Any:
        if not self._loaded:
            self._loaded = True
            if self._invoice_id is not None:
                table = Invoice.__table__
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
        old = committed.get(Invoice.__table__.c[name].name)
    return not _same(old, new), old, new


def _old_status(state, committed: _Committed) -> str:
    history = state.attrs["status"].history
    if history.deleted:
        return _plain(history.deleted[0])
    if history.added:
        return _plain(committed.get("status"))
    return _plain(state.obj().status)


def _allowed(name: str, old: Any, new: Any, allowance: Optional[tuple[str, frozenset]]) -> bool:
    if allowance is not None and name in allowance[1]:
        if name == "document_type":
            return _plain(old) == "proforma" and _plain(new) == "invoice"
        return True
    if name in ONCE_SET_COLUMNS and old is None:
        return True
    if name in NULLABLE_FOREIGN_KEYS and new is None:
        return True
    return False


def _violation(connection, target: Any, invoice_id: Optional[int], what: str, detail: dict) -> None:
    mode = _mode_on(connection)
    message = f"invoice {invoice_id}: {what} on a published invoice"
    if mode == "enforce":
        raise FrozenInvoiceError(message)
    logger.error(
        "invoice guard (log mode): %s %s\n%s",
        message,
        detail,
        "".join(traceback.format_stack(limit=12)),
    )
    session = object_session(target)
    record = {"invoice_id": invoice_id, "what": what, **{k: _plain(v) for k, v in detail.items()}}
    if session is None:
        _record([record])
    else:
        session.info.setdefault(_PENDING_KEY, []).append(record)


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
        if not changed or _allowed(name, old, new, allowance):
            continue
        _violation(
            connection,
            target,
            target.id,
            f"write to frozen column {name}",
            {"column": name, "old": old, "new": new, "status": old_status},
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
        invoice_id = target.invoice_id
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
            history = state.attrs["invoice_id"].history
            if history.deleted and history.deleted[0] is not None:
                invoice_id = history.deleted[0]
        status = _parent_status(connection, invoice_id)
        if status in (None, _DRAFT):
            return
        _violation(
            connection,
            target,
            invoice_id,
            f"invoice line {action}",
            {"column": "invoice_lines", "line_id": target.id, "status": status},
        )

    return guard


def _flush_pending(session: Session, transaction) -> None:
    if transaction.parent is not None:
        return
    records = session.info.pop(_PENDING_KEY, None)
    if records:
        _record(records)


_LISTENERS = (
    (Invoice, "before_update", _guard_invoice_update),
    (Invoice, "before_delete", _guard_invoice_delete),
    (InvoiceLine, "before_insert", _guard_line("insert")),
    (InvoiceLine, "before_update", _guard_line("update")),
    (InvoiceLine, "before_delete", _guard_line("delete")),
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
    event.listen(Session, "after_transaction_end", _flush_pending)


register()
