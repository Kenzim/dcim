"""
Client permission resolution: defaults -> product preset -> user preset ->
service preset -> per-service overrides, most-specific wins.
"""
from app.core.client_permissions import PermissionKey
from app.dao.permission_set_dao import PermissionSetDAO
from app.dao.product_catalog_dao import ProductDAO
from app.dao.service_dao import ServiceDAO
from app.dao.user_dao import UserDAO
from app.models.service import ProvisioningSource
from app.services.client_permission_resolver import (
    has_client_permission,
    require_client_permission,
    resolve_client_permissions,
)
import pytest
from fastapi import HTTPException


def _bare_metal_service(db_session, **kwargs):
    from app.dao.location_dao import LocationDAO
    from app.dao.server_dao import ServerDAO

    location = LocationDAO.create(db_session, name=f"loc-{kwargs.get('name', 'x')}")
    server = ServerDAO.create(
        db_session,
        name=f"srv-{kwargs.get('name', 'x')}",
        server_ip="10.0.0.1",
        plugin_name="ipmi",
        plugin_config={},
        location_id=location.id,
    )
    return ServiceDAO.create_bare_metal(
        db_session,
        name=kwargs.get("name", "svc"),
        server_id=server.id,
        provisioning_source=ProvisioningSource.INTERNAL,
        product_code=kwargs.get("product_code"),
        owner_user_id=kwargs.get("owner_user_id"),
    )


def test_defaults_match_pre_permission_system_behavior(db_session):
    """No presets assigned anywhere: BMS clients keep power + IPMI + portal."""
    service = _bare_metal_service(db_session, name="svc-defaults")
    perms = resolve_client_permissions(db_session, service)
    assert perms[PermissionKey.BMS_POWER] is True
    assert perms[PermissionKey.BMS_IPMI] is True
    assert perms[PermissionKey.BMS_KVM] is True
    assert perms[PermissionKey.BMS_SOL] is True
    assert perms[PermissionKey.SERVICE_PORTAL] is True
    assert perms[PermissionKey.BMS_REINSTALL] is False
    assert perms[PermissionKey.BMS_RUN_SCRIPT] is False


def test_product_preset_overrides_default(db_session):
    preset = PermissionSetDAO.create(
        db_session, name="ro-product", permissions={PermissionKey.BMS_POWER: False}
    )
    product = ProductDAO.create(db_session, family_id=None, name="P1", description=None, code="p1")
    ProductDAO.update(db_session, product, permission_set_id=preset.id)

    service = _bare_metal_service(db_session, name="svc-product", product_code="p1")
    perms = resolve_client_permissions(db_session, service)
    assert perms[PermissionKey.BMS_POWER] is False
    # Untouched keys still inherit the type default.
    assert perms[PermissionKey.BMS_IPMI] is True


def test_user_preset_overrides_product_preset(db_session):
    product_preset = PermissionSetDAO.create(
        db_session, name="product-preset", permissions={PermissionKey.BMS_POWER: False}
    )
    product = ProductDAO.create(db_session, family_id=None, name="P2", description=None, code="p2")
    ProductDAO.update(db_session, product, permission_set_id=product_preset.id)

    user = UserDAO.create(db_session, username="client1", email="client1@example.com")
    user_preset = PermissionSetDAO.create(
        db_session, name="user-preset", permissions={PermissionKey.BMS_POWER: True}
    )
    user.permission_set_id = user_preset.id
    UserDAO.update(db_session, user)

    service = _bare_metal_service(db_session, name="svc-user", product_code="p2", owner_user_id=user.id)
    perms = resolve_client_permissions(db_session, service)
    assert perms[PermissionKey.BMS_POWER] is True


def test_service_preset_overrides_user_preset(db_session):
    user = UserDAO.create(db_session, username="client2", email="client2@example.com")
    user_preset = PermissionSetDAO.create(
        db_session, name="user-preset-2", permissions={PermissionKey.BMS_IPMI: True}
    )
    user.permission_set_id = user_preset.id
    UserDAO.update(db_session, user)

    service = _bare_metal_service(db_session, name="svc-service-preset", owner_user_id=user.id)
    service_preset = PermissionSetDAO.create(
        db_session, name="service-preset", permissions={PermissionKey.BMS_IPMI: False}
    )
    service.permission_set_id = service_preset.id
    ServiceDAO.update(db_session, service)

    perms = resolve_client_permissions(db_session, service)
    assert perms[PermissionKey.BMS_IPMI] is False


def test_per_service_overrides_win_over_everything(db_session):
    service = _bare_metal_service(db_session, name="svc-overrides")
    service_preset = PermissionSetDAO.create(
        db_session, name="full", permissions={PermissionKey.BMS_REINSTALL: True}
    )
    service.permission_set_id = service_preset.id
    service.permission_overrides = {PermissionKey.BMS_REINSTALL: False}
    ServiceDAO.update(db_session, service)

    perms = resolve_client_permissions(db_session, service)
    assert perms[PermissionKey.BMS_REINSTALL] is False


def test_has_and_require_client_permission(db_session):
    service = _bare_metal_service(db_session, name="svc-guard")
    service.permission_overrides = {PermissionKey.BMS_REINSTALL: False}
    ServiceDAO.update(db_session, service)

    assert has_client_permission(db_session, service, PermissionKey.BMS_POWER) is True
    assert has_client_permission(db_session, service, PermissionKey.BMS_REINSTALL) is False

    require_client_permission(db_session, service, PermissionKey.BMS_POWER)
    with pytest.raises(HTTPException) as exc_info:
        require_client_permission(db_session, service, PermissionKey.BMS_REINSTALL)
    assert exc_info.value.status_code == 403
