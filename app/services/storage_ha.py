"""NFS storage failover as Rackflow sees it: placement, planned moves, drain, rebalance and return.

Rackflow owns placement and the runners own failover (``docs/nfs-storage-failover.md``). This
module decides who may serve which tenant (takeover order, moves), builds the failover parts of
the runner config and reads the failover parts of the runner status. A Rackflow outage never
stops storage: everything here only changes what is sent to the runners next.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.core.config import settings
from app.dao.storage_dao import ONLINE_THRESHOLD, StorageDAO
from app.models.storage import StorageEvent, StorageLocation, StorageNode, StorageTenant
from app.services import storage_location
from app.services.alerting.scrub import scrub
from app.services.storage_errors import StorageError

logger = logging.getLogger(__name__)

HA_MIN_VERSION = (0, 3, 0)
EVENT_KINDS = {"takeover", "handoff", "lost", "fenced", "move_failed", "sftp_master"}
MAX_EVENTS_PER_REPORT = 100
# A takeover the runner already reported is not recorded a second time.
EVENT_DEDUPE_WINDOW = timedelta(minutes=5)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _parse_time(value) -> Optional[datetime]:
    try:
        return _aware(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return None


def _short(value, limit: int) -> Optional[str]:
    return None if value is None else str(value)[:limit]


def _iso(value: Optional[datetime]) -> Optional[str]:
    value = _aware(value)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


# --- versions and the state of a location ---


def parse_version(value) -> Optional[Tuple[int, ...]]:
    """``0.3.0``, ``v0.3.1`` or ``0.3.0-dev`` as numbers; None when it is not a version."""
    text = str(value or "").strip().lstrip("vV")
    parts = []
    for piece in text.split("."):
        digits = ""
        for char in piece:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            break
        parts.append(int(digits))
        if digits != piece:
            break
    return tuple(parts) if parts else None


def supports_ha(node: StorageNode) -> bool:
    """True when the node's runner is new enough to take part in failover."""
    version = parse_version(node.runner_version)
    if version is None:
        return False
    return (version + (0, 0, 0))[:3] >= HA_MIN_VERSION


def ha_active(db: Session, location_id: Optional[int]) -> bool:
    """Failover is switched on for the location and every enabled node in it runs 0.3.0 or later."""
    if location_id is None:
        return False
    row = storage_location.get_location(db, location_id)
    if row is not None and not row.ha_enabled:
        return False
    nodes = [node for node in storage_location.location_nodes(db, location_id) if node.enabled]
    return bool(nodes) and all(supports_ha(node) for node in nodes)


def node_healthy(node: StorageNode, now: Optional[datetime] = None) -> bool:
    """Online, Ceph healthy, lease client healthy, not fenced and not draining."""
    if not node.enabled or node.draining:
        return False
    if not StorageDAO.is_online(node, now=_naive(now) if now is not None else None):
        return False
    if node.ceph_ok is False:
        return False
    state = node.ha_state or {}
    return state.get("lease_ok") is not False and not state.get("fenced")


def _eligible(nodes: Iterable[StorageNode]) -> List[StorageNode]:
    return [node for node in nodes if node.enabled and not node.draining]


def takeover_order(tenant: StorageTenant, nodes: Iterable[StorageNode], counts: Dict[int, int]) -> List[int]:
    """Node ids in the order they may take the tenant over: a pending move target, the preferred
    node, then the others by (tenants preferred there, id) rotated by the tenant id."""
    eligible = _eligible(nodes)
    ids = {node.id for node in eligible}
    others = sorted((n for n in eligible if n.id != tenant.node_id), key=lambda n: (counts.get(n.id, 0), n.id))
    if others:
        shift = tenant.id % len(others)
        others = others[shift:] + others[:shift]
    order = ([tenant.node_id] if tenant.node_id in ids else []) + [node.id for node in others]
    target = tenant.move_to_node_id
    if target in ids:
        order = [target] + [node_id for node_id in order if node_id != target]
    return order


def sftp_master_order(nodes: Iterable[StorageNode]) -> List[int]:
    return sorted(node.id for node in _eligible(nodes) if node.sftp_enabled)


# --- events ---


def record_event(
    db: Session,
    *,
    kind: str,
    location_id: Optional[int],
    tenant_id: Optional[int] = None,
    node_id: Optional[int] = None,
    from_node_id: Optional[int] = None,
    detail: Optional[str] = None,
    at: Optional[datetime] = None,
) -> StorageEvent:
    event = StorageEvent(
        at=at or _now(),
        kind=kind,
        location_id=location_id,
        tenant_id=tenant_id,
        node_id=node_id,
        from_node_id=from_node_id,
        detail=_short(detail, 2000),
    )
    db.add(event)
    return event


def _event_already_recorded(db: Session, kind: str, tenant: StorageTenant, node_id: int, now: datetime) -> bool:
    cutoff = now - EVENT_DEDUPE_WINDOW
    rows = (
        db.query(StorageEvent)
        .filter(StorageEvent.tenant_id == tenant.id, StorageEvent.kind == kind, StorageEvent.node_id == node_id)
        .all()
    )
    return any(_aware(row.at) >= cutoff for row in rows)


def prune_events(db: Session, now: Optional[datetime] = None) -> int:
    cutoff = (now or _now()) - timedelta(days=max(1, int(settings.storage_events_keep_days)))
    removed = db.query(StorageEvent).filter(StorageEvent.at < cutoff).delete(synchronize_session=False)
    db.commit()
    return removed


def recent_events(db: Session, location_id: int, limit: int = 30) -> List[dict]:
    rows = (
        db.query(StorageEvent)
        .filter(StorageEvent.location_id == location_id)
        .order_by(StorageEvent.id.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": row.id,
            "at": _iso(row.at),
            "kind": row.kind,
            "tenant_id": row.tenant_id,
            "node_id": row.node_id,
            "from_node_id": row.from_node_id,
            "detail": row.detail,
        }
        for row in rows
    ]


# --- planned moves ---


def clear_move(tenant: StorageTenant) -> None:
    tenant.move_to_node_id = None
    tenant.move_id = None
    tenant.move_requested_at = None
    tenant.move_set_preferred = True


def move_out(tenant: StorageTenant) -> Optional[dict]:
    if tenant.move_to_node_id is None:
        return None
    target = tenant.move_to_node
    return {
        "id": tenant.move_id,
        "to_node_id": tenant.move_to_node_id,
        "to_node_name": target.name if target is not None else None,
        "requested_at": _iso(tenant.move_requested_at),
        "set_preferred": bool(tenant.move_set_preferred),
    }


def request_move(
    db: Session, tenant: StorageTenant, node_id: int, *, set_preferred: bool = True, automatic: bool = False
) -> StorageTenant:
    """Ask for the tenant to be handed to ``node_id``. The move ends when that node reports
    serving it (``note_serving``) or is cleared as failed after ``storage_move_timeout_seconds``."""
    if tenant.terminated_at is not None:
        raise StorageError("Storage is terminated", 409)
    if tenant.node_id is None or tenant.location_id is None:
        raise StorageError("Link the storage to a private network first", 409)
    if not ha_active(db, tenant.location_id):
        raise StorageError("Failover is not active in this location, so storage cannot be moved", 409)
    if tenant.move_to_node_id is not None:
        raise StorageError("A move is already in progress for this storage", 409)
    target = StorageDAO.get_node(db, node_id)
    if target is None or storage_location.node_location_id(target) != tenant.location_id:
        raise StorageError("That storage node is not in this storage's location", 409)
    if not target.enabled or target.draining:
        raise StorageError("That storage node is disabled or draining", 409)
    usable = node_healthy(target) if automatic else StorageDAO.is_online(target)
    if not usable:
        raise StorageError("That storage node is not online and healthy", 409)
    if set_preferred and target.id != tenant.node_id:
        clash = StorageDAO.undestroyed_vlan_on_node(db, target.id, tenant.vlan, tenant.location_id)
        if clash is not None and clash.id != tenant.id:
            raise StorageError("That node still holds storage on the same VLAN", 409)
    if tenant.current_node_id == target.id:
        if not set_preferred or tenant.node_id == target.id:
            raise StorageError("That node already serves this storage", 409)
        tenant.node_id = target.id
        db.commit()
        db.refresh(tenant)
        return tenant
    tenant.move_to_node_id = target.id
    tenant.move_id = "m-" + secrets.token_hex(4)
    tenant.move_requested_at = _now()
    tenant.move_set_preferred = bool(set_preferred)
    db.commit()
    db.refresh(tenant)
    return tenant


def _complete_move(db: Session, tenant: StorageTenant, node: StorageNode, at: datetime) -> None:
    previous = tenant.current_node_id
    move_id = tenant.move_id
    if tenant.move_set_preferred:
        tenant.node_id = node.id
    clear_move(tenant)
    if not _event_already_recorded(db, "handoff", tenant, node.id, _now()):
        record_event(
            db,
            kind="handoff",
            location_id=tenant.location_id,
            tenant_id=tenant.id,
            node_id=node.id,
            from_node_id=previous,
            detail=f"planned move {move_id} finished",
            at=at,
        )


def expire_moves(db: Session, location_id: int, now: Optional[datetime] = None) -> int:
    """Clear moves whose target never reported serving, recording each as a failed move."""
    now = now or _now()
    limit = timedelta(seconds=max(1, int(settings.storage_move_timeout_seconds)))
    expired = 0
    for tenant in StorageDAO.tenants_for_location(db, location_id):
        if tenant.move_to_node_id is None:
            continue
        requested = _aware(tenant.move_requested_at)
        if requested is not None and now - requested < limit:
            continue
        record_event(
            db,
            kind="move_failed",
            location_id=location_id,
            tenant_id=tenant.id,
            node_id=tenant.move_to_node_id,
            from_node_id=tenant.current_node_id,
            detail=f"move {tenant.move_id} was not taken up within {int(limit.total_seconds())} seconds",
            at=now,
        )
        clear_move(tenant)
        expired += 1
    if expired:
        db.commit()
    return expired


# --- drain, rebalance and return ---


def set_draining(db: Session, node: StorageNode, draining: bool) -> StorageNode:
    """Mark the node draining (its tenants then move away one at a time) or clear the mark."""
    draining = bool(draining)
    location_id = storage_location.node_location_id(node)
    if draining and not node.draining:
        others = [n for n in storage_location.location_nodes(db, location_id) if n.id != node.id and node_healthy(n)]
        if StorageDAO.tenants_served_by(db, node.id) and ha_active(db, location_id) and not others:
            raise StorageError("No other healthy node in this location could take the storage on this node", 409)
        node.healthy_since = None
    node.draining = draining
    db.commit()
    db.refresh(node)
    if location_id is not None:
        advance_location(db, location_id)
    return node


def _live_tenants(db: Session, location_id: int) -> List[StorageTenant]:
    return [t for t in StorageDAO.tenants_for_location(db, location_id) if t.terminated_at is None]


def _load(tenants: Iterable[StorageTenant]) -> Dict[int, int]:
    """Tenants each node serves now or is about to serve."""
    counts: Dict[int, int] = {}
    for tenant in tenants:
        for node_id in (tenant.current_node_id, tenant.move_to_node_id):
            if node_id is not None:
                counts[node_id] = counts.get(node_id, 0) + 1
    return counts


def rebalance(db: Session, location_id: int) -> dict:
    """Give every healthy node its fair share of preferred tenants with as few changes as possible.

    Preferred nodes are changed here; the tenants then return to them one move at a time.
    """
    nodes = storage_location.require_storage_location(db, location_id)
    if not ha_active(db, location_id):
        raise StorageError("Failover is not active in this location, so storage cannot be rebalanced", 409)
    row = storage_location.ensure_location(db, location_id)
    by_id = {node.id: node for node in nodes}
    pool = [node for node in nodes if node_healthy(node)]
    if not pool:
        raise StorageError("No healthy storage node in this location", 409)
    pool_ids = {node.id for node in pool}
    owned: Dict[int, List[StorageTenant]] = {node.id: [] for node in pool}
    rehome: List[StorageTenant] = []
    for tenant in _live_tenants(db, location_id):
        if tenant.vlan is None:
            continue
        holder = by_id.get(tenant.node_id)
        if tenant.node_id in pool_ids:
            owned[tenant.node_id].append(tenant)
        elif holder is not None and (holder.draining or not holder.enabled):
            rehome.append(tenant)
    total = sum(len(v) for v in owned.values()) + len(rehome)
    base, extra = divmod(total, len(pool))
    ranked = sorted(pool, key=lambda n: (-len(owned[n.id]), n.id))
    quota = {node.id: base + (1 if index < extra else 0) for index, node in enumerate(ranked)}
    shed = list(rehome)
    for node in pool:
        surplus = len(owned[node.id]) - quota[node.id]
        if surplus > 0:
            leaving = sorted(owned[node.id], key=lambda t: t.id, reverse=True)[:surplus]
            shed.extend(leaving)
            owned[node.id] = [t for t in owned[node.id] if t not in leaving]
    changes = []
    for tenant in sorted(shed, key=lambda t: t.id):
        wanting = sorted(
            (n for n in pool if len(owned[n.id]) < quota[n.id]), key=lambda n: (len(owned[n.id]) - quota[n.id], n.id)
        )
        for node in wanting:
            clash = StorageDAO.undestroyed_vlan_on_node(db, node.id, tenant.vlan, location_id)
            if clash is not None and clash.id != tenant.id:
                continue
            changes.append(
                {
                    "service_id": tenant.service_id,
                    "tenant_id": tenant.id,
                    "from_node_id": tenant.node_id,
                    "to_node_id": node.id,
                }
            )
            tenant.node_id = node.id
            owned[node.id].append(tenant)
            break
    row.rebalancing = True
    db.commit()
    advance_location(db, location_id)
    db.refresh(row)
    return {"location_id": location_id, "changed": changes, "rebalancing": bool(row.rebalancing)}


def _start(db: Session, tenant: StorageTenant, node: StorageNode) -> bool:
    try:
        request_move(db, tenant, node.id, set_preferred=False, automatic=True)
    except StorageError as exc:
        logger.info("Storage: automatic move of tenant %s to node %s refused: %s", tenant.id, node.id, exc.message)
        db.rollback()
        return False
    return True


def advance_location(db: Session, location_id: int, now: Optional[datetime] = None) -> Optional[str]:
    """Settle moves and start at most one new one, in this order: draining a node, a rebalance
    step, a return to the preferred node. Returns what was started or None."""
    now = now or _now()
    row = storage_location.get_location(db, location_id)
    if not ha_active(db, location_id):
        changed = False
        for tenant in StorageDAO.tenants_for_location(db, location_id):
            if tenant.move_to_node_id is not None:
                clear_move(tenant)
                changed = True
        if row is not None and row.rebalancing:
            row.rebalancing = False
            changed = True
        if changed:
            db.commit()
        return None
    expire_moves(db, location_id, now)
    tenants = _live_tenants(db, location_id)
    if any(t.move_to_node_id is not None for t in tenants):
        return None
    nodes = {node.id: node for node in storage_location.location_nodes(db, location_id)}
    healthy = [node for node in nodes.values() if node_healthy(node, now)]

    draining = {node.id for node in nodes.values() if node.draining and node.enabled}
    load = _load(tenants)
    for tenant in tenants:
        if tenant.current_node_id in draining:
            candidates = [n for n in healthy if n.id != tenant.current_node_id]
            if candidates:
                target = min(candidates, key=lambda n: (load.get(n.id, 0), n.id))
                return "drain" if _start(db, tenant, target) else None

    if row is not None and row.rebalancing:
        misplaced = [
            t for t in tenants
            if t.current_node_id is not None and t.node_id != t.current_node_id and nodes.get(t.node_id) in healthy
        ]
        if not misplaced:
            row.rebalancing = False
            db.commit()
        else:
            return "rebalance" if _start(db, misplaced[0], nodes[misplaced[0].node_id]) else None

    if row is None or row.auto_failback:
        after = timedelta(seconds=max(0, int(settings.storage_return_after_seconds)))
        for tenant in tenants:
            preferred = nodes.get(tenant.node_id)
            if tenant.current_node_id is None or preferred is None or tenant.current_node_id == preferred.id:
                continue
            if preferred not in healthy or preferred.healthy_since is None:
                continue
            if now - _aware(preferred.healthy_since) < after:
                continue
            since = _aware(tenant.current_since)
            if since is None or now - since < after:
                continue
            return "return" if _start(db, tenant, preferred) else None
    return None


def reconcile(db: Session, now: Optional[datetime] = None) -> dict:
    """Periodic work: forget the current node of tenants whose node went offline, restart the
    healthy timer of nodes that are not healthy, advance moves and prune old events."""
    now = now or _now()
    counts = {"cleared": 0, "started": 0}
    nodes = StorageDAO.list_nodes(db)
    by_id = {node.id: node for node in nodes}
    for node in nodes:
        if node.healthy_since is not None and not node_healthy(node, now):
            node.healthy_since = None
    for tenant in db.query(StorageTenant).filter(StorageTenant.current_node_id.isnot(None)).all():
        holder = by_id.get(tenant.current_node_id)
        if holder is None or not holder.enabled or not StorageDAO.is_online(holder, now=_naive(now)):
            tenant.current_node_id = None
            tenant.current_since = now
            counts["cleared"] += 1
    from app.services import private_network_dhcp

    counts["presences_cleared"] = private_network_dhcp.reconcile(db, by_id, now)
    db.commit()
    for location_id in sorted({storage_location.node_location_id(node) for node in nodes} - {None}):
        if advance_location(db, location_id, now):
            counts["started"] += 1
    prune_events(db, now)
    return counts


# --- runner config ---


@dataclass
class HaContext:
    """What the runner config needs to know about the location of the node it is built for."""

    node: StorageNode
    location_id: Optional[int]
    row: Optional[StorageLocation]
    active: bool
    nodes: List[StorageNode] = field(default_factory=list)
    counts: Dict[int, int] = field(default_factory=dict)

    def tenants(self, db: Session) -> List[StorageTenant]:
        """Every tenant of the location when failover is active, else the node's preferred ones."""
        if self.active:
            return StorageDAO.tenants_for_location(db, self.location_id)
        return StorageDAO.tenants_for_node(db, self.node.id)

    def tenant_extras(self, tenant: StorageTenant) -> dict:
        if not self.active:
            return {"preferred_node_id": tenant.node_id, "takeover_order": [self.node.id]}
        out = {
            "preferred_node_id": tenant.node_id,
            "takeover_order": takeover_order(tenant, self.nodes, self.counts),
        }
        if tenant.move_to_node_id is not None:
            out["move"] = {"id": tenant.move_id, "to_node_id": tenant.move_to_node_id}
        return out

    def node_block(self) -> dict:
        return {"id": self.node.id, "name": self.node.name, "location_id": self.location_id}

    def ha_block(self) -> dict:
        return {
            "enabled": self.active,
            "lease_seconds": int(settings.storage_ha_lease_seconds),
            "renew_seconds": int(settings.storage_ha_renew_seconds),
            "stop_margin_seconds": int(settings.storage_ha_stop_margin_seconds),
            "guard_margin_seconds": int(settings.storage_ha_guard_margin_seconds),
            "poll_seconds": int(settings.storage_ha_poll_seconds),
            "rank_step_seconds": int(settings.storage_ha_rank_step_seconds),
            "fence_seconds": int(settings.storage_ha_fence_seconds),
            "nfs_lease_lifetime": int(settings.storage_ha_nfs_lease_lifetime),
            "nfs_grace_period": int(settings.storage_ha_nfs_grace_period),
            "draining": bool(self.node.draining),
            "nodes": [{"id": n.id, "name": n.name, "draining": bool(n.draining)} for n in self.nodes if n.enabled],
        }

    def sftp_block(self, base: dict) -> dict:
        """The node's SFTP settings plus the location's port, virtual IP, host key and master election."""
        row = self.row
        out = dict(base)
        if row is not None:
            out["enabled"] = bool(self.node.sftp_enabled and row.sftp_enabled)
            out["port"] = int(row.sftp_port or 2222)
        vip = row.sftp_virtual_ip if row is not None and row.sftp_enabled and self.active else None
        out.update(
            location_id=self.location_id,
            virtual_ip=vip,
            host_key=storage_location.runner_host_key(row),
            master_eligible=bool(out["enabled"] and self.node.enabled and not self.node.draining),
            master_order=sftp_master_order(self.nodes) if out["enabled"] else [],
        )
        return out


def ha_context(db: Session, node: StorageNode) -> HaContext:
    location_id = storage_location.node_location_id(node)
    return HaContext(
        node=node,
        location_id=location_id,
        row=storage_location.get_location(db, location_id),
        active=ha_active(db, location_id),
        nodes=storage_location.location_nodes(db, location_id) if location_id is not None else [node],
        counts=StorageDAO.live_tenant_counts(db),
    )


def sftp_uses_vip(db: Session, node: StorageNode) -> bool:
    """SFTP of the node's location is served by one master behind a virtual IP."""
    location_id = storage_location.node_location_id(node)
    row = storage_location.get_location(db, location_id)
    return bool(row is not None and row.sftp_enabled and row.sftp_virtual_ip and ha_active(db, location_id))


# --- runner status ---


def _record_reported_event(
    db: Session, location_id: Optional[int], node: StorageNode, known_nodes: set, tenants: Dict[int, StorageTenant], raw: dict
) -> None:
    kind = raw.get("kind")
    tenant_id = raw.get("tenant_id")
    if kind not in EVENT_KINDS or (tenant_id is not None and tenant_id not in tenants):
        return
    detail = scrub(raw.get("detail"), 1800)
    if raw.get("fence") in ("tenant", "node"):
        detail = f"fence: {raw['fence']}" + (f"; {detail}" if detail else "")
    from_node = raw.get("from_node_id")
    record_event(
        db,
        kind=kind,
        location_id=location_id,
        tenant_id=tenant_id,
        node_id=node.id,
        from_node_id=from_node if from_node in known_nodes else None,
        detail=detail,
        at=_parse_time(raw.get("at")) or _now(),
    )


def apply_node_report(
    db: Session,
    node: StorageNode,
    report: dict,
    previous_seen: Optional[datetime],
    tenants: Dict[int, StorageTenant],
    now: Optional[datetime] = None,
) -> None:
    """The node's own failover state: the ``ha`` block, SFTP master, health timer and events."""
    now = now or _now()
    location_id = storage_location.node_location_id(node)
    info = report.get("ha")
    claims = False
    if isinstance(info, dict):
        claims = bool(info.get("sftp_master"))
        node.ha_state = {
            "lease_ok": info["lease_ok"] if isinstance(info.get("lease_ok"), bool) else None,
            "fenced": bool(info.get("fenced")),
            "fenced_until": _short(info.get("fenced_until"), 40),
            "sftp_master": claims,
            "ha_active": bool(info.get("ha_active")),
        }
    else:
        node.ha_state = None
    _track_sftp_master(db, node, claims, location_id, now)

    gap = previous_seen is not None and _naive(now) - previous_seen > ONLINE_THRESHOLD
    if not node_healthy(node, now):
        node.healthy_since = None
    elif node.healthy_since is None or gap:
        node.healthy_since = _naive(now)

    known = {n.id for n in storage_location.location_nodes(db, location_id)} if location_id is not None else set()
    for raw in (report.get("events") or [])[:MAX_EVENTS_PER_REPORT]:
        if isinstance(raw, dict):
            _record_reported_event(db, location_id, node, known, tenants, raw)
    db.flush()


def _track_sftp_master(db: Session, node: StorageNode, claims: bool, location_id: Optional[int], now: datetime) -> None:
    """One SFTP master per location: when two nodes claim it, the latest report wins."""
    if not claims:
        if node.sftp_master:
            node.sftp_master = False
            record_event(
                db, kind="sftp_master", location_id=location_id, node_id=node.id,
                detail=f"{node.name} no longer reports being the SFTP master", at=now,
            )
        return
    others = []
    if location_id is not None:
        others = [n for n in storage_location.location_nodes(db, location_id) if n.id != node.id and n.sftp_master]
    for other in others:
        other.sftp_master = False
    if not node.sftp_master or others:
        names = ", ".join(other.name for other in others)
        detail = (
            f"{node.name} and {names} both reported being the SFTP master; keeping {node.name}"
            if others
            else f"{node.name} became the SFTP master"
        )
        record_event(
            db, kind="sftp_master", location_id=location_id, node_id=node.id,
            from_node_id=others[0].id if others else None, detail=detail, at=now,
        )
    node.sftp_master = True


def entry_serving(node: StorageNode, tenant: StorageTenant, entry: dict) -> bool:
    """Whether the node serves the tenant. A report without ``serving`` comes from a runner that
    serves exactly its own tenants."""
    flag = entry.get("serving")
    if isinstance(flag, bool):
        return flag
    return tenant.node_id == node.id


def note_serving(db: Session, node: StorageNode, tenant: StorageTenant, entry: dict, now: Optional[datetime] = None) -> None:
    """The node serves the tenant: set the current node, finish a planned move or detect a
    failover (a change of current node that is not a pending move's target)."""
    now = now or _now()
    since = _parse_time(entry.get("since"))
    at = since if since is not None and since <= now else now
    previous = tenant.current_node_id
    if previous == node.id:
        if tenant.move_to_node_id == node.id:
            _complete_move(db, tenant, node, now)
        return
    if tenant.move_to_node_id == node.id:
        _complete_move(db, tenant, node, at)
    elif previous is not None or node.id != tenant.node_id:
        tenant.last_failover_at = at
        tenant.last_failover_from_node_id = previous
        if not _event_already_recorded(db, "takeover", tenant, node.id, now):
            record_event(
                db,
                kind="takeover",
                location_id=tenant.location_id,
                tenant_id=tenant.id,
                node_id=node.id,
                from_node_id=previous,
                detail=f"{node.name} took over without a planned move",
                at=at,
            )
    tenant.current_node_id = node.id
    tenant.current_since = at


def note_not_serving(tenant: StorageTenant, node: StorageNode, now: Optional[datetime] = None) -> None:
    if tenant.current_node_id == node.id:
        tenant.current_node_id = None
        tenant.current_since = now or _now()
