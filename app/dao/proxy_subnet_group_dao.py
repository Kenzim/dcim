"""Data access for proxy IPAM subnet groups."""
from __future__ import annotations

import re
from typing import List, Optional, Sequence

from sqlalchemy.orm import Session, joinedload

from app.models.ipam import IPSubnet
from app.models.proxy_subnet_group import ProxySubnetGroup, ProxySubnetGroupMember


def _slug_code(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-") or "proxy-pool"
    return base[:120]


class ProxySubnetGroupDAO:
    @staticmethod
    def get_by_id(db: Session, group_id: int) -> Optional[ProxySubnetGroup]:
        return (
            db.query(ProxySubnetGroup)
            .options(joinedload(ProxySubnetGroup.members).joinedload(ProxySubnetGroupMember.subnet))
            .filter(ProxySubnetGroup.id == group_id)
            .first()
        )

    @staticmethod
    def get_by_code(db: Session, code: str) -> Optional[ProxySubnetGroup]:
        return db.query(ProxySubnetGroup).filter(ProxySubnetGroup.code == code).first()

    @staticmethod
    def list_all(db: Session) -> List[ProxySubnetGroup]:
        return (
            db.query(ProxySubnetGroup)
            .options(joinedload(ProxySubnetGroup.members).joinedload(ProxySubnetGroupMember.subnet))
            .order_by(ProxySubnetGroup.name, ProxySubnetGroup.id)
            .all()
        )

    @staticmethod
    def member_subnet_ids(db: Session, group_id: int, *, enabled_only: bool = True) -> List[int]:
        q = (
            db.query(ProxySubnetGroupMember.subnet_id)
            .join(IPSubnet, IPSubnet.id == ProxySubnetGroupMember.subnet_id)
            .filter(ProxySubnetGroupMember.group_id == group_id)
        )
        if enabled_only:
            q = q.filter(IPSubnet.enabled.is_(True))
        return [row[0] for row in q.all()]

    @staticmethod
    def create(
        db: Session,
        *,
        name: str,
        code: Optional[str] = None,
        description: Optional[str] = None,
        enabled: bool = True,
        subnet_ids: Optional[Sequence[int]] = None,
    ) -> ProxySubnetGroup:
        name = (name or "").strip()
        if not name:
            raise ValueError("name is required")
        code = (code or _slug_code(name)).strip()
        if ProxySubnetGroupDAO.get_by_code(db, code):
            # Ensure uniqueness with a numeric suffix.
            n = 2
            while ProxySubnetGroupDAO.get_by_code(db, f"{code}-{n}"):
                n += 1
            code = f"{code}-{n}"

        row = ProxySubnetGroup(
            name=name,
            code=code,
            description=(description or None),
            enabled=bool(enabled),
        )
        db.add(row)
        db.flush()
        ProxySubnetGroupDAO.set_members(db, row, subnet_ids or [], commit=False)
        db.commit()
        db.refresh(row)
        return ProxySubnetGroupDAO.get_by_id(db, row.id) or row

    @staticmethod
    def update(
        db: Session,
        row: ProxySubnetGroup,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        enabled: Optional[bool] = None,
        subnet_ids: Optional[Sequence[int]] = None,
    ) -> ProxySubnetGroup:
        if name is not None:
            cleaned = name.strip()
            if not cleaned:
                raise ValueError("name cannot be empty")
            row.name = cleaned
        if description is not None:
            row.description = description or None
        if enabled is not None:
            row.enabled = bool(enabled)
        if subnet_ids is not None:
            ProxySubnetGroupDAO.set_members(db, row, subnet_ids, commit=False)
        db.commit()
        return ProxySubnetGroupDAO.get_by_id(db, row.id) or row

    @staticmethod
    def set_members(
        db: Session,
        row: ProxySubnetGroup,
        subnet_ids: Sequence[int],
        *,
        commit: bool = True,
    ) -> None:
        wanted = []
        seen = set()
        for raw in subnet_ids or []:
            sid = int(raw)
            if sid in seen:
                continue
            seen.add(sid)
            subnet = db.query(IPSubnet).filter(IPSubnet.id == sid).first()
            if not subnet:
                raise ValueError(f"IPAM subnet {sid} not found")
            wanted.append(sid)

        db.query(ProxySubnetGroupMember).filter(ProxySubnetGroupMember.group_id == row.id).delete()
        for sid in wanted:
            db.add(ProxySubnetGroupMember(group_id=row.id, subnet_id=sid))
        if commit:
            db.commit()
        else:
            db.flush()

    @staticmethod
    def delete(db: Session, group_id: int) -> bool:
        row = db.query(ProxySubnetGroup).filter(ProxySubnetGroup.id == group_id).first()
        if not row:
            return False
        db.delete(row)
        db.commit()
        return True
