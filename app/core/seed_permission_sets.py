"""
Seed script to populate the built-in (system) client permission presets.
"""
from sqlalchemy.orm import Session

from app.core.client_permissions import PermissionKey, SYSTEM_PRESETS
from app.dao.permission_set_dao import PermissionSetDAO
from app.models.permission_set import PermissionSet
from app.models.service import Service


def seed_permission_sets(db: Session) -> None:
    """Create built-in permission presets and enable guest password change.

    - Missing system presets are created from ``SYSTEM_PRESETS``.
    - Existing presets (except Read-only) get ``vm.change_password`` forced on
      so clients can change guest passwords by default after upgrades.
    - Per-service overrides that explicitly deny ``vm.change_password`` are
      cleared so the new default / preset value applies.
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
        # Ensure SQLAlchemy detects JSON mutation.
        from sqlalchemy.orm.attributes import flag_modified

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
            from sqlalchemy.orm.attributes import flag_modified

            flag_modified(service, "permission_overrides")

    db.commit()
