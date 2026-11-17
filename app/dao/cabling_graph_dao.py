"""Read-only batch queries for the admin cabling graph.

Every method selects explicit columns so secret-bearing columns
(plugin_config, credentials, BMC viewer passwords) are never loaded.
"""
from typing import Iterable, List, Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models.cable_run import CableRun
from app.models.location import Location
from app.models.network_port import NetworkPort
from app.models.network_switch import NetworkSwitch
from app.models.rack import Rack
from app.models.server import Server
from app.models.switch_port import SwitchPort

_SERVER_COLUMNS = (
    Server.id,
    Server.name,
    Server.server_ip,
    Server.location_id,
    Server.rack_id,
    Server.rack_unit,
    Server.rack_units,
    Server.enabled,
    Server.locked,
)

_SWITCH_COLUMNS = (
    NetworkSwitch.id,
    NetworkSwitch.name,
    NetworkSwitch.model,
    NetworkSwitch.location_id,
    NetworkSwitch.rack_id,
    NetworkSwitch.rack_unit,
    NetworkSwitch.rack_units,
    NetworkSwitch.enabled,
)

_SERVER_PORT_COLUMNS = (
    NetworkPort.id,
    NetworkPort.server_id,
    NetworkPort.name,
    NetworkPort.mac_address,
    NetworkPort.speed_mbps,
    NetworkPort.is_physical,
    NetworkPort.lag_group,
    NetworkPort.pxe_boot,
)

_SWITCH_PORT_COLUMNS = (
    SwitchPort.id,
    SwitchPort.switch_id,
    SwitchPort.name,
    SwitchPort.if_index,
    SwitchPort.speed_mbps,
    SwitchPort.admin_status,
    SwitchPort.oper_status,
    SwitchPort.description,
)

_CABLE_COLUMNS = (
    CableRun.id,
    CableRun.end_a_switch_port_id,
    CableRun.end_a_server_port_id,
    CableRun.end_b_switch_port_id,
    CableRun.end_b_server_port_id,
    CableRun.cable_type,
    CableRun.speed_mbps,
    CableRun.description,
)


def _ids(values: Iterable[Optional[int]]) -> List[int]:
    return sorted({v for v in values if v is not None})


class CablingGraphDAO:
    """Static batch lookups used to build the cabling graph in a fixed number of queries."""

    @staticmethod
    def list_servers(
        db: Session,
        location_id: Optional[int] = None,
        rack_id: Optional[int] = None,
        server_ids: Optional[Iterable[int]] = None,
    ):
        q = db.query(*_SERVER_COLUMNS)
        if location_id is not None:
            q = q.filter(Server.location_id == location_id)
        if rack_id is not None:
            q = q.filter(Server.rack_id == rack_id)
        if server_ids is not None:
            ids = _ids(server_ids)
            if not ids:
                return []
            q = q.filter(Server.id.in_(ids))
        return q.order_by(Server.name).all()

    @staticmethod
    def list_switches(
        db: Session,
        location_id: Optional[int] = None,
        rack_id: Optional[int] = None,
        switch_ids: Optional[Iterable[int]] = None,
    ):
        q = db.query(*_SWITCH_COLUMNS)
        if location_id is not None:
            q = q.filter(NetworkSwitch.location_id == location_id)
        if rack_id is not None:
            q = q.filter(NetworkSwitch.rack_id == rack_id)
        if switch_ids is not None:
            ids = _ids(switch_ids)
            if not ids:
                return []
            q = q.filter(NetworkSwitch.id.in_(ids))
        return q.order_by(NetworkSwitch.name).all()

    @staticmethod
    def server_ports_for_servers(db: Session, server_ids: Iterable[int]):
        ids = _ids(server_ids)
        if not ids:
            return []
        return (
            db.query(*_SERVER_PORT_COLUMNS)
            .filter(NetworkPort.server_id.in_(ids))
            .order_by(NetworkPort.server_id, NetworkPort.name)
            .all()
        )

    @staticmethod
    def server_ports_by_ids(db: Session, port_ids: Iterable[int]):
        ids = _ids(port_ids)
        if not ids:
            return []
        return db.query(*_SERVER_PORT_COLUMNS).filter(NetworkPort.id.in_(ids)).all()

    @staticmethod
    def switch_ports_by_ids(db: Session, port_ids: Iterable[int]):
        ids = _ids(port_ids)
        if not ids:
            return []
        return db.query(*_SWITCH_PORT_COLUMNS).filter(SwitchPort.id.in_(ids)).all()

    @staticmethod
    def switch_port_counts(db: Session, switch_ids: Iterable[int]) -> dict:
        ids = _ids(switch_ids)
        if not ids:
            return {}
        rows = (
            db.query(SwitchPort.switch_id, func.count(SwitchPort.id))
            .filter(SwitchPort.switch_id.in_(ids))
            .group_by(SwitchPort.switch_id)
            .all()
        )
        return {switch_id: count for switch_id, count in rows}

    @staticmethod
    def cable_runs_for_devices(
        db: Session,
        server_ids: Iterable[int],
        switch_ids: Iterable[int],
    ):
        """Cable runs with at least one end on a port of the given servers or switches."""
        srv = _ids(server_ids)
        sw = _ids(switch_ids)
        clauses = []
        if srv:
            server_port_ids = db.query(NetworkPort.id).filter(NetworkPort.server_id.in_(srv))
            clauses.append(CableRun.end_a_server_port_id.in_(server_port_ids))
            clauses.append(CableRun.end_b_server_port_id.in_(server_port_ids))
        if sw:
            switch_port_ids = db.query(SwitchPort.id).filter(SwitchPort.switch_id.in_(sw))
            clauses.append(CableRun.end_a_switch_port_id.in_(switch_port_ids))
            clauses.append(CableRun.end_b_switch_port_id.in_(switch_port_ids))
        if not clauses:
            return []
        return db.query(*_CABLE_COLUMNS).filter(or_(*clauses)).order_by(CableRun.id).all()

    @staticmethod
    def racks_by_ids(db: Session, rack_ids: Iterable[int]):
        ids = _ids(rack_ids)
        if not ids:
            return []
        return (
            db.query(
                Rack.id,
                Rack.name,
                Rack.location_id,
                Rack.row,
                Rack.row_position,
                Rack.units,
            )
            .filter(Rack.id.in_(ids))
            .order_by(Rack.location_id, Rack.row, Rack.row_position, Rack.name)
            .all()
        )

    @staticmethod
    def locations_by_ids(db: Session, location_ids: Iterable[int]):
        ids = _ids(location_ids)
        if not ids:
            return []
        return db.query(Location.id, Location.name).filter(Location.id.in_(ids)).order_by(Location.name).all()
