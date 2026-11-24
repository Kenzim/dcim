"""Detectors: read recorded state and return the conditions that are true now.

Each detector takes ``(db, params, now)`` and returns a list of ``Finding``.
``now`` is a naive UTC datetime. Detectors only read; they never change state.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.dao.runner_dao import RunnerDAO
from app.dao.storage_dao import StorageDAO
from app.models.alerting import AlertSeverity, NotificationDelivery, VmBackupRun
from app.models.boot_task import BootTask, BootTaskStatus
from app.models.commerce_email import EmailMessage, EmailMessageStatus
from app.models.installation_task import InstallationStatus, InstallationTask
from app.models.network_switch import NetworkSwitch
from app.models.pdu import Pdu
from app.models.power_feed import PowerFeed
from app.models.private_network import AttachmentState, PrivateNetworkAttachment
from app.models.proxy_runner import ProxyRunner
from app.models.reseller import NotificationOutbox, NotificationOutboxStatus
from app.models.runner import Runner
from app.models.service import Service, ServiceStatus
from app.models.storage import StorageEvent, StorageLocation, StorageNode, StorageShare, StorageTenant
from app.models.switch_port import SwitchPort
from app.models.vm_deployment_job import DeploymentJobStatus, VMDeploymentJob
from app.services.alerting.billing_integrity import BILLING_DETECTORS
from app.services.alerting.catalogue import Finding, naive
from app.services.alerting.scrub import scrub
from app.services import storage_ha, storage_location

logger = logging.getLogger(__name__)

Detector = Callable[[Session, dict, datetime], "list[Finding]"]


def _age_seconds(value: datetime | None, now: datetime) -> float | None:
    value = naive(value)
    return None if value is None else (now - value).total_seconds()


def _human(seconds: float | None) -> str:
    if seconds is None:
        return "an unknown time"
    seconds = int(seconds)
    if seconds < 120:
        return f"{seconds} seconds"
    if seconds < 7200:
        return f"{seconds // 60} minutes"
    if seconds < 172800:
        return f"{seconds // 3600} hours"
    return f"{seconds // 86400} days"


def _iso(value: datetime | None) -> str | None:
    value = naive(value)
    return value.isoformat() + "Z" if value else None


# --------------------------------------------------------------------------- storage


def storage_node_offline(db: Session, params: dict, now: datetime) -> list[Finding]:
    limit = params["offline_after_seconds"]
    out = []
    for node in db.query(StorageNode).filter(StorageNode.enabled.is_(True)).all():
        age = _age_seconds(node.last_seen_at or node.created_at, now)
        if age is None or age < limit:
            continue
        seen = "never contacted Rackflow" if node.last_seen_at is None else f"last contact {_human(age)} ago"
        out.append(
            Finding(
                "storage_node_offline",
                "storage_node",
                str(node.id),
                node.name,
                f"Storage node {node.name} is offline",
                f"The storage runner on {node.name} has {seen}.",
                details={"last_seen_at": _iso(node.last_seen_at), "age_seconds": int(age)},
            )
        )
    return out


def _node_online(node: StorageNode, now: datetime, limit: float) -> bool:
    age = _age_seconds(node.last_seen_at, now)
    return age is not None and age < limit


def storage_node_ceph(db: Session, params: dict, now: datetime) -> list[Finding]:
    out = []
    for node in db.query(StorageNode).filter(StorageNode.enabled.is_(True)).all():
        if node.ceph_ok is False and _node_online(node, now, 180):
            out.append(
                Finding(
                    "storage_node_ceph",
                    "storage_node",
                    str(node.id),
                    node.name,
                    f"Ceph is unhealthy on storage node {node.name}",
                    "The storage runner reports that CephFS is not healthy on this node.",
                    details={"error": scrub(node.last_error)},
                )
            )
    return out


def storage_node_error(db: Session, params: dict, now: datetime) -> list[Finding]:
    limit = params["offline_after_seconds"]
    out = []
    for node in db.query(StorageNode).filter(StorageNode.enabled.is_(True)).all():
        if not _node_online(node, now, limit):
            continue
        problems = []
        if node.last_error:
            problems.append(f"runner error: {scrub(node.last_error)}")
        if node.sftp_enabled and node.sftp_listening is False and (node.sftp_master or not storage_ha.sftp_uses_vip(db, node)):
            problems.append("the SFTP gateway is not listening")
        if node.sftp_enabled and node.sftp_error:
            problems.append(f"SFTP error: {scrub(node.sftp_error)}")
        if problems:
            out.append(
                Finding(
                    "storage_node_error",
                    "storage_node",
                    str(node.id),
                    node.name,
                    f"Storage node {node.name} reports a problem",
                    "; ".join(problems),
                    details={"problems": problems},
                )
            )
    return out


def storage_sftp_no_master(db: Session, params: dict, now: datetime) -> list[Finding]:
    """A location whose SFTP is behind a virtual IP has no online node reporting to be the master.

    The time without a master counts from the latest of: the location settings change, the last
    SFTP master event of the location and the last report of a node still marked master.
    """
    limit = params["no_master_after_seconds"]
    out = []
    rows = (
        db.query(StorageLocation)
        .filter(StorageLocation.sftp_enabled.is_(True), StorageLocation.sftp_virtual_ip.isnot(None))
        .all()
    )
    for row in rows:
        if not storage_ha.ha_active(db, row.location_id):
            continue
        nodes = [node for node in storage_location.location_nodes(db, row.location_id) if node.enabled]
        if any(node.sftp_master and StorageDAO.is_online(node, now=now) for node in nodes):
            continue
        last_event = (
            db.query(func.max(StorageEvent.at))
            .filter(StorageEvent.location_id == row.location_id, StorageEvent.kind == "sftp_master")
            .scalar()
        )
        marks = [naive(row.updated_at), naive(last_event)]
        marks += [naive(node.last_seen_at) for node in nodes if node.sftp_master]
        since = max((mark for mark in marks if mark is not None), default=None)
        age = _age_seconds(since, now)
        if age is not None and age < limit:
            continue
        label = row.location.name if row.location is not None else f"location {row.location_id}"
        out.append(
            Finding(
                "storage_sftp_no_master",
                "storage_location",
                str(row.location_id),
                label,
                f"SFTP in {label} has no master node",
                f"No storage node has held the SFTP virtual IP {row.sftp_virtual_ip} for {_human(age)}, "
                "so SFTP is down for every customer in the location.",
                details={"virtual_ip": row.sftp_virtual_ip, "nodes": [node.id for node in nodes]},
            )
        )
    return out


def storage_tenant_error(db: Session, params: dict, now: datetime) -> list[Finding]:
    min_age = params["min_age_seconds"]
    unserved_after = params.get("unserved_after_seconds", 120)
    out = []
    tenants = (
        db.query(StorageTenant, Service)
        .join(Service, Service.id == StorageTenant.service_id)
        .filter(StorageTenant.terminated_at.is_(None), Service.status != ServiceStatus.TERMINATED)
        .all()
    )
    bad_shares: dict[int, list[StorageShare]] = {}
    for share in (
        db.query(StorageShare)
        .filter(StorageShare.runner_state == "error")
        .all()
    ):
        if getattr(share.state, "value", share.state) == "active":
            bad_shares.setdefault(share.tenant_id, []).append(share)
    ha_by_location: dict[int | None, bool] = {}
    for tenant, service in tenants:
        problems = []
        if tenant.runner_state == "error" or tenant.runner_error:
            problems.append(f"runner: {scrub(tenant.runner_error) or 'error state'}")
        if tenant.sftp_state == "error" or tenant.sftp_error:
            problems.append(f"SFTP: {scrub(tenant.sftp_error) or 'error state'}")
        for share in bad_shares.get(tenant.id, []):
            problems.append(f"share {share.name}: {scrub(share.runner_error) or 'error state'}")
        age = _age_seconds(tenant.updated_at, now)
        if age is not None and age < min_age:
            problems = []
        if (
            service.status == ServiceStatus.ACTIVE
            and tenant.node_id is not None
            and tenant.network_id is not None
            and tenant.current_node_id is None
        ):
            if tenant.location_id not in ha_by_location:
                ha_by_location[tenant.location_id] = storage_ha.ha_active(db, tenant.location_id)
            unserved = _age_seconds(tenant.current_since or tenant.updated_at, now)
            if ha_by_location[tenant.location_id] and unserved is not None and unserved >= unserved_after:
                problems.append(f"no node has served it for {_human(unserved)}")
        if not problems:
            continue
        out.append(
            Finding(
                "storage_tenant_error",
                "storage_tenant",
                str(tenant.id),
                service.name,
                f"Storage service {service.name} is in error",
                "; ".join(problems),
                details={"service_id": service.id, "problems": problems},
            )
        )
    return out


def storage_tenant_failover(db: Session, params: dict, now: datetime) -> list[Finding]:
    out = []
    tenants = (
        db.query(StorageTenant, Service)
        .join(Service, Service.id == StorageTenant.service_id)
        .filter(
            StorageTenant.terminated_at.is_(None),
            Service.status != ServiceStatus.TERMINATED,
            StorageTenant.current_node_id.isnot(None),
            StorageTenant.last_failover_at.isnot(None),
        )
        .all()
    )
    for tenant, service in tenants:
        if tenant.current_node_id == tenant.node_id:
            continue
        failed_over = naive(tenant.last_failover_at)
        since = naive(tenant.current_since)
        if since is not None and failed_over < since:
            continue
        current, preferred = tenant.current_node, tenant.node
        out.append(
            Finding(
                "storage_tenant_failover",
                "storage_tenant",
                str(tenant.id),
                service.name,
                f"Storage service {service.name} failed over to another node",
                f"It is served by {current.name if current else 'another node'} instead of its preferred node "
                f"{preferred.name if preferred else 'unknown'} since {_human(_age_seconds(failed_over, now))} ago.",
                details={
                    "service_id": service.id,
                    "current_node_id": tenant.current_node_id,
                    "preferred_node_id": tenant.node_id,
                    "failed_over_at": _iso(failed_over),
                },
            )
        )
    return out


def storage_location_degraded(db: Session, params: dict, now: datetime) -> list[Finding]:
    out = []
    by_location: dict[int, list[StorageNode]] = {}
    for node in db.query(StorageNode).filter(StorageNode.enabled.is_(True)).all():
        location_id = storage_location.node_location_id(node)
        if location_id is not None:
            by_location.setdefault(location_id, []).append(node)
    for location_id, nodes in sorted(by_location.items()):
        healthy = [node for node in nodes if storage_ha.node_healthy(node, now)]
        if len(nodes) < 2 or len(healthy) >= 2:
            continue
        server = nodes[0].server
        label = server.location.name if server is not None and server.location is not None else f"location {location_id}"
        out.append(
            Finding(
                "storage_location_degraded",
                "storage_location",
                str(location_id),
                label,
                f"Storage in {label} has fewer than two healthy nodes",
                f"{len(healthy)} of {len(nodes)} storage nodes are healthy (online, Ceph healthy, lease client "
                "healthy, not fenced, not draining), so another failure would stop storage.",
                details={
                    "healthy_nodes": [n.id for n in healthy],
                    "unhealthy_nodes": [n.id for n in nodes if n not in healthy],
                },
            )
        )
    return out


# --------------------------------------------------------------------------- runners


def proxy_runner_offline(db: Session, params: dict, now: datetime) -> list[Finding]:
    limit = params["offline_after_seconds"]
    out = []
    for runner in db.query(ProxyRunner).filter(ProxyRunner.enabled.is_(True)).all():
        age = _age_seconds(runner.last_seen_at or runner.created_at, now)
        if age is None or age < limit:
            continue
        seen = "never contacted Rackflow" if runner.last_seen_at is None else f"last contact {_human(age)} ago"
        out.append(
            Finding(
                "proxy_runner_offline",
                "proxy_runner",
                str(runner.id),
                runner.name,
                f"Proxy runner {runner.name} is offline",
                f"The proxy runner has {seen}.",
                details={"last_seen_at": _iso(runner.last_seen_at), "age_seconds": int(age)},
            )
        )
    return out


def _runner_connected(runner: Runner, now: datetime) -> bool:
    from app.services.runners import cache as runner_cache
    from app.services.runners.hub import get_hub

    if get_hub().is_connected(runner.id):
        return True
    try:
        if runner_cache.redis_online(runner.id):
            return True
    except Exception:  # noqa: BLE001 - Redis down: fall back to the stored heartbeat
        logger.debug("Redis unavailable while checking runner %s", runner.id)
    return RunnerDAO.is_online(runner, now=now)


def runner_offline(db: Session, params: dict, now: datetime) -> list[Finding]:
    limit = params["offline_after_seconds"]
    out = []
    for runner in db.query(Runner).filter(Runner.enabled.is_(True)).all():
        if _runner_connected(runner, now):
            continue
        age = _age_seconds(runner.last_seen_at or runner.created_at, now)
        if age is None or age < limit:
            continue
        roles = ", ".join(runner.capabilities or []) or "runner"
        seen = "never connected" if runner.last_seen_at is None else f"last contact {_human(age)} ago"
        out.append(
            Finding(
                "runner_offline",
                "runner",
                str(runner.id),
                f"{runner.name} ({roles})",
                f"Runner {runner.name} ({roles}) is offline",
                f"The runner has {seen}.",
                details={
                    "capabilities": list(runner.capabilities or []),
                    "location_id": runner.location_id,
                    "last_seen_at": _iso(runner.last_seen_at),
                    "age_seconds": int(age),
                },
            )
        )
    return out


# --------------------------------------------------------------------------- power


def pdu_poll_failed(db: Session, params: dict, now: datetime) -> list[Finding]:
    limit = params["stale_after_seconds"]
    out = []
    for pdu in db.query(Pdu).filter(Pdu.enabled.is_(True)).all():
        polled = naive(pdu.last_polled_at)
        if polled is None:
            if not pdu.last_poll_error:
                continue
            age = _age_seconds(pdu.created_at, now)
            if age is None or age < limit:
                continue
        else:
            age = _age_seconds(polled, now)
            if age < limit:
                continue
        reason = f"; last error: {scrub(pdu.last_poll_error)}" if pdu.last_poll_error else ""
        since = "has never been polled successfully" if polled is None else f"was last polled {_human(age)} ago"
        out.append(
            Finding(
                "pdu_poll_failed",
                "pdu",
                str(pdu.id),
                pdu.name,
                f"PDU {pdu.name} is not being polled",
                f"The PDU {since}{reason}.",
                details={"last_polled_at": _iso(polled), "error": scrub(pdu.last_poll_error)},
            )
        )
    return out


def _feeds_with_current(db: Session):
    from app.services.power_service import feed_summary

    for feed in db.query(PowerFeed).all():
        summary = feed_summary(feed)
        if summary["current_a"] is None:
            continue
        yield feed, summary


def _feed_label(feed: PowerFeed) -> str:
    rack = feed.rack.name if feed.rack is not None else "unplaced"
    return f"{feed.name} (rack {rack})"


def power_feed_near_limit(db: Session, params: dict, now: datetime) -> list[Finding]:
    ratio = params["warn_percent"] / 100.0
    out = []
    for feed, summary in _feeds_with_current(db):
        usable = summary["usable_current_a"]
        current = summary["current_a"]
        if usable and ratio * usable <= current <= usable:
            out.append(
                Finding(
                    "power_feed_near_limit",
                    "power_feed",
                    str(feed.id),
                    _feed_label(feed),
                    f"Power feed {feed.name} is near its limit",
                    f"Drawing {current:.1f} A of a usable {usable:.1f} A "
                    f"({current / usable * 100:.0f}% of the limit).",
                    details={"current_a": current, "usable_current_a": usable},
                )
            )
    return out


def power_feed_over_limit(db: Session, params: dict, now: datetime) -> list[Finding]:
    out = []
    for feed, summary in _feeds_with_current(db):
        if summary["over_limit"]:
            usable = summary["usable_current_a"]
            current = summary["current_a"]
            out.append(
                Finding(
                    "power_feed_over_limit",
                    "power_feed",
                    str(feed.id),
                    _feed_label(feed),
                    f"Power feed {feed.name} is over its limit",
                    f"Drawing {current:.1f} A against a usable {usable:.1f} A "
                    f"({feed.max_utilisation_percent}% of the {feed.amperage} A breaker).",
                    details={"current_a": current, "usable_current_a": usable},
                )
            )
    return out


# --------------------------------------------------------------------------- switches


def switch_unreachable(db: Session, params: dict, now: datetime) -> list[Finding]:
    limit = params["stale_after_seconds"]
    newest = dict(
        db.query(SwitchPort.switch_id, func.max(SwitchPort.status_updated_at))
        .group_by(SwitchPort.switch_id)
        .all()
    )
    out = []
    for switch in db.query(NetworkSwitch).filter(NetworkSwitch.enabled.is_(True)).all():
        seen = newest.get(switch.id)
        if seen is None:
            continue
        age = _age_seconds(seen, now)
        if age is not None and age >= limit:
            out.append(
                Finding(
                    "switch_unreachable",
                    "switch",
                    str(switch.id),
                    switch.name,
                    f"Switch {switch.name} is not reporting",
                    f"No port status has been stored for {_human(age)}; the SNMP poller cannot reach it "
                    "or is not running.",
                    details={"last_status_at": _iso(seen), "age_seconds": int(age)},
                )
            )
    return out


def switch_vpc_inconsistent(db: Session, params: dict, now: datetime) -> list[Finding]:
    switches = {s.id: s for s in db.query(NetworkSwitch).filter(NetworkSwitch.enabled.is_(True)).all()}
    out = []
    for switch in switches.values():
        problem = None
        if switch.vpc_error:
            problem = scrub(switch.vpc_error, 400)
        elif switch.vpc_peer_switch_id:
            peer = switches.get(switch.vpc_peer_switch_id)
            if peer is None:
                problem = "the recorded vPC peer is missing or disabled"
            elif peer.vpc_peer_switch_id != switch.id:
                problem = f"{peer.name} does not name this switch as its vPC peer"
            elif peer.vpc_domain_id != switch.vpc_domain_id:
                problem = (
                    f"vPC domain {switch.vpc_domain_id} here but {peer.vpc_domain_id} on {peer.name}"
                )
        if problem:
            out.append(
                Finding(
                    "switch_vpc_inconsistent",
                    "switch",
                    str(switch.id),
                    switch.name,
                    f"vPC pairing of switch {switch.name} is inconsistent",
                    problem,
                    details={"domain_id": switch.vpc_domain_id, "detected_at": _iso(switch.vpc_detected_at)},
                )
            )
    return out


# --------------------------------------------------------------------------- jobs and tasks


def _latest_job_ids(db: Session, service_ids: list[int]) -> dict[int, int]:
    if not service_ids:
        return {}
    rows = (
        db.query(VMDeploymentJob.service_id, func.max(VMDeploymentJob.id))
        .filter(VMDeploymentJob.service_id.in_(service_ids))
        .group_by(VMDeploymentJob.service_id)
        .all()
    )
    return dict(rows)


def deployment_job_failed(db: Session, params: dict, now: datetime) -> list[Finding]:
    since = now - timedelta(hours=params["lookback_hours"])
    jobs = (
        db.query(VMDeploymentJob)
        .filter(VMDeploymentJob.status == DeploymentJobStatus.FAILED)
        .filter(func.coalesce(VMDeploymentJob.finished_at, VMDeploymentJob.updated_at) >= since)
        .order_by(VMDeploymentJob.id)
        .limit(500)
        .all()
    )
    latest = _latest_job_ids(db, [j.service_id for j in jobs])
    out = []
    for job in jobs:
        service = job.service
        if latest.get(job.service_id) != job.id:
            continue
        if service is None or service.status == ServiceStatus.TERMINATED:
            continue
        out.append(
            Finding(
                "deployment_job_failed",
                "deployment_job",
                str(job.id),
                f"{service.name} ({job.strategy_name})",
                f"Deployment job failed for {service.name}",
                f"{job.strategy_name} failed: {scrub(job.error_message) or 'no error recorded'}",
                details={
                    "service_id": job.service_id,
                    "job_id": job.id,
                    "strategy": job.strategy_name,
                    "attempt": job.attempt,
                    "finished_at": _iso(job.finished_at),
                },
            )
        )
    return out


def deployment_job_stuck(db: Session, params: dict, now: datetime) -> list[Finding]:
    queued_after = timedelta(minutes=params["queued_after_minutes"])
    retry_after = timedelta(minutes=params["retrying_after_minutes"])
    waiting_after = timedelta(hours=params["waiting_after_hours"])
    running_after = timedelta(minutes=params["running_after_minutes"])
    active = (
        DeploymentJobStatus.QUEUED,
        DeploymentJobStatus.WAITING,
        DeploymentJobStatus.RUNNING,
    )
    out = []
    for job in db.query(VMDeploymentJob).filter(VMDeploymentJob.status.in_(active)).limit(1000).all():
        service = job.service
        if service is None or service.status == ServiceStatus.TERMINATED:
            continue
        created = naive(job.created_at) or now
        started = naive(job.started_at) or created
        reason = None
        if job.status == DeploymentJobStatus.QUEUED:
            if now - created >= queued_after:
                reason = f"queued for {_human((now - created).total_seconds())} and no deployment worker has claimed it"
        elif job.status == DeploymentJobStatus.WAITING:
            if job.error_message and now - started >= retry_after:
                reason = (
                    f"retrying a failing step for {_human((now - started).total_seconds())}: "
                    f"{scrub(job.error_message)}"
                )
            elif not job.error_message and now - started >= waiting_after:
                reason = f"waiting for {_human((now - started).total_seconds())}"
        else:
            locked = naive(job.locked_at)
            if locked is not None and now - locked >= running_after:
                reason = f"marked running but its lease was last renewed {_human((now - locked).total_seconds())} ago"
        if reason is None:
            continue
        out.append(
            Finding(
                "deployment_job_stuck",
                "deployment_job",
                str(job.id),
                f"{service.name} ({job.strategy_name})",
                f"Deployment job for {service.name} is stuck",
                f"{job.strategy_name}: {reason}.",
                details={
                    "service_id": job.service_id,
                    "job_id": job.id,
                    "strategy": job.strategy_name,
                    "status": getattr(job.status, "value", job.status),
                },
            )
        )
    return out


def boot_task_stuck(db: Session, params: dict, now: datetime) -> list[Finding]:
    newest = now - timedelta(minutes=params["pending_after_minutes"])
    oldest = now - timedelta(hours=params["max_age_hours"])
    tasks = (
        db.query(BootTask)
        .filter(BootTask.status == BootTaskStatus.PENDING, BootTask.pxe_nonce_used_at.is_(None))
        .filter(BootTask.created_at <= newest, BootTask.created_at >= oldest)
        .limit(500)
        .all()
    )
    out = []
    for task in tasks:
        name = task.server.name if task.server is not None else f"server {task.server_id}"
        age = _age_seconds(task.created_at, now)
        out.append(
            Finding(
                "boot_task_stuck",
                "boot_task",
                str(task.id),
                name,
                f"Boot task for {name} is stuck",
                f"The boot task has been pending for {_human(age)} and the server has not fetched its boot script.",
                details={"server_id": task.server_id, "boot_type": getattr(task.boot_type, "value", task.boot_type)},
            )
        )
    return out


def install_failed(db: Session, params: dict, now: datetime) -> list[Finding]:
    since = now - timedelta(hours=params["lookback_hours"])
    tasks = (
        db.query(InstallationTask)
        .filter(InstallationTask.status == InstallationStatus.FAILED)
        .filter(func.coalesce(InstallationTask.completed_at, InstallationTask.created_at) >= since)
        .limit(500)
        .all()
    )
    latest = dict(
        db.query(InstallationTask.server_id, func.max(InstallationTask.id))
        .filter(InstallationTask.server_id.in_([t.server_id for t in tasks] or [0]))
        .group_by(InstallationTask.server_id)
        .all()
    )
    out = []
    for task in tasks:
        if latest.get(task.server_id) != task.id:
            continue
        name = task.server.name if task.server is not None else f"server {task.server_id}"
        out.append(
            Finding(
                "install_failed",
                "install_task",
                str(task.id),
                name,
                f"OS install failed on {name}",
                f"Template {task.template_id}: {scrub(task.error_message) or 'no error recorded'}",
                details={"server_id": task.server_id, "template_id": task.template_id},
            )
        )
    return out


def install_stuck(db: Session, params: dict, now: datetime) -> list[Finding]:
    limit = now - timedelta(minutes=params["stuck_after_minutes"])
    tasks = (
        db.query(InstallationTask)
        .filter(InstallationTask.status == InstallationStatus.IN_PROGRESS)
        .filter(func.coalesce(InstallationTask.started_at, InstallationTask.created_at) <= limit)
        .limit(500)
        .all()
    )
    out = []
    for task in tasks:
        name = task.server.name if task.server is not None else f"server {task.server_id}"
        age = _age_seconds(task.started_at or task.created_at, now)
        out.append(
            Finding(
                "install_stuck",
                "install_task",
                str(task.id),
                name,
                f"OS install on {name} is stuck",
                f"The install has been in progress for {_human(age)} ({task.progress_percent or 0}% reported).",
                details={"server_id": task.server_id, "progress_percent": task.progress_percent},
            )
        )
    return out


def private_network_error(db: Session, params: dict, now: datetime) -> list[Finding]:
    pending_limit = now - timedelta(minutes=params["min_age_minutes"])
    out = []
    for att in db.query(PrivateNetworkAttachment).all():
        reason = None
        if att.state == AttachmentState.ERROR:
            reason = f"attach failed: {scrub(att.last_error) or 'no error recorded'}"
        elif att.state == AttachmentState.DETACHING and att.last_error:
            reason = f"detach keeps failing: {scrub(att.last_error)}"
        elif att.state == AttachmentState.PENDING and (naive(att.created_at) or now) <= pending_limit:
            reason = "still pending and not applied"
        if reason is None:
            continue
        network = att.network.name if att.network is not None else f"network {att.network_id}"
        out.append(
            Finding(
                "private_network_error",
                "private_network_attachment",
                str(att.id),
                network,
                f"Private network attachment problem on {network}",
                reason,
                details={
                    "network_id": att.network_id,
                    "service_id": att.service_id,
                    "server_id": att.server_id,
                    "state": getattr(att.state, "value", att.state),
                },
            )
        )
    return out


def vm_backup_failed(db: Session, params: dict, now: datetime) -> list[Finding]:
    since = now - timedelta(hours=params["lookback_hours"])
    runs = (
        db.query(VmBackupRun)
        .filter(VmBackupRun.status == "failed", VmBackupRun.finished_at >= since)
        .all()
    )
    latest = dict(
        db.query(VmBackupRun.service_id, func.max(VmBackupRun.id))
        .filter(VmBackupRun.service_id.in_([r.service_id for r in runs] or [0]))
        .group_by(VmBackupRun.service_id)
        .all()
    )
    out = []
    for run in runs:
        if latest.get(run.service_id) != run.id:
            continue
        service = db.get(Service, run.service_id)
        if service is None or service.status == ServiceStatus.TERMINATED:
            continue
        out.append(
            Finding(
                "vm_backup_failed",
                "service",
                str(service.id),
                service.name,
                f"Backup failed for {service.name}",
                f"The backup task failed: {scrub(run.error) or 'no error recorded'}",
                details={"service_id": service.id, "finished_at": _iso(run.finished_at)},
            )
        )
    return out


def client_storage_quota(db: Session, params: dict, now: datetime) -> list[Finding]:
    """Client-audience: a share past the warning share of its quota (owner is told if opted in)."""
    ratio = params["quota_percent"] / 100.0
    rows = (
        db.query(StorageShare, StorageTenant, Service)
        .join(StorageTenant, StorageTenant.id == StorageShare.tenant_id)
        .join(Service, Service.id == StorageTenant.service_id)
        .filter(
            StorageTenant.terminated_at.is_(None),
            Service.owner_user_id.isnot(None),
            Service.status != ServiceStatus.TERMINATED,
            StorageShare.used_bytes.isnot(None),
            StorageShare.quota_bytes > 0,
        )
        .all()
    )
    out = []
    for share, _tenant, service in rows:
        if getattr(share.state, "value", share.state) != "active":
            continue
        if share.used_bytes < share.quota_bytes * ratio:
            continue
        percent = min(999, int(share.used_bytes * 100 // share.quota_bytes))
        out.append(
            Finding(
                "client_storage_quota",
                "storage_share",
                str(share.id),
                f"{service.name}/{share.name}",
                f"Storage share {share.name} is {percent}% full",
                "",
                details={
                    "service_id": service.id,
                    "service_name": service.name,
                    "share_name": share.name,
                    "percent": percent,
                },
                owner_user_id=service.owner_user_id,
            )
        )
    return out


# --------------------------------------------------------------------------- VM firewall


def _firewall_service_label(db: Session, service_id: int) -> tuple[str, Service | None]:
    service = db.get(Service, service_id)
    return (service.name if service is not None else f"service {service_id}"), service


def vm_firewall_apply_failed(db: Session, params: dict, now: datetime) -> list[Finding]:
    from app.models.vm_firewall import FirewallState, ServiceFirewall

    since = now - timedelta(minutes=params["error_after_minutes"])
    rows = (
        db.query(ServiceFirewall)
        .filter(
            ServiceFirewall.state == FirewallState.ERROR.value,
            ServiceFirewall.failing_since.isnot(None),
            ServiceFirewall.failing_since <= since,
        )
        .all()
    )
    out = []
    for row in rows:
        label, service = _firewall_service_label(db, row.service_id)
        if service is not None and service.status == ServiceStatus.TERMINATED:
            continue
        out.append(
            Finding(
                "vm_firewall_apply_failed",
                "service",
                str(row.service_id),
                label,
                f"Firewall of {label} not applied for {_human(_age_seconds(row.failing_since, now))}",
                scrub(row.last_error) or "no error recorded",
                details={"service_id": row.service_id, "failing_since": _iso(row.failing_since),
                         "revision": row.revision, "applied_revision": row.applied_revision},
            )
        )
    return out


def vm_firewall_drift_repeated(db: Session, params: dict, now: datetime) -> list[Finding]:
    from app.models.vm_firewall import ServiceFirewall

    threshold = params["repairs_threshold"]
    rows = db.query(ServiceFirewall).filter(ServiceFirewall.drift_repairs_24h > threshold).all()
    out = []
    for row in rows:
        label, _service = _firewall_service_label(db, row.service_id)
        out.append(
            Finding(
                "vm_firewall_drift_repeated",
                "service",
                str(row.service_id),
                label,
                f"Firewall of {label} repaired {row.drift_repairs_24h} times in 24 hours",
                "Something keeps changing this VM's firewall on Proxmox. Set the firewall to unmanaged "
                "if hand-made rules are needed.",
                details={"service_id": row.service_id, "repairs": row.drift_repairs_24h},
            )
        )
    return out


def vm_firewall_cluster_not_ready(db: Session, params: dict, now: datetime) -> list[Finding]:
    from app.models.proxmox_inventory import ProxmoxCluster
    from app.models.vm_firewall import FirewallLocationSettings

    out = []
    for location in db.query(FirewallLocationSettings).filter(FirewallLocationSettings.enabled.is_(True)).all():
        clusters = ((location.readiness or {}).get("clusters") or {})
        for cluster_id, entry in sorted(clusters.items()):
            if not isinstance(entry, dict) or entry.get("ready", True):
                continue
            cluster = db.get(ProxmoxCluster, int(cluster_id)) if str(cluster_id).isdigit() else None
            if cluster is None or cluster.location_id != location.location_id:
                continue
            reasons = [str(reason) for reason in entry.get("reasons") or []]
            out.append(
                Finding(
                    "vm_firewall_cluster_not_ready",
                    "proxmox_cluster",
                    str(cluster.id),
                    cluster.name,
                    f"VM firewalls not enforced on cluster {cluster.name}",
                    "; ".join(reasons) or "readiness check failed",
                    details={"cluster_id": cluster.id, "location_id": location.location_id, "reasons": reasons,
                             "checked_at": _iso(location.readiness_checked_at)},
                )
            )
    return out


def vm_firewall_worker_stale(db: Session, params: dict, now: datetime) -> list[Finding]:
    from app.models.vm_firewall import FirewallLocationSettings
    from app.services.vm_firewall import reconcile

    if not db.query(FirewallLocationSettings).filter(FirewallLocationSettings.enabled.is_(True)).first():
        return []
    interval = reconcile.reconcile_interval(db)
    limit = interval * params["intervals"]
    status = reconcile.read_status(db)
    last = naive(reconcile.status_time(status, "last_sweep_at"))
    age = _age_seconds(last, now)
    if age is not None and age <= limit:
        return []
    return [
        Finding(
            "vm_firewall_worker_stale",
            "system",
            "vm_firewall_worker",
            "VM firewall worker",
            "VM firewall worker has not completed a drift sweep",
            f"Last completed sweep: {_human(age) + ' ago' if age is not None else 'never'} "
            f"(reconcile interval {interval} seconds). Is the worker running?",
            details={"last_sweep_at": _iso(last), "last_tick_at": status.get("last_tick_at"),
                     "owner": status.get("owner")},
        )
    ]


# --------------------------------------------------------------------------- notifications


def notification_delivery_failing(db: Session, params: dict, now: datetime) -> list[Finding]:
    window = now - timedelta(minutes=params["window_minutes"])
    stale = now - timedelta(minutes=params["queued_after_minutes"])
    threshold = params["failed_threshold"]
    problems: list[str] = []

    failed_outbox = (
        db.query(func.count(NotificationOutbox.id))
        .filter(NotificationOutbox.status == NotificationOutboxStatus.FAILED, NotificationOutbox.updated_at >= window)
        .scalar()
        or 0
    )
    failed_mail = (
        db.query(func.count(EmailMessage.id))
        .filter(EmailMessage.status == EmailMessageStatus.FAILED, EmailMessage.updated_at >= window)
        .scalar()
        or 0
    )
    dead_alerts = (
        db.query(func.count(NotificationDelivery.id))
        .filter(
            NotificationDelivery.audience == "staff",
            NotificationDelivery.status.in_(("dead", "failed")),
            NotificationDelivery.updated_at >= window,
        )
        .scalar()
        or 0
    )
    if failed_outbox >= threshold:
        problems.append(f"{failed_outbox} billing notifications failed to send")
    if failed_mail >= threshold:
        problems.append(f"{failed_mail} account emails failed to send")
    if dead_alerts >= threshold:
        problems.append(f"{dead_alerts} alert notices failed or gave up")

    if settings.smtp_enabled:
        stuck_outbox = (
            db.query(func.count(NotificationOutbox.id))
            .filter(NotificationOutbox.status == NotificationOutboxStatus.QUEUED, NotificationOutbox.created_at <= stale)
            .scalar()
            or 0
        )
        stuck_mail = (
            db.query(func.count(EmailMessage.id))
            .filter(EmailMessage.status == EmailMessageStatus.QUEUED, EmailMessage.created_at <= stale)
            .scalar()
            or 0
        )
        if stuck_outbox:
            problems.append(f"{stuck_outbox} billing notifications are queued and unsent")
        if stuck_mail:
            problems.append(f"{stuck_mail} account emails are queued and unsent (is the notification worker running?)")
    stuck_alerts = (
        db.query(func.count(NotificationDelivery.id))
        .filter(NotificationDelivery.status == "queued", NotificationDelivery.created_at <= stale)
        .scalar()
        or 0
    )
    if stuck_alerts:
        problems.append(f"{stuck_alerts} alert notices are queued and unsent")
    if not problems:
        return []
    return [
        Finding(
            "notification_delivery_failing",
            "system",
            "notifications",
            "Notifications",
            "Notification delivery is failing",
            "; ".join(problems) + ".",
            details={"problems": problems},
        )
    ]


# --------------------------------------------------------------------------- plugins


def _report_time(value) -> datetime | None:
    try:
        return naive(datetime.fromisoformat(str(value).removesuffix("Z")))
    except (TypeError, ValueError):
        return None


def plugin_handlers_abandoned(db: Session, params: dict, now: datetime) -> list[Finding]:
    """Plugins with sync handlers that timed out and are still running, from each process's report.

    Each process with such handlers keeps a ``system_settings`` row (``app/plugin_host/invoke.py``)
    fresh while they run; a row not refreshed for ``stale_after_seconds`` belongs to a process that
    has stopped, whose threads are gone with it, and is ignored.
    """
    from app.models.commerce_account import SystemSetting
    from app.models.plugin_host import PluginInstallation
    from app.plugin_host.invoke import REPORT_PREFIX

    stale = now - timedelta(seconds=params["stale_after_seconds"])
    by_plugin: dict[str, dict] = {}
    rows = db.query(SystemSetting).filter(SystemSetting.key.startswith(REPORT_PREFIX, autoescape=True)).all()
    for row in rows:
        value = row.value if isinstance(row.value, dict) else {}
        seen = _report_time(value.get("seen_at"))
        if seen is None or seen < stale:
            continue
        slots = value.get("slots_per_plugin")
        handlers = [h for h in value.get("handlers") or [] if isinstance(h, dict)]
        for plugin_id, count in (value.get("plugins") or {}).items():
            if not isinstance(count, int) or count <= 0:
                continue
            entry = by_plugin.setdefault(plugin_id, {"abandoned": 0, "processes": [], "handlers": [], "hung": False})
            entry["abandoned"] += count
            entry["processes"].append(value.get("process"))
            entry["handlers"].extend(h for h in handlers if h.get("plugin_id") == plugin_id)
            entry["hung"] = entry["hung"] or (isinstance(slots, int) and count >= slots)
    out = []
    for plugin_id, entry in sorted(by_plugin.items()):
        inst = db.get(PluginInstallation, plugin_id)
        name = inst.name if inst is not None else plugin_id
        count = entry["abandoned"]
        handlers = sorted(entry["handlers"], key=lambda h: str(h.get("abandoned_at") or ""))
        oldest = handlers[0] if handlers else {}
        hung = " Every slot of the plugin is held, so its calls are refused." if entry["hung"] else ""
        out.append(
            Finding(
                "plugin_handlers_abandoned",
                "plugin",
                plugin_id,
                name,
                f"Plugin {name} has {count} handler{'s' if count != 1 else ''} stuck after timing out",
                f"{count} of the plugin's handlers timed out and are still running, each holding a thread and "
                f"a database session; the oldest is {oldest.get('handler', 'unknown')}, running for "
                f"{_human(oldest.get('running_seconds'))}.{hung}",
                severity=AlertSeverity.CRITICAL if entry["hung"] else None,
                details={
                    "abandoned": count,
                    "hung": entry["hung"],
                    "processes": entry["processes"],
                    "handlers": handlers[:10],
                },
            )
        )
    return out


DETECTORS: dict[str, Detector] = {
    "storage_node_offline": storage_node_offline,
    "storage_node_ceph": storage_node_ceph,
    "storage_node_error": storage_node_error,
    "storage_tenant_error": storage_tenant_error,
    "storage_tenant_failover": storage_tenant_failover,
    "storage_location_degraded": storage_location_degraded,
    "storage_sftp_no_master": storage_sftp_no_master,
    "proxy_runner_offline": proxy_runner_offline,
    "runner_offline": runner_offline,
    "pdu_poll_failed": pdu_poll_failed,
    "power_feed_near_limit": power_feed_near_limit,
    "power_feed_over_limit": power_feed_over_limit,
    "switch_unreachable": switch_unreachable,
    "switch_vpc_inconsistent": switch_vpc_inconsistent,
    "deployment_job_failed": deployment_job_failed,
    "deployment_job_stuck": deployment_job_stuck,
    "boot_task_stuck": boot_task_stuck,
    "install_failed": install_failed,
    "install_stuck": install_stuck,
    "private_network_error": private_network_error,
    "vm_backup_failed": vm_backup_failed,
    "notification_delivery_failing": notification_delivery_failing,
    "client_storage_quota": client_storage_quota,
    "vm_firewall_apply_failed": vm_firewall_apply_failed,
    "vm_firewall_drift_repeated": vm_firewall_drift_repeated,
    "vm_firewall_cluster_not_ready": vm_firewall_cluster_not_ready,
    "vm_firewall_worker_stale": vm_firewall_worker_stale,
    "plugin_handlers_abandoned": plugin_handlers_abandoned,
    **BILLING_DETECTORS,
}
