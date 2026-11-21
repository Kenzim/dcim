"""The context object handed to every plugin handler (docs/plugin-system.md section 8).

A plugin reaches Rackflow only through this object. Every method checks the
permission the plugin was granted and, for service calls, that the plugin is
active for that service.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import types
from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.core.config import settings as app_settings
from app.models.service import Service, ServiceStatus, ServiceType
from app.plugin_host import service_links
from app.plugin_host.catalogue import API_VERSION, POWER_ACTIONS
from app.plugin_host.common import LOG_LEVELS, audit, rate_hit, utcnow, write_log
from app.plugin_host.manifest import WEEKDAYS, Manifest
from app.plugin_host.state import StateView


class PluginError(Exception):
    """Base class of the errors the SDK raises."""


class PermissionDenied(PluginError):
    pass


class RateLimited(PluginError):
    pass


class NotActive(PluginError):
    pass


class GuestAgentError(PluginError):
    pass


class ActionError(Exception):
    """Raised by an action handler to show a failure message to the user."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = str(message)[:500]


class Veto:
    def __init__(self, reason: str) -> None:
        self.reason = str(reason)[:300] or "Refused"


class Alter:
    def __init__(self, fields: dict) -> None:
        self.fields = dict(fields or {})


errors = types.SimpleNamespace(
    PluginError=PluginError,
    PermissionDenied=PermissionDenied,
    RateLimited=RateLimited,
    NotActive=NotActive,
    GuestAgentError=GuestAgentError,
    ActionError=ActionError,
)

MAX_OUTPUT = 64 * 1024


async def resolve_vm(db: Session, service: Service):
    """Proxmox adapter for a VM service; tests replace this."""
    from app.services.proxmox_placement import resolve_proxmox_plugin_for_service

    plugin, _cid, _node, _vmid = await resolve_proxmox_plugin_for_service(db, service)
    return plugin


def service_info(service: Service) -> dict:
    kind = service.service_type
    status = service.status
    return {
        "id": service.id,
        "name": service.name,
        "service_type": kind.value if isinstance(kind, ServiceType) else kind,
        "status": status.value if isinstance(status, ServiceStatus) else status,
        "owner_user_id": service.owner_user_id,
        "product_code": service.product_code,
        "os_code": service.os_code,
        "created_at": service.created_at.isoformat() if service.created_at else None,
    }


class _Settings:
    def __init__(self, ctx: "PluginContext") -> None:
        self._ctx = ctx
        self._cache: Optional[dict] = None

    def all(self) -> dict:
        if self._cache is None:
            from app.plugin_host import settings_store

            self._cache = settings_store.values(self._ctx._db, self._ctx.plugin_id, self._ctx._manifest)
        return dict(self._cache)

    def get(self, key: str, default: Any = None) -> Any:
        value = self.all().get(key)
        return default if value is None else value


class _Log:
    def __init__(self, ctx: "PluginContext") -> None:
        self._ctx = ctx

    def _write(self, level: str, message: str, service_id: Optional[int] = None, **fields: Any) -> None:
        write_log(self._ctx.plugin_id, level, str(message), service_id=service_id, context=fields or None)

    def __getattr__(self, level: str):
        if level not in LOG_LEVELS:
            raise AttributeError(level)
        return lambda message, service_id=None, **fields: self._write(level, message, service_id, **fields)


class _Services:
    def __init__(self, ctx: "PluginContext") -> None:
        self._ctx = ctx

    def get(self, service_id: int) -> dict:
        self._ctx._require("service.read")
        return service_info(self._ctx._active_service(service_id))

    def active(self, service_type: Optional[str] = None) -> list[int]:
        self._ctx._require("service.read")
        return service_links.active_service_ids(
            self._ctx._db, self._ctx.plugin_id, self._ctx._manifest, service_type=service_type
        )


class _Guest:
    def __init__(self, ctx: "PluginContext") -> None:
        self._ctx = ctx

    async def _vm(self, service_id: int):
        ctx = self._ctx
        ctx._require("guest.exec")
        service = ctx._active_service(service_id, vm_only=True, running=True)
        try:
            return service, await resolve_vm(ctx._db, service)
        except Exception as exc:  # noqa: BLE001 - placement errors become SDK errors
            raise GuestAgentError(f"The VM cannot be reached: {exc}") from exc

    async def ping(self, service_id: int) -> bool:
        _service, vm = await self._vm(service_id)
        try:
            return bool(await asyncio.wait_for(vm.guest_agent_ready(), 20))
        except Exception:  # noqa: BLE001 - any failure means the agent did not answer
            return False

    async def exec(
        self,
        service_id: int,
        argv: list,
        *,
        timeout: int = 60,
        input: Optional[str] = None,
        sensitive: bool = False,
    ) -> dict:
        ctx = self._ctx
        ctx._require("guest.exec")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise PluginError("argv must be a non-empty list of strings")
        timeout = int(timeout)
        if timeout < 1 or timeout > 300:
            raise PluginError("timeout must be between 1 and 300 seconds")
        service, vm = await self._vm(service_id)
        if not rate_hit(f"guest:{ctx.plugin_id}:{service_id}", app_settings.plugin_guest_exec_per_minute):
            raise RateLimited("Too many guest commands for this service; try again in a minute")
        if not rate_hit(f"guest:{ctx.plugin_id}", app_settings.plugin_guest_exec_per_minute_total):
            raise RateLimited("Too many guest commands for this plugin; try again in a minute")
        shown = (
            [argv[0], "sha256:" + hashlib.sha256(json.dumps(argv[1:]).encode()).hexdigest()]
            if sensitive
            else list(argv)
        )
        started = time.monotonic()
        meta: dict[str, Any] = {"argv": shown}
        try:
            result = await asyncio.wait_for(vm.guest_exec(list(argv), input_data=input, max_wait=float(timeout)), timeout + 15)
        except Exception as exc:  # noqa: BLE001
            meta.update({"ok": False, "error": str(exc)[:300], "duration_ms": int((time.monotonic() - started) * 1000)})
            ctx._audit_service("plugin.guest_exec", service.id, meta)
            raise GuestAgentError(f"The guest command failed: {exc}") from exc
        out = {
            "exit_code": result.get("exitcode"),
            "stdout": str(result.get("out-data") or "")[:MAX_OUTPUT],
            "stderr": str(result.get("err-data") or "")[:MAX_OUTPUT],
        }
        meta.update({"ok": True, "exit_code": out["exit_code"], "duration_ms": int((time.monotonic() - started) * 1000)})
        ctx._audit_service("plugin.guest_exec", service.id, meta)
        return out


class _Vm:
    def __init__(self, ctx: "PluginContext") -> None:
        self._ctx = ctx

    async def power(self, service_id: int, action: str) -> bool:
        from app.plugin_host import events

        ctx = self._ctx
        ctx._require("vm.power")
        if action not in POWER_ACTIONS:
            raise PluginError(f"action must be one of {', '.join(POWER_ACTIONS)}")
        service = ctx._active_service(service_id, vm_only=True, running=action != "off")
        await events.power_requested(ctx._db, service, action, source="plugin", exclude_plugin=ctx.plugin_id)
        try:
            vm = await resolve_vm(ctx._db, service)
            if action == "on":
                ok = await vm.power_on()
            elif action == "off":
                ok = await vm.power_off(force=True)
            elif action == "shutdown":
                ok = await vm.power_off(force=False)
            else:
                ok = await vm.power_reset()
        except Exception as exc:  # noqa: BLE001
            ctx._audit_service("plugin.vm_power", service.id, {"action": action, "ok": False, "error": str(exc)[:300]})
            raise PluginError(f"The power action failed: {exc}") from exc
        ctx._audit_service("plugin.vm_power", service.id, {"action": action, "ok": bool(ok)})
        return bool(ok)


class _Notify:
    def __init__(self, ctx: "PluginContext") -> None:
        self._ctx = ctx

    def client(self, service_id: int, message: str, *, key: str) -> int:
        from app.services import client_notifications

        ctx = self._ctx
        ctx._require("notify.client")
        service = ctx._active_service(service_id)
        message = str(message)[:500]
        queued = client_notifications.notify(
            ctx._db,
            service.owner_user_id,
            "plugin",
            message,
            event_key=f"plugin:{ctx.plugin_id}:{str(key)[:120]}",
            data={"service_id": service.id, "service_name": service.name, "plugin_id": ctx.plugin_id},
        )
        ctx._audit_service("plugin.notify_client", service.id, {"queued": queued, "key": str(key)[:120]})
        return queued


class _Http:
    def __init__(self, ctx: "PluginContext") -> None:
        self._ctx = ctx

    async def _call(self, method: str, url: str, *, json_body=None, body=None, headers=None, timeout: float = 10) -> dict:
        from urllib.parse import urlsplit

        from app.services.alerting import safe_http

        ctx = self._ctx
        ctx._require("http.outbound")
        headers = {str(k): str(v) for k, v in (headers or {}).items()}
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        timeout = max(1.0, min(float(timeout), 30.0))
        host = urlsplit(url).hostname if isinstance(url, str) else None
        try:
            result = await asyncio.to_thread(
                safe_http.request,
                method,
                url,
                body,
                headers,
                public_only=True,
                https_only=not app_settings.plugin_http_allow_plain,
                timeout=timeout,
                max_bytes=MAX_OUTPUT,
            )
        except Exception as exc:  # noqa: BLE001
            ctx._audit("plugin.http", {"method": method, "host": host, "ok": False, "error": str(exc)[:200]})
            raise PluginError(f"HTTP {method} failed: {exc}") from exc
        ctx._audit("plugin.http", {"method": method, "host": host, "status": result.status})
        return {"status": result.status, "text": result.body}

    async def post(self, url: str, *, json=None, body=None, headers=None, timeout: float = 10) -> dict:
        return await self._call("POST", url, json_body=json, body=body, headers=headers, timeout=timeout)

    async def get(self, url: str, *, headers=None, timeout: float = 10) -> dict:
        return await self._call("GET", url, headers=headers, timeout=timeout)


class PluginContext:
    """One plugin, one invocation. Commits its state writes when the handler returns."""

    errors = errors
    ActionError = ActionError

    def __init__(
        self,
        db: Session,
        *,
        plugin_id: str,
        plugin_version: str,
        manifest: Manifest,
        granted: set[str],
        kind: str,
        actor: Optional[dict] = None,
    ) -> None:
        self._db = db
        self._manifest = manifest
        self._granted = set(granted)
        self.plugin_id = plugin_id
        self.plugin_version = plugin_version
        self.api_version = API_VERSION
        self.kind = kind
        self.actor = dict(actor) if actor else None
        self.abandoned = False
        self.settings = _Settings(self)
        self.log = _Log(self)
        self.store = StateView(db, plugin_id)
        self.services = _Services(self)
        self.guest = _Guest(self)
        self.vm = _Vm(self)
        self.notify = _Notify(self)
        self.http = _Http(self)

    def _require(self, permission: str) -> None:
        if permission not in self._granted:
            raise PermissionDenied(f"The plugin was not granted the {permission} permission")

    def _active_service(self, service_id: int, *, vm_only: bool = False, running: bool = False) -> Service:
        service = self._db.get(Service, int(service_id)) if service_id is not None else None
        if service is None or not service_links.is_active(self._db, self.plugin_id, self._manifest, service):
            raise NotActive(f"The plugin is not active for service {service_id}")
        if vm_only and service.service_type != ServiceType.VM:
            raise NotActive("This call works on VM services only")
        if running and service.status != ServiceStatus.ACTIVE:
            raise NotActive("The service is not active")
        return service

    def _audit(self, action: str, metadata: dict) -> None:
        user_id = (self.actor or {}).get("user_id") if (self.actor or {}).get("type") == "staff" else None
        audit(action, plugin_id=self.plugin_id, actor_user_id=user_id, resource_type="plugin",
              resource_id=self.plugin_id, metadata=metadata)

    def _audit_service(self, action: str, service_id: int, metadata: dict) -> None:
        user_id = (self.actor or {}).get("user_id")
        audit(action, plugin_id=self.plugin_id, actor_user_id=user_id, resource_type="service",
              resource_id=str(service_id), metadata=metadata)

    def now(self) -> datetime:
        return utcnow()

    def state(self, service_id: int) -> StateView:
        service = self._active_service(service_id)
        return StateView(self._db, self.plugin_id, service_id=service.id)

    def in_window(self, setting_key: str, at: Optional[datetime] = None) -> bool:
        window = self.settings.get(setting_key)
        if not isinstance(window, dict):
            return False
        return in_window(window, at or self.now())

    def audit(self, action: str, *, service_id: Optional[int] = None, details: Optional[dict] = None) -> None:
        name = f"plugin.{self.plugin_id}.{action}"[:64]
        meta = {"details": details} if details else {}
        if service_id is not None:
            self._audit_service(name, service_id, meta)
        else:
            self._audit(name, meta)

    @staticmethod
    def veto(reason: str) -> Veto:
        return Veto(reason)

    @staticmethod
    def alter(fields: dict) -> Alter:
        return Alter(fields)

    def set_active(self, service_id: int, enabled: bool) -> None:
        """Switch this plugin on or off for one service (never overrides an admin decision)."""
        self._require("service.read")
        service = self._db.get(Service, int(service_id))
        if service is None or not service_links.applies_to_type(self._manifest, service):
            raise NotActive(f"Service {service_id} is not a type this plugin applies to")
        service_links.set_plugin_link(self._db, self.plugin_id, service.id, bool(enabled))

    def _commit(self) -> None:
        self._db.commit()

    def _rollback(self) -> None:
        self._db.rollback()

    def _close(self) -> None:
        self._db.close()


def in_window(window: dict, at: datetime) -> bool:
    tz = ZoneInfo(window.get("timezone") or "UTC")
    local = at.astimezone(tz)
    start_h, start_m = (int(x) for x in window["start"].split(":"))
    end_h, end_m = (int(x) for x in window["end"].split(":"))
    days = window.get("days") or []
    minute = local.hour * 60 + local.minute
    start, end = start_h * 60 + start_m, end_h * 60 + end_m
    today = WEEKDAYS[local.weekday()]
    if start <= end:
        return today in days and start <= minute < end
    if minute >= start:
        return today in days
    yesterday = WEEKDAYS[(local - timedelta(days=1)).weekday()]
    return minute < end and yesterday in days
