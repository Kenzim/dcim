"""Per-plugin failure counting, circuit breaker and auto-disable (docs/plugin-system.md section 11)."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from sqlalchemy import or_, update

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


def record_success(plugin_id: str) -> None:
    now = utcnow()
    db = new_session()
    try:
        db.execute(
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
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.warning("Recording plugin success failed for %s", plugin_id, exc_info=True)
    finally:
        db.close()


def record_failure(
    plugin_id: str,
    message: str,
    *,
    service_id: Optional[int] = None,
    detail: Optional[str] = None,
    provider: Optional[str] = None,
) -> None:
    """Count one failure, log it, open the circuit or disable the plugin when the thresholds are reached.

    ``provider`` (``<kind>:<provider_id>``) names the provider whose call failed; when that failure
    opens the circuit, the audit entry ``plugin.provider_failed`` records it.
    """
    from app.plugin_host import runtime

    now = utcnow()
    write_log(plugin_id, "error", message, service_id=service_id, context={"traceback": detail} if detail else None)
    db = new_session()
    disabled = False
    opened_for: Optional[int] = None
    try:
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
        db.commit()
        inst = db.get(PluginInstallation, plugin_id)
        if inst is None:
            return
        db.refresh(inst)
        failures = inst.consecutive_failures
        if failures >= FAILURES_TO_DISABLE and inst.status == PluginStatus.ENABLED:
            inst.status = PluginStatus.FAILED
            inst.disabled_reason = f"Disabled automatically after {failures} failures in a row: {message[:300]}"
            disabled = True
        elif failures >= FAILURES_TO_OPEN and not circuit_open(inst, now):
            seconds = min(OPEN_MAX_SECONDS, OPEN_BASE_SECONDS * 2 ** min(inst.circuit_opens or 0, 10))
            inst.circuit_open_until = now + timedelta(seconds=seconds)
            inst.circuit_opens = (inst.circuit_opens or 0) + 1
            opened_for = seconds
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.warning("Recording plugin failure failed for %s", plugin_id, exc_info=True)
    finally:
        db.close()
    if disabled:
        runtime.invalidate()
        audit("plugin.auto_disabled", plugin_id=plugin_id, resource_type="plugin", resource_id=plugin_id,
              metadata={"error": message[:500]})
    if provider and opened_for is not None:
        audit("plugin.provider_failed", plugin_id=plugin_id, resource_type="plugin", resource_id=plugin_id,
              metadata={"provider": provider, "failures": failures, "open_seconds": opened_for, "error": message[:500]})


def reset(db, inst: PluginInstallation) -> None:
    inst.consecutive_failures = 0
    inst.circuit_open_until = None
    inst.circuit_opens = 0
