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


def _copy_sol_from_kvm(permissions: dict) -> bool:
    """Copy ``bms.kvm`` onto ``bms.sol`` when SOL was never set.

    Read-only presets that deny KVM would otherwise inherit the bare-metal
    default (SOL on) via resolver fallthrough.
    """
    if PermissionKey.BMS_SOL in permissions:
        return False
    if PermissionKey.BMS_KVM not in permissions:
        return False
    permissions[PermissionKey.BMS_SOL] = bool(permissions[PermissionKey.BMS_KVM])
    return True


def _copy_virtual_media_from_kvm(permissions: dict) -> bool:
    """Copy ``bms.kvm`` onto ``bms.virtual_media`` when it was never set."""
    if PermissionKey.BMS_VIRTUAL_MEDIA in permissions:
        return False
    if PermissionKey.BMS_KVM not in permissions:
        return False
    permissions[PermissionKey.BMS_VIRTUAL_MEDIA] = bool(permissions[PermissionKey.BMS_KVM])
    return True


def _backfill_bms_keys(permissions: dict) -> bool:
    changed = _copy_kvm_from_ipmi(permissions)
    if _copy_sol_from_kvm(permissions):
        changed = True
    if _copy_virtual_media_from_kvm(permissions):
        changed = True
    return changed


def _ensure_system_presets(db: Session) -> None:
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


def _enable_change_password_on_presets(db: Session) -> None:
    for row in db.query(PermissionSet).all():
        if (row.name or "").strip().lower() == "read-only":
            continue
        perms = dict(row.permissions or {})
        if perms.get(PermissionKey.VM_CHANGE_PASSWORD) is True:
            continue
        perms[PermissionKey.VM_CHANGE_PASSWORD] = True
        row.permissions = perms
        flag_modified(row, "permissions")


def _clear_sparse_change_password_denials(db: Session) -> None:
    services = db.query(Service).filter(Service.permission_overrides.isnot(None)).all()
    for service in services:
        overrides = dict(service.permission_overrides or {})
        if PermissionKey.VM_CHANGE_PASSWORD not in overrides:
            continue
        if overrides.get(PermissionKey.VM_CHANGE_PASSWORD) is False:
            del overrides[PermissionKey.VM_CHANGE_PASSWORD]
            service.permission_overrides = overrides or None
            flag_modified(service, "permission_overrides")


def _backfill_bms_keys_on_presets(db: Session) -> None:
    for row in db.query(PermissionSet).all():
        perms = dict(row.permissions or {})
        if _backfill_bms_keys(perms):
            row.permissions = perms
            flag_modified(row, "permissions")


def _backfill_bms_keys_on_services(db: Session) -> None:
    services = db.query(Service).filter(Service.permission_overrides.isnot(None)).all()
    for service in services:
        overrides = dict(service.permission_overrides or {})
        if _backfill_bms_keys(overrides):
            service.permission_overrides = overrides
            flag_modified(service, "permission_overrides")


def seed_permission_sets(db: Session) -> None:
    """Create built-in permission presets and apply one-time key backfills.

    - Missing system presets are created from ``SYSTEM_PRESETS``.
    - Existing presets (except Read-only) get ``vm.change_password`` forced on
      so clients can change guest passwords by default after upgrades.
    - Per-service overrides that explicitly deny ``vm.change_password`` are
      cleared so the new default / preset value applies.
    - Existing presets and sparse overrides that set ``bms.ipmi`` but not
      ``bms.kvm`` copy the IPMI value onto KVM so access does not change.
    - Existing presets and sparse overrides that set ``bms.kvm`` but not
      ``bms.sol`` / ``bms.virtual_media`` copy the KVM value onto those keys.
    """
    _ensure_system_presets(db)
    _enable_change_password_on_presets(db)
    _clear_sparse_change_password_denials(db)
    _backfill_bms_keys_on_presets(db)
    _backfill_bms_keys_on_services(db)
    db.commit()
