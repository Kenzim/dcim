"""Queue applies when a global setting the VM firewall depends on changes (registered with
``settings_store.on_setting_change`` for the ``vm_firewall`` group).

``vm_firewall_ipv6_ra_bridges`` changes what renders (the IPv6 prefix allowance and the
``net0_bridge_unknown`` fail-safe); ``vm_firewall_infrastructure_bridges`` changes eligibility.
The VMs affected get their revision bumped so the apply queue reaches them without waiting for
the drift sweep.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.vm_firewall import ServiceFirewall
from app.services import settings_store
from app.services.vm_firewall.service import bump_revisions

BRIDGE_SETTINGS = ("vm_firewall_ipv6_ra_bridges", "vm_firewall_infrastructure_bridges")


def _bridges(value: Any) -> set[str]:
    return {part.strip() for part in str(value or "").split(",") if part.strip()}


def affected_by_bridge_change(db: Session, key: str, old: Any, new: Any) -> list[int]:
    """Rows whose observed net0 bridge joined or left the list; for the router-advert list also
    the rows with an unknown bridge when the list goes between empty and not empty."""
    before, after = _bridges(old), _bridges(new)
    changed = sorted(before ^ after)
    conditions = [ServiceFirewall.net0_bridge.in_(changed)] if changed else []
    if key == "vm_firewall_ipv6_ra_bridges" and bool(before) != bool(after):
        conditions.append(ServiceFirewall.net0_bridge.is_(None))
    if not conditions:
        return []
    return sorted(db.execute(select(ServiceFirewall.service_id).where(or_(*conditions))).scalars())


def on_setting_changed(db: Session, key: str, old: Any, new: Any) -> None:
    if key in BRIDGE_SETTINGS:
        bump_revisions(db, affected_by_bridge_change(db, key, old, new))


settings_store.on_setting_change("vm_firewall", on_setting_changed)
