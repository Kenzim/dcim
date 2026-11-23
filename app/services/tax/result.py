"""The output of the tax engine (docs/billing-phase2-design.md 4.3 item 7)."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class LineTaxPart:
    """One tax applied to one line (one ``invoice_line_taxes`` row)."""

    rule_id: Optional[int]
    name: str
    rate_percent: Decimal
    jurisdiction: str
    base_cents: int
    tax_cents: int
    reverse_charge: bool = False
    legal_note_key: Optional[str] = None
    # ``invoice_line_taxes.source``: ``rules``, ``tax_rates`` or ``provider:<plugin_id>:<id>``.
    source: str = "rules"


@dataclass(frozen=True)
class LineTax:
    key: str
    parts: tuple[LineTaxPart, ...]
    tax_cents: int


@dataclass(frozen=True)
class TaxSummaryEntry:
    name: str
    rate_percent: Decimal
    jurisdiction: str
    base_cents: int
    tax_cents: int
    reverse_charge: bool = False


@dataclass(frozen=True)
class TaxEvidence:
    values: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TaxResult:
    lines: Mapping[str, LineTax]
    summary: tuple[TaxSummaryEntry, ...]
    legal_note_keys: tuple[str, ...]
    registration_id: Optional[int]
    evidence: TaxEvidence
    engine: str
    context_hash: str
    scope: str = "in_scope"

    @property
    def tax_cents(self) -> int:
        return sum(int(line.tax_cents) for line in self.lines.values())

    def line(self, key: str) -> Optional[LineTax]:
        return self.lines.get(key)

    def summary_json(self) -> list[dict[str, Any]]:
        """``invoices.tax_summary``: one entry per name, rate, jurisdiction and reverse charge."""
        return [
            {
                "name": entry.name,
                "rate_percent": str(entry.rate_percent),
                "jurisdiction": entry.jurisdiction,
                "reverse_charge": bool(entry.reverse_charge),
                "base_cents": int(entry.base_cents),
                "tax_cents": int(entry.tax_cents),
            }
            for entry in self.summary
        ]
