"""Property: every move the v2 status rule makes is an edge of the v2 transition table.

The rule depends only on the signs of the balance components, the current
status, the cause and the dispute policy, so an exhaustive search over small
values covers every case.
"""

from __future__ import annotations

import itertools
from types import SimpleNamespace
from unittest import mock

from app.models.reseller import InvoiceStatus
from app.services.invoice_service import (
    TERMINAL_V2,
    V2_EDGES,
    InvoiceBalance,
    InvoiceService,
)

S = InvoiceStatus
LIVE = [s for s in S if s not in TERMINAL_V2 and s != S.DRAFT]
CAUSES = ("settlement", "payment", "refund", "credit_note", "dispute")


def _balance(total, debit, credit, paid_gross, refunded, pending):
    worth = total + debit - credit
    paid_net = paid_gross - refunded
    due = worth - paid_net
    return InvoiceBalance(total, debit, credit, worth, paid_gross, refunded, paid_net, pending,
                          due, max(0, due))


def _balances():
    for total, debit, credit, paid_gross, pending in itertools.product(
        (0, 1, 2), (0, 1), (0, 1, 2, 3), (0, 1, 2, 3), (0, 1)
    ):
        for refunded in range(paid_gross + 1):
            balance = _balance(total, debit, credit, paid_gross, refunded, pending)
            if balance.due_cents >= 0:  # settlement converts an overpayment first
                yield balance


def _target(current, balance, cause):
    return InvoiceService.target_status(None, SimpleNamespace(status=current), balance, cause=cause)


def test_status_rule_only_takes_table_edges():
    missing = set()
    for reopens in (False, True):
        with mock.patch.object(InvoiceService, "_dispute_reopens", staticmethod(lambda db, inv: reopens)):
            for current, balance, cause in itertools.product(LIVE, list(_balances()), CAUSES):
                target = _target(current, balance, cause)
                if target != current and target not in V2_EDGES[current]:
                    missing.add((current.value, target.value))
    assert missing == set()


def test_terminal_and_draft_statuses_never_move():
    for current in (*TERMINAL_V2, S.DRAFT):
        for balance in _balances():
            assert _target(current, balance, "settlement") == current


def test_full_refund_without_note_then_credit_note_ends_refunded():
    reopened = _balance(1000, 0, 0, 1000, 1000, 0)
    assert _target(S.PAID, reopened, "refund") == S.OPEN
    cleared = _balance(1000, 0, 1000, 1000, 1000, 0)
    target = _target(S.OPEN, cleared, "credit_note")
    assert target == S.REFUNDED and target in V2_EDGES[S.OPEN]
