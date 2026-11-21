"""Billing shadow comparisons: record differences, store scan results, read engine modes.

In engine mode ``shadow`` billing keeps its legacy answer and also computes the
v2 answer; every difference goes through ``BillingShadowService.record``
(docs/billing-phase1-design.md 6.4). The daily ``scan`` belongs to the credit
balance worker (P1-C) and is a stub here.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.billing_shadow import BillingShadowMismatch
from app.models.commerce_account import SystemSetting

logger = logging.getLogger(__name__)

ENGINES = ("invoice", "credit")
SCAN_RESULT_KEY = "billing_shadow_scan"
SWITCH_RECORD_PREFIX = "billing_engine_switch:"
ENGINE_SETTING = {"invoice": "billing_invoice_engine", "credit": "billing_credit_engine"}
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
    """The effective mode of ``engine`` (``invoice`` or ``credit``): legacy, shadow or v2.

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
        row = db.get(SystemSetting, SCAN_RESULT_KEY)
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
    def scan(db: Session, now: datetime, *, dry_run: bool = False) -> dict:
        """Daily comparison of both engines (c2_* and i2_scan checks); filled in by P1-C."""
        raise NotImplementedError("BillingShadowService.scan is implemented by P1-C")
