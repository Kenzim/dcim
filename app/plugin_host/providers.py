"""Calls to plugin providers of the API 1.1 kinds (docs/billing-phase2-design.md 8.5 to 8.7).

``call_provider`` is the only way billing reaches a provider. It checks that the plugin is
enabled and its circuit closed, applies the provider's rate limit, runs the method on a thread
of its own with a wall-clock limit, validates the answer and reports success or failure to the
circuit breaker. It raises only ``ProviderUnavailable``; billing never sees a plugin's own
exception. The call blocks for up to the timeout, so call it from a worker thread or a sync
endpoint, never while holding a billing row lock.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import logging
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Optional

from app.models.plugin_host import PluginInstallation, PluginStatus
from app.plugin_host import health, kinds, runtime
from app.plugin_host.common import new_session, rate_hit
from app.plugin_host.manifest import Manifest
from app.plugin_host.sdk import PluginContext

logger = logging.getLogger(__name__)

# Every call runs on a thread of its own, so no call waits in a queue behind another plugin's.
# A plugin may have at most SLOTS_PER_PLUGIN calls whose code is still running, counting calls
# that timed out but have not returned yet; a call beyond that is refused with ``busy``. A hung
# plugin therefore ties up at most this many threads and database sessions.
SLOTS_PER_PLUGIN = 2
# How long a caller waits for its new thread to start before it gives up with ``busy``.
START_SECONDS = 2.0

_slots_lock = threading.Lock()
_running: dict[str, int] = {}  # by plugin id: calls whose code has not returned yet
_abandoned: dict[str, int] = {}  # by plugin id: of those, calls whose caller gave up waiting


class ProviderUnavailable(Exception):
    """The only exception ``call_provider`` raises; ``reason`` says why.

    Reasons: ``circuit_open``, ``rate_limited``, ``busy``, ``timeout``, ``error``,
    ``invalid_answer``, ``disabled``, ``not_installed``, ``not_loaded``, ``not_permitted``,
    ``unknown_provider``, ``unknown_method`` and ``bad_arguments``.
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


# The helpers below format a plugin's exception. Its __str__, __repr__ and even its class name
# are plugin code, which may raise anything, KeyboardInterrupt included; they catch BaseException
# so that nothing a plugin raises while being described reaches billing.


def _safe_trace(exc: BaseException) -> Optional[str]:
    try:
        return _trace(exc)
    except BaseException:  # noqa: BLE001 - a plugin exception that cannot be formatted has no traceback text
        return None


def _type_name(exc: BaseException) -> str:
    try:
        name = type(exc).__name__
    except BaseException:  # noqa: BLE001
        return "Exception"
    return name if type(name) is str else "Exception"


def _text_of(exc: BaseException) -> str:
    """The message of a plugin's exception, else its repr; never raises."""
    for show in (str, repr):
        try:
            text = show(exc)
        except BaseException:  # noqa: BLE001
            continue
        if type(text) is str:
            return text
    return "<the message could not be shown>"


def _describe(exc: BaseException) -> str:
    return f"{_type_name(exc)}: {_text_of(exc)}"


async def _await(awaitable: Any, timeout: float) -> Any:
    return await asyncio.wait_for(awaitable, timeout)


@dataclass(eq=False)
class _Call:
    """One provider call, shared by the caller and the thread that runs it."""

    inst: PluginInstallation  # detached from its session
    manifest: Manifest
    kind: str
    provider_id: str
    method: str
    call_args: tuple  # the plugin's copies
    args: tuple  # billing's own arguments, which the answer is checked against
    seconds: float
    started: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    started_at: float = 0.0
    finished: bool = False  # set under _slots_lock when the thread is done with the plugin
    abandoned: bool = False  # set under _slots_lock when the caller stops waiting
    answer: Any = None
    failure: Optional[tuple[str, str, Optional[str]]] = None  # reason, message, traceback
    refusal: Optional[str] = None  # a reason that is not the plugin's failure

    @property
    def plugin_id(self) -> str:
        return self.inst.id


class _Refused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _method_of(call: _Call) -> Any:
    """Load the plugin (its import and factories are plugin code, so this runs on the call's thread) and find the method."""
    loaded = runtime.load_installed(call.inst)
    if loaded is None:  # load_installed has already marked the plugin failed or changed
        raise _Refused("not_loaded")
    obj = loaded.handlers.providers.get((call.kind, call.provider_id))
    fn = getattr(obj, call.method, None) if obj is not None else None
    if not callable(fn):
        raise _Refused("unknown_provider")
    return fn


def _take_slot(plugin_id: str) -> bool:
    with _slots_lock:
        if _running.get(plugin_id, 0) >= SLOTS_PER_PLUGIN:
            return False
        _running[plugin_id] = _running.get(plugin_id, 0) + 1
        return True


def _release_slot(call: _Call) -> None:
    """Called once per call, when its thread is done (or could not start)."""
    with _slots_lock:
        call.finished = True
        left = _running.get(call.plugin_id, 1) - 1
        if left > 0:
            _running[call.plugin_id] = left
        else:
            _running.pop(call.plugin_id, None)
        if call.abandoned:
            gone = _abandoned.get(call.plugin_id, 1) - 1
            if gone > 0:
                _abandoned[call.plugin_id] = gone
            else:
                _abandoned.pop(call.plugin_id, None)


def _abandon(call: _Call) -> bool:
    """Stop waiting for a call; False when its thread finished in the meantime."""
    with _slots_lock:
        if call.finished:
            return False
        call.abandoned = True
        _abandoned[call.plugin_id] = _abandoned.get(call.plugin_id, 0) + 1
        return True


def _execute(call: _Call) -> Any:
    """Run the plugin's method in a context of its own, validate the answer, then commit or roll back."""
    fn = _method_of(call)
    ctx = PluginContext(
        new_session(),
        plugin_id=call.plugin_id,
        plugin_version=call.inst.version,
        manifest=call.manifest,
        granted=set(call.inst.granted_permissions or []),
        kind="provider",
    )
    ok = False
    try:
        token = kinds._bind_context(ctx)
        try:
            value = fn(*call.call_args)
            if inspect.isawaitable(value):
                left = call.seconds - (time.monotonic() - call.started_at)
                value = asyncio.run(_await(value, max(0.01, left)))
            answer = kinds.validate_answer(call.kind, call.method, call.args, value)
            ok = True
            return answer
        finally:
            kinds._unbind_context(token)
    finally:
        # Always closed here, also for a call whose caller gave up long ago, so a hung plugin
        # holds its session only while its code runs.
        try:
            if ok and not call.abandoned:
                ctx._commit()
            else:
                ctx._rollback()
        finally:
            ctx._close()


def _work(call: _Call) -> None:
    """Thread body. Nothing escapes it; the outcome is left on ``call``."""
    try:
        call.started_at = time.monotonic()
        call.started.set()
        call.answer = _execute(call)
    except _Refused as exc:
        call.refusal = exc.reason
    except kinds.InvalidAnswer as exc:
        call.failure = ("invalid_answer", f"gave an invalid answer: {_text_of(exc)}", None)
    except asyncio.TimeoutError:
        call.failure = ("timeout", f"timed out after {call.seconds:g}s", None)
    except Exception as exc:  # noqa: BLE001 - every plugin failure stops here
        call.failure = ("error", f"failed: {_describe(exc)}", _safe_trace(exc))
    except BaseException as exc:  # noqa: BLE001 - a plugin's SystemExit must not end the thread unreported
        call.failure = ("error", f"failed: the provider raised {_type_name(exc)}", None)
    finally:
        _release_slot(call)
        call.done.set()


def _fail(plugin_id: str, provider: str, reason: str, message: str, detail: Optional[str] = None) -> ProviderUnavailable:
    health.record_failure(plugin_id, message, detail=detail, provider=provider)
    return ProviderUnavailable(reason)


def _resolve(kind: str, plugin_id: str, provider_id: str):
    """The installation, manifest and manifest entry to call, or ``ProviderUnavailable``."""
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
    return inst, manifest, provide


def call_provider(kind: str, provider_ref: str, method: str, *args: Any, timeout_seconds: Optional[float] = None) -> Any:
    """Call ``method`` of the provider ``provider_ref`` (``<plugin_id>:<provider_id>``) and return its validated answer.

    ``timeout_seconds`` may only lower the provider's own ``timeout_seconds``. Raises only
    ``ProviderUnavailable``. Every timeout, exception and invalid answer counts as a failure for the
    plugin's circuit breaker; a valid answer of a metered method counts as a success.
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
    inst, manifest, provide = _resolve(kind, plugin_id, provider_id)

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
    call = _Call(
        inst=inst,
        manifest=manifest,
        kind=kind,
        provider_id=provider_id,
        method=method,
        call_args=call_args,
        args=args,
        seconds=seconds,
    )
    # A refused or unstarted call is the host's limit at work, not a failure of the plugin.
    if not _take_slot(plugin_id):
        raise ProviderUnavailable("busy")
    try:
        threading.Thread(target=_work, args=(call,), name=f"rackflow-provider-{plugin_id}", daemon=True).start()
    except Exception:  # noqa: BLE001 - no thread to be had
        _release_slot(call)
        raise ProviderUnavailable("busy") from None
    if not call.started.wait(START_SECONDS) and _abandon(call):
        raise ProviderUnavailable("busy")
    # The plugin's time starts when its code starts.
    left = seconds - (time.monotonic() - call.started_at)
    if not call.done.wait(max(0.0, left)) and _abandon(call):
        raise _fail(plugin_id, provider, "timeout", f"{label} timed out after {seconds:g}s")
    call.done.wait()  # finished just as the time ran out: the outcome is being stored
    if call.refusal is not None:
        raise ProviderUnavailable(call.refusal)
    if call.failure is not None:
        reason, message, detail = call.failure
        raise _fail(plugin_id, provider, reason, f"{label} {message}"[:500], detail)
    if method in spec.metered:
        # Only the methods that do real work prove the plugin healthy; a cheap supports() answer
        # before every failing check_online must not keep resetting the circuit.
        health.record_success(plugin_id)
    return call.answer
