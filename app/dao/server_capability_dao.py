from typing import Dict, Iterable, List, Optional, Set

from sqlalchemy.orm import Session

from app.models.server_capability import ServerCapability


class ServerCapabilityDAO:
    """DAO for per-server capability states."""

    @staticmethod
    def list_by_server(db: Session, server_id: int) -> List[ServerCapability]:
        return (
            db.query(ServerCapability)
            .filter(ServerCapability.server_id == server_id)
            .order_by(ServerCapability.capability_id.asc())
            .all()
        )

    @staticmethod
    def get_by_server_and_capability(
        db: Session, server_id: int, capability_id: str
    ) -> Optional[ServerCapability]:
        return (
            db.query(ServerCapability)
            .filter(
                ServerCapability.server_id == server_id,
                ServerCapability.capability_id == capability_id,
            )
            .first()
        )

    @staticmethod
    def get_enabled_capability_ids(db: Session, server_id: int) -> Set[str]:
        rows = (
            db.query(ServerCapability.capability_id)
            .filter(
                ServerCapability.server_id == server_id,
                ServerCapability.enabled.is_(True),
            )
            .all()
        )
        return {row[0] for row in rows}

    @staticmethod
    def set_capability_states(
        db: Session,
        server_id: int,
        capability_states: Dict[str, bool],
        source: Optional[str] = "manual",
    ) -> List[ServerCapability]:
        existing = {
            row.capability_id: row
            for row in db.query(ServerCapability).filter(ServerCapability.server_id == server_id).all()
        }
        for capability_id, enabled in capability_states.items():
            row = existing.get(capability_id)
            if not row:
                row = ServerCapability(
                    server_id=server_id,
                    capability_id=capability_id,
                )
                db.add(row)
                existing[capability_id] = row
            row.enabled = bool(enabled)
            if source:
                row.source = source
        db.commit()
        return list(existing.values())

    @staticmethod
    def sync_states_for_capabilities(
        db: Session,
        server_id: int,
        capability_ids: Iterable[str],
        enabled_capability_ids: Iterable[str],
        source: Optional[str] = "manual",
    ) -> List[ServerCapability]:
        cap_set = {c for c in capability_ids if c}
        enabled_set = {c for c in enabled_capability_ids if c}
        states = {cap_id: (cap_id in enabled_set) for cap_id in cap_set}
        return ServerCapabilityDAO.set_capability_states(
            db=db,
            server_id=server_id,
            capability_states=states,
            source=source,
        )
