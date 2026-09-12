"""Redfish virtual media helpers (no live BMC)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.services.virtual_media.redfish import (
    _join,
    _status_from_resource,
    discover_cd_url,
    eject_image,
    image_filename,
    insert_image,
    read_status,
)


def test_join_absolute_url_passthrough():
    assert _join("http://bmc", "https://other/path") == "https://other/path"
    assert _join("http://bmc", "http://other/path") == "http://other/path"


def test_join_relative_path():
    assert _join("http://bmc/", "/redfish/v1/Managers/1") == "http://bmc/redfish/v1/Managers/1"


def test_image_filename_from_url():
    assert image_filename("http://rackflow/isos/ubuntu-22.04.iso") == "ubuntu-22.04.iso"


def test_status_from_resource():
    status = _status_from_resource(
        {"Inserted": True, "Image": "http://iso/ubuntu.iso", "Id": "CD1"},
        "http://bmc/redfish/v1/Managers/1/VirtualMedia/CD1",
    )
    assert status.inserted is True
    assert status.image_name == "ubuntu.iso"
    assert status.device == "CD1"


def _mock_client(responses: dict[str, object]) -> httpx.AsyncClient:
    client = MagicMock(spec=httpx.AsyncClient)

    async def _get(url: str, *args, **kwargs):
        payload = responses.get(url)
        resp = MagicMock()
        if payload is None:
            resp.status_code = 404
            resp.json = MagicMock(side_effect=ValueError("no json"))
        else:
            resp.status_code = 200
            resp.json = MagicMock(return_value=payload)
        return resp

    async def _post(url: str, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.json = MagicMock(return_value={})
        return resp

    client.get = AsyncMock(side_effect=_get)
    client.post = AsyncMock(side_effect=_post)
    return client


@pytest.mark.asyncio
async def test_discover_cd_url_prefers_cd_device():
    origin = "http://bmc"
    vm1 = f"{origin}/redfish/v1/Managers/1/VirtualMedia/1"
    vm2 = f"{origin}/redfish/v1/Managers/1/VirtualMedia/2"
    responses = {
        f"{origin}/redfish/v1/Managers": {"Members": [{"@odata.id": "/redfish/v1/Managers/1"}]},
        f"{origin}/redfish/v1/Managers/1/VirtualMedia": {
            "Members": [{"@odata.id": vm1}, {"@odata.id": vm2}]
        },
        vm1: {"Id": "USB", "Name": "USB stick"},
        vm2: {"Id": "CD", "Name": "Virtual CD", "MediaTypes": ["CD", "DVD"]},
    }
    client = _mock_client(responses)
    url = await discover_cd_url(client, origin)
    assert url == vm2


@pytest.mark.asyncio
async def test_read_status_and_insert_eject():
    origin = "http://bmc"
    device = f"{origin}/redfish/v1/Managers/1/VirtualMedia/CD1"
    responses = {
        f"{origin}/redfish/v1/Managers": {"Members": [{"@odata.id": "/redfish/v1/Managers/1"}]},
        f"{origin}/redfish/v1/Managers/1/VirtualMedia": {"Members": [{"@odata.id": device}]},
        device: {"Id": "CD1", "Inserted": False, "Image": ""},
    }
    client = _mock_client(responses)
    status = await read_status(client, origin)
    assert status.inserted is False
    responses[device] = {"Id": "CD1", "Inserted": True, "Image": "http://iso/x.iso"}
    inserted = await insert_image(client, origin, "http://iso/x.iso")
    assert inserted.inserted is True
    responses[device] = {"Id": "CD1", "Inserted": False, "Image": ""}
    ejected = await eject_image(client, origin)
    assert ejected.inserted is False
