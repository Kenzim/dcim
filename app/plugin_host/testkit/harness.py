"""A small Rackflow for plugin tests: the real plugin host on an in-memory database.

Everything between "an event is queued" and "the plugin's handler ran" is the host's own
code: install and enable, the event bus and outbox, the scheduler, panel rendering, action
and tool dispatch, settings with sealed secrets, plugin state, circuit breaker and logs.
Only the outside world is replaced: the database is SQLite in memory, Redis is a counter
dictionary, ``ctx.http`` talks to ``FakeHttp`` and VMs are ``FakeVm``.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Union

from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.plugin_host.testkit.fakes import FakeHttp, FakeRedis, FakeVm
from app.plugin_host.testkit.samples import sample_payload


def run(coro):
    """Run a coroutine to completion from a plain (synchronous) test."""
    return asyncio.run(coro)


@dataclass
class DeliveryInfo:
    plugin_id: str
    event: str
    status: str
    attempts: int
    last_error: Optional[str]
    envelope: dict

    @property
    def payload(self) -> dict:
        return self.envelope.get("payload", {})


@dataclass
class Fired:
    """The outcome of ``Harness.fire``: what was queued and what each subscribed plugin did."""

    queued: int
    deliveries: list[DeliveryInfo] = field(default_factory=list)

    def _with(self, status: str) -> list[DeliveryInfo]:
        return [d for d in self.deliveries if d.status == status]

    @property
    def delivered(self) -> list[DeliveryInfo]:
        return self._with("delivered")

    @property
    def failed(self) -> list[DeliveryInfo]:
        return self._with("failed") + self._with("dead")

    @property
    def skipped(self) -> list[DeliveryInfo]:
        return self._with("skipped")

    @property
    def errors(self) -> list[str]:
        return [d.last_error for d in self.deliveries if d.last_error]


@dataclass
class ScheduleRun:
    schedule: str
    scope: str
    service_id: Optional[int]
    status: Optional[str]
    error: Optional[str]


class Clock:
    """The time ``ctx.now()`` and ``ctx.in_window()`` see. The host's own timers stay on real time."""

    def __init__(self) -> None:
        self._fixed: Optional[datetime] = None

    def now(self) -> datetime:
        return self._fixed or datetime.now(timezone.utc)

    def set(self, moment: datetime) -> None:
        self._fixed = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)

    def advance(self, **delta: float) -> datetime:
        self._fixed = self.now() + timedelta(**delta)
        return self._fixed

    def reset(self) -> None:
        self._fixed = None


class InstalledPlugin:
    """One installed plugin: its settings, state, logs and lifecycle, plus shortcuts into the harness."""

    def __init__(self, harness: "Harness", plugin_id: str, source: Path) -> None:
        self.harness = harness
        self.id = plugin_id
        self.source = source

    @property
    def installation(self):
        from app.models.plugin_host import PluginInstallation

        self.harness.db.expire_all()
        return self.harness.db.get(PluginInstallation, self.id)

    @property
    def status(self) -> str:
        return self.installation.status

    @property
    def manifest(self):
        from app.plugin_host import runtime

        return runtime.manifest_of(self.installation)

    def configure(self, **values: Any) -> list[str]:
        """Save settings through the same validation the admin screen uses. Returns the keys that changed."""
        from app.plugin_host import settings_store

        db = self.harness.db
        changed = settings_store.update(db, self.id, self.manifest, values)
        db.commit()
        return changed

    def settings(self) -> dict:
        """Settings as the admin API shows them: secrets are masked."""
        from app.plugin_host import settings_store

        return settings_store.api_view(self.harness.db, self.id, self.manifest)["values"]

    def secret(self, key: str) -> Optional[str]:
        """A secret setting's stored value, decrypted (for assertions only)."""
        from app.plugin_host import settings_store

        return settings_store.values(self.harness.db, self.id, self.manifest).get(key)

    def state(self, service: Any = None) -> dict:
        """The plugin's key-value state: plugin-wide, or for one service."""
        from app.plugin_host import state as state_module

        scope = state_module.service_scope(service.id) if service is not None else state_module.GLOBAL_SCOPE
        self.harness.db.expire_all()
        return state_module.read_scope(self.harness.db, self.id, scope)

    def logs(self, level: Optional[str] = None) -> list[Any]:
        from app.models.plugin_host import PluginLog

        self.harness.db.expire_all()
        query = select(PluginLog).where(PluginLog.plugin_id == self.id).order_by(PluginLog.id)
        if level:
            query = query.where(PluginLog.level == level)
        return list(self.harness.db.execute(query).scalars())

    def log_messages(self, level: Optional[str] = None) -> list[str]:
        return [row.message for row in self.logs(level)]

    def activate(self, service: Any, on: bool = True) -> None:
        """Switch the plugin on or off for one service, as an admin does on the plugin's services tab."""
        from app.plugin_host import service_links

        service_links.set_admin_link(self.harness.db, self.id, service.id, on)
        self.harness.db.commit()
        from app.plugin_host import runtime

        runtime.invalidate()

    def enable(self) -> None:
        from app.plugin_host import lifecycle

        run(lifecycle.set_enabled(self.harness.db, self.installation, True))

    def disable(self) -> None:
        from app.plugin_host import lifecycle

        run(lifecycle.set_enabled(self.harness.db, self.installation, False))

    def upgrade(self, source: Optional[Union[str, Path]] = None, *, accept_permissions: Optional[list[str]] = None) -> None:
        """Replace the installed files with a newer folder and run the host's upgrade path."""
        from app.plugin_host import lifecycle
        from app.plugin_host.loader import find_package

        folder = Path(source) if source is not None else self.source
        self.harness._copy(folder, self.id)
        package = find_package(self.id)
        accept = list(package.manifest.permissions) if accept_permissions is None else accept_permissions
        run(lifecycle.upgrade(self.harness.db, self.installation, accept_permissions=accept))

    def uninstall(self, *, keep_data: bool = False) -> None:
        from app.plugin_host import lifecycle

        run(lifecycle.uninstall(self.harness.db, self.installation, keep_data=keep_data))


class Harness:
    def __init__(self, monkeypatch: Any, tmp_path: Path) -> None:
        import app.models  # noqa: F401  (registers every table and the service event listeners)
        from app.core import database
        from app.core import redis as redis_module
        from app.core.config import settings
        from app.plugin_host import runtime, sdk

        self._monkeypatch = monkeypatch
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        database.Base.metadata.create_all(self.engine)
        self._sessions = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        monkeypatch.setattr(database, "engine", self.engine)
        monkeypatch.setattr(database, "SessionLocal", self._sessions)
        self.redis = FakeRedis()
        monkeypatch.setattr(redis_module, "redis_client", self.redis)
        self.plugins_dir = tmp_path / "plugins"
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(settings, "plugins_dir", str(self.plugins_dir))
        # No bundled packages: a plugin under test must not clash with the copy Rackflow ships.
        monkeypatch.setattr(settings, "bundled_plugins_dir", str(tmp_path / "bundled"))
        monkeypatch.setattr(settings, "require_service_instance_encryption", False)
        monkeypatch.setattr(settings, "plugin_http_allow_plain", False)
        self.http = FakeHttp()
        self.http.install(monkeypatch)
        self.vm = FakeVm()
        self.vm.install(monkeypatch)
        self.clock = Clock()
        monkeypatch.setattr(sdk, "utcnow", self.clock.now)
        runtime.reset_all()
        self.db: Session = self._sessions()
        self._users = 0

    def close(self) -> None:
        from app.plugin_host import runtime

        self.db.close()
        runtime.reset_all()
        self.engine.dispose()

    # ------------------------------------------------------------------ installing

    def _copy(self, source: Path, directory: str) -> Path:
        target = self.plugins_dir / directory
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(
            source,
            target,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests", ".pytest_cache", ".git", "conftest.py"),
        )
        return target

    def install(
        self,
        source: Union[str, Path],
        *,
        enable: bool = True,
        settings: Optional[dict] = None,
        accept_permissions: Optional[list[str]] = None,
    ) -> InstalledPlugin:
        """Copy a plugin folder into the plugins directory and install it with the real lifecycle.

        The folder is installed under its own name, so a folder whose name differs from the
        manifest id fails here exactly as it would for an admin. ``accept_permissions``
        defaults to everything the manifest asks for. Settings are saved before the plugin
        is enabled, so the ``enable`` lifecycle step already sees them.
        """
        from app.plugin_host import lifecycle
        from app.plugin_host.loader import find_package

        source = Path(source).resolve()
        self._copy(source, source.name)
        package = find_package(source.name)
        if package.manifest is None:
            raise lifecycle.LifecycleError("; ".join(package.errors))
        accept = list(package.manifest.permissions) if accept_permissions is None else accept_permissions
        run(lifecycle.install(self.db, source.name, accept_permissions=accept, enable=False))
        plugin = InstalledPlugin(self, package.manifest.id, source)
        if settings:
            plugin.configure(**settings)
        if enable:
            plugin.enable()
        return plugin

    # ------------------------------------------------------------------ people and services

    def make_user(self, username: Optional[str] = None, *, admin: bool = False, plugin_notices: bool = True):
        """A user. By default the user has opted in to plugin notices, as a client must for ``ctx.notify.client``."""
        from app.models.user import User
        from app.services import client_notifications

        self._users += 1
        username = username or f"user{self._users}"
        user = User(username=username, email=f"{username}@example.com", is_admin=admin)
        user.set_password("password12345")
        self.db.add(user)
        self.db.commit()
        self.db.refresh(user)
        if plugin_notices and not admin:
            client_notifications.update_preferences(self.db, user.id, categories={"plugin": True})
        return user

    def make_service(
        self,
        *,
        owner: Any = None,
        type: str = "vm",
        status: str = "active",
        name: Optional[str] = None,
        product_code: Optional[str] = None,
        os_code: Optional[str] = None,
    ):
        """Insert a service the way the rest of Rackflow does; subscribed plugins receive a real ``service.created``."""
        from app.models.service import ProvisioningSource, Service, ServiceStatus, ServiceType

        owner = owner or self.make_user()
        service = Service(
            name=name or f"{type}-{self._users}-service",
            service_type=ServiceType(type),
            status=ServiceStatus(status),
            owner_user_id=owner.id,
            product_code=product_code,
            os_code=os_code,
            provisioning_source=ProvisioningSource.INTERNAL,
        )
        self.db.add(service)
        self.db.commit()
        self.db.refresh(service)
        return service

    def set_status(self, service: Any, status: str) -> None:
        """Change a service's status; the host queues ``service.suspended``, ``service.terminated`` and so on."""
        from app.models.service import ServiceStatus

        service.status = ServiceStatus(status)
        self.db.commit()

    def client_notices(self, user: Any) -> list[dict]:
        """Notices queued for a client (subject and text), as ``ctx.notify.client`` creates them."""
        from app.models.alerting import NotificationDelivery

        rows = self.db.execute(
            select(NotificationDelivery).where(NotificationDelivery.user_id == user.id).order_by(NotificationDelivery.id)
        ).scalars()
        return [dict(row.payload or {}, channel=row.channel_kind) for row in rows]

    # ------------------------------------------------------------------ events

    def emit(self, name: str, service: Any = None, **fields: Any) -> int:
        """Queue an event for subscribed plugins. Missing payload fields get sample values. Returns the deliveries queued."""
        from app.plugin_host import events

        payload = sample_payload(name, service, **fields)
        queued = events.emit(self.db, name, payload, service_id=payload.get("service_id") if service is not None else None)
        self.db.commit()
        return queued

    def deliver(self) -> dict[str, int]:
        """Run the outbox once, as the plugin worker does: returns counts per outcome."""
        from app.plugin_host import outbox

        self.db.expire_all()
        return run(outbox.run_once())

    def deliveries(
        self, plugin_id: Optional[str] = None, event: Optional[str] = None, status: Optional[str] = None
    ) -> list[DeliveryInfo]:
        from app.models.plugin_host import PluginDelivery

        self.db.expire_all()
        query = select(PluginDelivery).order_by(PluginDelivery.id)
        if plugin_id:
            query = query.where(PluginDelivery.plugin_id == plugin_id)
        if event:
            query = query.where(PluginDelivery.event == event)
        if status:
            query = query.where(PluginDelivery.status == status)
        return [
            DeliveryInfo(r.plugin_id, r.event, r.status, r.attempts or 0, r.last_error, dict(r.envelope or {}))
            for r in self.db.execute(query).scalars()
        ]

    def fire(self, name: str, service: Any = None, **fields: Any) -> Fired:
        """Emit an event and deliver it at once; returns what each subscribed plugin did."""
        from app.models.plugin_host import PluginDelivery

        before = self.db.execute(select(PluginDelivery.id).order_by(PluginDelivery.id.desc()).limit(1)).scalar() or 0
        queued = self.emit(name, service, **fields)
        self.deliver()
        self.db.expire_all()
        rows = self.db.execute(
            select(PluginDelivery).where(PluginDelivery.id > before, PluginDelivery.event == name).order_by(PluginDelivery.id)
        ).scalars()
        infos = [
            DeliveryInfo(r.plugin_id, r.event, r.status, r.attempts or 0, r.last_error, dict(r.envelope or {}))
            for r in rows
        ]
        return Fired(queued=queued, deliveries=infos)

    def request_power(self, service: Any, action: str = "reboot", *, source: str = "admin") -> Optional[str]:
        """Run the power before hooks. Returns the veto message, or ``None`` when every hook allowed it."""
        from app.plugin_host import events

        try:
            run(events.power_requested(self.db, service, action, source=source))
        except events.OperationVetoed as exc:
            return exc.message
        return None

    # ------------------------------------------------------------------ schedules

    def run_schedule(self, plugin: InstalledPlugin, schedule_id: str, service: Any = None) -> list[ScheduleRun]:
        """Make a schedule due and run it through the real scheduler. Returns one entry per scope that ran."""
        from app.models.plugin_host import PluginSchedule
        from app.plugin_host import scheduler

        scheduler.sync_rows(self.db)
        due = datetime.now(timezone.utc) - timedelta(seconds=1)
        match = [PluginSchedule.plugin_id == plugin.id, PluginSchedule.schedule == schedule_id]
        if service is not None:
            match.append(PluginSchedule.service_id == service.id)
        self.db.execute(
            update(PluginSchedule).where(*match).values(next_run_at=due, lease_owner=None, lease_expires_at=None)
        )
        self.db.commit()
        run(scheduler.run_once())
        self.db.expire_all()
        rows = self.db.execute(select(PluginSchedule).where(*match).order_by(PluginSchedule.id)).scalars()
        return [ScheduleRun(r.schedule, r.scope, r.service_id, r.last_status, r.last_error) for r in rows]

    # ------------------------------------------------------------------ interface

    def panels(self, service: Any, audience: str = "client") -> list[dict]:
        """The panel documents the portal (client) or the admin interface (admin) gets for a service."""
        from app.plugin_host import ui

        self.db.expire_all()
        return ui.render(self.db, service, audience)["panels"]

    def panel(self, service: Any, panel_id: str, audience: str = "client") -> Optional[dict]:
        return next((p for p in self.panels(service, audience) if p["panel_id"] == panel_id), None)

    def run_action(
        self,
        plugin: InstalledPlugin,
        service: Any,
        action_id: str,
        params: Optional[dict] = None,
        *,
        as_: str = "admin",
        confirm: bool = False,
    ) -> dict:
        """Run an action as an admin or the service's client. Raises ``PluginRequestError`` with the HTTP status the API would send."""
        from app.plugin_host import ui

        if as_ == "client":
            actor = {"type": "client", "user_id": service.owner_user_id, "via": "portal"}
        else:
            actor = {"type": "staff", "user_id": None, "via": "admin"}
        self.db.expire_all()
        return run(
            ui.run_action(self.db, service, plugin.id, action_id, params=params, confirm=confirm, actor=actor)
        )

    def run_tool(
        self, plugin: InstalledPlugin, tool_id: str, params: Optional[dict] = None, *, audience: str = "staff", owner: Any = None
    ) -> Any:
        """Run an MCP tool as the staff or client MCP would."""
        from app.plugin_host import ui

        if audience == "client":
            actor = {"type": "client", "user_id": getattr(owner, "id", None), "via": "client_mcp"}
        else:
            actor = {"type": "staff", "user_id": None, "via": "mcp"}
        self.db.expire_all()
        return run(ui.run_tool(self.db, plugin.id, tool_id, params=params, actor=actor, audience=audience))

    def reset_rate_limits(self) -> None:
        self.redis.counters.clear()

    # ------------------------------------------------------------------ audit

    def audit_entries(self, prefix: str = "plugin") -> list[dict]:
        """Audit rows whose action starts with ``prefix``: ``action``, ``resource_type``, ``resource_id``, ``metadata``."""
        from app.models.commerce_audit import UserAuditEvent

        self.db.expire_all()
        rows = self.db.execute(
            select(UserAuditEvent).where(UserAuditEvent.action.like(f"{prefix}%")).order_by(UserAuditEvent.id)
        ).scalars()
        return [
            {
                "action": r.action,
                "resource_type": r.resource_type,
                "resource_id": r.resource_id,
                "metadata": dict(r.event_metadata or {}),
            }
            for r in rows
        ]
