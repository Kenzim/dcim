"""Redfish virtual media helpers (no live BMC)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.services.virtual_media.base import VirtualMediaUnavailable
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


def _mock_client_with_posts(
    responses: dict[str, object],
    post_status: dict[str, int] | None = None,
    post_errors: set[str] | None = None,
) -> httpx.AsyncClient:
    client = MagicMock(spec=httpx.AsyncClient)
    post_status = post_status or {}
    post_errors = post_errors or set()

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
        if url in post_errors:
            raise httpx.ConnectError("connection refused", request=MagicMock())
        resp = MagicMock()
        resp.status_code = post_status.get(url, 200)
        if resp.status_code >= 400:
            resp.json = MagicMock(
                return_value={"error": {"message": "BMC rejected action"}}
            )
        else:
            resp.json = MagicMock(return_value={})
        return resp

    client.get = AsyncMock(side_effect=_get)
    client.post = AsyncMock(side_effect=_post)
    return client


@pytest.mark.asyncio
async def test_discover_cd_url_raises_when_no_devices():
    origin = "http://bmc"
    responses = {
        f"{origin}/redfish/v1/Managers": {"Members": []},
    }
    client = _mock_client(responses)
    with pytest.raises(VirtualMediaUnavailable, match="no Redfish VirtualMedia CD"):
        await discover_cd_url(client, origin)


@pytest.mark.asyncio
async def test_insert_image_retries_minimal_payload():
    origin = "http://bmc"
    device = f"{origin}/redfish/v1/Managers/1/VirtualMedia/CD1"
    insert_url = f"{device}/Actions/VirtualMedia.InsertMedia"
    responses = {
        f"{origin}/redfish/v1/Managers": {"Members": [{"@odata.id": "/redfish/v1/Managers/1"}]},
        f"{origin}/redfish/v1/Managers/1/VirtualMedia": {"Members": [{"@odata.id": device}]},
        device: {"Id": "CD1", "Inserted": False, "Image": ""},
    }
    client = _mock_client_with_posts(responses)
    call_count = 0

    async def _retry_post(url: str, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        resp = MagicMock()
        if url == insert_url and call_count == 1:
            resp.status_code = 400
            resp.json = MagicMock(return_value={"error": {"message": "extra fields"}})
        else:
            resp.status_code = 200
            resp.json = MagicMock(return_value={})
        return resp

    client.post = AsyncMock(side_effect=_retry_post)
    responses[device] = {"Id": "CD1", "Inserted": True, "Image": "http://iso/retry.iso"}
    inserted = await insert_image(client, origin, "http://iso/retry.iso")
    assert inserted.inserted is True
    assert call_count >= 2


@pytest.mark.asyncio
async def test_insert_image_raises_when_bmc_refuses():
    origin = "http://bmc"
    device = f"{origin}/redfish/v1/Managers/1/VirtualMedia/CD1"
    insert_url = f"{device}/Actions/VirtualMedia.InsertMedia"
    responses = {
        f"{origin}/redfish/v1/Managers": {"Members": [{"@odata.id": "/redfish/v1/Managers/1"}]},
        f"{origin}/redfish/v1/Managers/1/VirtualMedia": {"Members": [{"@odata.id": device}]},
        device: {"Id": "CD1", "Inserted": False, "Image": ""},
    }
    client = _mock_client_with_posts(responses, post_status={insert_url: 500})
    with pytest.raises(VirtualMediaUnavailable, match="BMC rejected"):
        await insert_image(client, origin, "http://iso/fail.iso")


@pytest.mark.asyncio
async def test_insert_image_ejects_existing_media_first():
    origin = "http://bmc"
    device = f"{origin}/redfish/v1/Managers/1/VirtualMedia/CD1"
    eject_url = f"{device}/Actions/VirtualMedia.EjectMedia"
    insert_url = f"{device}/Actions/VirtualMedia.InsertMedia"
    responses = {
        f"{origin}/redfish/v1/Managers": {"Members": [{"@odata.id": "/redfish/v1/Managers/1"}]},
        f"{origin}/redfish/v1/Managers/1/VirtualMedia": {"Members": [{"@odata.id": device}]},
        device: {"Id": "CD1", "Inserted": True, "Image": "http://iso/old.iso"},
    }
    client = _mock_client_with_posts(responses)
    posted: list[str] = []

    async def _track_post(url: str, *args, **kwargs):
        posted.append(url)
        resp = MagicMock()
        resp.status_code = 200
        resp.json = MagicMock(return_value={})
        return resp

    client.post = AsyncMock(side_effect=_track_post)
    responses[device] = {"Id": "CD1", "Inserted": True, "Image": "http://iso/new.iso"}
    result = await insert_image(client, origin, "http://iso/new.iso")
    assert eject_url in posted
    assert insert_url in posted
    assert result.inserted is True


@pytest.mark.asyncio
async def test_insert_image_request_error():
    origin = "http://bmc"
    device = f"{origin}/redfish/v1/Managers/1/VirtualMedia/CD1"
    insert_url = f"{device}/Actions/VirtualMedia.InsertMedia"
    responses = {
        f"{origin}/redfish/v1/Managers": {"Members": [{"@odata.id": "/redfish/v1/Managers/1"}]},
        f"{origin}/redfish/v1/Managers/1/VirtualMedia": {"Members": [{"@odata.id": device}]},
        device: {"Id": "CD1", "Inserted": False, "Image": ""},
    }
    client = _mock_client_with_posts(
        responses, post_errors={insert_url}
    )
    with pytest.raises(VirtualMediaUnavailable, match="Could not reach BMC"):
        await insert_image(client, origin, "http://iso/x.iso")


@pytest.mark.asyncio
async def test_eject_image_tolerates_already_empty():
    origin = "http://bmc"
    device = f"{origin}/redfish/v1/Managers/1/VirtualMedia/CD1"
    eject_url = f"{device}/Actions/VirtualMedia.EjectMedia"
    responses = {
        f"{origin}/redfish/v1/Managers": {"Members": [{"@odata.id": "/redfish/v1/Managers/1"}]},
        f"{origin}/redfish/v1/Managers/1/VirtualMedia": {"Members": [{"@odata.id": device}]},
        device: {"Id": "CD1", "Inserted": False, "Image": ""},
    }
    client = _mock_client_with_posts(responses, post_status={eject_url: 400})
    status = await eject_image(client, origin)
    assert status.inserted is False


@pytest.mark.asyncio
async def test_eject_image_raises_when_still_inserted():
    origin = "http://bmc"
    device = f"{origin}/redfish/v1/Managers/1/VirtualMedia/CD1"
    eject_url = f"{device}/Actions/VirtualMedia.EjectMedia"
    responses = {
        f"{origin}/redfish/v1/Managers": {"Members": [{"@odata.id": "/redfish/v1/Managers/1"}]},
        f"{origin}/redfish/v1/Managers/1/VirtualMedia": {"Members": [{"@odata.id": device}]},
        device: {"Id": "CD1", "Inserted": True, "Image": "http://iso/stuck.iso"},
    }
    client = _mock_client_with_posts(responses, post_status={eject_url: 500})
    with pytest.raises(VirtualMediaUnavailable, match="refused virtual media eject"):
        await eject_image(client, origin)
