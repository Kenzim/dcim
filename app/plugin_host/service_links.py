"""Which services a plugin is active for, admin overrides and add-on links (docs/plugin-system.md section 9)."""

from __future__ import annotations

import re
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.plugin_host import PluginInstallation, PluginServiceLink, PluginStatus
from app.models.service import Service, ServiceStatus, ServiceType
from app.plugin_host.manifest import Manifest

ADDON_REF_RE = re.compile(r"^(whmcs|store):[0-9]{1,18}$")
ADDON_STATUSES = ("active", "suspended", "terminated")


def _type_value(service: Service) -> str:
    kind = service.service_type
    return kind.value if isinstance(kind, ServiceType) else str(kind)


def link_of(db: Session, plugin_id: str, service_id: int) -> Optional[PluginServiceLink]:
    return db.execute(
        select(PluginServiceLink).where(
            PluginServiceLink.plugin_id == plugin_id, PluginServiceLink.service_id == service_id
        )
    ).scalar_one_or_none()


def applies_to_type(manifest: Manifest, service: Service) -> bool:
    return manifest.services is not None and _type_value(service) in manifest.services.types


def is_active(db: Session, plugin_id: str, manifest: Manifest, service: Service) -> bool:
    if not applies_to_type(manifest, service) or service.status == ServiceStatus.TERMINATED:
        return False
    link = link_of(db, plugin_id, service.id)
    if link is not None:
        return bool(link.enabled)
    return manifest.services.enablement == "all"


def active_service_ids(
    db: Session, plugin_id: str, manifest: Manifest, *, service_type: Optional[str] = None
) -> list[int]:
    if manifest.services is None:
        return []
    types = [t for t in manifest.services.types if service_type in (None, t)]
    if not types:
        return []
    candidates = select(Service.id).where(
        Service.service_type.in_([ServiceType(t) for t in types]),
        Service.status != ServiceStatus.TERMINATED,
    )
    links = dict(
        db.execute(
            select(PluginServiceLink.service_id, PluginServiceLink.enabled).where(
                PluginServiceLink.plugin_id == plugin_id
            )
        ).all()
    )
    if manifest.services.enablement == "all":
        ids = db.execute(candidates).scalars().all()
        return sorted(i for i in ids if links.get(i, True))
    wanted = [sid for sid, enabled in links.items() if enabled]
    if not wanted:
        return []
    return sorted(db.execute(candidates.where(Service.id.in_(wanted))).scalars().all())


def set_admin_link(db: Session, plugin_id: str, service_id: int, enabled: Optional[bool]) -> None:
    """True or False records an admin decision; None removes any decision so the default applies."""
    link = link_of(db, plugin_id, service_id)
    if enabled is None:
        if link is not None:
            db.delete(link)
    elif link is None:
        db.add(PluginServiceLink(plugin_id=plugin_id, service_id=service_id, enabled=enabled, source="admin"))
    else:
        link.enabled = enabled
        link.source = "admin"
        link.addon_ref = None
    db.flush()


def set_plugin_link(db: Session, plugin_id: str, service_id: int, enabled: bool) -> None:
    """A plugin switching itself on or off for a service never overrides an admin decision."""
    link = link_of(db, plugin_id, service_id)
    if link is not None and link.source == "admin":
        return
    if link is None:
        db.add(PluginServiceLink(plugin_id=plugin_id, service_id=service_id, enabled=enabled, source="plugin"))
    else:
        link.enabled = enabled
        link.source = "plugin"
    db.flush()


def plugins_for_addon(db: Session, addon_ref: str) -> list[PluginInstallation]:
    rows = db.execute(select(PluginInstallation).order_by(PluginInstallation.id)).scalars()
    return [row for row in rows if addon_ref in (row.addon_refs or [])]


def apply_addon(db: Session, service: Service, addon_ref: str, status: str) -> list[dict]:
    """Switch linked plugins on (active) or off (suspended, terminated) for one service."""
    if not ADDON_REF_RE.match(addon_ref or ""):
        raise ValueError("addon_ref must look like whmcs:<id> or store:<id>")
    if status not in ADDON_STATUSES:
        raise ValueError("status must be active, suspended or terminated")
    enabled = status == "active"
    out = []
    for inst in plugins_for_addon(db, addon_ref):
        link = link_of(db, inst.id, service.id)
        if link is not None and link.source == "admin":
            out.append({"plugin_id": inst.id, "enabled": bool(link.enabled), "changed": False, "reason": "admin decision"})
            continue
        if link is None:
            db.add(
                PluginServiceLink(
                    plugin_id=inst.id, service_id=service.id, enabled=enabled, source="addon", addon_ref=addon_ref
                )
            )
        else:
            link.enabled = enabled
            link.source = "addon"
            link.addon_ref = addon_ref
        out.append({"plugin_id": inst.id, "enabled": enabled, "changed": True, "reason": None,
                    "plugin_enabled": inst.status == PluginStatus.ENABLED})
    db.flush()
    return out


def normalise_addon_refs(refs) -> list[str]:
    if not isinstance(refs, list):
        raise ValueError("addon_refs must be a list")
    out = []
    for ref in refs:
        ref = str(ref).strip()
        if not ADDON_REF_RE.match(ref):
            raise ValueError(f"{ref!r} is not an add-on reference; use whmcs:<id> or store:<id>")
        if ref not in out:
            out.append(ref)
    return out
