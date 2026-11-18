"""DAO for PDU power history: raw samples and rollups of every series kind (see app/models/pdu_sample.py)."""
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence

from sqlalchemy import and_, delete, func
from sqlalchemy.orm import Session

from app.models.pdu_sample import SAMPLE_KINDS, SampleKind


def _kind(kind: str) -> SampleKind:
    return SAMPLE_KINDS[kind]


def _key_filters(model: Any, filters: Optional[Mapping[str, Any]]) -> list:
    """Equality for a scalar, IN for a list or set, on key columns of the model."""
    clauses = []
    for col, value in (filters or {}).items():
        column = getattr(model, col)
        if isinstance(value, (list, tuple, set, frozenset)):
            clauses.append(column.in_(list(value)))
        else:
            clauses.append(column == value)
    return clauses


class PduSampleDAO:
    """Data access for PDU history. Writers do not commit; the caller owns the transaction."""

    @staticmethod
    def insert_raw(db: Session, kind: str, rows: Sequence[Dict[str, Any]]) -> None:
        """Insert raw samples with one executemany."""
        if rows:
            db.bulk_insert_mappings(_kind(kind).raw, list(rows))

    @staticmethod
    def fetch_raw(
        db: Session,
        kind: str,
        since: datetime,
        until: datetime,
        filters: Optional[Mapping[str, Any]] = None,
    ) -> List[Any]:
        """Raw rows with since <= sampled_at < until, oldest first."""
        model = _kind(kind).raw
        return (
            db.query(model)
            .filter(model.sampled_at >= since, model.sampled_at < until, *_key_filters(model, filters))
            .order_by(model.sampled_at)
            .all()
        )

    @staticmethod
    def fetch_rollups(
        db: Session,
        kind: str,
        bucket_seconds: int,
        since: datetime,
        until: datetime,
        filters: Optional[Mapping[str, Any]] = None,
    ) -> List[Any]:
        """Rollup rows with since <= bucket_start < until, oldest first."""
        model = _kind(kind).rollup
        return (
            db.query(model)
            .filter(
                model.bucket_seconds == bucket_seconds,
                model.bucket_start >= since,
                model.bucket_start < until,
                *_key_filters(model, filters),
            )
            .order_by(model.bucket_start)
            .all()
        )

    @staticmethod
    def oldest_raw(db: Session, kind: str) -> Optional[datetime]:
        model = _kind(kind).raw
        return db.query(func.min(model.sampled_at)).scalar()

    @staticmethod
    def oldest_rollup(db: Session, kind: str, bucket_seconds: int) -> Optional[datetime]:
        model = _kind(kind).rollup
        return db.query(func.min(model.bucket_start)).filter(model.bucket_seconds == bucket_seconds).scalar()

    @staticmethod
    def newest_rollup(db: Session, kind: str, bucket_seconds: int) -> Optional[datetime]:
        model = _kind(kind).rollup
        return db.query(func.max(model.bucket_start)).filter(model.bucket_seconds == bucket_seconds).scalar()

    @staticmethod
    def replace_rollups(
        db: Session,
        kind: str,
        bucket_seconds: int,
        start: datetime,
        end: datetime,
        rows: Sequence[Dict[str, Any]],
    ) -> None:
        """
        Make the rollups of start <= bucket_start < end exactly `rows`: delete the window, insert
        the rows. Running it again with the same rows leaves the table unchanged.
        """
        model = _kind(kind).rollup
        db.execute(
            delete(model)
            .where(and_(model.bucket_seconds == bucket_seconds, model.bucket_start >= start, model.bucket_start < end))
            .execution_options(synchronize_session=False)
        )
        if rows:
            db.bulk_insert_mappings(model, list(rows))

    @staticmethod
    def delete_raw_before(db: Session, kind: str, cutoff: datetime) -> int:
        model = _kind(kind).raw
        return db.execute(
            delete(model).where(model.sampled_at < cutoff).execution_options(synchronize_session=False)
        ).rowcount or 0

    @staticmethod
    def delete_rollups_before(db: Session, kind: str, bucket_seconds: int, cutoff: datetime) -> int:
        model = _kind(kind).rollup
        result = db.execute(
            delete(model)
            .where(and_(model.bucket_seconds == bucket_seconds, model.bucket_start < cutoff))
            .execution_options(synchronize_session=False)
        )
        return result.rowcount or 0
