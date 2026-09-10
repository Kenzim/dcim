"""
Seed script to populate the built-in (system) client permission presets.
"""
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.core.client_permissions import PermissionKey, SYSTEM_PRESETS
from app.dao.permission_set_dao import PermissionSetDAO
from app.models.permission_set import PermissionSet
from app.models.service import Service


def _copy_kvm_from_ipmi(permissions: dict) -> bool:
    """Copy ``bms.ipmi`` onto ``bms.kvm`` when KVM was never set.

    Before ``bms.kvm`` existed, HTML5 KVM was gated by ``bms.ipmi``. Existing
    presets/overrides that mention IPMI but not KVM keep the same effective
    access until an admin sets the two keys independently.
    """
    if PermissionKey.BMS_KVM in permissions:
        return False
    if PermissionKey.BMS_IPMI not in permissions:
        return False
    permissions[PermissionKey.BMS_KVM] = bool(permissions[PermissionKey.BMS_IPMI])
    return True


def seed_permission_sets(db: Session) -> None:
    """Create built-in permission presets and apply one-time key backfills.

    - Missing system presets are created from ``SYSTEM_PRESETS``.
    - Existing presets (except Read-only) get ``vm.change_password`` forced on
      so clients can change guest passwords by default after upgrades.
    - Per-service overrides that explicitly deny ``vm.change_password`` are
      cleared so the new default / preset value applies.
    - Existing presets and sparse overrides that set ``bms.ipmi`` but not
      ``bms.kvm`` copy the IPMI value onto KVM so access does not change.
    """
    for preset in SYSTEM_PRESETS:
        existing = PermissionSetDAO.get_by_name(db, preset["name"])
        if existing:
            continue
        PermissionSetDAO.create(
            db,
            name=preset["name"],
            description=preset.get("description"),
            permissions=preset.get("permissions") or {},
            is_system=True,
        )

    # Enable change-password on every non-read-only preset (system + custom).
    presets = db.query(PermissionSet).all()
    for row in presets:
        if (row.name or "").strip().lower() == "read-only":
            continue
        perms = dict(row.permissions or {})
        if perms.get(PermissionKey.VM_CHANGE_PASSWORD) is True:
            continue
        perms[PermissionKey.VM_CHANGE_PASSWORD] = True
        row.permissions = perms
        flag_modified(row, "permissions")

    # Drop sparse denials so the updated defaults/presets take effect.
    services = (
        db.query(Service)
        .filter(Service.permission_overrides.isnot(None))
        .all()
    )
    for service in services:
        overrides = dict(service.permission_overrides or {})
        if PermissionKey.VM_CHANGE_PASSWORD not in overrides:
            continue
        if overrides.get(PermissionKey.VM_CHANGE_PASSWORD) is False:
            del overrides[PermissionKey.VM_CHANGE_PASSWORD]
            service.permission_overrides = overrides or None
            flag_modified(service, "permission_overrides")

    # Split HTML5 KVM from IPMI proxy without changing existing grants/denials.
    for row in db.query(PermissionSet).all():
        perms = dict(row.permissions or {})
        if _copy_kvm_from_ipmi(perms):
            row.permissions = perms
            flag_modified(row, "permissions")

    services = (
        db.query(Service)
        .filter(Service.permission_overrides.isnot(None))
        .all()
    )
    for service in services:
        overrides = dict(service.permission_overrides or {})
        if _copy_kvm_from_ipmi(overrides):
            service.permission_overrides = overrides
            flag_modified(service, "permission_overrides")

    db.commit()
