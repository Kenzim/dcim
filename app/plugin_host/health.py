"""Per-plugin failure counting, circuit breaker and auto-disable (docs/plugin-system.md section 11)."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from app.models.plugin_host import PluginInstallation, PluginStatus
from app.plugin_host.common import audit, aware, new_session, utcnow, write_log

logger = logging.getLogger(__name__)

FAILURES_TO_OPEN = 5
FAILURES_TO_DISABLE = 25
OPEN_BASE_SECONDS = 60
OPEN_MAX_SECONDS = 1800


def circuit_open(inst: PluginInstallation, now=None) -> bool:
    until = aware(inst.circuit_open_until)
    return until is not None and until > (now or utcnow())


def state_of(inst: PluginInstallation, now=None) -> str:
    if inst.status == PluginStatus.FAILED:
        return "failed"
    if inst.status != PluginStatus.ENABLED:
        return "disabled"
    if circuit_open(inst, now):
        return "circuit_open"
    if inst.consecutive_failures:
        return "degraded"
    return "ok"


def panel_health(inst: PluginInstallation) -> tuple[str, Optional[str]]:
    state = state_of(inst)
    if state == "circuit_open":
        return "unavailable", "This plugin is not responding; the information shown may be out of date."
    if state == "degraded":
        return "degraded", "This plugin had recent errors; the information shown may be out of date."
    return "ok", None


def _sqlite_caller(db: Optional[Session]) -> Optional[Session]:
    """The caller's session when it is on SQLite, else ``None`` for a separate session.

    A separate session keeps the health record when the caller rolls back. SQLite has one
    write lock for the whole database, so a second connection cannot write while the caller's
    transaction holds it; there the record joins the caller's transaction in a savepoint.
    """
    if db is None:
        return None
    try:
        return db if db.get_bind().dialect.name == "sqlite" else None
    except Exception:  # noqa: BLE001 - a session without a usable bind gets the separate session
        return None


def record_success(plugin_id: str, *, db: Optional[Session] = None) -> None:
    """Close the circuit and reset the count. ``db``: the caller's session, used only on SQLite."""
    now = utcnow()
    shared = _sqlite_caller(db)
    statement = (
        update(PluginInstallation)
        .where(
            PluginInstallation.id == plugin_id,
            or_(
                PluginInstallation.consecutive_failures > 0,
                PluginInstallation.last_success_at.is_(None),
                PluginInstallation.last_success_at < now - timedelta(seconds=60),
                PluginInstallation.circuit_open_until.is_not(None),
            ),
        )
        .values(consecutive_failures=0, circuit_open_until=None, circuit_opens=0, last_success_at=now)
        .execution_options(synchronize_session=False)
    )
    if shared is not None:
        try:
            with shared.begin_nested():
                shared.execute(statement)
        except Exception:  # noqa: BLE001
            logger.warning("Recording plugin success failed for %s", plugin_id, exc_info=True)
        return
    session = new_session()
    try:
        session.execute(statement)
        session.commit()
    except Exception:  # noqa: BLE001
        session.rollback()
        logger.warning("Recording plugin success failed for %s", plugin_id, exc_info=True)
    finally:
        session.close()


def _count_failure(db: Session, plugin_id: str, message: str, now, finish) -> Optional[tuple[bool, Optional[int], int]]:
    """Count one failure on ``db``; ``finish`` ends each step (commit, or flush in a savepoint).

    Returns whether the plugin was disabled, the seconds the circuit opened for, and the count.
    """
    db.execute(
        update(PluginInstallation)
        .where(PluginInstallation.id == plugin_id)
        .values(
            consecutive_failures=PluginInstallation.consecutive_failures + 1,
            total_failures=PluginInstallation.total_failures + 1,
            last_error=message[:2000],
            last_error_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    finish()
    inst = db.get(PluginInstallation, plugin_id)
    if inst is None:
        return None
    db.refresh(inst)
    failures = inst.consecutive_failures
    disabled = False
    opened_for: Optional[int] = None
    if failures >= FAILURES_TO_DISABLE and inst.status == PluginStatus.ENABLED:
        inst.status = PluginStatus.FAILED
        inst.disabled_reason = f"Disabled automatically after {failures} failures in a row: {message[:300]}"
        disabled = True
    elif failures >= FAILURES_TO_OPEN and not circuit_open(inst, now):
        seconds = min(OPEN_MAX_SECONDS, OPEN_BASE_SECONDS * 2 ** min(inst.circuit_opens or 0, 10))
        inst.circuit_open_until = now + timedelta(seconds=seconds)
        inst.circuit_opens = (inst.circuit_opens or 0) + 1
        opened_for = seconds
    finish()
    return disabled, opened_for, failures


def record_failure(
    plugin_id: str,
    message: str,
    *,
    service_id: Optional[int] = None,
    detail: Optional[str] = None,
    provider: Optional[str] = None,
    db: Optional[Session] = None,
) -> None:
    """Count one failure, log it, open the circuit or disable the plugin when the thresholds are reached.

    ``provider`` (``<kind>:<provider_id>``) names the provider whose call failed; when that failure
    opens the circuit, the audit entry ``plugin.provider_failed`` records it. ``db`` is the caller's
    session: on SQLite the log line, the count and the audit entries are written in a savepoint of
    its transaction (and so are lost if the caller rolls back); on other databases, and without
    ``db``, they are committed on separate sessions.
    """
    from app.plugin_host import runtime

    now = utcnow()
    shared = _sqlite_caller(db)
    outcome = None
    if shared is not None:
        try:
            with shared.begin_nested():
                write_log(plugin_id, "error", message, service_id=service_id,
                          context={"traceback": detail} if detail else None, db=shared)
                outcome = _count_failure(shared, plugin_id, message, now, shared.flush)
                _failure_audits(plugin_id, message, provider, outcome, shared)
        except Exception:  # noqa: BLE001
            logger.warning("Recording plugin failure failed for %s", plugin_id, exc_info=True)
            return
        if outcome is not None and outcome[0]:
            runtime.invalidate()
        return

    write_log(plugin_id, "error", message, service_id=service_id, context={"traceback": detail} if detail else None)
    session = new_session()
    try:
        outcome = _count_failure(session, plugin_id, message, now, session.commit)
    except Exception:  # noqa: BLE001
        session.rollback()
        logger.warning("Recording plugin failure failed for %s", plugin_id, exc_info=True)
    finally:
        session.close()
    if outcome is not None and outcome[0]:
        runtime.invalidate()
    _failure_audits(plugin_id, message, provider, outcome, None)


def _failure_audits(plugin_id: str, message: str, provider: Optional[str], outcome, db: Optional[Session]) -> None:
    if outcome is None:
        return
    disabled, opened_for, failures = outcome
    if disabled:
        audit("plugin.auto_disabled", plugin_id=plugin_id, resource_type="plugin", resource_id=plugin_id,
              metadata={"error": message[:500]}, db=db)
    if provider and opened_for is not None:
        audit("plugin.provider_failed", plugin_id=plugin_id, resource_type="plugin", resource_id=plugin_id,
              metadata={"provider": provider, "failures": failures, "open_seconds": opened_for, "error": message[:500]},
              db=db)


def reset(db, inst: PluginInstallation) -> None:
    inst.consecutive_failures = 0
    inst.circuit_open_until = None
    inst.circuit_opens = 0
