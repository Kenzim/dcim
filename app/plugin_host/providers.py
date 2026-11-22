"""Calls to plugin providers of the API 1.1 kinds (docs/billing-phase2-design.md 8.5 to 8.7).

``call_provider`` is the only way billing reaches a provider. It checks that the plugin is
enabled and its circuit closed, applies the provider's rate limit, runs the method on a worker
thread with a wall-clock limit, validates the answer and reports success or failure to the
circuit breaker. It raises only ``ProviderUnavailable``; billing never sees a plugin's own
exception. The call blocks for up to the timeout, so call it from a worker thread or a sync
endpoint, never while holding a billing row lock.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import logging
import traceback
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Any, Optional

from app.models.plugin_host import PluginInstallation, PluginStatus
from app.plugin_host import health, kinds, runtime
from app.plugin_host.common import new_session, rate_hit
from app.plugin_host.sdk import PluginContext

logger = logging.getLogger(__name__)

# Separate from the pool of actions and events, so a provider that hangs cannot starve them.
_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="rackflow-provider")


class ProviderUnavailable(Exception):
    """The only exception ``call_provider`` raises; ``reason`` says why.

    Reasons: ``circuit_open``, ``rate_limited``, ``timeout``, ``error``, ``invalid_answer``,
    ``disabled``, ``not_installed``, ``not_loaded``, ``not_permitted``, ``unknown_provider``,
    ``unknown_method`` and ``bad_arguments``.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ProviderRef:
    plugin_id: str
    provider_id: str
    kind: str
    label: str = ""

    @property
    def ref(self) -> str:
        return f"{self.plugin_id}:{self.provider_id}"


def providers_of(kind: str) -> list[ProviderRef]:
    """Enabled providers of ``kind`` whose circuit is closed, ordered by plugin id and manifest order."""
    spec = kinds.KINDS.get(kind)
    if spec is None:
        return []
    db = new_session()
    try:
        out: list[ProviderRef] = []
        for inst in runtime.enabled_installations(db):
            if health.circuit_open(inst) or spec.permission not in (inst.granted_permissions or []):
                continue
            try:
                manifest = runtime.manifest_of(inst)
            except Exception:  # noqa: BLE001 - a stored manifest that no longer validates offers nothing
                continue
            out.extend(ProviderRef(inst.id, p.id, p.kind, p.label) for p in manifest.provides if p.kind == kind)
        return out
    finally:
        db.close()


def _trace(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__, limit=8))[-4000:]


async def _await(awaitable: Any, timeout: float) -> Any:
    return await asyncio.wait_for(awaitable, timeout)


def _finish(ctx: PluginContext, ok: bool) -> None:
    try:
        if ok and not ctx.abandoned:
            ctx._commit()
        else:
            ctx._rollback()
    finally:
        ctx._close()


def _run(ctx: PluginContext, fn: Any, kind: str, method: str, args: tuple, timeout: float) -> Any:
    """Worker thread body: call the method, wait for a coroutine, validate, then commit the plugin's state."""
    token = kinds._bind_context(ctx)
    try:
        value = fn(*args)
        if inspect.isawaitable(value):
            value = asyncio.run(_await(value, timeout))
        answer = kinds.validate_answer(kind, method, args, value)
    except Exception:
        _finish(ctx, False)
        raise
    except BaseException as exc:  # a plugin's SystemExit must not unwind billing's thread
        _finish(ctx, False)
        raise RuntimeError(f"the provider raised {type(exc).__name__}") from exc
    else:
        _finish(ctx, True)
        return answer
    finally:
        kinds._unbind_context(token)


def _fail(plugin_id: str, provider: str, reason: str, message: str, detail: Optional[str] = None) -> ProviderUnavailable:
    health.record_failure(plugin_id, message, detail=detail, provider=provider)
    return ProviderUnavailable(reason)


def _resolve(kind: str, plugin_id: str, provider_id: str, method: str):
    """The installation, manifest entry and provider object to call, or ``ProviderUnavailable``."""
    db = new_session()
    try:
        inst = db.get(PluginInstallation, plugin_id)
        if inst is None:
            raise ProviderUnavailable("not_installed")
        db.expunge(inst)
    finally:
        db.close()
    if inst.status != PluginStatus.ENABLED:
        raise ProviderUnavailable("disabled")
    if health.circuit_open(inst):
        raise ProviderUnavailable("circuit_open")
    try:
        manifest = runtime.manifest_of(inst)
    except Exception as exc:  # noqa: BLE001
        raise ProviderUnavailable("not_loaded") from exc
    provide = manifest.provider(kind, provider_id)
    if provide is None:
        raise ProviderUnavailable("unknown_provider")
    if kinds.KINDS[kind].permission not in (inst.granted_permissions or []):
        raise ProviderUnavailable("not_permitted")
    loaded = runtime.load_installed(inst)
    if loaded is None:
        raise ProviderUnavailable("not_loaded")
    obj = loaded.handlers.providers.get((kind, provider_id))
    fn = getattr(obj, method, None) if obj is not None else None
    if not callable(fn):
        raise ProviderUnavailable("unknown_provider")
    return inst, manifest, provide, fn


def call_provider(kind: str, provider_ref: str, method: str, *args: Any, timeout_seconds: Optional[float] = None) -> Any:
    """Call ``method`` of the provider ``provider_ref`` (``<plugin_id>:<provider_id>``) and return its validated answer.

    ``timeout_seconds`` may only lower the provider's own ``timeout_seconds``. Raises only
    ``ProviderUnavailable``. Every timeout, exception and invalid answer counts as a failure for the
    plugin's circuit breaker; an answer that passes validation counts as a success.
    """
    try:
        return _call(kind, provider_ref, method, args, timeout_seconds)
    except ProviderUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - the contract: nothing else leaves this function
        logger.warning("Provider call %s %s failed unexpectedly", provider_ref, method, exc_info=True)
        raise ProviderUnavailable("error") from exc


def _call(kind: str, provider_ref: str, method: str, args: tuple, timeout_seconds: Optional[float]) -> Any:
    spec = kinds.KINDS.get(kind)
    if spec is None or method not in spec.methods:
        raise ProviderUnavailable("unknown_method")
    if len(args) != spec.methods[method]:
        raise ProviderUnavailable("bad_arguments")
    plugin_id, separator, provider_id = (provider_ref or "").partition(":")
    if not separator or not plugin_id or not provider_id:
        raise ProviderUnavailable("unknown_provider")
    inst, manifest, provide, fn = _resolve(kind, plugin_id, provider_id, method)

    if method in spec.metered and provide.rate_limit is not None:
        limit = provide.rate_limit
        if not rate_hit(f"provider:{plugin_id}:{kind}:{provider_id}", limit.calls, limit.per_seconds):
            raise ProviderUnavailable("rate_limited")

    seconds = float(provide.timeout_seconds)
    if timeout_seconds is not None:
        seconds = max(0.1, min(seconds, float(timeout_seconds)))
    try:
        # The plugin gets copies, so nothing it does to its arguments reaches billing's objects.
        call_args = copy.deepcopy(args)
    except Exception as exc:  # noqa: BLE001
        raise ProviderUnavailable("bad_arguments") from exc

    provider = f"{kind}:{provider_id}"
    label = f"provider {provider} {method}"
    ctx = PluginContext(
        new_session(),
        plugin_id=plugin_id,
        plugin_version=inst.version,
        manifest=manifest,
        granted=set(inst.granted_permissions or []),
        kind="provider",
    )
    future = _executor.submit(_run, ctx, fn, kind, method, call_args, seconds)
    try:
        answer = future.result(seconds)
    except (FutureTimeout, asyncio.TimeoutError):
        ctx.abandoned = True
        if future.cancel():  # still queued: the worker will never run, so close the session here
            ctx._close()
        raise _fail(plugin_id, provider, "timeout", f"{label} timed out after {seconds:g}s") from None
    except kinds.InvalidAnswer as exc:
        raise _fail(plugin_id, provider, "invalid_answer", f"{label} gave an invalid answer: {exc}"[:500]) from None
    except Exception as exc:  # noqa: BLE001 - every plugin failure stops here
        raise _fail(
            plugin_id, provider, "error", f"{label} failed: {type(exc).__name__}: {exc}"[:500], _trace(exc)
        ) from None
    if method in spec.metered:
        # Only the methods that do real work prove the plugin healthy; a cheap supports() answer
        # before every failing check_online must not keep resetting the circuit.
        health.record_success(plugin_id)
    return answer
