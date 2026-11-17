"""Collect LLDP neighbour tables from switches through their plugins and store the snapshot."""
import logging
from typing import Any, Dict

from sqlalchemy.orm import Session

from app.dao.switch_lldp_neighbor_dao import SwitchLldpNeighborDAO
from app.plugins.switch_registry import get_switch_registry

logger = logging.getLogger(__name__)


class LldpNotSupported(Exception):
    """The switch's plugin does not support LLDP discovery."""


def supports_lldp(plugin_name: str) -> bool:
    return get_switch_registry().supports_lldp(plugin_name)


async def collect_for_switch(db: Session, switch: Any) -> Dict[str, Any]:
    """Read one switch's LLDP neighbours and replace its stored snapshot.

    On a plugin error the previous snapshot is kept, the error is stored on the switch and re-raised.
    """
    if not supports_lldp(switch.plugin_name):
        raise LldpNotSupported(f"Plugin '{switch.plugin_name}' does not support LLDP discovery")
    plugin = get_switch_registry().get_plugin(switch.plugin_name, switch.plugin_config or {})
    try:
        neighbours = await plugin.get_lldp_neighbors()
    except Exception as e:
        SwitchLldpNeighborDAO.record_error(db, switch.id, str(e))
        raise
    rows = SwitchLldpNeighborDAO.replace_for_switch(db, switch.id, neighbours)
    matched = sum(1 for r in rows if r.switch_port_id is not None)
    logger.info("LLDP for switch %s: %d neighbours (%d on known ports)", switch.name, len(rows), matched)
    return {"switch_id": switch.id, "neighbours": len(rows), "matched_ports": matched}
