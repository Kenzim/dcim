from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel
import asyncio
from datetime import datetime, timezone, timedelta
from app.core.database import get_db, SessionLocal
from app.core.auth import require_admin
from app.dao import NetworkSwitchDAO, LocationDAO, RackDAO, SwitchPortDAO, CableRunDAO, NetworkPortDAO, SwitchBandwidthSampleDAO
from app.models.network_switch import NetworkSwitch
from app.models.server import Server
from app.plugins.switch_registry import get_switch_registry
from app.core.plugin_capabilities import get_switch_plugin_capabilities, switch_plugin_supports
import logging

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_SWITCH_RACK_UNITS = 10


def _rack_placement_overlaps_switch(
    db: Session,
    rack_id: int,
    rack_unit: int,
    rack_units: int,
    exclude_switch_id: Optional[int] = None,
) -> bool:
    """Return True if the given placement overlaps any server or other switch in the rack."""
    start, end = rack_unit, rack_unit + rack_units - 1
    for s in db.query(Server).filter(Server.rack_id == rack_id, Server.rack_unit.isnot(None)).all():
        s_end = s.rack_unit + (getattr(s, "rack_units", 1) or 1) - 1
        if start <= s_end and s.rack_unit <= end:
            return True
    q = db.query(NetworkSwitch).filter(
        NetworkSwitch.rack_id == rack_id,
        NetworkSwitch.rack_unit.isnot(None),
    )
    if exclude_switch_id is not None:
        q = q.filter(NetworkSwitch.id != exclude_switch_id)
    for sw in q.all():
        sw_end = sw.rack_unit + (getattr(sw, "rack_units", 1) or 1) - 1
        if start <= sw_end and sw.rack_unit <= end:
            return True
    return False


class NetworkSwitchCreate(BaseModel):
    name: str
    description: str | None = None
    location_id: int
    rack_id: int | None = None
    rack_unit: int | None = None
    plugin_name: str
    plugin_config: dict
    enabled: bool = True
    port_count: int | None = None
    model: str | None = None
    serial_number: str | None = None
    firmware_version: str | None = None


class NetworkSwitchUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    location_id: int | None = None
    rack_id: int | None = None
    rack_unit: int | None = None
    rack_units: int | None = None
    plugin_name: str | None = None
    plugin_config: dict | None = None
    enabled: bool | None = None
    port_count: int | None = None
    model: str | None = None
    serial_number: str | None = None
    firmware_version: str | None = None


class NetworkSwitchResponse(BaseModel):
    id: int
    name: str
    description: str | None
    location_id: int
    rack_id: int | None = None
    rack_unit: int | None = None
    rack_units: int = 1
    plugin_name: str
    plugin_config: dict
    enabled: bool
    port_count: int | None = None
    model: str | None = None
    serial_number: str | None = None
    firmware_version: str | None = None
    tested_capabilities: List[str] | None = None
    test_logs: str | None = None

    class Config:
        from_attributes = True


class NetworkSwitchTestRequest(BaseModel):
    plugin_name: str
    plugin_config: dict


class SwitchPortUpdate(BaseModel):
    """Payload for updating a single switch port's physical spec."""
    id: int
    speed_mbps: int | None = None
    description: str | None = None


class SwitchPortBulkUpdate(BaseModel):
    """Bulk update payload for switch ports."""
    ports: List[SwitchPortUpdate]


@router.get("/", response_model=List[NetworkSwitchResponse])
async def list_switches(
    skip: int = 0,
    limit: int = 100,
    enabled_only: bool = False,
    location_id: int | None = None,
    rack_id: int | None = None,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """List all network switches"""
    if location_id:
        switches = NetworkSwitchDAO.get_by_location(db, location_id)
    elif rack_id:
        switches = NetworkSwitchDAO.get_by_rack(db, rack_id)
    else:
        switches = NetworkSwitchDAO.get_all(db, skip=skip, limit=limit, enabled_only=enabled_only)
    
    result = []
    for switch in switches:
        switch_dict = {k: v.value if hasattr(v, 'value') else v for k, v in switch.__dict__.items()}
        if switch_dict.get("rack_units") is None:
            switch_dict["rack_units"] = 1
        result.append(switch_dict)
    return result


@router.post("/", response_model=NetworkSwitchResponse, status_code=status.HTTP_201_CREATED)
async def create_switch(
    switch_data: NetworkSwitchCreate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Create a new network switch"""
    # Validate plugin exists in registry
    registry = get_switch_registry()
    if switch_data.plugin_name not in registry._plugins:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Switch plugin '{switch_data.plugin_name}' not found in registry"
        )
    
    # Validate location exists
    location = LocationDAO.get_by_id(db, switch_data.location_id)
    if not location:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Location not found"
        )
    
    # Validate rack if provided
    if switch_data.rack_id is not None:
        rack = RackDAO.get_by_id(db, switch_data.rack_id)
        if not rack:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Rack not found"
            )
        # Ensure rack is in the same location
        if rack.location_id != switch_data.location_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Rack must be in the same location as the switch"
            )
        # Validate rack_unit and rack_units if provided
        rack_units_create = getattr(switch_data, "rack_units", 1) or 1
        if rack_units_create < 1 or rack_units_create > min(rack.units, MAX_SWITCH_RACK_UNITS):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"rack_units must be between 1 and {min(rack.units, MAX_SWITCH_RACK_UNITS)}"
            )
        if switch_data.rack_unit is not None:
            if switch_data.rack_unit < 1 or switch_data.rack_unit > rack.units:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Rack unit must be between 1 and {rack.units}"
                )
            if switch_data.rack_unit + rack_units_create - 1 > rack.units:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Switch (U{switch_data.rack_unit}-{switch_data.rack_unit + rack_units_create - 1}) would extend past rack height ({rack.units}U)"
                )
            if _rack_placement_overlaps_switch(db, switch_data.rack_id, switch_data.rack_unit, rack_units_create):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Rack position U{switch_data.rack_unit}-{switch_data.rack_unit + rack_units_create - 1} overlaps a server or switch"
                )
    
    # Check if switch with same name already exists
    existing = NetworkSwitchDAO.get_by_name(db, switch_data.name)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Switch with this name already exists"
        )
    
    # Create switch
    rack_units_create = getattr(switch_data, "rack_units", 1) or 1
    switch = NetworkSwitchDAO.create(
        db,
        name=switch_data.name,
        description=switch_data.description,
        location_id=switch_data.location_id,
        rack_id=switch_data.rack_id,
        rack_unit=switch_data.rack_unit,
        rack_units=rack_units_create,
        plugin_name=switch_data.plugin_name,
        plugin_config=switch_data.plugin_config,
        enabled=switch_data.enabled,
        port_count=switch_data.port_count,
        model=switch_data.model,
        serial_number=switch_data.serial_number,
        firmware_version=switch_data.firmware_version
    )
    
    # Populate switch ports in the background so we don't block the response.
    # Port population can take a long time (many SNMP round-trips); user can also use "Regenerate ports" on the switch detail page.
    if switch_plugin_supports(switch_data.plugin_name, "monitoring"):
        async def _populate_ports_background(switch_id: int, switch_name: str, plugin_name: str, plugin_config: dict):
            db_bg = SessionLocal()
            try:
                plugin_instance = registry.get_plugin(plugin_name, plugin_config)
                port_stats = await plugin_instance.get_all_port_statistics()
                ports_to_create = []
                for port_name, port_data in port_stats.items():
                    if port_data.get("ifType") == 6:  # ethernetCsmacd
                        ports_to_create.append({
                            "name": port_name,
                            "ifIndex": port_data.get("ifIndex"),
                            "ifSpeed": port_data.get("ifSpeed", 0),
                            "ifAdminStatus": port_data.get("ifAdminStatus"),
                            "ifOperStatus": port_data.get("ifOperStatus"),
                            "description": f"Port {port_name}"
                        })
                if ports_to_create:
                    SwitchPortDAO.create_bulk(db_bg, switch_id, ports_to_create)
                    logger.info(f"Created {len(ports_to_create)} ports for switch {switch_name}")
                else:
                    logger.warning(f"No physical Ethernet ports found for switch {switch_name} (found {len(port_stats)} total interfaces)")
            except Exception as e:
                logger.exception(f"Failed to populate ports for switch {switch_name}: {e}")
            finally:
                db_bg.close()
        
        asyncio.create_task(_populate_ports_background(
            switch.id, switch.name, switch_data.plugin_name, switch_data.plugin_config
        ))
    
    switch_dict = {k: v.value if hasattr(v, 'value') else v for k, v in switch.__dict__.items()}
    if switch_dict.get("rack_units") is None:
        switch_dict["rack_units"] = 1
    return switch_dict


@router.get("/{switch_id}", response_model=NetworkSwitchResponse)
async def get_switch(
    switch_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Get a network switch by ID"""
    switch = NetworkSwitchDAO.get_by_id(db, switch_id)
    if not switch:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Switch not found"
        )
    
    switch_dict = {k: v.value if hasattr(v, 'value') else v for k, v in switch.__dict__.items()}
    if switch_dict.get("rack_units") is None:
        switch_dict["rack_units"] = 1
    return switch_dict


@router.put("/{switch_id}", response_model=NetworkSwitchResponse)
async def update_switch(
    switch_id: int,
    switch_data: NetworkSwitchUpdate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Update a network switch"""
    switch = NetworkSwitchDAO.get_by_id(db, switch_id)
    if not switch:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Switch not found"
        )
    
    # Update fields if provided
    if switch_data.name is not None:
        # Check if name is being changed and if new name already exists
        if switch_data.name != switch.name:
            existing = NetworkSwitchDAO.get_by_name(db, switch_data.name)
            if existing:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Switch with this name already exists"
                )
        switch.name = switch_data.name
    
    if switch_data.description is not None:
        switch.description = switch_data.description
    if switch_data.enabled is not None:
        switch.enabled = switch_data.enabled
    if switch_data.port_count is not None:
        switch.port_count = switch_data.port_count
    if switch_data.model is not None:
        switch.model = switch_data.model
    if switch_data.serial_number is not None:
        switch.serial_number = switch_data.serial_number
    if switch_data.firmware_version is not None:
        switch.firmware_version = switch_data.firmware_version
    
    if switch_data.location_id is not None:
        location = LocationDAO.get_by_id(db, switch_data.location_id)
        if not location:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Location not found"
            )
        switch.location_id = switch_data.location_id
    
    # Handle rack assignment
    if switch_data.rack_id is not None or switch_data.rack_unit is not None or getattr(switch_data, "rack_units", None) is not None:
        # If rack_id is being cleared (set to None explicitly), clear both
        if switch_data.rack_id is None:
            switch.rack_id = None
            switch.rack_unit = None
            switch.rack_units = 1
        else:
            # Validate rack exists
            rack = RackDAO.get_by_id(db, switch_data.rack_id)
            if not rack:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Rack not found"
                )
            # Ensure rack is in the same location as switch
            if rack.location_id != switch.location_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Rack must be in the same location as the switch"
                )
            switch.rack_id = switch_data.rack_id
            rack_units_val = getattr(switch_data, "rack_units", None) if getattr(switch_data, "rack_units", None) is not None else (getattr(switch, "rack_units", 1) or 1)
            if rack_units_val < 1 or rack_units_val > min(rack.units, MAX_SWITCH_RACK_UNITS):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"rack_units must be between 1 and {min(rack.units, MAX_SWITCH_RACK_UNITS)}"
                )
            switch.rack_units = rack_units_val
            effective_rack_unit = switch_data.rack_unit if switch_data.rack_unit is not None else switch.rack_unit
            if effective_rack_unit is not None:
                if effective_rack_unit < 1 or effective_rack_unit > rack.units:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Rack unit must be between 1 and {rack.units}"
                    )
                if effective_rack_unit + rack_units_val - 1 > rack.units:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Switch (U{effective_rack_unit}-{effective_rack_unit + rack_units_val - 1}) would extend past rack height ({rack.units}U)"
                    )
                if _rack_placement_overlaps_switch(db, switch_data.rack_id, effective_rack_unit, rack_units_val, exclude_switch_id=switch.id):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Rack position U{effective_rack_unit}-{effective_rack_unit + rack_units_val - 1} overlaps a server or switch"
                    )
                switch.rack_unit = effective_rack_unit
            elif switch_data.rack_id is not None:
                switch.rack_unit = None
    
    if switch_data.plugin_name is not None:
        # Validate plugin exists in registry
        registry = get_switch_registry()
        if switch_data.plugin_name not in registry._plugins:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Switch plugin '{switch_data.plugin_name}' not found in registry"
            )
        switch.plugin_name = switch_data.plugin_name
    
    if switch_data.plugin_config is not None:
        switch.plugin_config = switch_data.plugin_config
    
    switch = NetworkSwitchDAO.update(db, switch)
    
    switch_dict = {k: v.value if hasattr(v, 'value') else v for k, v in switch.__dict__.items()}
    if switch_dict.get("rack_units") is None:
        switch_dict["rack_units"] = 1
    return switch_dict


@router.delete("/{switch_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_switch(
    switch_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Delete a network switch"""
    success = NetworkSwitchDAO.delete(db, switch_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Switch not found"
        )


@router.post("/test", response_model=dict)
async def test_switch_connection(
    test_data: NetworkSwitchTestRequest,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Test switch connection using plugin's test_connection method"""
    # Get switch plugin instance from registry
    registry = get_switch_registry()
    try:
        plugin_instance = registry.get_plugin(test_data.plugin_name, test_data.plugin_config)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Plugin '{test_data.plugin_name}' not found in switch registry"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to instantiate plugin: {str(e)}"
        )
    
    # Call test_connection
    try:
        result = await plugin_instance.test_connection()
        
        # If connection test succeeds, also get switch info
        if result.get("success"):
            try:
                switch_info = await plugin_instance.get_switch_info()
                result["switch_info"] = switch_info
            except Exception as e:
                # Don't fail the test if info retrieval fails, just log it
                logger.warning(f"Failed to get switch info after successful connection test: {e}")
                result["switch_info"] = None
        
        return result
    except Exception as e:
        return {
            "success": False,
            "message": f"Connection test failed: {str(e)}",
            "details": {"error": str(e)}
        }


@router.get("/{switch_id}/switch-ports", response_model=dict)
async def get_switch_ports_db(
    switch_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Get list of switch ports from database (with cable run info)"""
    switch = NetworkSwitchDAO.get_by_id(db, switch_id)
    if not switch:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Switch not found"
        )
    
    # Get ports from database
    ports = SwitchPortDAO.get_by_switch(db, switch_id)
    
    # Get cable runs for this switch (device-agnostic: other end can be switch or server)
    cable_runs = CableRunDAO.get_by_switch(db, switch_id)
    cable_run_map = {}
    for cr in cable_runs:
        if cr.end_a_switch_port_id is not None:
            cable_run_map[cr.end_a_switch_port_id] = cr
        if cr.end_b_switch_port_id is not None:
            cable_run_map[cr.end_b_switch_port_id] = cr

    result = []
    for port in ports:
        port_dict = {
            "id": port.id,
            "name": port.name,
            "if_index": port.if_index,
            "speed_mbps": port.speed_mbps,
            "admin_status": port.admin_status,
            "oper_status": port.oper_status,
            "description": port.description,
            "cable_run": None
        }

        if port.id in cable_run_map:
            cr = cable_run_map[port.id]
            other = cr.get_other_end(switch_port_id=port.id)
            if other:
                other_type, other_port_id = other
                port_name = None
                device_id = None
                device_name = None
                if other_type == "switch":
                    other_port = SwitchPortDAO.get_by_id(db, other_port_id)
                    if other_port and other_port.switch:
                        port_name = other_port.name
                        device_id = other_port.switch_id
                        device_name = other_port.switch.name
                else:
                    other_port = NetworkPortDAO.get_by_id(db, other_port_id)
                    if other_port and other_port.server:
                        port_name = other_port.name
                        device_id = other_port.server_id
                        device_name = other_port.server.name
                port_dict["cable_run"] = {
                    "id": cr.id,
                    "other_end_type": other_type,
                    "other_end_port_id": other_port_id,
                    "other_end_port_name": port_name,
                    "other_end_device_id": device_id,
                    "other_end_device_name": device_name,
                    "cable_type": cr.cable_type,
                    "speed_mbps": cr.speed_mbps,
                    "description": cr.description
                }
        result.append(port_dict)
    
    return {
        "success": True,
        "ports": result,
        "count": len(result)
    }


@router.put("/{switch_id}/switch-ports", response_model=dict)
async def update_switch_ports_db(
    switch_id: int,
    payload: SwitchPortBulkUpdate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Bulk update switch ports (physical speed / description)."""
    switch = NetworkSwitchDAO.get_by_id(db, switch_id)
    if not switch:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Switch not found"
        )
    if not payload.ports:
        return {"success": True, "updated": 0, "errors": []}

    updated = 0
    errors: List[dict] = []

    for upd in payload.ports:
        port = SwitchPortDAO.get_by_id(db, upd.id)
        if not port or port.switch_id != switch_id:
            errors.append({"id": upd.id, "error": "not_found"})
            continue
        changed = False
        if upd.speed_mbps is not None and upd.speed_mbps != port.speed_mbps:
            port.speed_mbps = upd.speed_mbps
            changed = True
        if upd.description is not None and upd.description != port.description:
            port.description = upd.description
            changed = True
        if changed:
            updated += 1

    if updated:
        db.commit()

    return {
        "success": True,
        "updated": updated,
        "errors": errors,
    }


@router.get("/{switch_id}/ports", response_model=dict)
async def get_switch_ports(
    switch_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Get list of ports and their statistics for a switch (live from plugin)"""
    switch = NetworkSwitchDAO.get_by_id(db, switch_id)
    if not switch:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Switch not found"
        )
    
    # Get switch plugin instance from registry
    registry = get_switch_registry()
    try:
        plugin_instance = registry.get_plugin(switch.plugin_name, switch.plugin_config)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Plugin '{switch.plugin_name}' not found in switch registry"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to instantiate plugin: {str(e)}"
        )
    
    # Check if plugin supports monitoring
    if not switch_plugin_supports(switch.plugin_name, "monitoring"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Plugin '{switch.plugin_name}' does not support monitoring"
        )
    
    # Get port statistics
    try:
        ports = await plugin_instance.get_all_port_statistics()
        return {
            "success": True,
            "ports": ports,
            "count": len(ports)
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get port statistics: {str(e)}"
        )


# 32-bit ifSpeed max; devices use this for high-speed ports when real speed > ~4 Gbps.
_IF_SPEED_32BIT_MAX_BPS = 4_294_967_295


def _physical_speed_mbps(port_name: str, port_data: dict) -> int | None:
    """Physical port capability in Mbps. We always show port spec, never link/operational speed.

    Preference order (name first so we can rely on naming when SNMP is wrong or link-speed):
      1. Heuristics from port name (25G, 100G, 10G, etc.)
      2. ifHighSpeed (Mbps) from IF-MIB::ifXTable
      3. ifSpeed (bps) from IF-MIB::ifTable only when valid (reject 32-bit overflow value).
    """
    # 1) Infer from port name first (physical capability; good for testing and when SNMP reports link speed)
    name = (port_name or "").lower()
    if "100g" in name or "100gig" in name:
        return 100_000
    if "40g" in name or "40gig" in name:
        return 40_000
    if "25g" in name or "25gig" in name:
        return 25_000
    if "10g" in name or "tengigabit" in name or "10gig" in name:
        return 10_000
    if "gigabit" in name or "1g" in name or "ge" in name:
        return 1_000
    if "fast" in name or "100m" in name or "fe" in name:
        return 100

    # 2) Use ifHighSpeed when available (physical/capability speed in Mbps)
    hs = port_data.get("ifHighSpeed")
    try:
        if hs is not None:
            hs_val = int(hs)
            if hs_val > 0:
                return hs_val
    except (ValueError, TypeError):
        pass

    # 3) Fallback: ifSpeed (bps) only when it's a real value, not the 32-bit overflow
    if_speed = port_data.get("ifSpeed")
    if if_speed is not None:
        try:
            bps = int(if_speed)
            if bps > 0 and bps < _IF_SPEED_32BIT_MAX_BPS:
                return bps // 1_000_000
        except (ValueError, TypeError):
            pass
    return None


@router.post("/{switch_id}/regenerate-ports", response_model=dict)
async def regenerate_switch_ports(
    switch_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Regenerate switch ports from plugin"""
    print(f"Regenerate ports: starting for switch_id={switch_id}")
    switch = NetworkSwitchDAO.get_by_id(db, switch_id)
    if not switch:
        logger.warning(f"Regenerate ports: switch_id={switch_id} not found")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Switch not found"
        )
    print(f"Regenerate ports: switch '{switch.name}' (plugin={switch.plugin_name})")
    
    # Check if plugin supports monitoring
    if not switch_plugin_supports(switch.plugin_name, "monitoring"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Plugin '{switch.plugin_name}' does not support monitoring"
        )
    
    # Get switch plugin instance from registry
    registry = get_switch_registry()
    try:
        plugin_instance = registry.get_plugin(switch.plugin_name, switch.plugin_config)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Plugin '{switch.plugin_name}' not found in registry"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to instantiate plugin: {str(e)}"
        )
    
    try:
        # Get port statistics from plugin
        print(f"Regenerate ports: fetching port statistics from plugin for '{switch.name}' (SNMP/API may take a few seconds)...")
        port_stats = await plugin_instance.get_all_port_statistics()
        total_interfaces = len(port_stats)
        ethernet_count = sum(1 for p in port_stats.values() if p.get("ifType") == 6)
        print(f"Regenerate ports: got {total_interfaces} interfaces from plugin ({ethernet_count} physical Ethernet, ifType=6)")
        
        if total_interfaces == 0:
            logger.warning(f"Regenerate ports: switch '{switch.name}' returned no interfaces (SNMP timeout or unreachable)")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Switch did not respond to SNMP in time or returned no interfaces. Check connectivity and SNMP settings, then try again."
            )
        
        # Get existing ports to check for cable runs
        existing_ports = SwitchPortDAO.get_by_switch(db, switch_id)
        print(f"Regenerate ports: found {len(existing_ports)} existing ports in DB")
        existing_port_ids_with_cables = set()
        for port in existing_ports:
            cable_run = CableRunDAO.get_by_switch_port(db, port.id)
            if cable_run:
                existing_port_ids_with_cables.add(port.id)
        print(f"Regenerate ports: {len(existing_port_ids_with_cables)} existing ports have cable runs (will not delete those)")
        
        # Delete existing ports that don't have cable runs
        ports_to_delete = [p.id for p in existing_ports if p.id not in existing_port_ids_with_cables]
        if ports_to_delete:
            print(f"Regenerate ports: deleting {len(ports_to_delete)} existing ports without cable runs")
            for port_id in ports_to_delete:
                SwitchPortDAO.delete(db, port_id)
        else:
            print("Regenerate ports: no existing ports to delete")

        # Map port name -> SNMP data for physical Ethernet ports (for create + update)
        name_to_port_data = {
            name: data for name, data in port_stats.items()
            if data.get("ifType") == 6
        }

        # Update existing ports' physical spec (speed_mbps, if_index) from current SNMP data
        updated_count = 0
        for port in existing_ports:
            if port.id in ports_to_delete:
                continue
            port_data = name_to_port_data.get(port.name)
            if not port_data:
                continue
            speed_mbps = _physical_speed_mbps(port.name, port_data)
            if_index = port_data.get("ifIndex")
            if speed_mbps != port.speed_mbps or if_index != port.if_index:
                port.speed_mbps = speed_mbps
                port.if_index = if_index if if_index is not None else port.if_index
                SwitchPortDAO.update(db, port)
                updated_count += 1
        if updated_count:
            print(f"Regenerate ports: updated speed/if_index for {updated_count} existing ports")
        
        # Create ports that don't exist yet (physical Ethernet only)
        ports_to_create = []
        existing_port_names = {p.name for p in existing_ports}
        for port_name, port_data in name_to_port_data.items():
            if port_name not in existing_port_names:
                ports_to_create.append({
                    "name": port_name,
                    "ifIndex": port_data.get("ifIndex"),
                    "speed_mbps": _physical_speed_mbps(port_name, port_data),
                    "description": f"Port {port_name}"
                })
        
        created_count = 0
        if ports_to_create:
            print(f"Regenerate ports: creating {len(ports_to_create)} new ports for '{switch.name}'")
            SwitchPortDAO.create_bulk(db, switch.id, ports_to_create)
            created_count = len(ports_to_create)
        else:
            print("Regenerate ports: no new ports to create")
        
        print(f"Regenerate ports: done for '{switch.name}' — created={created_count}, updated={updated_count}, deleted={len(ports_to_delete)}")
        return {
            "success": True,
            "message": f"Regenerated ports: {created_count} created, {updated_count} updated, {len(ports_to_delete)} removed",
            "created": created_count,
            "updated": updated_count,
            "deleted": len(ports_to_delete)
        }
    except Exception as e:
        logger.exception(f"Regenerate ports: failed for switch '{switch.name}': {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to regenerate ports: {str(e)}"
        )


class BandwidthSampleResponse(BaseModel):
    sampled_at: str
    bytes_in: int
    bytes_out: int
    bytes_in_interval: int | None = None  # bytes in this period (delta); None for first sample
    bytes_out_interval: int | None = None
    in_errors: int
    out_errors: int
    in_discards: int
    out_discards: int
    rate_in_mbps: float | None = None
    rate_out_mbps: float | None = None


class PortBandwidthResponse(BaseModel):
    port_identifier: str
    samples: List[BandwidthSampleResponse]


def _downsample_bandwidth(samples: list, resolution_minutes: int):
    """Bucket samples by time window and output one row per window with rate over that interval."""
    if not samples or resolution_minutes < 2:
        return samples
    bucket_sec = resolution_minutes * 60
    by_bucket: dict[int, list] = {}
    for row in samples:
        try:
            ts_str = row.get("sampled_at")
            if not ts_str:
                continue
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            key = int(ts.timestamp() // bucket_sec) * bucket_sec
            if key not in by_bucket:
                by_bucket[key] = []
            by_bucket[key].append(row)
        except Exception:
            continue
    out = []
    for key in sorted(by_bucket.keys()):
        bucket = by_bucket[key]
        first = bucket[0]
        last = bucket[-1]
        try:
            ts_first = datetime.fromisoformat((first.get("sampled_at") or "").replace("Z", "+00:00"))
            ts_last = datetime.fromisoformat((last.get("sampled_at") or "").replace("Z", "+00:00"))
            duration_sec = (ts_last - ts_first).total_seconds()
            if duration_sec <= 0:
                duration_sec = float(bucket_sec)
        except Exception:
            duration_sec = float(bucket_sec)
        delta_in = last.get("bytes_in", 0) - first.get("bytes_in", 0)
        delta_out = last.get("bytes_out", 0) - first.get("bytes_out", 0)
        if delta_in < 0:
            delta_in = last.get("bytes_in", 0)
        if delta_out < 0:
            delta_out = last.get("bytes_out", 0)
        rate_in = delta_in * 8 / 1_000_000 / duration_sec
        rate_out = delta_out * 8 / 1_000_000 / duration_sec
        out.append({
            "sampled_at": first.get("sampled_at"),
            "bytes_in": last.get("bytes_in", 0),
            "bytes_out": last.get("bytes_out", 0),
            "bytes_in_interval": delta_in,
            "bytes_out_interval": delta_out,
            "in_errors": last.get("in_errors", 0),
            "out_errors": last.get("out_errors", 0),
            "in_discards": last.get("in_discards", 0),
            "out_discards": last.get("out_discards", 0),
            "rate_in_mbps": round(rate_in, 4),
            "rate_out_mbps": round(rate_out, 4),
        })
    return out


@router.get("/{switch_id}/bandwidth", response_model=dict)
async def get_switch_bandwidth(
    switch_id: int,
    hours: int = 24,
    port_identifier: str | None = None,
    resolution_minutes: int = 0,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Get stored bandwidth samples for a switch (optionally a single port). Bytes are cumulative; Rate is the difference between samples over the chosen interval."""
    switch = NetworkSwitchDAO.get_by_id(db, switch_id)
    if not switch:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Switch not found",
        )
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    raw = SwitchBandwidthSampleDAO.get_history(
        db, switch_id=switch_id, port_identifier=port_identifier, since=since, limit=5000
    )
    by_port: dict[str, list] = {}
    prev_by_port: dict[str, object] = {}
    for s in raw:
        key = s.port_identifier
        if key not in by_port:
            by_port[key] = []
        prev = prev_by_port.get(key)
        rate_in = rate_out = None
        bytes_in_interval = bytes_out_interval = None
        if prev is not None and s.sampled_at and prev.sampled_at:
            try:
                dt = (s.sampled_at - prev.sampled_at).total_seconds()
                if dt > 0:
                    delta_in = s.bytes_in - prev.bytes_in
                    delta_out = s.bytes_out - prev.bytes_out
                    if delta_in < 0:
                        delta_in = s.bytes_in
                    if delta_out < 0:
                        delta_out = s.bytes_out
                    bytes_in_interval = delta_in
                    bytes_out_interval = delta_out
                    rate_in = delta_in * 8 / 1_000_000 / dt
                    rate_out = delta_out * 8 / 1_000_000 / dt
            except Exception:
                pass
        prev_by_port[key] = s
        by_port[key].append({
            "sampled_at": s.sampled_at.isoformat() if s.sampled_at else None,
            "bytes_in": s.bytes_in,
            "bytes_out": s.bytes_out,
            "bytes_in_interval": bytes_in_interval,
            "bytes_out_interval": bytes_out_interval,
            "in_errors": s.in_errors,
            "out_errors": s.out_errors,
            "in_discards": s.in_discards,
            "out_discards": s.out_discards,
            "rate_in_mbps": round(rate_in, 4) if rate_in is not None else None,
            "rate_out_mbps": round(rate_out, 4) if rate_out is not None else None,
        })
    if resolution_minutes >= 2:
        for pid in by_port:
            by_port[pid] = _downsample_bandwidth(by_port[pid], resolution_minutes)
    ports = [
        {"port_identifier": pid, "samples": samples}
        for pid, samples in sorted(by_port.items())
    ]
    return {"switch_id": switch_id, "hours": hours, "resolution_minutes": resolution_minutes or None, "ports": ports}


@router.get("/{switch_id}/ports/{port}", response_model=dict)
async def get_switch_port_statistics(
    switch_id: int,
    port: str,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Get statistics for a specific port on a switch"""
    switch = NetworkSwitchDAO.get_by_id(db, switch_id)
    if not switch:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Switch not found"
        )
    
    # Get switch plugin instance from registry
    registry = get_switch_registry()
    try:
        plugin_instance = registry.get_plugin(switch.plugin_name, switch.plugin_config)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Plugin '{switch.plugin_name}' not found in switch registry"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to instantiate plugin: {str(e)}"
        )
    
    # Check if plugin supports monitoring
    if not switch_plugin_supports(switch.plugin_name, "monitoring"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Plugin '{switch.plugin_name}' does not support monitoring"
        )
    
    # Get port statistics
    try:
        port_stats = await plugin_instance.get_port_statistics(port)
        return {
            "success": True,
            "port": port,
            "statistics": port_stats
        }
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get port statistics: {str(e)}"
        )
