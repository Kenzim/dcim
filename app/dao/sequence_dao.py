"""Transactional named counters shared by invoices, orders, and tickets."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models.reseller import InvoiceSequence


class SequenceError(ValueError):
    pass


class SequenceDAO:
    @staticmethod
    def _initial_next_value(db: Session, table_model: Any, column: Any, first_number: int) -> int:
        largest = db.scalar(select(func.max(column)))
        return max(first_number, int(largest or 0) + 1)

    @staticmethod
    def allocate(
        db: Session,
        name: str,
        *,
        table_model: Any,
        column: Any,
        first_number: int = 100001,
    ) -> int:
        dialect = db.get_bind().dialect.name
        if dialect == "sqlite":
            initial = SequenceDAO._initial_next_value(db, table_model, column, first_number)
            db.execute(
                sqlite_insert(InvoiceSequence)
                .values(name=name, next_value=initial)
                .on_conflict_do_nothing(index_elements=["name"])
            )
            allocated = db.scalar(
                update(InvoiceSequence)
                .where(InvoiceSequence.name == name)
                .values(next_value=InvoiceSequence.next_value + 1)
                .returning(InvoiceSequence.next_value - 1)
            )
            if allocated is None:
                raise SequenceError(f"Could not allocate sequence {name!r}")
            return int(allocated)

        sequence = db.execute(
            select(InvoiceSequence).where(InvoiceSequence.name == name).with_for_update()
        ).scalar_one_or_none()
        if sequence is None:
            sequence = InvoiceSequence(
                name=name,
                next_value=SequenceDAO._initial_next_value(
                    db, table_model, column, first_number
                ),
            )
            db.add(sequence)
            db.flush()
        allocated = int(sequence.next_value)
        sequence.next_value = allocated + 1
        db.flush()
        return allocated
