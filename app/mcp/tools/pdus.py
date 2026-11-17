"""PDU, power feed and power input work for the admin MCP server.

The REST handlers already hold the validation, masking and outlet guards, so these
functions call them directly, as the network tools do for switches.
"""

from typing import Any, Dict, Optional

from app.api import pdu as pdu_api
from app.api import power_feed as power_feed_api
from app.api import rack as rack_api
from app.api import server_power as server_power_api
from app.mcp.runtime import run_tool


def _given(**values: Any) -> Dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


async def pdus(location_id: Optional[int] = None, rack_id: Optional[int] = None, pdu_id: Optional[int] = None):
    async def work(db, ctx):
        if pdu_id is not None:
            return await pdu_api.get_pdu(pdu_id=pdu_id, auth={}, db=db)
        return await pdu_api.list_pdus(location_id=location_id, rack_id=rack_id, auth={}, db=db)

    return await run_tool("pdus", "read", work, args=_given(location_id=location_id, rack_id=rack_id, pdu_id=pdu_id))


async def save_pdu(
    pdu_id: Optional[int] = None,
    name: Optional[str] = None,
    location_id: Optional[int] = None,
    plugin_name: Optional[str] = None,
    plugin_config: Optional[Dict[str, Any]] = None,
    description: Optional[str] = None,
    rack_id: Optional[int] = None,
    rack_unit: Optional[int] = None,
    rack_units: Optional[int] = None,
    enabled: Optional[bool] = None,
    inlet_count: Optional[int] = None,
    outlet_count: Optional[int] = None,
    outlet_metering: Optional[bool] = None,
    outlet_switching: Optional[bool] = None,
    outlet_control_enabled: Optional[bool] = None,
    model: Optional[str] = None,
):
    fields = _given(
        name=name,
        location_id=location_id,
        plugin_name=plugin_name,
        plugin_config=plugin_config,
        description=description,
        rack_id=rack_id,
        rack_unit=rack_unit,
        rack_units=rack_units,
        enabled=enabled,
        inlet_count=inlet_count,
        outlet_count=outlet_count,
        outlet_metering=outlet_metering,
        outlet_switching=outlet_switching,
        outlet_control_enabled=outlet_control_enabled,
        model=model,
    )

    async def work(db, ctx):
        if pdu_id is None:
            return await pdu_api.create_pdu(data=pdu_api.PduCreate(**fields), auth={}, db=db)
        return await pdu_api.update_pdu(pdu_id=pdu_id, data=pdu_api.PduUpdate(**fields), auth={}, db=db)

    return await run_tool(
        "save_pdu",
        "write",
        work,
        args=_given(pdu_id=pdu_id, name=name, location_id=location_id, plugin_name=plugin_name, rack_id=rack_id),
    )


async def pdu_action(
    pdu_id: int,
    action: str,
    inlet_id: Optional[int] = None,
    power_feed_id: Optional[int] = None,
    outlet_id: Optional[int] = None,
    server_power_input_id: Optional[int] = None,
    name: Optional[str] = None,
    bank: Optional[str] = None,
    description: Optional[str] = None,
    locked: Optional[bool] = None,
    inlet_count: Optional[int] = None,
    outlet_count: Optional[int] = None,
    confirm: bool = False,
):
    async def work(db, ctx):
        if action == "test":
            pdu = await pdu_api.get_pdu(pdu_id=pdu_id, auth={}, db=db)
            request = pdu_api.PduTestRequest(plugin_name=pdu["plugin_name"], pdu_id=pdu_id)
            return await pdu_api.test_pdu_connection(data=request, auth={}, db=db)
        if action == "regenerate_ports":
            request = pdu_api.RegenerateRequest(inlet_count=inlet_count, outlet_count=outlet_count)
            return await pdu_api.regenerate_pdu_ports(pdu_id=pdu_id, data=request, auth={}, db=db)
        if action == "poll":
            return await pdu_api.poll_pdu_now(pdu_id=pdu_id, auth={}, db=db)
        if action == "link_inlet":
            update = pdu_api.InletUpdate(power_feed_id=power_feed_id)
            return await pdu_api.update_inlet(pdu_id=pdu_id, inlet_id=inlet_id, data=update, auth={}, db=db)
        if action == "update_outlet":
            update = pdu_api.OutletUpdate(**_given(name=name, bank=bank, description=description, locked=locked))
            return await pdu_api.update_outlet(pdu_id=pdu_id, outlet_id=outlet_id, data=update, auth={}, db=db)
        if action == "link_outlet":
            update = pdu_api.OutletUpdate(server_power_input_id=server_power_input_id)
            return await pdu_api.update_outlet(pdu_id=pdu_id, outlet_id=outlet_id, data=update, auth={}, db=db)
        return await pdu_api.delete_pdu(pdu_id=pdu_id, auth={}, db=db)

    destructive = action == "delete"
    return await run_tool(
        "pdu_action",
        "destructive" if destructive else "write",
        work,
        destructive=destructive,
        confirm=confirm,
        args=_given(pdu_id=pdu_id, action=action, inlet_id=inlet_id, outlet_id=outlet_id, confirm=confirm),
    )


async def outlet_power(
    action: str,
    confirm: bool = False,
    pdu_id: Optional[int] = None,
    outlet_id: Optional[int] = None,
    server_id: Optional[int] = None,
    power_input_id: Optional[int] = None,
):
    async def work(db, ctx):
        data = pdu_api.OutletAction(action=action, confirm=confirm)
        if server_id is not None:
            return await server_power_api.power_input_outlet_action(
                server_id=server_id, input_id=power_input_id, data=data, auth={}, db=db
            )
        return await pdu_api.outlet_action(pdu_id=pdu_id, outlet_id=outlet_id, data=data, auth={}, db=db)

    return await run_tool(
        "outlet_power",
        "destructive",
        work,
        destructive=True,
        confirm=confirm,
        args=_given(action=action, pdu_id=pdu_id, outlet_id=outlet_id, server_id=server_id, power_input_id=power_input_id),
    )


async def power_feeds(rack_id: Optional[int] = None, feed_id: Optional[int] = None):
    async def work(db, ctx):
        if feed_id is not None:
            return await power_feed_api.get_power_feed(feed_id=feed_id, auth={}, db=db)
        return await power_feed_api.list_power_feeds(rack_id=rack_id, auth={}, db=db)

    return await run_tool("power_feeds", "read", work, args=_given(rack_id=rack_id, feed_id=feed_id))


async def save_power_feed(
    feed_id: Optional[int] = None,
    rack_id: Optional[int] = None,
    name: Optional[str] = None,
    voltage: Optional[int] = None,
    amperage: Optional[int] = None,
    phase: Optional[str] = None,
    max_utilisation_percent: Optional[int] = None,
    description: Optional[str] = None,
):
    fields = _given(
        name=name,
        voltage=voltage,
        amperage=amperage,
        phase=phase,
        max_utilisation_percent=max_utilisation_percent,
        description=description,
    )

    async def work(db, ctx):
        if feed_id is None:
            return await power_feed_api.create_power_feed(
                data=power_feed_api.PowerFeedCreate(rack_id=rack_id, **fields), auth={}, db=db
            )
        return await power_feed_api.update_power_feed(
            feed_id=feed_id, data=power_feed_api.PowerFeedUpdate(**fields), auth={}, db=db
        )

    return await run_tool(
        "save_power_feed",
        "write",
        work,
        args=_given(feed_id=feed_id, rack_id=rack_id, name=name),
    )


async def delete_power_feed(feed_id: int, confirm: bool = False):
    async def work(db, ctx):
        await power_feed_api.delete_power_feed(feed_id=feed_id, auth={}, db=db)
        return {"deleted": True, "feed_id": feed_id}

    return await run_tool(
        "delete_power_feed",
        "destructive",
        work,
        destructive=True,
        confirm=confirm,
        args={"feed_id": feed_id, "confirm": confirm},
    )


async def rack_power(rack_id: int):
    async def work(db, ctx):
        return await rack_api.get_rack_power(rack_id=rack_id, auth={}, db=db)

    return await run_tool("rack_power", "read", work, args={"rack_id": rack_id})


async def server_power(server_id: int):
    async def work(db, ctx):
        return await server_power_api.get_server_power(server_id=server_id, auth={}, db=db)

    return await run_tool("server_power", "read", work, args={"server_id": server_id})


async def server_power_inputs(
    server_id: int,
    action: str,
    input_id: Optional[int] = None,
    name: Optional[str] = None,
    description: Optional[str] = None,
    pdu_outlet_id: Optional[int] = None,
    confirm: bool = False,
):
    async def work(db, ctx):
        if action == "add":
            data = server_power_api.PowerInputCreate(**_given(name=name, description=description, pdu_outlet_id=pdu_outlet_id))
            return await server_power_api.add_power_input(server_id=server_id, data=data, auth={}, db=db)
        if action == "update":
            data = server_power_api.PowerInputUpdate(**_given(name=name, description=description))
            return await server_power_api.update_power_input(
                server_id=server_id, input_id=input_id, data=data, auth={}, db=db
            )
        if action == "link":
            data = server_power_api.PowerInputUpdate(pdu_outlet_id=pdu_outlet_id)
            return await server_power_api.update_power_input(
                server_id=server_id, input_id=input_id, data=data, auth={}, db=db
            )
        return await server_power_api.delete_power_input(server_id=server_id, input_id=input_id, auth={}, db=db)

    destructive = action == "remove"
    return await run_tool(
        "server_power_inputs",
        "destructive" if destructive else "write",
        work,
        destructive=destructive,
        confirm=confirm,
        args=_given(server_id=server_id, action=action, input_id=input_id, pdu_outlet_id=pdu_outlet_id, confirm=confirm),
    )
