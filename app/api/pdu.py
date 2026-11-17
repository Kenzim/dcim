"""PDU API: PDUs, their inlets and outlets, readings and guarded outlet control."""
import asyncio
import logging
from typing import Annotated, Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import require_admin
from app.core.database import get_db
from app.core.openapi_responses import COMMON_ERROR_RESPONSES
from app.dao import LocationDAO, RackDAO
from app.dao.pdu_dao import PduDAO, PowerFeedDAO, ServerPowerInputDAO, pdu_overlaps_range
from app.models.network_switch import NetworkSwitch
from app.models.pdu import Pdu, PduOutlet
from app.models.server import Server
from app.plugins.pdu.base import OUTLET_ACTIONS
from app.plugins.pdu.registry import get_pdu_registry
from app.services import power_service

logger = logging.getLogger(__name__)

DbDep = Annotated[Session, Depends(get_db)]
AdminDep = Annotated[dict, Depends(require_admin)]

router = APIRouter()

MAX_PDU_RACK_UNITS = 4
PLUGIN_PROBE_TIMEOUT_S = 45


class PduCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: Optional[str] = None
    location_id: int
    rack_id: Optional[int] = None
    rack_unit: Optional[int] = None
    rack_units: int = 0
    plugin_name: str
    plugin_config: Dict[str, Any] = {}
    enabled: bool = True
    # Used when the plugin cannot describe the unit (or to override it)
    inlet_count: Optional[int] = None
    outlet_count: Optional[int] = None
    outlet_metering: Optional[bool] = None
    outlet_switching: Optional[bool] = None
    outlet_control_enabled: bool = False
    model: Optional[str] = None


class PduUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = None
    location_id: Optional[int] = None
    rack_id: Optional[int] = None
    rack_unit: Optional[int] = None
    rack_units: Optional[int] = None
    plugin_name: Optional[str] = None
    plugin_config: Optional[Dict[str, Any]] = None
    enabled: Optional[bool] = None
    model: Optional[str] = None
    serial_number: Optional[str] = None
    firmware_version: Optional[str] = None
    outlet_metering: Optional[bool] = None
    outlet_switching: Optional[bool] = None
    outlet_control_enabled: Optional[bool] = None


class PduTestRequest(BaseModel):
    plugin_name: str
    plugin_config: Dict[str, Any] = {}
    pdu_id: Optional[int] = None  # Fill masked secrets from this PDU's stored settings


class RegenerateRequest(BaseModel):
    inlet_count: Optional[int] = None
    outlet_count: Optional[int] = None


class InletUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    power_feed_id: Optional[int] = None  # Send null to unlink


class OutletUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    bank: Optional[str] = Field(default=None, max_length=64)
    description: Optional[str] = None
    locked: Optional[bool] = None
    server_power_input_id: Optional[int] = None  # Send null to unlink


class OutletAction(BaseModel):
    action: str
    confirm: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_pdu_or_404(db: Session, pdu_id: int) -> Pdu:
    pdu = PduDAO.get_by_id(db, pdu_id)
    if not pdu:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PDU not found")
    return pdu


def _get_outlet_or_404(db: Session, pdu: Pdu, outlet_id: int) -> PduOutlet:
    outlet = PduDAO.get_outlet(db, outlet_id)
    if not outlet or outlet.pdu_id != pdu.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Outlet not found")
    return outlet


def _require_plugin(plugin_name: str) -> None:
    if not get_pdu_registry().has(plugin_name):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"PDU plugin '{plugin_name}' not found")


def _validate_placement(
    db: Session,
    location_id: int,
    rack_id: Optional[int],
    rack_unit: Optional[int],
    rack_units: int,
    exclude_pdu_id: Optional[int] = None,
) -> None:
    if rack_units < 0 or rack_units > MAX_PDU_RACK_UNITS:
        raise HTTPException(status_code=400, detail=f"rack_units must be between 0 and {MAX_PDU_RACK_UNITS}")
    if rack_id is None:
        if rack_unit is not None:
            raise HTTPException(status_code=400, detail="rack_unit requires rack_id")
        return
    rack = RackDAO.get_by_id(db, rack_id)
    if not rack:
        raise HTTPException(status_code=404, detail="Rack not found")
    if rack.location_id != location_id:
        raise HTTPException(status_code=400, detail="Rack must be in the same location as the PDU")
    if rack_unit is None or rack_units == 0:
        return
    end = rack_unit + rack_units - 1
    if rack_unit < 1 or end > rack.units:
        raise HTTPException(status_code=400, detail=f"PDU (U{rack_unit}-{end}) must fit within the rack (1-{rack.units})")
    for s in db.query(Server).filter(Server.rack_id == rack_id, Server.rack_unit.isnot(None)).all():
        if rack_unit <= s.rack_unit + (s.rack_units or 1) - 1 and s.rack_unit <= end:
            raise HTTPException(status_code=400, detail=f"Rack position U{rack_unit}-{end} overlaps server {s.name}")
    for sw in db.query(NetworkSwitch).filter(NetworkSwitch.rack_id == rack_id, NetworkSwitch.rack_unit.isnot(None)).all():
        if rack_unit <= sw.rack_unit + (sw.rack_units or 1) - 1 and sw.rack_unit <= end:
            raise HTTPException(status_code=400, detail=f"Rack position U{rack_unit}-{end} overlaps switch {sw.name}")
    if pdu_overlaps_range(db, rack_id, rack_unit, end, exclude_pdu_id=exclude_pdu_id):
        raise HTTPException(status_code=400, detail=f"Rack position U{rack_unit}-{end} overlaps another PDU")


def _iso(dt) -> Optional[str]:
    return dt.isoformat() if dt else None


def _pdu_dict(pdu: Pdu) -> Dict[str, Any]:
    registry = get_pdu_registry()
    capabilities = (
        [c.value for c in registry.get_class(pdu.plugin_name).SUPPORTED_CAPABILITIES]
        if registry.has(pdu.plugin_name)
        else []
    )
    return {
        "id": pdu.id,
        "name": pdu.name,
        "description": pdu.description,
        "location_id": pdu.location_id,
        "rack_id": pdu.rack_id,
        "rack_name": pdu.rack.name if pdu.rack else None,
        "rack_unit": pdu.rack_unit,
        "rack_units": pdu.rack_units,
        "plugin_name": pdu.plugin_name,
        "plugin_config": power_service.mask_plugin_config(pdu.plugin_name, pdu.plugin_config),
        "plugin_capabilities": capabilities,
        "enabled": pdu.enabled,
        "model": pdu.model,
        "serial_number": pdu.serial_number,
        "firmware_version": pdu.firmware_version,
        "inlet_count": pdu.inlet_count,
        "outlet_count": pdu.outlet_count,
        "outlet_metering": pdu.outlet_metering,
        "outlet_switching": pdu.outlet_switching,
        "outlet_control_enabled": pdu.outlet_control_enabled,
        "power_w": power_service.pdu_power_w(pdu),
        "current_a": power_service.pdu_current_a(pdu),
        "last_readings": pdu.last_readings,
        "last_polled_at": _iso(pdu.last_polled_at),
        "last_poll_error": pdu.last_poll_error,
    }


def _pdu_detail(db: Session, pdu: Pdu) -> Dict[str, Any]:
    data = _pdu_dict(pdu)
    data["inlets"] = [
        {
            "id": i.id,
            "position": i.position,
            "name": i.name,
            "power_feed_id": i.power_feed_id,
            "power_feed_name": i.power_feed.name if i.power_feed else None,
            "power_feed_rack_id": i.power_feed.rack_id if i.power_feed else None,
            "voltage_v": i.voltage_v,
            "current_a": i.current_a,
            "power_w": i.power_w,
            "apparent_power_va": i.apparent_power_va,
            "energy_kwh": i.energy_kwh,
            "active": i.active,
        }
        for i in pdu.inlets
    ]
    links = {spi.pdu_outlet_id: spi for spi in ServerPowerInputDAO.get_by_outlets(db, [o.id for o in pdu.outlets])}
    outlets = []
    for o in pdu.outlets:
        spi = links.get(o.id)
        can_control, reason = power_service.outlet_control_status(o)
        outlets.append({
            "id": o.id,
            "position": o.position,
            "name": o.name,
            "bank": o.bank,
            "description": o.description,
            "current_a": o.current_a,
            "power_w": o.power_w,
            "state": o.state,
            "locked": o.locked,
            "can_control": can_control,
            "control_blocked_reason": reason,
            "server_power_input": (
                {
                    "id": spi.id,
                    "name": spi.name,
                    "server_id": spi.server_id,
                    "server_name": spi.server.name if spi.server else None,
                }
                if spi
                else None
            ),
        })
    data["outlets"] = outlets
    return data


async def _probe_info(plugin_name: str, config: Dict[str, Any]) -> Dict[str, Any]:
    plugin = get_pdu_registry().get_plugin(plugin_name, config)
    return await asyncio.wait_for(plugin.get_pdu_info(), timeout=PLUGIN_PROBE_TIMEOUT_S)


def _apply_overrides(info: Dict[str, Any], **overrides: Any) -> Dict[str, Any]:
    merged = dict(info)
    for key, value in overrides.items():
        if value is not None:
            merged[key] = value
    return merged


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/plugins", response_model=List[dict], responses=COMMON_ERROR_RESPONSES)
async def list_pdu_plugins(auth: AdminDep):
    """PDU plugins with their capabilities and settings form."""
    return get_pdu_registry().list_plugins()


@router.get("/", response_model=List[dict], responses=COMMON_ERROR_RESPONSES)
async def list_pdus(
    location_id: Optional[int] = None,
    rack_id: Optional[int] = None,
    *,
    auth: AdminDep,
    db: DbDep,
):
    return [_pdu_dict(p) for p in PduDAO.get_all(db, location_id=location_id, rack_id=rack_id)]


@router.post("/", response_model=dict, status_code=status.HTTP_201_CREATED, responses=COMMON_ERROR_RESPONSES)
async def create_pdu(data: PduCreate, auth: AdminDep, db: DbDep):
    """Create a PDU and generate its inlets and outlets from the plugin (or the given counts)."""
    _require_plugin(data.plugin_name)
    if not LocationDAO.get_by_id(db, data.location_id):
        raise HTTPException(status_code=404, detail="Location not found")
    if PduDAO.get_by_name(db, data.name):
        raise HTTPException(status_code=400, detail="PDU with this name already exists")
    _validate_placement(db, data.location_id, data.rack_id, data.rack_unit, data.rack_units)

    pdu = Pdu(
        name=data.name,
        description=data.description,
        location_id=data.location_id,
        rack_id=data.rack_id,
        rack_unit=data.rack_unit if data.rack_units > 0 else None,
        rack_units=data.rack_units,
        plugin_name=data.plugin_name,
        plugin_config=data.plugin_config or {},
        enabled=data.enabled,
        model=data.model,
        outlet_control_enabled=data.outlet_control_enabled,
    )
    db.add(pdu)
    db.commit()
    db.refresh(pdu)

    probe_error = None
    info: Dict[str, Any] = {}
    try:
        info = await _probe_info(pdu.plugin_name, pdu.plugin_config)
    except asyncio.TimeoutError:
        probe_error = "PDU did not answer in time"
    except Exception as exc:
        probe_error = str(exc)
    info = _apply_overrides(
        info,
        inlet_count=data.inlet_count,
        outlet_count=data.outlet_count,
        outlet_metering=data.outlet_metering,
        outlet_switching=data.outlet_switching,
    )
    if "inlet_count" not in info and not info.get("inlets"):
        info["inlet_count"] = 1
    stats = power_service.sync_pdu_ports(db, pdu, info)
    result = _pdu_detail(db, pdu)
    result["port_generation"] = {**stats, "probe_error": probe_error}
    return result


@router.post("/test", response_model=dict, responses=COMMON_ERROR_RESPONSES)
async def test_pdu_connection(data: PduTestRequest, auth: AdminDep, db: DbDep):
    """Test the plugin settings and, if that works, describe the unit."""
    _require_plugin(data.plugin_name)
    config = data.plugin_config or {}
    if data.pdu_id is not None:
        existing = PduDAO.get_by_id(db, data.pdu_id)
        if existing:
            config = power_service.merge_plugin_config(data.plugin_name, config, existing.plugin_config)
    try:
        plugin = get_pdu_registry().get_plugin(data.plugin_name, config)
        result = await asyncio.wait_for(plugin.test_connection(), timeout=PLUGIN_PROBE_TIMEOUT_S)
    except asyncio.TimeoutError:
        return {"success": False, "message": "PDU did not answer in time", "details": {}}
    except Exception as exc:
        return {"success": False, "message": str(exc), "details": {}}
    if result.get("success"):
        try:
            result["pdu_info"] = await _probe_info(data.plugin_name, config)
        except Exception as exc:
            logger.warning("PDU info after successful test failed: %s", exc)
            result["pdu_info"] = None
    return result


@router.get("/{pdu_id}", response_model=dict, responses=COMMON_ERROR_RESPONSES)
async def get_pdu(pdu_id: int, auth: AdminDep, db: DbDep):
    return _pdu_detail(db, _get_pdu_or_404(db, pdu_id))


@router.put("/{pdu_id}", response_model=dict, responses=COMMON_ERROR_RESPONSES)
async def update_pdu(pdu_id: int, data: PduUpdate, auth: AdminDep, db: DbDep):
    pdu = _get_pdu_or_404(db, pdu_id)
    fields = data.model_fields_set

    if data.name is not None and data.name != pdu.name:
        if PduDAO.get_by_name(db, data.name):
            raise HTTPException(status_code=400, detail="PDU with this name already exists")
        pdu.name = data.name
    if data.location_id is not None and data.location_id != pdu.location_id:
        if not LocationDAO.get_by_id(db, data.location_id):
            raise HTTPException(status_code=404, detail="Location not found")
        pdu.location_id = data.location_id
    if fields & {"rack_id", "rack_unit", "rack_units", "location_id"}:
        rack_id = data.rack_id if "rack_id" in fields else pdu.rack_id
        rack_units = data.rack_units if data.rack_units is not None else pdu.rack_units
        rack_unit = data.rack_unit if "rack_unit" in fields else pdu.rack_unit
        if rack_id is None or rack_units == 0:
            rack_unit = None
        _validate_placement(db, pdu.location_id, rack_id, rack_unit, rack_units, exclude_pdu_id=pdu.id)
        pdu.rack_id, pdu.rack_unit, pdu.rack_units = rack_id, rack_unit, rack_units
    if data.plugin_name is not None and data.plugin_name != pdu.plugin_name:
        _require_plugin(data.plugin_name)
        pdu.plugin_name = data.plugin_name
    if data.plugin_config is not None:
        pdu.plugin_config = power_service.merge_plugin_config(pdu.plugin_name, data.plugin_config, pdu.plugin_config)
    for field in (
        "description", "enabled", "model", "serial_number", "firmware_version",
        "outlet_metering", "outlet_switching", "outlet_control_enabled",
    ):
        value = getattr(data, field)
        if value is not None:
            setattr(pdu, field, value)
    db.commit()
    db.refresh(pdu)
    return _pdu_detail(db, pdu)


@router.delete("/{pdu_id}", status_code=status.HTTP_204_NO_CONTENT, responses=COMMON_ERROR_RESPONSES)
async def delete_pdu(pdu_id: int, auth: AdminDep, db: DbDep):
    pdu = _get_pdu_or_404(db, pdu_id)
    outlet_ids = [o.id for o in pdu.outlets]
    for spi in ServerPowerInputDAO.get_by_outlets(db, outlet_ids):
        spi.pdu_outlet_id = None
    db.delete(pdu)
    db.commit()


@router.post("/{pdu_id}/regenerate-ports", response_model=dict, responses=COMMON_ERROR_RESPONSES)
async def regenerate_pdu_ports(pdu_id: int, auth: AdminDep, db: DbDep, data: Optional[RegenerateRequest] = None):
    """Re-read the unit and add, rename or remove inlets and outlets. Linked ones are never removed."""
    pdu = _get_pdu_or_404(db, pdu_id)
    data = data or RegenerateRequest()
    try:
        info = await _probe_info(pdu.plugin_name, pdu.plugin_config or {})
    except asyncio.TimeoutError:
        if data.inlet_count is None and data.outlet_count is None:
            raise HTTPException(status_code=503, detail="PDU did not answer in time")
        info = {}
    except Exception as exc:
        if data.inlet_count is None and data.outlet_count is None:
            raise HTTPException(status_code=502, detail=f"Could not read the PDU: {exc}")
        info = {}
    info = _apply_overrides(info, inlet_count=data.inlet_count, outlet_count=data.outlet_count)
    if "inlet_count" not in info and not info.get("inlets"):
        info["inlet_count"] = pdu.inlet_count
    if "outlet_count" not in info and not info.get("outlets"):
        info["outlet_count"] = pdu.outlet_count
    stats = power_service.sync_pdu_ports(db, pdu, info)
    return {"success": True, **stats, "pdu": _pdu_detail(db, pdu)}


@router.post("/{pdu_id}/poll", response_model=dict, responses=COMMON_ERROR_RESPONSES)
async def poll_pdu_now(pdu_id: int, auth: AdminDep, db: DbDep):
    """Read the PDU now and store the readings."""
    pdu = _get_pdu_or_404(db, pdu_id)
    result = await power_service.poll_pdu(db, pdu)
    db.refresh(pdu)
    return {**result, "pdu": _pdu_detail(db, pdu)}


@router.put("/{pdu_id}/inlets/{inlet_id}", response_model=dict, responses=COMMON_ERROR_RESPONSES)
async def update_inlet(pdu_id: int, inlet_id: int, data: InletUpdate, auth: AdminDep, db: DbDep):
    """Rename an inlet or link it to a power feed (null unlinks)."""
    pdu = _get_pdu_or_404(db, pdu_id)
    inlet = PduDAO.get_inlet(db, inlet_id)
    if not inlet or inlet.pdu_id != pdu.id:
        raise HTTPException(status_code=404, detail="Inlet not found")
    if data.name is not None:
        inlet.name = data.name
    if "power_feed_id" in data.model_fields_set:
        if data.power_feed_id is not None:
            feed = PowerFeedDAO.get_by_id(db, data.power_feed_id)
            if not feed:
                raise HTTPException(status_code=404, detail="Power feed not found")
            if feed.rack and feed.rack.location_id != pdu.location_id:
                raise HTTPException(status_code=400, detail="Power feed must be in the same location as the PDU")
        inlet.power_feed_id = data.power_feed_id
    db.commit()
    db.refresh(pdu)
    return _pdu_detail(db, pdu)


@router.put("/{pdu_id}/outlets/{outlet_id}", response_model=dict, responses=COMMON_ERROR_RESPONSES)
async def update_outlet(pdu_id: int, outlet_id: int, data: OutletUpdate, auth: AdminDep, db: DbDep):
    """Edit an outlet, or link it to a server power input (null unlinks)."""
    pdu = _get_pdu_or_404(db, pdu_id)
    outlet = _get_outlet_or_404(db, pdu, outlet_id)
    for field in ("name", "bank", "description", "locked"):
        value = getattr(data, field)
        if value is not None:
            setattr(outlet, field, value)
    if "server_power_input_id" in data.model_fields_set:
        current = ServerPowerInputDAO.get_by_outlet(db, outlet.id)
        if data.server_power_input_id is None:
            if current:
                current.pdu_outlet_id = None
        else:
            spi = ServerPowerInputDAO.get_by_id(db, data.server_power_input_id)
            if not spi:
                raise HTTPException(status_code=404, detail="Server power input not found")
            if current and current.id != spi.id:
                raise HTTPException(
                    status_code=409,
                    detail=f"Outlet already feeds {current.server.name if current.server else 'a server'} {current.name}",
                )
            spi.pdu_outlet_id = outlet.id
    db.commit()
    db.refresh(pdu)
    return _pdu_detail(db, pdu)


@router.post("/{pdu_id}/outlets/{outlet_id}/action", response_model=dict, responses=COMMON_ERROR_RESPONSES)
async def outlet_action(pdu_id: int, outlet_id: int, data: OutletAction, auth: AdminDep, db: DbDep):
    """Switch an outlet on, off or power-cycle it. Needs confirm=true and outlet control enabled on the PDU."""
    pdu = _get_pdu_or_404(db, pdu_id)
    outlet = _get_outlet_or_404(db, pdu, outlet_id)
    return await run_outlet_action(db, outlet, data, source=_admin_source(auth))


def _admin_source(auth: dict) -> str:
    return f"admin:{auth.get('username') or auth.get('user_id') or 'admin'}"


async def run_outlet_action(db: Session, outlet: PduOutlet, data: OutletAction, source: str) -> Dict[str, Any]:
    """Shared by the PDU and server routes: validate, then switch the outlet."""
    if data.action not in OUTLET_ACTIONS:
        raise HTTPException(status_code=400, detail=f"action must be one of {', '.join(OUTLET_ACTIONS)}")
    if not data.confirm:
        raise HTTPException(status_code=400, detail="Outlet actions need confirm=true")
    allowed, reason = power_service.outlet_control_status(outlet)
    if not allowed:
        raise HTTPException(status_code=403, detail=reason)
    try:
        return await power_service.perform_outlet_action(db, outlet, data.action, source=source)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except NotImplementedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("Outlet action failed")
        raise HTTPException(status_code=502, detail=f"Outlet action failed: {exc}")

