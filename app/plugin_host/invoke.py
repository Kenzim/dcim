"""The one boundary every call into plugin code goes through (docs/plugin-system.md section 11)."""

from __future__ import annotations

import asyncio
import inspect
import logging
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Optional

from app.models.plugin_host import PluginInstallation
from app.plugin_host import health, runtime
from app.plugin_host.common import new_session
from app.plugin_host.sdk import ActionError, PluginContext

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="rackflow-plugin")


@dataclass
class Outcome:
    ok: bool
    value: Any = None
    error: Optional[str] = None
    refused: Optional[str] = None
    timed_out: bool = False
    unavailable: bool = False


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
    did not register succeed without running anything.
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
    label = f"{kind} {name}"

    def finish(success: bool) -> None:
        try:
            if success and not ctx.abandoned:
                ctx._commit()
            else:
                ctx._rollback()
        finally:
            ctx._close()

    if inspect.iscoroutinefunction(handler):
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

    def run_sync():
        try:
            result = handler(ctx, *args)
        except BaseException:
            finish(False)
            raise
        finish(True)
        return result

    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(_executor, run_sync)
    try:
        value = await asyncio.wait_for(asyncio.shield(future), timeout)
    except ActionError as exc:
        return Outcome(ok=False, refused=exc.message)
    except asyncio.TimeoutError:
        ctx.abandoned = True
        if count_failures:
            health.record_failure(inst.id, f"{label} timed out after {timeout:g}s", service_id=service_id)
        return Outcome(ok=False, timed_out=True, error=f"The plugin did not answer within {timeout:g} seconds")
    except Exception as exc:  # noqa: BLE001
        if count_failures:
            health.record_failure(inst.id, f"{label} failed: {type(exc).__name__}: {exc}",
                                  service_id=service_id, detail=_trace(exc))
        return Outcome(ok=False, error=f"{type(exc).__name__}: {exc}"[:500])
    if count_failures:
        health.record_success(inst.id)
    return Outcome(ok=True, value=value)
