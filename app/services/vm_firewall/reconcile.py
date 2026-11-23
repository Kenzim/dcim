"""Reconcile logic of the VM firewall worker (docs/vm-firewall.md section 6).

The worker loop (``app.workers.vm_firewall``) calls ``tick`` every few seconds under its lease.
A tick releases due staged-rollout rows, works the apply queue, checks a slice of the managed VMs
for drift (so every VM is checked once per ``vm_firewall_reconcile_interval_seconds``), checks
cluster readiness once per interval and records its progress for the stale-worker alert.

Every Proxmox call goes through ``ProxmoxAccess``: production uses ``PluginAccess`` (the existing
placement resolver and ``PluginTransport``), tests plug in the fake. Every write goes through
``apply.apply``, which reads the VM config (and so net0's bridge) before it renders.
"""

from __future__ import annotations

import asyncio
import logging
import math
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, ContextManager, Iterator, Optional, Protocol

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

from app.models.commerce_account import SystemSetting
from app.models.commerce_audit import UserAuditEvent
from app.models.proxmox_inventory import ProxmoxCluster
from app.models.service import Service, ServiceStatus
from app.models.service_vm import ServiceVm
from app.models.vm_firewall import FirewallLocationSettings, FirewallState, ServiceFirewall
from app.services.vm_firewall import service as fw
from app.services.vm_firewall.apply import CLAIM_LOST, ApplyResult, apply, check, readiness, record_failure
from app.services.vm_firewall.proxmox_fw import (
    ClusterReader,
    PluginTransport,
    ProxmoxApiError,
    VmFirewallClient,
    VmMissing,
    net_firewall_flag,
)
from app.services.vm_firewall.render import as_utc, render

logger = logging.getLogger(__name__)

LEASE_NAME = "vm_firewall"
STATUS_KEY = "vm_firewall_worker"
TICK_SECONDS = 5
CLAIM_SECONDS = 120
QUEUE_BATCH = 100
ROLLOUT_PAUSE_AFTER = 3
# Eligibility reasons the renderer handles itself (``enable: 0`` for a disabled location, no
# ``ipfilter`` without the location's IPv6 prefix), so they do not stop an apply.
RENDERED_REASONS = frozenset({"location_disabled", "ipv6_ra_bridge_needs_prefix"})
_ENFORCED = (FirewallState.APPLIED, FirewallState.PENDING_REBOOT)


class ProxmoxAccess(Protocol):
    async def vm_client(self, db: Session, service: Service) -> VmFirewallClient:
        """Client for the VM of ``service``; raises ``VmMissing`` when Proxmox has no such VM."""

    async def cluster_reader(self, db: Session, cluster: ProxmoxCluster) -> ClusterReader: ...


class PluginAccess:
    """Production access through the Proxmox plugin (authentication and node relocation)."""

    async def vm_client(self, db: Session, service: Service) -> VmFirewallClient:
        from app.services.proxmox_placement import ProxmoxPlacementError, resolve_proxmox_plugin_for_service

        try:
            plugin, _cluster_id, _node, vmid = await resolve_proxmox_plugin_for_service(db, service)
        except ProxmoxPlacementError as exc:
            if exc.status_code == 404:
                raise VmMissing(404, str(exc), method="GET", path="") from exc
            raise
        return VmFirewallClient(PluginTransport(plugin), vmid)

    async def cluster_reader(self, db: Session, cluster: ProxmoxCluster) -> ClusterReader:
        from app.plugins.registry import get_registry
        from app.services.proxmox_placement import cluster_to_proxmox_plugin_config

        nodes = [node.node_name for node in (cluster.nodes or []) if node.enabled]
        if not nodes:
            raise ProxmoxApiError(0, "cluster has no enabled node in the inventory", method="GET", path="/nodes")
        # The reader only uses cluster-wide paths; node and VMID are placeholders the plugin needs.
        config = cluster_to_proxmox_plugin_config(cluster, nodes[0], 100)
        return ClusterReader(PluginTransport(get_registry().get_plugin("proxmox", config)))


# Time and settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _naive(value: datetime) -> datetime:
    """Database comparisons use naive UTC, the form stored values come back in."""
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def _setting(db: Session, key: str) -> Any:
    from app.services.settings_store import get_setting

    return get_setting(db, key)


def reconcile_interval(db: Session) -> int:
    return int(_setting(db, "vm_firewall_reconcile_interval_seconds"))


# Status (read by the stale-worker alert)


def read_status(db: Session) -> dict[str, Any]:
    row = db.get(SystemSetting, STATUS_KEY)
    return dict(row.value) if row is not None and isinstance(row.value, dict) else {}


def _write_status(db: Session, **values: Any) -> None:
    row = db.get(SystemSetting, STATUS_KEY)
    merged = read_status(db)
    merged.update({key: (value.isoformat() if isinstance(value, datetime) else value) for key, value in values.items()})
    if row is None:
        db.add(SystemSetting(key=STATUS_KEY, value=merged))
    else:
        row.value = merged
    db.flush()


def status_time(status: dict[str, Any], key: str) -> Optional[datetime]:
    raw = status.get(key)
    if not raw:
        return None
    try:
        return as_utc(datetime.fromisoformat(raw))
    except ValueError:
        return None


# Locations and clusters


def _cluster_id(db: Session, service_id: int) -> Optional[int]:
    vm = db.get(ServiceVm, service_id)
    return vm.proxmox_cluster_id if vm is not None else None


def _location(db: Session, service_id: int) -> Optional[FirewallLocationSettings]:
    cluster_id = _cluster_id(db, service_id)
    cluster = db.get(ProxmoxCluster, cluster_id) if cluster_id is not None else None
    if cluster is None or cluster.location_id is None:
        return None
    return db.get(FirewallLocationSettings, cluster.location_id)


def _location_enabled_expr():
    """Correlated: whether the row's location has the firewall switched on."""
    return (
        select(FirewallLocationSettings.enabled)
        .join(ProxmoxCluster, ProxmoxCluster.location_id == FirewallLocationSettings.location_id)
        .join(ServiceVm, ServiceVm.proxmox_cluster_id == ProxmoxCluster.id)
        .where(ServiceVm.service_id == ServiceFirewall.service_id)
        .limit(1)
        .scalar_subquery()
    )


def cluster_ready(db: Session, service_id: int) -> Optional[bool]:
    """The cached readiness of the VM's cluster: True, False, or None when never checked."""
    location = _location(db, service_id)
    cluster_id = _cluster_id(db, service_id)
    if location is None or cluster_id is None:
        return None
    entry = ((location.readiness or {}).get("clusters") or {}).get(str(cluster_id))
    if not isinstance(entry, dict) or "ready" not in entry:
        return None
    return bool(entry["ready"])


def _enforcement_state(db: Session, row: ServiceFirewall) -> None:
    """``applied``/``pending_reboot`` become ``not_enforced`` while the cluster is not ready."""
    if row.state in _ENFORCED and cluster_ready(db, row.service_id) is False:
        row.state = FirewallState.NOT_ENFORCED


# Claims


def claim(db: Session, service_id: int, *, owner: str, now: datetime) -> bool:
    """Take the row for ``CLAIM_SECONDS`` (conditional update); commits."""
    result = db.execute(
        update(ServiceFirewall)
        .where(
            ServiceFirewall.service_id == service_id,
            or_(
                ServiceFirewall.claimed_until.is_(None),
                ServiceFirewall.claimed_until <= _naive(now),
                ServiceFirewall.claimed_by == owner,
            ),
        )
        .values(claimed_by=owner, claimed_until=now + timedelta(seconds=CLAIM_SECONDS))
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount == 1


def claim_clock(now: datetime) -> Callable[[], datetime]:
    """``now`` moved on by the wall-clock time since this call. Claims taken late in a long tick
    use it, so they expire ``CLAIM_SECONDS`` after they are taken, not after the tick started."""
    started = utcnow()
    return lambda: now + (utcnow() - started)


def renew(db: Session, service_id: int, *, owner: str, now: datetime) -> bool:
    """Extend ``owner``'s claim to ``CLAIM_SECONDS`` from ``now`` (conditional update); false when
    the row is no longer claimed by ``owner``. Commits."""
    result = db.execute(
        update(ServiceFirewall)
        .where(ServiceFirewall.service_id == service_id, ServiceFirewall.claimed_by == owner)
        .values(claimed_until=now + timedelta(seconds=CLAIM_SECONDS))
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount == 1


@dataclass
class Claim:
    """An applier's claim on one row, kept alive while ``apply`` writes to Proxmox.

    ``renew`` runs in a short session of its own on ``bind``, so it commits at once without
    committing the apply's session, whose row changes stay unflushed until the apply records its
    outcome. ``confirm`` runs in the apply's session and locks the row until that session commits."""

    bind: Any
    service_id: int
    owner: str
    clock: Callable[[], datetime]

    @classmethod
    def of(cls, db: Session, service_id: int, *, owner: str, clock: Callable[[], datetime]) -> "Claim":
        return cls(db.get_bind(), service_id, owner, clock)

    def renew(self) -> bool:
        with Session(bind=self.bind, autoflush=False) as session:
            return renew(session, self.service_id, owner=self.owner, now=self.clock())

    def confirm(self, db: Session) -> bool:
        claimed_by = db.execute(
            select(ServiceFirewall.claimed_by)
            .where(ServiceFirewall.service_id == self.service_id)
            .with_for_update()
        ).scalar_one_or_none()
        return claimed_by == self.owner


def unclaim(db: Session, service_id: int, *, owner: str) -> None:
    db.execute(
        update(ServiceFirewall)
        .where(ServiceFirewall.service_id == service_id, ServiceFirewall.claimed_by == owner)
        .values(claimed_by=None, claimed_until=None)
        .execution_options(synchronize_session=False)
    )
    db.commit()


# Rollout pause (section 8.3)


def _count_rollout_outcome(db: Session, row: ServiceFirewall, result: Optional[ApplyResult], now: datetime) -> None:
    """The first apply after ``release_due_rollouts`` released a row counts towards its location's
    pause: 3 failures in a row pause the rollout, a success resets the count."""
    if row.rollout_released_at is None:
        return
    row.rollout_released_at = None
    if result is None or result.state == FirewallState.VM_MISSING.value:
        return
    location = _location(db, row.service_id)
    if location is None:
        return
    # Applies run concurrently, each session holding the location as it read it before its Proxmox
    # calls, so the count is changed in SQL and read back rather than written from that copy.
    table = FirewallLocationSettings
    counted = func.coalesce(table.rollout_failures, 0) + 1
    db.execute(
        update(table)
        .where(table.location_id == location.location_id)
        .values(rollout_failures=0 if result.ok else counted)
        .execution_options(synchronize_session=False)
    )
    db.refresh(location, ["rollout_failures", "rollout_paused_at"])
    if result.ok:
        return
    if location.rollout_failures >= ROLLOUT_PAUSE_AFTER and location.rollout_paused_at is None:
        location.rollout_paused_at = now
        location.rollout_pause_reason = (
            f"{location.rollout_failures} rollout applies failed in a row; last: service {row.service_id}: "
            f"{result.error or 'unknown error'}"
        )[:2000]
        fw._audit(db, fw.Actor.system(), "firewall.rollout_paused", resource_type="location",
                  resource_id=location.location_id, subject_user_id=None,
                  metadata={"failures": location.rollout_failures, "reason": location.rollout_pause_reason})
        logger.warning("[vm_firewall] rollout paused in location %s: %s", location.location_id,
                       location.rollout_pause_reason)


def resume_rollout(db: Session, actor: fw.Actor, location_id: int, *, now: Optional[datetime] = None) -> FirewallLocationSettings:
    """Staff resume a paused rollout. The rows still waiting are staggered again from now, so they
    do not all come due at once after a long pause. The caller commits."""
    fw._require_staff(actor)
    now = now or utcnow()
    location = fw.get_location_settings(db, location_id)
    was = {"paused_at": location.rollout_paused_at.isoformat() if location.rollout_paused_at else None,
           "reason": location.rollout_pause_reason, "failures": location.rollout_failures}
    location.rollout_paused_at = None
    location.rollout_pause_reason = None
    location.rollout_failures = 0
    waiting = sorted(
        row.service_id
        for row in db.execute(
            select(ServiceFirewall).where(
                ServiceFirewall.service_id.in_(sorted(fw._services_in_location(db, location_id)) or [0]),
                ServiceFirewall.rollout_after.is_not(None),
            )
        ).scalars()
    )
    last_due = fw._schedule_rollout(db, waiting, now) if waiting else None
    fw._audit(db, actor, "firewall.rollout_resumed", resource_type="location", resource_id=location_id,
              subject_user_id=None,
              metadata={"before": was, "waiting": len(waiting),
                        "last_due_at": last_due.isoformat() if last_due else None})
    db.flush()
    return location


# Applying one VM


def _hard_reasons(reasons: list[str]) -> list[str]:
    return [reason for reason in reasons if reason not in RENDERED_REASONS]


_HELD_PREFIX = "not eligible: "


def held_reasons(row: ServiceFirewall) -> list[str]:
    """The reasons the worker found when it last held this VM as ineligible (``_hold_ineligible``),
    which can come from its live config and so are not in the stored eligibility; empty otherwise."""
    state = getattr(row.state, "value", row.state)
    if state != FirewallState.UNMANAGED.value or not (row.last_error or "").startswith(_HELD_PREFIX):
        return []
    return [reason.strip() for reason in row.last_error[len(_HELD_PREFIX):].split(",") if reason.strip()]


def _hold_ineligible(db: Session, row: ServiceFirewall, reasons: list[str], now: datetime) -> None:
    """An ineligible VM (infrastructure, no VM record, ...) is never written; the queue looks at it
    again after one reconcile interval, the sweep each interval."""
    row.state = FirewallState.UNMANAGED
    row.last_error = (_HELD_PREFIX + ", ".join(reasons))[:2000]
    row.last_checked_at = now
    row.next_attempt_at = now + timedelta(seconds=reconcile_interval(db))
    db.flush()


def _after_apply(db: Session, row: ServiceFirewall, result: ApplyResult, now: datetime) -> None:
    if result.state == FirewallState.VM_MISSING.value:
        # Terminated VMs end here (section 6.6); the alert reads only ``error`` rows.
        row.next_attempt_at = now + timedelta(seconds=reconcile_interval(db))
    elif result.ok:
        _enforcement_state(db, row)
    _count_rollout_outcome(db, row, result, now)
    db.flush()


async def _client_and_config(db: Session, service: Service, access: ProxmoxAccess):
    client = await access.vm_client(db, service)
    return client, await client.get_config()


async def apply_one(
    db: Session,
    service_id: int,
    access: ProxmoxAccess,
    *,
    now: Optional[datetime] = None,
    held: Optional[Claim] = None,
) -> Optional[ApplyResult]:
    """Check eligibility with the VM config just read, then apply under the ``held`` claim.
    Returns None when nothing was attempted or the claim was lost (the row is left for the next
    tick). Commits."""
    now = now or utcnow()
    row = db.get(ServiceFirewall, service_id)
    service = db.get(Service, service_id)
    if row is None or service is None:
        return None
    try:
        if not row.managed or row.excluded:
            row.state = FirewallState.UNMANAGED
            row.last_checked_at = now
            return None
        hard = _hard_reasons(fw.eligibility(db, service_id).reasons)
        if hard:
            _hold_ineligible(db, row, hard, now)
            _count_rollout_outcome(db, row, None, now)
            return None
        try:
            client, config = await _client_and_config(db, service, access)
        except Exception as exc:  # Proxmox errors, placement errors, transport failures
            result = record_failure(db, row, exc, now=now)
            _after_apply(db, row, result, now)
            return result
        hard = _hard_reasons(fw.eligibility(db, service_id, vm_config=config).reasons)
        if hard:
            _hold_ineligible(db, row, hard, now)
            _count_rollout_outcome(db, row, None, now)
            return None
        result = await apply(db, service_id, client, now=now, vm_config=config, claim=held)
        if result.state == CLAIM_LOST:
            return None
        _after_apply(db, row, result, now)
        return result
    finally:
        db.commit()


class ProvisionFirewallError(RuntimeError):
    pass


def firewall_configured(db: Session, service_id: int) -> bool:
    """Whether ``ApplyFirewallStep`` has work: the VM has a firewall row, or its location has the
    firewall switched on. A location that is off, with or without a settings row, is left as it is
    today, so reading a location's settings (which creates the row) never changes provisioning."""
    if db.get(ServiceFirewall, service_id) is not None:
        return True
    location = _location(db, service_id)
    return location is not None and bool(location.enabled)


async def apply_for_provision(
    db: Session, service_id: int, client: VmFirewallClient, *, owner: str, now: Optional[datetime] = None
) -> Optional[ApplyResult]:
    """The firewall work of ``ApplyFirewallStep`` (section 6.5), run while the VM is off: create the
    row (SMTP block for new VMs), set net0's firewall flag when the location asks for it, and write
    the rendered firewall, ``enable: 0`` included, so a ``.fw`` the clone inherited is replaced.
    Ineligible VMs are left alone (returns None). Raises ``ProvisionFirewallError`` when the write
    fails, so the deployment step is retried. Commits."""
    now = now or utcnow()
    fw.init_for_new_vm(db, service_id, fw.Actor.system())
    db.commit()
    if not claim(db, service_id, owner=owner, now=now):
        raise ProvisionFirewallError("firewall is being applied by the worker; retrying")
    held = Claim.of(db, service_id, owner=owner, clock=claim_clock(now))
    try:
        row = db.get(ServiceFirewall, service_id)
        if not row.managed or row.excluded:
            return None
        try:
            config = await client.get_config()
            hard = _hard_reasons(fw.eligibility(db, service_id, vm_config=config).reasons)
            if hard:
                _hold_ineligible(db, row, hard, now)
                return None
            location = _location(db, service_id)
            if location is not None and location.enabled and location.nic_flag_on_provision:
                if not net_firewall_flag(config.get("net0")):
                    if not held.renew():
                        raise ProvisionFirewallError("firewall is being applied by the worker; retrying")
                    await client.set_net0_firewall_flag(True)
                    config = await client.get_config()
        except ProvisionFirewallError:
            raise
        except Exception as exc:  # Proxmox errors, transport failures
            failure = record_failure(db, row, exc, now=now)
            raise ProvisionFirewallError(f"firewall apply failed: {failure.error or failure.state}") from exc
        result = await apply(db, service_id, client, now=now, vm_config=config, claim=held)
        if result.state == CLAIM_LOST:
            raise ProvisionFirewallError("firewall is being applied by the worker; retrying")
        _after_apply(db, row, result, now)
        if not result.ok:
            raise ProvisionFirewallError(f"firewall apply failed: {result.error or result.state}")
        return result
    finally:
        db.commit()
        unclaim(db, service_id, owner=owner)


# Apply queue (section 6.2, duty 1)


@dataclass
class QueueResult:
    released: list[int] = field(default_factory=list)
    attempted: list[int] = field(default_factory=list)
    applied: list[int] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)


def mark_unmanaged(db: Session) -> None:
    db.execute(
        update(ServiceFirewall)
        .where(
            or_(ServiceFirewall.managed.is_(False), ServiceFirewall.excluded.is_(True)),
            ServiceFirewall.state != FirewallState.UNMANAGED.value,
        )
        .values(state=FirewallState.UNMANAGED.value)
        .execution_options(synchronize_session="fetch")
    )


def due_for_apply(db: Session, *, now: datetime, limit: int = QUEUE_BATCH) -> list[tuple[int, Optional[int]]]:
    """``(service_id, cluster_id)`` of rows waiting for a write: a revision not yet applied, or an
    error whose backoff has passed. Rows in a location that is off and never written are left."""
    naive_now = _naive(now)
    rows = db.execute(
        select(ServiceFirewall.service_id, ServiceVm.proxmox_cluster_id)
        .outerjoin(ServiceVm, ServiceVm.service_id == ServiceFirewall.service_id)
        .where(
            ServiceFirewall.managed.is_(True),
            ServiceFirewall.excluded.is_(False),
            or_(
                ServiceFirewall.applied_revision.is_(None),
                ServiceFirewall.revision != ServiceFirewall.applied_revision,
                ServiceFirewall.state == FirewallState.ERROR.value,
            ),
            or_(ServiceFirewall.next_attempt_at.is_(None), ServiceFirewall.next_attempt_at <= naive_now),
            or_(ServiceFirewall.claimed_until.is_(None), ServiceFirewall.claimed_until <= naive_now),
            or_(ServiceFirewall.applied_revision.is_not(None), _location_enabled_expr().is_(True)),
        )
        .order_by(ServiceFirewall.next_attempt_at.is_not(None), ServiceFirewall.next_attempt_at,
                  ServiceFirewall.service_id)
        .limit(limit)
    ).all()
    return [(service_id, cluster_id) for service_id, cluster_id in rows]


SessionScope = Callable[[], ContextManager[Session]]


def shared_session(db: Session) -> SessionScope:
    @contextmanager
    def scope() -> Iterator[Session]:
        yield db

    return scope


async def process_queue(
    db: Session,
    access: ProxmoxAccess,
    *,
    owner: str,
    now: Optional[datetime] = None,
    session_scope: Optional[SessionScope] = None,
    limit: int = QUEUE_BATCH,
    clock: Optional[Callable[[], datetime]] = None,
) -> QueueResult:
    """One pass of the apply queue: release due rollout rows, then apply what is due, at most
    ``vm_firewall_cluster_concurrency`` VMs of one cluster at a time. Each VM's apply runs in its own
    session from ``session_scope`` (the shared ``db`` when not given)."""
    now = now or utcnow()
    clock = clock or claim_clock(now)
    out = QueueResult()
    out.released = fw.release_due_rollouts(db, now=now)
    mark_unmanaged(db)
    db.commit()
    due = due_for_apply(db, now=now, limit=limit)
    if not due:
        return out
    per_cluster = max(1, int(_setting(db, "vm_firewall_cluster_concurrency")))
    semaphores: dict[Optional[int], asyncio.Semaphore] = {}
    scope = session_scope or shared_session(db)

    async def one(service_id: int, cluster_id: Optional[int]) -> None:
        semaphore = semaphores.setdefault(cluster_id, asyncio.Semaphore(per_cluster))
        async with semaphore:
            with scope() as session:
                if not claim(session, service_id, owner=owner, now=clock()):
                    return
                try:
                    out.attempted.append(service_id)
                    held = Claim.of(session, service_id, owner=owner, clock=clock)
                    result = await apply_one(session, service_id, access, now=now, held=held)
                    if result is not None:
                        (out.applied if result.ok else out.failed).append(service_id)
                except Exception:  # one VM must not stop the queue
                    logger.exception("[vm_firewall] apply of service %s crashed", service_id)
                    session.rollback()
                    out.failed.append(service_id)
                finally:
                    unclaim(session, service_id, owner=owner)

    await asyncio.gather(*(one(service_id, cluster_id) for service_id, cluster_id in due))
    return out


# Drift sweep (section 6.2, duty 2)


@dataclass
class SweepResult:
    checked: list[int] = field(default_factory=list)
    in_sync: list[int] = field(default_factory=list)
    repaired: list[int] = field(default_factory=list)
    reported: list[int] = field(default_factory=list)
    queued: list[int] = field(default_factory=list)
    completed: bool = False


def _sweepable(now: datetime):
    """Rows the sweep checks: managed, written at the current revision, not in error (the queue
    owns those), in an enabled location."""
    return and_(
        ServiceFirewall.managed.is_(True),
        ServiceFirewall.excluded.is_(False),
        ServiceFirewall.applied_revision.is_not(None),
        ServiceFirewall.revision == ServiceFirewall.applied_revision,
        ServiceFirewall.state != FirewallState.ERROR.value,
        or_(ServiceFirewall.claimed_until.is_(None), ServiceFirewall.claimed_until <= _naive(now)),
        _location_enabled_expr().is_(True),
    )


def _due_for_check(now: datetime, interval: int):
    return or_(
        ServiceFirewall.last_checked_at.is_(None),
        ServiceFirewall.last_checked_at <= _naive(now - timedelta(seconds=interval)),
    )


def _drift_repairs_24h(db: Session, service_id: int, now: datetime) -> int:
    return int(
        db.execute(
            select(func.count(UserAuditEvent.id)).where(
                UserAuditEvent.action == "firewall.drift_repaired",
                UserAuditEvent.resource_type == "service",
                UserAuditEvent.resource_id == str(service_id),
                UserAuditEvent.created_at >= _naive(now - timedelta(hours=24)),
            )
        ).scalar()
        or 0
    )


async def sweep_one(
    db: Session,
    service_id: int,
    access: ProxmoxAccess,
    *,
    now: Optional[datetime] = None,
    held: Optional[Claim] = None,
) -> str:
    """Check one VM; a repair runs under the ``held`` claim. Returns what happened: ``in_sync``,
    ``repaired``, ``reported``, ``queued`` (a change the apply queue picks up, such as a moved
    bridge), ``unmanaged``, ``vm_missing``, ``error`` or ``claim_lost`` (a repair stopped because
    another applier took the VM; nothing recorded). Commits."""
    now = now or utcnow()
    row = db.get(ServiceFirewall, service_id)
    service = db.get(Service, service_id)
    if row is None or service is None:
        return "unmanaged"
    try:
        hard = _hard_reasons(fw.eligibility(db, service_id).reasons)
        if hard:
            _hold_ineligible(db, row, hard, now)
            return "unmanaged"
        try:
            client, config = await _client_and_config(db, service, access)
        except Exception as exc:  # Proxmox errors, placement errors, transport failures
            result = record_failure(db, row, exc, now=now)
            _after_apply(db, row, result, now)
            return result.state
        row.last_checked_at = now
        if fw.observe_net0_bridge(row, config):
            # Staff moved the public NIC: the revision is bumped and the queue renders for the new bridge.
            row.next_attempt_at = None
            return "queued"
        hard = _hard_reasons(fw.eligibility(db, service_id, vm_config=config, observe=False).reasons)
        if hard:
            _hold_ineligible(db, row, hard, now)
            return "unmanaged"
        if row.state in (FirewallState.UNMANAGED, FirewallState.VM_MISSING):
            # Eligible again, or the VM is back: write it afresh through the queue.
            fw._bump(row)
            row.state = FirewallState.PENDING
            row.next_attempt_at = None
            return "queued"

        report = await check(db, service_id, client)
        if report.vm_missing:
            result = record_failure(db, row, VmMissing(404, "VM not found on Proxmox", method="GET", path=""), now=now)
            _after_apply(db, row, result, now)
            return "vm_missing"
        if row.drift_repairs_24h:
            row.drift_repairs_24h = _drift_repairs_24h(db, service_id, now)
        if report.in_sync:
            row.nic_flag = report.nic_flag
            enabled = int(render(db, service_id, now=now).options["enable"]) == 1
            if not enabled:
                row.state = FirewallState.OFF
            elif report.nic_flag == "pending_reboot":
                row.state = FirewallState.PENDING_REBOOT
            else:
                row.state = FirewallState.APPLIED
            if row.state != FirewallState.OFF:
                row.last_error = None
            _enforcement_state(db, row)
            return "in_sync"

        if str(_setting(db, "vm_firewall_drift_mode")) != "repair":
            row.state = FirewallState.DRIFT
            row.last_error = ("drift: " + "; ".join(report.differences))[:2000]
            return "reported"
        result = await apply(db, service_id, client, now=now, claim=held)
        if result.state == CLAIM_LOST:
            return CLAIM_LOST
        _after_apply(db, row, result, now)
        if not result.ok:
            return result.state
        fw._audit(db, fw.Actor.system(), "firewall.drift_repaired", resource_type="service", resource_id=service_id,
                  subject_user_id=service.owner_user_id,
                  metadata={"differences": report.differences[:50], "ops": result.ops})
        row.drift_repairs_24h = _drift_repairs_24h(db, service_id, now)
        return "repaired"
    finally:
        db.flush()
        db.commit()


async def drift_sweep(
    db: Session,
    access: ProxmoxAccess,
    *,
    owner: str,
    now: Optional[datetime] = None,
    tick_seconds: int = TICK_SECONDS,
    batch: Optional[int] = None,
    clock: Optional[Callable[[], datetime]] = None,
) -> SweepResult:
    """Check the slice of managed VMs due this tick: enough that each is checked once per
    ``vm_firewall_reconcile_interval_seconds``. ``completed`` is true when nothing due is left."""
    now = now or utcnow()
    clock = clock or claim_clock(now)
    interval = reconcile_interval(db)
    out = SweepResult()
    if batch is None:
        total = db.execute(select(func.count()).select_from(ServiceFirewall).where(_sweepable(now))).scalar() or 0
        batch = max(1, math.ceil(total * tick_seconds / max(1, interval)))
    ids = list(
        db.execute(
            select(ServiceFirewall.service_id)
            .where(_sweepable(now), _due_for_check(now, interval))
            .order_by(ServiceFirewall.last_checked_at.is_not(None), ServiceFirewall.last_checked_at,
                      ServiceFirewall.service_id)
            .limit(batch)
        ).scalars()
    )
    buckets = {"in_sync": out.in_sync, "repaired": out.repaired, "reported": out.reported, "queued": out.queued}
    for service_id in ids:
        if not claim(db, service_id, owner=owner, now=clock()):
            continue
        try:
            held = Claim.of(db, service_id, owner=owner, clock=clock)
            outcome = await sweep_one(db, service_id, access, now=now, held=held)
            out.checked.append(service_id)
            buckets.get(outcome, []).append(service_id)
        except Exception:  # one VM must not stop the sweep
            logger.exception("[vm_firewall] drift check of service %s crashed", service_id)
            db.rollback()
        finally:
            unclaim(db, service_id, owner=owner)
    remaining = db.execute(
        select(func.count()).select_from(ServiceFirewall).where(_sweepable(now), _due_for_check(now, interval))
    ).scalar() or 0
    out.completed = remaining == 0
    return out


# Readiness (section 6.2, duty 3)


def _set_cluster_enforcement(db: Session, cluster_id: int, ready: bool) -> list[int]:
    """Flip the managed VMs of a cluster between enforced states and ``not_enforced``."""
    ids = list(
        db.execute(select(ServiceVm.service_id).where(ServiceVm.proxmox_cluster_id == cluster_id)).scalars()
    )
    if not ids:
        return []
    rows = db.execute(
        select(ServiceFirewall).where(ServiceFirewall.service_id.in_(ids), ServiceFirewall.managed.is_(True))
    ).scalars()
    changed = []
    for row in rows:
        if not ready and row.state in _ENFORCED:
            row.state = FirewallState.NOT_ENFORCED
            changed.append(row.service_id)
        elif ready and row.state == FirewallState.NOT_ENFORCED:
            row.state = FirewallState.PENDING_REBOOT if row.nic_flag == "pending_reboot" else FirewallState.APPLIED
            changed.append(row.service_id)
    db.flush()
    return changed


async def check_readiness(db: Session, access: ProxmoxAccess, *, now: Optional[datetime] = None) -> dict[int, bool]:
    """Readiness of every cluster in an enabled location, cached on the location; VMs on a cluster
    that is not ready become ``not_enforced``. Commits."""
    now = now or utcnow()
    clusters = db.execute(
        select(ProxmoxCluster)
        .join(FirewallLocationSettings, FirewallLocationSettings.location_id == ProxmoxCluster.location_id)
        .where(FirewallLocationSettings.enabled.is_(True))
        .order_by(ProxmoxCluster.id)
    ).scalars().all()
    out: dict[int, bool] = {}
    for cluster in clusters:
        try:
            reader = await access.cluster_reader(db, cluster)
            result = await readiness(reader)
        except Exception:  # unreachable: keep the last cached result
            logger.warning("[vm_firewall] readiness check of cluster %s failed", cluster.id, exc_info=True)
            continue
        fw.record_readiness(db, cluster.location_id, cluster.id, result.to_dict(), checked_at=now)
        _set_cluster_enforcement(db, cluster.id, result.ready)
        out[cluster.id] = result.ready
        db.commit()
    return out


# Client notice after the alert threshold (section 11)


def apply_failed_threshold_minutes(db: Session) -> int:
    from app.services.alerting.catalogue import effective_settings

    return int(effective_settings(db)["vm_firewall_apply_failed"].params["error_after_minutes"])


def notify_apply_failures(db: Session, *, now: Optional[datetime] = None) -> int:
    """Tell opted-in owners once per failing episode that their firewall could not be applied."""
    from app.services.client_notifications import notify_firewall_apply_failed

    now = now or utcnow()
    since = _naive(now - timedelta(minutes=apply_failed_threshold_minutes(db)))
    rows = db.execute(
        select(ServiceFirewall).where(
            ServiceFirewall.state == FirewallState.ERROR.value,
            ServiceFirewall.failing_since.is_not(None),
            ServiceFirewall.failing_since <= since,
        )
    ).scalars().all()
    queued = 0
    for row in rows:
        service = db.get(Service, row.service_id)
        if service is None or service.status == ServiceStatus.TERMINATED:
            continue
        queued += notify_firewall_apply_failed(db, service, row.failing_since)
    db.commit()
    return queued


# One tick


@dataclass
class TickResult:
    queue: QueueResult
    sweep: Optional[SweepResult] = None
    readiness: Optional[dict[int, bool]] = None


async def tick(
    db: Session,
    access: ProxmoxAccess,
    *,
    owner: str,
    now: Optional[datetime] = None,
    session_scope: Optional[SessionScope] = None,
    sweep: bool = True,
) -> TickResult:
    now = now or utcnow()
    clock = claim_clock(now)
    result = TickResult(queue=await process_queue(db, access, owner=owner, now=now, session_scope=session_scope,
                                                  clock=clock))
    if not sweep:
        _write_status(db, last_tick_at=now, owner=owner)
        db.commit()
        return result
    result.sweep = await drift_sweep(db, access, owner=owner, now=now, clock=clock)
    status = read_status(db)
    interval = reconcile_interval(db)
    last_readiness = status_time(status, "last_readiness_at")
    values: dict[str, Any] = {"last_tick_at": now, "owner": owner}
    if last_readiness is None or (now - last_readiness).total_seconds() >= interval:
        result.readiness = await check_readiness(db, access, now=now)
        notify_apply_failures(db, now=now)
        values["last_readiness_at"] = now
    if result.sweep.completed:
        values["last_sweep_at"] = now
    _write_status(db, **values)
    db.commit()
    return result


__all__ = [
    "Claim",
    "LEASE_NAME",
    "PluginAccess",
    "ProxmoxAccess",
    "QueueResult",
    "SweepResult",
    "TickResult",
    "apply_one",
    "check_readiness",
    "claim",
    "drift_sweep",
    "due_for_apply",
    "notify_apply_failures",
    "process_queue",
    "read_status",
    "renew",
    "resume_rollout",
    "sweep_one",
    "tick",
    "unclaim",
]
