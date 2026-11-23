"""The one boundary every call into plugin code goes through (docs/plugin-system.md section 11)."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import socket
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional

from app.models.plugin_host import PluginInstallation
from app.plugin_host import common, health, runtime
from app.plugin_host.common import new_session, utcnow, write_log
from app.plugin_host.sdk import ActionError, PluginContext

logger = logging.getLogger(__name__)

# A sync handler runs on a thread of its own, so no call waits in a queue behind another
# plugin's. A plugin may have at most SLOTS_PER_PLUGIN sync handlers whose code is still
# running, counting handlers that timed out but have not returned yet, and the process at most
# MAX_RUNNING in all; a call beyond either limit is refused with ``busy`` instead of waiting.
# A hung plugin therefore ties up at most SLOTS_PER_PLUGIN threads and database sessions.
# MAX_RUNNING stays below SQLAlchemy's default pool of 15 connections per process.
SLOTS_PER_PLUGIN = 4
MAX_RUNNING = 12

# While handlers that timed out are still running, each process keeps one system_settings row
# up to date for the alert detector ``plugin_handlers_abandoned``: it is written when a handler
# is abandoned and when one returns (the row is deleted when the last one does), and a heartbeat
# thread rewrites it every REPORT_SECONDS so that a row left by a stopped process ages out.
REPORT_PREFIX = "plugin_abandoned:"
REPORT_SECONDS = 30
REPORT_HANDLERS = 50  # handlers listed in a report; its counts cover them all

_slots_lock = threading.Lock()
_running: dict[str, int] = {}  # by plugin id: sync handlers whose code has not returned yet
_abandoned: dict[str, int] = {}  # by plugin id: of those, handlers whose caller stopped waiting
_calls: set["_Call"] = set()  # the running sync handlers
_report_lock = threading.Lock()  # one report write at a time, so the last one written is the newest
_heartbeat_wake = threading.Event()  # set when the last abandoned handler returns
_heartbeat: Optional[threading.Thread] = None


@dataclass
class Outcome:
    ok: bool
    value: Any = None
    error: Optional[str] = None
    refused: Optional[str] = None
    timed_out: bool = False
    unavailable: bool = False
    busy: bool = False  # not run: the plugin's slots, or the process's, are all in use


@dataclass(eq=False)
class _Call:
    """One sync handler, shared by the caller and the thread that runs it."""

    plugin_id: str
    label: str
    started_at: float  # monotonic
    thread: Optional[threading.Thread] = None
    finished: bool = False  # set under _slots_lock when the thread is done with the plugin
    abandoned: bool = False  # set under _slots_lock when the caller stops waiting
    committing: bool = False  # set under _slots_lock when the handler succeeded and its writes are being kept
    abandoned_at: Optional[datetime] = None


def _handler_table(loaded, kind: str) -> dict[str, Callable]:
    reg = loaded.handlers
    return {
        "event": reg.on,
        "before": reg.before_hooks,
        "schedule": reg.schedules,
        "action": reg.actions,
        "mcp_tool": reg.mcp_tools,
        "lifecycle": reg.lifecycle_hooks,
    }[kind]


def _trace(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__, limit=8))[-4000:]


# ------------------------------------------------------------------ slots


def _take_slot(call: _Call) -> Optional[str]:
    """Take a slot: ``None`` when taken, else ``"in_flight"``, ``"abandoned"`` or ``"host"``.

    ``"abandoned"`` means every slot of the plugin is held by a handler that already timed out,
    so the plugin is hung rather than busy; ``"host"`` means the process limit is reached.
    """
    with _slots_lock:
        running = _running.get(call.plugin_id, 0)
        if running >= SLOTS_PER_PLUGIN:
            return "abandoned" if _abandoned.get(call.plugin_id, 0) >= running else "in_flight"
        if sum(_running.values()) >= MAX_RUNNING:
            return "host"
        _running[call.plugin_id] = running + 1
        _calls.add(call)
        return None


def _release_slot(call: _Call) -> bool:
    """Called once per call, when its thread is done (or could not start). True when it was abandoned."""
    with _slots_lock:
        call.finished = True
        _calls.discard(call)
        left = _running.get(call.plugin_id, 1) - 1
        if left > 0:
            _running[call.plugin_id] = left
        else:
            _running.pop(call.plugin_id, None)
        if not call.abandoned:
            return False
        gone = _abandoned.get(call.plugin_id, 1) - 1
        if gone > 0:
            _abandoned[call.plugin_id] = gone
        else:
            _abandoned.pop(call.plugin_id, None)
        if not _abandoned:
            _heartbeat_wake.set()
        return True


def _abandon(call: _Call) -> Optional[int]:
    """Stop waiting for a call: the plugin's count of abandoned handlers, or None when its thread finished
    meanwhile or is already committing a successful result (the caller then waits for that outcome)."""
    global _heartbeat
    with _slots_lock:
        if call.finished or call.committing:
            return None
        call.abandoned = True
        call.abandoned_at = utcnow()
        held = _abandoned.get(call.plugin_id, 0) + 1
        _abandoned[call.plugin_id] = held
        if _heartbeat is None:
            _heartbeat_wake.clear()
            heartbeat = threading.Thread(target=_heartbeat_loop, name="rackflow-plugin-report", daemon=True)
            try:
                heartbeat.start()
                _heartbeat = heartbeat
            except RuntimeError:
                logger.warning("Could not start the abandoned plugin handler heartbeat", exc_info=True)
        return held


def _refuse(inst: PluginInstallation, label: str, reason: str, *, count_failures: bool,
            service_id: Optional[int]) -> Outcome:
    """The outcome of a call that was not run because no slot was free; logged on the plugin log."""
    if reason == "host":
        logger.warning("Plugin %s %s refused: %d sync plugin handlers are running in this process",
                       inst.id, label, MAX_RUNNING)
        return Outcome(ok=False, busy=True, error="Rackflow is running too many plugin calls; try again shortly")
    if reason == "abandoned":
        # A hung plugin: each refusal counts, so that its circuit opens and the operator hears of it.
        message = f"{label} refused: all {SLOTS_PER_PLUGIN} of the plugin's handlers timed out and are still running"
        logger.warning("Plugin %s: %s", inst.id, message)
        if count_failures:
            health.record_failure(inst.id, message, service_id=service_id)
        else:
            write_log(inst.id, "error", message, service_id=service_id)
        return Outcome(ok=False, busy=True,
                       error="The plugin is not responding: its earlier calls timed out and are still running")
    message = f"{label} refused: {SLOTS_PER_PLUGIN} of the plugin's handlers are still running"
    logger.info("Plugin %s: %s", inst.id, message)
    write_log(inst.id, "warning", message, service_id=service_id)
    return Outcome(ok=False, busy=True, error="The plugin is busy with other calls; try again shortly")


# ------------------------------------------------------------------ the report for the alert


def _process_key() -> str:
    return f"{REPORT_PREFIX}{socket.gethostname()[:80]}:{os.getpid()}"


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.replace(tzinfo=None).isoformat() + "Z" if value is not None else None


def _write_report() -> None:
    """Write this process's abandoned handlers, or delete its row when there are none. Never raises."""
    with _report_lock:
        _write_report_locked()


def _write_report_locked() -> None:
    from app.models.commerce_account import SystemSetting

    now = time.monotonic()
    with _slots_lock:
        counts = dict(_abandoned)
        calls = sorted((c for c in _calls if c.abandoned), key=lambda c: c.started_at)
    key = _process_key()
    value = {
        "seen_at": _iso(utcnow()),
        "process": key[len(REPORT_PREFIX):],
        "slots_per_plugin": SLOTS_PER_PLUGIN,
        "plugins": counts,
        "handlers": [
            {
                "plugin_id": c.plugin_id,
                "handler": c.label,
                "abandoned_at": _iso(c.abandoned_at),
                "running_seconds": int(now - c.started_at),
            }
            for c in calls[:REPORT_HANDLERS]
        ],
    }
    db = None
    try:
        db = common.new_session()
        row = db.get(SystemSetting, key)
        if not counts:
            if row is not None:
                db.delete(row)
        elif row is None:
            db.add(SystemSetting(key=key, value=value))
        else:
            row.value = value
        db.commit()
    except Exception:  # noqa: BLE001 - the report is best effort; a stale one ages out of the alert
        if db is not None:
            db.rollback()
        logger.warning("Writing the abandoned plugin handler report failed", exc_info=True)
    finally:
        if db is not None:
            db.close()


def _heartbeat_loop() -> None:
    """Rewrite the report every REPORT_SECONDS while abandoned handlers run; stop when none are left."""
    global _heartbeat
    while True:
        _heartbeat_wake.wait(REPORT_SECONDS)
        with _slots_lock:
            if not _abandoned:
                _heartbeat = None
                return
            _heartbeat_wake.clear()
        _write_report()


def _threads() -> list[threading.Thread]:
    """The running handler threads, then the heartbeat (tests wait for them at teardown)."""
    with _slots_lock:
        threads = [c.thread for c in _calls if c.thread is not None]
        if _heartbeat is not None:
            threads.append(_heartbeat)
    return threads


# ------------------------------------------------------------------ invoke


def _run_handler(handler: Callable, ctx: PluginContext, args: tuple, finish: Callable[[bool], None]) -> tuple[bool, Any]:
    """Run a sync handler and commit or roll back its context; never raises."""
    try:
        try:
            result = handler(ctx, *args)
        except BaseException:
            finish(False)
            raise
        finish(True)
        return True, result
    except BaseException as exc:  # noqa: BLE001 - a plugin's SystemExit must not end the thread unreported
        return False, exc


def _settle(done: asyncio.Future, outcome: tuple[bool, Any]) -> None:
    if not done.done():
        done.set_result(outcome)


async def invoke(
    inst: PluginInstallation,
    kind: str,
    name: str,
    args: tuple,
    *,
    timeout: float,
    actor: Optional[dict] = None,
    service_id: Optional[int] = None,
    require_enabled: bool = True,
    respect_circuit: bool = True,
    count_failures: bool = True,
    optional: bool = False,
) -> Outcome:
    """Run one handler with a time limit; commit its state writes on success, roll back on failure.

    ``args`` follow the context argument. ``optional`` handlers (lifecycle steps) that a plugin
    did not register succeed without running anything. A sync handler that finds no free slot
    is not run: the outcome has ``busy`` set.
    """
    if respect_circuit and health.circuit_open(inst):
        return Outcome(ok=False, unavailable=True, error="The plugin is temporarily unavailable")
    loaded = runtime.load_installed(inst, require_enabled=require_enabled)
    if loaded is None:
        return Outcome(ok=False, unavailable=True, error="The plugin is not available")
    handler = _handler_table(loaded, kind).get(name)
    if handler is None:
        if optional:
            return Outcome(ok=True)
        return Outcome(ok=False, error=f"The plugin has no {kind} handler {name!r}")
    label = f"{kind} {name}"
    is_async = inspect.iscoroutinefunction(handler)
    call = _Call(inst.id, label, time.monotonic())
    if not is_async:
        refused = _take_slot(call)
        if refused is not None:
            return _refuse(inst, label, refused, count_failures=count_failures, service_id=service_id)

    db = None
    try:
        db = new_session()
        ctx = PluginContext(
            db,
            plugin_id=inst.id,
            plugin_version=inst.version,
            manifest=loaded.manifest,
            granted=set(inst.granted_permissions or []),
            kind=kind,
            actor=actor,
        )
    except BaseException:
        if db is not None:
            db.close()
        if not is_async:
            _release_slot(call)
        raise

    def finish(success: bool) -> None:
        keep = False
        if success:
            # Decided under the slot lock so that a caller timing out at this moment either sees
            # the commit coming (and waits for it) or has already abandoned the call (and it rolls back).
            with _slots_lock:
                if not call.abandoned:
                    call.committing = True
                    keep = True
        try:
            if keep and not ctx.abandoned:
                ctx._commit()
            else:
                ctx._rollback()
        finally:
            ctx._close()

    if is_async:
        try:
            value = await asyncio.wait_for(handler(ctx, *args), timeout)
        except ActionError as exc:
            finish(False)
            return Outcome(ok=False, refused=exc.message)
        except asyncio.TimeoutError:
            finish(False)
            if count_failures:
                health.record_failure(inst.id, f"{label} timed out after {timeout:g}s", service_id=service_id)
            return Outcome(ok=False, timed_out=True, error=f"The plugin did not answer within {timeout:g} seconds")
        except Exception as exc:  # noqa: BLE001 - every plugin failure stops here
            finish(False)
            if count_failures:
                health.record_failure(inst.id, f"{label} failed: {type(exc).__name__}: {exc}",
                                      service_id=service_id, detail=_trace(exc))
            return Outcome(ok=False, error=f"{type(exc).__name__}: {exc}"[:500])
        try:
            finish(True)
        except Exception as exc:  # noqa: BLE001 - a commit failure is the plugin's failure
            if count_failures:
                health.record_failure(inst.id, f"{label} could not save its state: {exc}", service_id=service_id)
            return Outcome(ok=False, error="The plugin could not save its state")
        if count_failures:
            health.record_success(inst.id)
        return Outcome(ok=True, value=value)

    loop = asyncio.get_running_loop()
    done: asyncio.Future = loop.create_future()

    def work() -> None:
        outcome: tuple[bool, Any] = (False, RuntimeError("the handler did not finish"))
        try:
            outcome = _run_handler(handler, ctx, args, finish)
        finally:
            try:
                if _release_slot(call):
                    message = (f"{label} returned after {time.monotonic() - call.started_at:.1f}s, after Rackflow "
                               "had stopped waiting; its state changes were discarded")
                    logger.info("Plugin %s: %s", inst.id, message)
                    write_log(inst.id, "warning", message, service_id=service_id)
                    _write_report()
            except Exception:  # noqa: BLE001 - the caller must still get the outcome
                logger.warning("Recording the late return of plugin %s %s failed", inst.id, label, exc_info=True)
            finally:
                try:
                    loop.call_soon_threadsafe(_settle, done, outcome)
                except RuntimeError:  # the caller's event loop has closed
                    pass

    call.thread = threading.Thread(target=work, name=f"rackflow-plugin-{inst.id}", daemon=True)
    try:
        call.thread.start()
    except RuntimeError:  # no thread to be had
        call.thread = None
        _release_slot(call)
        finish(False)
        logger.warning("Plugin %s %s refused: no thread could be started", inst.id, label, exc_info=True)
        return Outcome(ok=False, busy=True, error="Rackflow could not start the plugin call; try again shortly")

    try:
        succeeded, value = await asyncio.wait_for(asyncio.shield(done), timeout)
    except asyncio.TimeoutError:
        held = _abandon(call)
        if held is None:  # finished, or committing its result, just as the time ran out
            succeeded, value = await done
        else:
            ctx.abandoned = True
            message = (f"{label} timed out after {timeout:g}s; its code is still running "
                       f"({held} of the plugin's {SLOTS_PER_PLUGIN} slots held by handlers that timed out)")
            logger.warning("Plugin %s: %s", inst.id, message)
            if count_failures:
                health.record_failure(inst.id, message, service_id=service_id)
            else:
                write_log(inst.id, "error", message, service_id=service_id)
            # The report is a database write; keep it off the event loop thread.
            await asyncio.to_thread(_write_report)
            return Outcome(ok=False, timed_out=True, error=f"The plugin did not answer within {timeout:g} seconds")
    except asyncio.CancelledError:
        if _abandon(call) is not None:
            ctx.abandoned = True
            _write_report()
        raise
    if succeeded:
        if count_failures:
            health.record_success(inst.id)
        return Outcome(ok=True, value=value)
    exc = value
    if isinstance(exc, ActionError):
        return Outcome(ok=False, refused=exc.message)
    if count_failures:
        health.record_failure(inst.id, f"{label} failed: {type(exc).__name__}: {exc}",
                              service_id=service_id, detail=_trace(exc))
    return Outcome(ok=False, error=f"{type(exc).__name__}: {exc}"[:500])
