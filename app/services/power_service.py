"""
Power domain logic: generating PDU inlets/outlets, storing readings, aggregating usage per
power feed, rack and server, and guarded outlet control.
"""
import logging
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.dao.pdu_dao import PduDAO, PowerFeedDAO, ServerPowerInputDAO
from app.models.pdu import Pdu, PduInlet, PduOutlet
from app.models.power_feed import PowerFeed
from app.models.server import Server
from app.plugins.pdu.base import OUTLET_ACTIONS, PduCapability, secret_fields
from app.plugins.pdu.registry import get_pdu_registry
from app.services.power_history_service import record_samples

logger = logging.getLogger(__name__)

MASKED_SECRET = "********"
MAX_OUTLETS = 96
MAX_INLETS = 4


# ---------------------------------------------------------------------------
# Plugin config secrets
# ---------------------------------------------------------------------------

def _plugin_secret_fields(plugin_name: str) -> List[str]:
    registry = get_pdu_registry()
    if not registry.has(plugin_name):
        return []
    return secret_fields(registry.get_class(plugin_name).CONFIG_TEMPLATE)


def mask_plugin_config(plugin_name: str, config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Copy of config with secret values replaced by MASKED_SECRET."""
    masked = dict(config or {})
    for key in _plugin_secret_fields(plugin_name):
        if masked.get(key):
            masked[key] = MASKED_SECRET
    return masked


def merge_plugin_config(
    plugin_name: str, incoming: Dict[str, Any], existing: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """Keep stored secrets when the client sends back the mask or leaves a secret empty."""
    merged = dict(incoming or {})
    existing = existing or {}
    for key in _plugin_secret_fields(plugin_name):
        if merged.get(key) in (None, "", MASKED_SECRET) and existing.get(key):
            merged[key] = existing[key]
    return merged


# ---------------------------------------------------------------------------
# Inlets and outlets
# ---------------------------------------------------------------------------

def _clamp(value: Any, default: int, low: int, high: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = default
    return max(low, min(high, v))


def _default_inlet_name(position: int, count: int) -> str:
    return "Input" if count == 1 else f"Input {chr(ord('A') + position - 1)}"


def sync_pdu_ports(db: Session, pdu: Pdu, info: Dict[str, Any]) -> Dict[str, int]:
    """
    Make the PDU's inlets and outlets match the plugin's description (info from get_pdu_info,
    or a hand-built dict with inlet_count / outlet_count). Rows beyond the new count are removed
    unless they are linked (inlet to a feed, outlet to a server power input). Existing rows keep
    their names unless the plugin supplies one.
    """
    for field in ("model", "serial_number", "firmware_version"):
        if info.get(field):
            setattr(pdu, field, str(info[field])[:255])
    if "outlet_metering" in info:
        pdu.outlet_metering = bool(info["outlet_metering"])
    if "outlet_switching" in info:
        pdu.outlet_switching = bool(info["outlet_switching"])

    inlet_specs = {int(i["position"]): i for i in (info.get("inlets") or []) if i.get("position")}
    outlet_specs = {int(o["position"]): o for o in (info.get("outlets") or []) if o.get("position")}
    inlet_count = _clamp(info.get("inlet_count") or len(inlet_specs) or 1, 1, 1, MAX_INLETS)
    outlet_count = _clamp(
        info.get("outlet_count") if info.get("outlet_count") is not None else len(outlet_specs), 0, 0, MAX_OUTLETS
    )
    pdu.inlet_count = inlet_count
    pdu.outlet_count = outlet_count

    stats = {"created": 0, "updated": 0, "deleted": 0, "kept_linked": 0}

    existing_inlets = {i.position: i for i in pdu.inlets}
    for pos in range(1, inlet_count + 1):
        spec = inlet_specs.get(pos, {})
        inlet = existing_inlets.get(pos)
        if inlet is None:
            pdu.inlets.append(
                PduInlet(position=pos, name=(spec.get("name") or _default_inlet_name(pos, inlet_count))[:64])
            )
            stats["created"] += 1
        elif spec.get("name") and spec["name"] != inlet.name:
            inlet.name = spec["name"][:64]
            stats["updated"] += 1
    for pos, inlet in existing_inlets.items():
        if pos > inlet_count:
            if inlet.power_feed_id is not None:
                stats["kept_linked"] += 1
                continue
            pdu.inlets.remove(inlet)
            stats["deleted"] += 1

    existing_outlets = {o.position: o for o in pdu.outlets}
    linked_outlet_ids = {
        spi.pdu_outlet_id for spi in ServerPowerInputDAO.get_by_outlets(db, [o.id for o in pdu.outlets])
    }
    for pos in range(1, outlet_count + 1):
        spec = outlet_specs.get(pos, {})
        outlet = existing_outlets.get(pos)
        if outlet is None:
            pdu.outlets.append(
                PduOutlet(
                    position=pos,
                    name=(spec.get("name") or f"Outlet {pos}")[:64],
                    bank=(str(spec["bank"])[:64] if spec.get("bank") else None),
                )
            )
            stats["created"] += 1
            continue
        changed = False
        if spec.get("name") and spec["name"] != outlet.name:
            outlet.name = spec["name"][:64]
            changed = True
        if spec.get("bank") and str(spec["bank"]) != outlet.bank:
            outlet.bank = str(spec["bank"])[:64]
            changed = True
        if changed:
            stats["updated"] += 1
    for pos, outlet in existing_outlets.items():
        if pos > outlet_count:
            if outlet.id in linked_outlet_ids:
                stats["kept_linked"] += 1
                continue
            pdu.outlets.remove(outlet)
            stats["deleted"] += 1

    db.commit()
    db.refresh(pdu)
    return stats


# ---------------------------------------------------------------------------
# Readings
# ---------------------------------------------------------------------------

def _num(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else round(f, 3)


def apply_readings(db: Session, pdu: Pdu, readings: Dict[str, Any], now: Optional[datetime] = None) -> None:
    """
    Store a get_readings() result on the PDU, its inlets and outlets, and add it to the power
    history in the same transaction. A failure while writing history is logged and does not stop
    the latest values from being saved.
    """
    previous_poll = pdu.last_polled_at
    inlet_rows = {int(r["position"]): r for r in (readings.get("inlets") or []) if r.get("position")}
    outlet_rows = {int(r["position"]): r for r in (readings.get("outlets") or []) if r.get("position")}
    totals = dict(readings.get("totals") or {})
    estimated = False

    for inlet in pdu.inlets:
        row = inlet_rows.get(inlet.position, {})
        inlet.voltage_v = _num(row.get("voltage_v"))
        inlet.current_a = _num(row.get("current_a"))
        inlet.power_w = _num(row.get("power_w"))
        inlet.apparent_power_va = _num(row.get("apparent_power_va"))
        inlet.energy_kwh = _num(row.get("energy_kwh"))
        inlet.active = row.get("active") if isinstance(row.get("active"), bool) else None
        if inlet.power_w is None and inlet.current_a is not None and inlet.voltage_v is not None:
            inlet.power_w = round(inlet.current_a * inlet.voltage_v, 1)
            estimated = True

    # Unit totals but no per-inlet figures (typical of an ATS): the load is on the active inlet.
    if pdu.inlets and all(i.current_a is None and i.power_w is None for i in pdu.inlets):
        active = next((i for i in pdu.inlets if i.active), None) or pdu.inlets[0]
        active.current_a = _num(totals.get("current_a"))
        active.power_w = _num(totals.get("power_w"))
        active.apparent_power_va = _num(totals.get("apparent_power_va"))
        if active.power_w is None and active.current_a is not None and active.voltage_v is not None:
            active.power_w = round(active.current_a * active.voltage_v, 1)
            estimated = True
        for other in pdu.inlets:
            if other is not active and other.current_a is None:
                other.current_a = 0.0 if active.current_a is not None else None
                other.power_w = 0.0 if active.power_w is not None else None

    for outlet in pdu.outlets:
        row = outlet_rows.get(outlet.position)
        if row is None:
            continue
        outlet.current_a = _num(row.get("current_a"))
        outlet.power_w = _num(row.get("power_w"))
        state = row.get("state")
        outlet.state = state if state in ("on", "off") else None

    if totals.get("power_w") is None:
        inlet_power = [i.power_w for i in pdu.inlets if i.power_w is not None]
        if inlet_power:
            totals["power_w"] = round(sum(inlet_power), 1)
    if totals.get("current_a") is None:
        inlet_current = [i.current_a for i in pdu.inlets if i.current_a is not None]
        if inlet_current:
            totals["current_a"] = round(sum(inlet_current), 3)

    pdu.last_readings = {
        "totals": {k: _num(v) for k, v in totals.items()},
        "banks": readings.get("banks") or [],
        "sensors": readings.get("sensors") or [],
        "power_estimated": estimated,
    }
    pdu.last_polled_at = now or datetime.now(timezone.utc)
    pdu.last_poll_error = None
    _record_history(db, pdu, previous_poll, set(outlet_rows))
    db.commit()


def _record_history(db: Session, pdu: Pdu, previous_poll: Optional[datetime], metered_outlets: set) -> None:
    """One raw history sample per series, at most one per second per PDU (the table keys on the second)."""
    sampled_at = pdu.last_polled_at.replace(microsecond=0)
    if previous_poll is not None:
        earlier = previous_poll if previous_poll.tzinfo else previous_poll.replace(tzinfo=timezone.utc)
        if sampled_at.astimezone(timezone.utc) <= earlier.astimezone(timezone.utc).replace(microsecond=0):
            return
    try:
        with db.begin_nested():
            record_samples(db, pdu, sampled_at, metered_outlets)
    except SQLAlchemyError as exc:
        logger.warning("Could not record power history for %s: %s", pdu.name, exc)


async def poll_pdu(db: Session, pdu: Pdu) -> Dict[str, Any]:
    """Read the PDU through its plugin and store the result. Never raises for device errors."""
    registry = get_pdu_registry()
    if not registry.supports(pdu.plugin_name, PduCapability.METERING):
        return {"success": False, "message": f"Plugin '{pdu.plugin_name}' does not provide readings"}
    try:
        plugin = registry.get_plugin(pdu.plugin_name, pdu.plugin_config or {})
        readings = await plugin.get_readings()
    except Exception as exc:
        logger.warning("PDU poll failed for %s: %s", pdu.name, exc)
        pdu.last_poll_error = str(exc)[:1000]
        db.commit()
        return {"success": False, "message": str(exc)}
    apply_readings(db, pdu, readings)
    return {"success": True, "message": "Readings updated", "polled_at": pdu.last_polled_at.isoformat()}


async def poll_all_pdus(db: Session) -> Dict[str, int]:
    """Poll every enabled PDU whose plugin provides readings, one after another."""
    registry = get_pdu_registry()
    ok = failed = 0
    for pdu in PduDAO.get_all(db, enabled_only=True):
        if not registry.supports(pdu.plugin_name, PduCapability.METERING):
            continue
        result = await poll_pdu(db, pdu)
        if result.get("success"):
            ok += 1
        else:
            failed += 1
    return {"polled": ok, "failed": failed}


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def feed_capacity(feed: PowerFeed) -> Dict[str, float]:
    """Nominal and usable (derated) capacity of a feed."""
    factor = math.sqrt(3) if (feed.phase or "single") == "three" else 1.0
    nominal_w = feed.voltage * feed.amperage * factor
    derate = (feed.max_utilisation_percent or 100) / 100.0
    return {
        "capacity_w": round(nominal_w, 1),
        "usable_w": round(nominal_w * derate, 1),
        "usable_current_a": round(feed.amperage * derate, 2),
    }


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _sum(values: List[Optional[float]]) -> Optional[float]:
    present = [v for v in values if v is not None]
    return round(sum(present), 3) if present else None


def pdu_power_w(pdu: Pdu) -> Optional[float]:
    total = _sum([i.power_w for i in pdu.inlets])
    if total is None:
        total = _num(((pdu.last_readings or {}).get("totals") or {}).get("power_w"))
    return total


def pdu_current_a(pdu: Pdu) -> Optional[float]:
    total = _sum([i.current_a for i in pdu.inlets])
    if total is None:
        total = _num(((pdu.last_readings or {}).get("totals") or {}).get("current_a"))
    return total


def feed_summary(feed: PowerFeed) -> Dict[str, Any]:
    inlets = []
    for inlet in feed.pdu_inlets:
        if not inlet.pdu or not inlet.pdu.enabled:
            continue
        inlets.append({
            "inlet_id": inlet.id,
            "inlet_name": inlet.name,
            "pdu_id": inlet.pdu_id,
            "pdu_name": inlet.pdu.name,
            "current_a": inlet.current_a,
            "power_w": inlet.power_w,
            "active": inlet.active,
            "last_polled_at": _iso(inlet.pdu.last_polled_at),
        })
    current = _sum([i["current_a"] for i in inlets])
    power = _sum([i["power_w"] for i in inlets])
    cap = feed_capacity(feed)
    return {
        "id": feed.id,
        "rack_id": feed.rack_id,
        "name": feed.name,
        "voltage": feed.voltage,
        "amperage": feed.amperage,
        "phase": feed.phase,
        "max_utilisation_percent": feed.max_utilisation_percent,
        "description": feed.description,
        **cap,
        "current_a": current,
        "power_w": power,
        "utilisation_percent": round(current / feed.amperage * 100, 1) if current is not None and feed.amperage else None,
        "over_limit": bool(current is not None and current > cap["usable_current_a"]),
        "inlets": inlets,
    }


def rack_power_summary(db: Session, rack_id: int) -> Dict[str, Any]:
    feeds = [feed_summary(f) for f in PowerFeedDAO.get_by_rack(db, rack_id)]
    pdus_by_id: Dict[int, Pdu] = {p.id: p for p in PduDAO.get_all(db, rack_id=rack_id)}
    for p in PduDAO.get_by_feed_rack(db, rack_id):
        pdus_by_id.setdefault(p.id, p)
    pdus = []
    for p in sorted(pdus_by_id.values(), key=lambda x: x.name):
        pdus.append({
            "id": p.id,
            "name": p.name,
            "enabled": p.enabled,
            "rack_id": p.rack_id,
            "rack_unit": p.rack_unit,
            "rack_units": p.rack_units,
            "model": p.model,
            "power_w": pdu_power_w(p) if p.enabled else None,
            "current_a": pdu_current_a(p) if p.enabled else None,
            "banks": (p.last_readings or {}).get("banks") or [],
            "last_polled_at": _iso(p.last_polled_at),
            "last_poll_error": p.last_poll_error,
            "unlinked_inlets": sum(1 for i in p.inlets if i.power_feed_id is None),
        })
    return {
        "rack_id": rack_id,
        "feeds": feeds,
        "pdus": pdus,
        "total_power_w": _sum([p["power_w"] for p in pdus]),
        "total_current_a": _sum([p["current_a"] for p in pdus]),
        "total_capacity_w": round(sum(f["capacity_w"] for f in feeds), 1) if feeds else None,
        "total_usable_w": round(sum(f["usable_w"] for f in feeds), 1) if feeds else None,
    }


# ---------------------------------------------------------------------------
# Outlet control
# ---------------------------------------------------------------------------

def outlet_control_status(outlet: PduOutlet) -> Tuple[bool, Optional[str]]:
    """(allowed, reason it is not) for switching this outlet from Rackflow."""
    pdu = outlet.pdu
    if pdu is None:
        return False, "Outlet has no PDU"
    if not pdu.enabled:
        return False, "PDU is disabled"
    if not get_pdu_registry().supports(pdu.plugin_name, PduCapability.OUTLET_CONTROL):
        return False, "PDU plugin does not support outlet control"
    if not pdu.outlet_switching:
        return False, "PDU does not have switched outlets"
    if not pdu.outlet_control_enabled:
        return False, "Outlet control is not enabled on this PDU"
    if outlet.locked:
        return False, "Outlet is locked"
    return True, None


async def perform_outlet_action(db: Session, outlet: PduOutlet, action: str, source: str) -> Dict[str, Any]:
    """Switch an outlet after the safety checks. Logs to the activity of any linked server."""
    from app.models.server_activity import ServerActivityEventType
    from app.services.server_activity_logger import (
        log_server_activity_failure,
        log_server_activity_success,
    )

    if action not in OUTLET_ACTIONS:
        raise ValueError(f"Unknown outlet action '{action}'")
    allowed, reason = outlet_control_status(outlet)
    if not allowed:
        raise PermissionError(reason)
    pdu = outlet.pdu
    linked = ServerPowerInputDAO.get_by_outlet(db, outlet.id)
    details = {"pdu_id": pdu.id, "pdu": pdu.name, "outlet": outlet.position, "action": action}
    message = f"PDU {pdu.name} outlet {outlet.position} ({outlet.name}): {action}"
    try:
        plugin = get_pdu_registry().get_plugin(pdu.plugin_name, pdu.plugin_config or {})
        result = await plugin.set_outlet_state(outlet.position, action)
    except Exception as exc:
        if linked:
            log_server_activity_failure(
                db, server_id=linked.server_id, event_type=ServerActivityEventType.POWER,
                action=f"pdu_outlet_{action}", source=source, message=message, details=details, error=exc,
            )
        raise
    if action in ("on", "off"):
        outlet.state = action
    elif action == "cycle":
        outlet.state = "on"
    db.commit()
    if linked:
        log_server_activity_success(
            db, server_id=linked.server_id, event_type=ServerActivityEventType.POWER,
            action=f"pdu_outlet_{action}", source=source, message=message, details=details,
        )
    return {"success": True, "message": message, "result": result}


# ---------------------------------------------------------------------------
# Servers
# ---------------------------------------------------------------------------

def server_power_summary(db: Session, server: Server) -> Dict[str, Any]:
    """The server's power inputs, what feeds them, and aggregated draw where outlets are metered."""
    inputs = []
    for spi in ServerPowerInputDAO.get_by_server(db, server.id):
        outlet = spi.pdu_outlet
        entry: Dict[str, Any] = {
            "id": spi.id,
            "name": spi.name,
            "description": spi.description,
            "outlet": None,
            "pdu": None,
            "feeds": [],
            "can_control": False,
            "control_blocked_reason": None,
        }
        if outlet is not None and outlet.pdu is not None:
            pdu = outlet.pdu
            entry["outlet"] = {
                "id": outlet.id,
                "position": outlet.position,
                "name": outlet.name,
                "bank": outlet.bank,
                "state": outlet.state,
                "current_a": outlet.current_a if pdu.outlet_metering else None,
                "power_w": outlet.power_w if pdu.outlet_metering else None,
                "locked": outlet.locked,
            }
            entry["pdu"] = {
                "id": pdu.id,
                "name": pdu.name,
                "enabled": pdu.enabled,
                "outlet_metering": pdu.outlet_metering,
                "outlet_switching": pdu.outlet_switching,
                "outlet_control_enabled": pdu.outlet_control_enabled,
                "last_polled_at": _iso(pdu.last_polled_at),
            }
            entry["feeds"] = [
                {"id": i.power_feed.id, "name": i.power_feed.name, "inlet_name": i.name, "active": i.active}
                for i in pdu.inlets
                if i.power_feed is not None
            ]
            entry["can_control"], entry["control_blocked_reason"] = outlet_control_status(outlet)
        inputs.append(entry)

    metered = [i for i in inputs if i["pdu"] and i["pdu"]["outlet_metering"]]
    return {
        "server_id": server.id,
        "inputs": inputs,
        "metered": bool(metered),
        "fully_metered": bool(inputs) and len(metered) == len(inputs),
        "switched": any(i["pdu"] and i["pdu"]["outlet_switching"] for i in inputs),
        "total_power_w": _sum([i["outlet"]["power_w"] for i in metered]),
        "total_current_a": _sum([i["outlet"]["current_a"] for i in metered]),
    }
