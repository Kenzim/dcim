"""Seed copies bms.ipmi onto bms.kvm for existing presets and overrides."""
from app.core.client_permissions import PermissionKey
from app.core.seed_permission_sets import _copy_kvm_from_ipmi, seed_permission_sets
from app.dao.permission_set_dao import PermissionSetDAO
from app.dao.service_dao import ServiceDAO
from app.models.service import ProvisioningSource


def _bare_metal_service(db_session):
    from app.dao.location_dao import LocationDAO
    from app.dao.server_dao import ServerDAO

    location = LocationDAO.create(db_session, name="loc-seed-kvm")
    server = ServerDAO.create(
        db_session,
        name="srv-seed-kvm",
        server_ip="10.0.0.9",
        plugin_name="ipmi",
        plugin_config={},
        location_id=location.id,
    )
    return ServiceDAO.create_bare_metal(
        db_session,
        name="svc-seed-kvm",
        server_id=server.id,
        provisioning_source=ProvisioningSource.INTERNAL,
    )


def test_copy_kvm_from_ipmi_only_when_unset():
    granted = {PermissionKey.BMS_IPMI: True}
    assert _copy_kvm_from_ipmi(granted) is True
    assert granted[PermissionKey.BMS_KVM] is True

    denied = {PermissionKey.BMS_IPMI: False}
    assert _copy_kvm_from_ipmi(denied) is True
    assert denied[PermissionKey.BMS_KVM] is False

    already = {PermissionKey.BMS_IPMI: False, PermissionKey.BMS_KVM: True}
    assert _copy_kvm_from_ipmi(already) is False
    assert already[PermissionKey.BMS_KVM] is True

    empty = {}
    assert _copy_kvm_from_ipmi(empty) is False
    assert PermissionKey.BMS_KVM not in empty


def test_seed_backfills_kvm_from_ipmi_on_preset_and_override(db_session):
    preset = PermissionSetDAO.create(
        db_session,
        name="legacy-ipmi-only",
        permissions={PermissionKey.BMS_IPMI: False, PermissionKey.BMS_POWER: True},
    )
    service = _bare_metal_service(db_session)
    service.permission_overrides = {PermissionKey.BMS_IPMI: False}
    ServiceDAO.update(db_session, service)

    seed_permission_sets(db_session)

    db_session.refresh(preset)
    db_session.refresh(service)
    assert preset.permissions[PermissionKey.BMS_KVM] is False
    assert service.permission_overrides[PermissionKey.BMS_KVM] is False
