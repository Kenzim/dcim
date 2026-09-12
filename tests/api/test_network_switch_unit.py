from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.api import network_switch as api


@pytest.mark.parametrize("name, data, expected", [
    ("Ethernet100G1/1", {}, 100000), ("100gig uplink", {}, 100000),
    ("40GigE", {}, 40000), ("xe-25g-0/0", {}, 25000),
    ("TenGigabitEthernet1", {}, 10000), ("10gig uplink", {}, 10000),
    ("GigabitEthernet0/1", {}, 1000), ("ge-0/0/1", {}, 1000),
    ("FastEthernet0/1", {}, 100), ("fe-0/0/1", {}, 100),
    ("unknown", {"ifHighSpeed": 2500}, 2500),
    ("unknown", {"ifHighSpeed": "10000"}, 10000),
    ("unknown", {"ifHighSpeed": 0, "ifSpeed": 1_000_000_000}, 1000),
    ("unknown", {"ifHighSpeed": "bad", "ifSpeed": "100000000"}, 100),
    ("unknown", {"ifSpeed": api._IF_SPEED_32BIT_MAX_BPS}, None),
    ("unknown", {"ifSpeed": 0}, None), ("", {}, None),
])
def test_physical_speed_mbps_prefers_physical_name_and_fallbacks(name, data, expected):
    assert api._physical_speed_mbps(name, data) == expected


@pytest.mark.parametrize("query_servers, query_switches, placement, expected", [
    ([], [], (1, 1, 1), False),
    ([SimpleNamespace(rack_unit=1, rack_units=1)], [], (1, 1, 1), True),
    ([SimpleNamespace(rack_unit=2, rack_units=2)], [], (1, 1, 1), False),
    ([SimpleNamespace(rack_unit=2, rack_units=2)], [], (1, 3, 1), True),
    ([], [SimpleNamespace(id=2, rack_unit=5, rack_units=2)], (1, 6, 1), True),
])
def test_rack_placement_overlap_detects_servers_and_switches(
    query_servers, query_switches, placement, expected
):
    server_query = MagicMock()
    server_query.filter.return_value.all.return_value = query_servers
    switch_query = MagicMock()
    switch_query.filter.return_value = switch_query
    switch_query.all.return_value = query_switches
    db = MagicMock()
    db.query.side_effect = [server_query, switch_query]
    assert api._rack_placement_overlaps_switch(db, *placement) is expected


def test_rack_placement_overlap_excludes_switch_being_updated():
    server_query = MagicMock()
    server_query.filter.return_value.all.return_value = []
    switch_query = MagicMock()
    switch_query.filter.return_value = switch_query
    switch_query.all.return_value = []
    db = MagicMock()
    db.query.side_effect = [server_query, switch_query]
    assert api._rack_placement_overlaps_switch(
        db, 1, 6, 1, exclude_switch_id=2
    ) is False
    assert switch_query.filter.call_count == 2


def test_track_background_task_keeps_and_discards_task():
    task = MagicMock()
    api._background_tasks.clear()
    api._track_background_task(task)
    assert task in api._background_tasks
    callback = task.add_done_callback.call_args.args[0]
    callback(task)
    assert task not in api._background_tasks


@pytest.mark.parametrize("samples, resolution, expected_count, interval", [
    ([], 5, 0, None),
    ([{"sampled_at": "2026-01-01T00:00:00Z"}], 1, 1, None),
    ([
        {"sampled_at": "2026-01-01T00:00:00Z", "bytes_in": 10, "bytes_out": 20},
        {"sampled_at": "2026-01-01T00:01:00Z", "bytes_in": 110, "bytes_out": 220},
    ], 5, 1, 100),
    ([
        {"sampled_at": "bad", "bytes_in": 1, "bytes_out": 1},
        {"sampled_at": "2026-01-01T00:06:00Z", "bytes_in": 3, "bytes_out": 4},
    ], 5, 1, 0),
])
def test_downsample_bandwidth_handles_windows_and_bad_timestamps(
    samples, resolution, expected_count, interval
):
    result = api._downsample_bandwidth(samples, resolution)
    assert len(result) == expected_count
    if interval is not None:
        assert result[0]["bytes_in_interval"] == interval


@pytest.mark.asyncio
async def test_switch_connection_returns_plugin_success_and_optional_info(monkeypatch):
    plugin = MagicMock()
    plugin.test_connection = AsyncMock(return_value={"success": True})
    plugin.get_switch_info = AsyncMock(return_value={"model": "test"})
    registry = MagicMock()
    registry.get_plugin.return_value = plugin
    monkeypatch.setattr(api, "get_switch_registry", lambda: registry)
    result = await api.test_switch_connection(
        api.NetworkSwitchTestRequest(plugin_name="x", plugin_config={}),
        auth={},
        db=MagicMock(),
    )
    assert result == {"success": True, "switch_info": {"model": "test"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("exc_type, status_code", [
    (KeyError("missing"), 404),
    (RuntimeError("bad config"), 400),
])
async def test_switch_connection_maps_plugin_construction_errors(monkeypatch, exc_type, status_code):
    registry = MagicMock()
    registry.get_plugin.side_effect = exc_type
    monkeypatch.setattr(api, "get_switch_registry", lambda: registry)
    with pytest.raises(HTTPException) as exc:
        await api.test_switch_connection(
            api.NetworkSwitchTestRequest(plugin_name="x", plugin_config={}),
            auth={},
            db=MagicMock(),
        )
    assert exc.value.status_code == status_code
