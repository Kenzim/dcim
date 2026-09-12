"""Focused unit coverage for billing helpers and error translation."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

import app.api.billing as billing
from app.models.service import ServiceStatus, ServiceType
from app.services.proxmox_placement import ProxmoxPlacementError
from app.services.service_lifecycle import ServiceLifecycleError


def _service(*, service_type=ServiceType.BARE_METAL, owner=None):
    return SimpleNamespace(
        id=12,
        service_type=service_type,
        owner_user=owner,
        status=ServiceStatus.ACTIVE,
    )


def test_billing_ownership_and_linkability_guards():
    integration = SimpleNamespace(id=3)
    owned = _service(owner=SimpleNamespace(billing_integration_id=3))
    internal = _service(owner=None)
    foreign = _service(owner=SimpleNamespace(billing_integration_id=4))

    billing._assert_billing_owned_service(owned, integration)
    billing._assert_linkable_service(owned, integration)
    billing._assert_linkable_service(internal, integration)
    for func, service in (
        (billing._assert_billing_owned_service, internal),
        (billing._assert_billing_owned_service, foreign),
        (billing._assert_linkable_service, foreign),
    ):
        with pytest.raises(HTTPException) as exc:
            func(service, integration)
        assert exc.value.status_code == 404


def test_group_selection_and_template_rules(monkeypatch):
    disabled = SimpleNamespace(id=1, enabled=False)
    occupied = SimpleNamespace(id=2, enabled=True)
    free = SimpleNamespace(id=3, enabled=True)
    monkeypatch.setattr(
        billing.ServerGroupDAO, "get_by_id",
        lambda _db, ident: None if ident == 0 else SimpleNamespace(
            name="pool",
            enable_os_templates=True,
            servers=[] if ident == 1 else [disabled, occupied, free],
            permitted_os_templates=["debian"],
        ),
    )
    monkeypatch.setattr(
        billing.ServiceDAO, "get_by_server",
        lambda _db, ident: [SimpleNamespace(status=ServiceStatus.ACTIVE)] if ident == 2 else [],
    )
    assert billing._select_free_server_in_group(None, 2) is free
    assert billing._determine_template_for_group(None, 2, None) == "debian"
    assert billing._determine_template_for_group(None, 2, "debian") == "debian"
    for fn in (
        lambda: billing._select_free_server_in_group(None, 0),
        lambda: billing._select_free_server_in_group(None, 1),
        lambda: billing._determine_template_for_group(None, 0, None),
        lambda: billing._determine_template_for_group(None, 2, "rocky"),
    ):
        with pytest.raises(HTTPException):
            fn()


def test_determine_template_requires_enable_and_lists_choices(monkeypatch):
    monkeypatch.setattr(
        billing.ServerGroupDAO,
        "get_by_id",
        lambda _db, ident: SimpleNamespace(
            name="multi",
            enable_os_templates=ident == 2,
            permitted_os_templates=["ubuntu-cloud-image", "windows-server-2022"],
        ),
    )
    with pytest.raises(HTTPException) as disabled:
        billing._determine_template_for_group(None, 1, "ubuntu-cloud-image")
    assert disabled.value.status_code == 400
    assert "not enabled" in str(disabled.value.detail)

    with pytest.raises(HTTPException) as missing:
        billing._determine_template_for_group(None, 2, None)
    assert missing.value.status_code == 400
    assert "ubuntu-cloud-image" in str(missing.value.detail)
    assert "windows-server-2022" in str(missing.value.detail)
    assert billing._determine_template_for_group(None, 2, "ubuntu-cloud-image") == "ubuntu-cloud-image"


def test_validate_cluster_and_activity_keyword(monkeypatch):
    monkeypatch.setattr(billing.ProxmoxInventoryDAO, "get_cluster", lambda *_: None)
    with pytest.raises(HTTPException) as exc:
        billing._validate_optional_proxmox_cluster(None, 99)
    assert exc.value.status_code == 404
    billing._validate_optional_proxmox_cluster(None, None)

    service = _service()
    monkeypatch.setattr(billing, "service_linked_server", lambda *_: SimpleNamespace(id=9))
    assert billing._billing_activity_log_kw(None, service) == {"server_id": 9}
    monkeypatch.setattr(billing, "service_linked_server", lambda *_: None)
    assert billing._billing_activity_log_kw(None, service) == {"service_id": 12}
    assert billing._power_permission_key(_service(service_type=ServiceType.VM)) == "vm.power"


@pytest.mark.asyncio
async def test_plugin_resolution_and_lifecycle_error_translation(monkeypatch):
    vm = _service(service_type=ServiceType.VM)
    plugin = object()

    async def resolved(*_args):
        return plugin, 1, "node", 100

    monkeypatch.setattr(billing, "resolve_proxmox_plugin_for_service", resolved)
    assert await billing._billing_get_plugin_instance(None, vm) == (plugin, None)

    async def placement_error(*_args):
        raise ProxmoxPlacementError("bad placement", status_code=409)

    monkeypatch.setattr(billing, "resolve_proxmox_plugin_for_service", placement_error)
    with pytest.raises(HTTPException) as exc:
        await billing._billing_get_plugin_instance(None, vm)
    assert exc.value.status_code == 409

    monkeypatch.setattr(billing, "service_linked_server", lambda *_: None)
    with pytest.raises(HTTPException) as exc:
        await billing._billing_get_plugin_instance(None, _service(service_type=ServiceType.HTTP_PROXY))
    assert exc.value.status_code == 400

    lifecycle = MagicMock()
    lifecycle.ensure_powered_off = AsyncMock(side_effect=ServiceLifecycleError("cannot stop", status_code=502))
    monkeypatch.setattr(billing, "ServiceLifecycle", lambda **_kwargs: lifecycle)
    with pytest.raises(HTTPException) as exc:
        await billing._ensure_service_powered_off(None, _service())
    assert exc.value.status_code == 502


def test_billing_vm_ip_fields_and_external_user_id():
    vm_alloc = SimpleNamespace(
        vm=SimpleNamespace(
            vm_ip_allocation_id=9,
            vm_ip_allocation=SimpleNamespace(ip_address="198.51.100.5"),
            vm_template_id=3,
            guest_state=None,
        ),
        config={"vm_ip_address": "ignored"},
        owner_user_id=42,
        owner_user=SimpleNamespace(billing_integration_id=2),
    )
    assert billing._billing_vm_ip_fields(vm_alloc) == (9, "198.51.100.5")
    assert billing._billing_external_user_id(vm_alloc) == 42

    cfg_only = SimpleNamespace(
        vm=SimpleNamespace(vm_ip_allocation_id=None, vm_ip_allocation=None, vm_template_id=None, guest_state=None),
        config={"vm_ip_allocation_id": 4, "vm_ip_address": "203.0.113.7"},
        owner_user=None,
    )
    assert billing._billing_vm_ip_fields(cfg_only) == (4, "203.0.113.7")
    assert billing._billing_external_user_id(cfg_only) is None


def test_billing_product_helpers():
    family = SimpleNamespace(
        service_type="vm",
        defaults={"memory_mb": 2048},
        os_mappings=[
            SimpleNamespace(os_profile=SimpleNamespace(id=1, code="deb", name="Debian", enabled=True, os_family="linux", strategy_name="cloudinit")),
            SimpleNamespace(os_profile=SimpleNamespace(id=2, code="off", name="Off", enabled=False, os_family="linux", strategy_name="cloudinit")),
        ],
    )
    product = SimpleNamespace(
        overrides={"cores": 2},
        vm_template_mappings=[
            SimpleNamespace(vm_template=SimpleNamespace(id=10, code="tpl", name="Ubuntu", enabled=True, os_type="linux-cloudinit", proxmox_template_name="ubuntu")),
        ],
    )
    profiles = billing._billing_product_os_profiles(family)
    assert len(profiles) == 1
    assert profiles[0]["code"] == "deb"
    templates = billing._billing_product_vm_templates(product)
    assert templates[0]["code"] == "tpl"
    assert billing._billing_product_checkout_os_mode("vm", templates, profiles) == "vm_template"
    assert billing._billing_product_checkout_os_mode("bare_metal", [], profiles) == "server_group"
    assert billing._billing_product_checkout_os_mode("vm", [], []) == "none"


def test_apply_lookup_filters(monkeypatch):
    query = MagicMock()
    filtered = MagicMock()
    query.filter.return_value = filtered

    monkeypatch.setattr(billing.ServerDAO, "get_by_ip", lambda db, ip: None)
    q, empty = billing._apply_lookup_server_ip_filter(query, None, "1.2.3.4", text=None, proxmox_vmid=None)
    assert empty is True
    assert q is query

    server = SimpleNamespace(id=55)
    monkeypatch.setattr(billing.ServerDAO, "get_by_ip", lambda db, ip: server)
    q2, empty2 = billing._apply_lookup_server_ip_filter(query, None, "1.2.3.4", text=None, proxmox_vmid=None)
    assert empty2 is False
    query.filter.assert_called()

    text_q = billing._apply_lookup_text_filter(query, "5100")
    assert text_q is query.filter.return_value
