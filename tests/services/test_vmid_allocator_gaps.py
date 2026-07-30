from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import vmid_allocator as allocator


@pytest.mark.parametrize("minimum, maximum, expected", [
    (None, None, (200000, 299999)), (1, 1, (1, 1)), ("10", "20", (10, 20)),
    (200000, 299999, (200000, 299999)),
])
def test_resolve_cluster_range_uses_defaults_and_coerces_numbers(minimum, maximum, expected):
    assert allocator._resolve_cluster_range(SimpleNamespace(vmid_min=minimum, vmid_max=maximum)) == expected


@pytest.mark.parametrize("minimum, maximum", [(0, 1), (1, 0), (-1, 2), (3, 2)])
def test_resolve_cluster_range_rejects_invalid_ranges(minimum, maximum):
    with pytest.raises(ValueError, match="Invalid cluster VMID range"):
        allocator._resolve_cluster_range(SimpleNamespace(vmid_min=minimum, vmid_max=maximum))


def test_reserve_returns_sticky_reservation_for_same_cluster(monkeypatch):
    existing = SimpleNamespace(vmid=777, cluster_id=3)
    monkeypatch.setattr(allocator.VMIDReservationDAO, "get_by_service_id", lambda *args: existing)
    assert allocator.reserve_vmid_for_service(MagicMock(), cluster_id=3, service_id=9) == 777
    assert allocator.reserve_vmid_for_service(MagicMock(), cluster_id=3, service_id=9, requested_vmid=777) == 777


def test_reserve_rejects_cross_cluster_sticky_reservation(monkeypatch):
    monkeypatch.setattr(allocator.VMIDReservationDAO, "get_by_service_id", lambda *args: SimpleNamespace(vmid=777, cluster_id=3))
    with pytest.raises(ValueError, match="cross-cluster"):
        allocator.reserve_vmid_for_service(MagicMock(), cluster_id=4, service_id=9)


@pytest.mark.parametrize("requested, allow_outside, message", [
    (4, False, "outside cluster range"), (11, False, "outside cluster range"),
    (0, True, "must be positive"),
])
def test_requested_vmid_validates_range_and_positive(monkeypatch, requested, allow_outside, message):
    monkeypatch.setattr(allocator.VMIDReservationDAO, "get_by_service_id", lambda *args: None)
    monkeypatch.setattr(allocator.ProxmoxInventoryDAO, "get_cluster", lambda *args: SimpleNamespace(vmid_min=5, vmid_max=10))
    with pytest.raises(ValueError, match=message):
        allocator.reserve_vmid_for_service(MagicMock(), cluster_id=1, service_id=2, requested_vmid=requested, allow_outside_range=allow_outside)


def test_requested_vmid_creates_reservation_and_detects_conflict(monkeypatch):
    created = []
    monkeypatch.setattr(allocator.VMIDReservationDAO, "get_by_service_id", lambda *args: None)
    monkeypatch.setattr(allocator.ProxmoxInventoryDAO, "get_cluster", lambda *args: SimpleNamespace(vmid_min=5, vmid_max=10))
    monkeypatch.setattr(allocator.VMIDReservationDAO, "get_by_cluster_vmid", lambda *args: None)
    monkeypatch.setattr(allocator.VMIDReservationDAO, "create", lambda *args, **kwargs: created.append(kwargs))
    assert allocator.reserve_vmid_for_service(MagicMock(), cluster_id=1, service_id=2, requested_vmid=7) == 7
    assert created == [{"cluster_id": 1, "service_id": 2, "vmid": 7}]
    monkeypatch.setattr(allocator.VMIDReservationDAO, "get_by_cluster_vmid", lambda *args: object())
    with pytest.raises(ValueError, match="already reserved"):
        allocator.reserve_vmid_for_service(MagicMock(), cluster_id=1, service_id=2, requested_vmid=7)


@pytest.mark.parametrize("suggested, taken, expected", [
    (7, set(), 7), (4, set(), 5), (11, set(), 5), (7, {7}, 5),
])
def test_auto_allocation_uses_valid_suggestion_then_first_gap(monkeypatch, suggested, taken, expected):
    created = []
    monkeypatch.setattr(allocator.VMIDReservationDAO, "get_by_service_id", lambda *args: None)
    monkeypatch.setattr(allocator.ProxmoxInventoryDAO, "get_cluster", lambda *args: SimpleNamespace(vmid_min=5, vmid_max=10))
    monkeypatch.setattr(allocator.VMIDReservationDAO, "get_by_cluster_vmid", lambda db, cid, vmid: object() if vmid in taken else None)
    monkeypatch.setattr(allocator.VMIDReservationDAO, "create", lambda *args, **kwargs: created.append(kwargs))
    assert allocator.reserve_vmid_for_service(MagicMock(), cluster_id=1, service_id=2, suggested_vmid=suggested) == expected
    assert created[0]["vmid"] == expected


def test_auto_allocation_rejects_exhausted_range(monkeypatch):
    monkeypatch.setattr(allocator.VMIDReservationDAO, "get_by_service_id", lambda *args: None)
    monkeypatch.setattr(allocator.ProxmoxInventoryDAO, "get_cluster", lambda *args: SimpleNamespace(vmid_min=5, vmid_max=6))
    monkeypatch.setattr(allocator.VMIDReservationDAO, "get_by_cluster_vmid", lambda *args: object())
    with pytest.raises(ValueError, match="No free VMID"):
        allocator.reserve_vmid_for_service(MagicMock(), cluster_id=1, service_id=2)


@pytest.mark.asyncio
async def test_aligned_allocator_returns_sticky_before_proxmox_calls(monkeypatch):
    monkeypatch.setattr(allocator.VMIDReservationDAO, "get_by_service_id", lambda *args: SimpleNamespace(vmid=44, cluster_id=1))
    assert await allocator.reserve_vmid_aligned_with_proxmox(MagicMock(), cluster_id=1, service_id=2, node_name="pve") == 44


@pytest.mark.asyncio
@pytest.mark.parametrize("requested, adopt, exists, available, expected_error", [
    (7, False, True, False, "not available"),
    (7, True, False, True, "No Proxmox guest"),
])
async def test_aligned_allocator_validates_requested_vmid(monkeypatch, requested, adopt, exists, available, expected_error):
    cluster = SimpleNamespace(
        vmid_min=5,
        vmid_max=10,
        api_url="https://pve.example:8006",
        username="root@pam",
        password="secret",
        verify_ssl=False,
    )
    plugin = MagicMock()
    plugin.vm_exists = AsyncMock(return_value=exists)
    plugin.check_vmid_available_for_new = AsyncMock(return_value=available)
    monkeypatch.setattr(allocator.VMIDReservationDAO, "get_by_service_id", lambda *args: None)
    monkeypatch.setattr(allocator.ProxmoxInventoryDAO, "get_cluster", lambda *args: cluster)
    monkeypatch.setattr("app.plugins.registry.get_registry", lambda: MagicMock(get_plugin=lambda *args, **kwargs: plugin))
    with pytest.raises(ValueError, match=expected_error):
        await allocator.reserve_vmid_aligned_with_proxmox(MagicMock(), cluster_id=1, service_id=2, node_name="pve", requested_vmid=requested, adopt_existing=adopt)
