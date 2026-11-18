"""Data access for backup slots and retained backups."""

from typing import Iterable, List, Optional

from sqlalchemy.orm import Session

from app.models.backup_slot import BackupSlot, RetainedBackup


class BackupSlotDAO:
    @staticmethod
    def create_slot(db: Session, *, service_id: int, size_bytes: int) -> BackupSlot:
        slot = BackupSlot(service_id=service_id, size_bytes=size_bytes)
        db.add(slot)
        db.flush()
        return slot

    @staticmethod
    def get_for_service(db: Session, service_id: int) -> Optional[BackupSlot]:
        return db.query(BackupSlot).filter(BackupSlot.service_id == service_id).first()

    @staticmethod
    def list_for_owner(db: Session, owner_user_id: int) -> List[BackupSlot]:
        from app.models.service import Service

        return (
            db.query(BackupSlot)
            .join(Service, Service.id == BackupSlot.service_id)
            .filter(Service.owner_user_id == owner_user_id)
            .order_by(BackupSlot.id)
            .all()
        )

    @staticmethod
    def unterminated(db: Session) -> List[BackupSlot]:
        return db.query(BackupSlot).filter(BackupSlot.terminated_at.is_(None)).all()

    @staticmethod
    def terminated(db: Session) -> List[BackupSlot]:
        return db.query(BackupSlot).filter(BackupSlot.terminated_at.isnot(None)).all()

    @staticmethod
    def delete_slot(db: Session, slot: BackupSlot) -> None:
        db.delete(slot)
        db.commit()


class RetainedBackupDAO:
    @staticmethod
    def get(db: Session, retained_id: int) -> Optional[RetainedBackup]:
        return db.query(RetainedBackup).filter(RetainedBackup.id == retained_id).first()

    @staticmethod
    def get_by_volid(db: Session, volid: str) -> Optional[RetainedBackup]:
        return db.query(RetainedBackup).filter(RetainedBackup.volid == volid).first()

    @staticmethod
    def list_for_owner(db: Session, owner_user_id: int) -> List[RetainedBackup]:
        return (
            db.query(RetainedBackup)
            .filter(RetainedBackup.owner_user_id == owner_user_id)
            .order_by(RetainedBackup.id)
            .all()
        )

    @staticmethod
    def by_volid(db: Session, volids: Iterable[str]) -> dict:
        """Retained rows keyed by volid, for the volids given."""
        wanted = [str(v) for v in volids if v]
        if not wanted:
            return {}
        rows = db.query(RetainedBackup).filter(RetainedBackup.volid.in_(wanted)).all()
        return {row.volid: row for row in rows}

    @staticmethod
    def add(db: Session, row: RetainedBackup) -> RetainedBackup:
        db.add(row)
        db.flush()
        return row

    @staticmethod
    def remove(db: Session, row: RetainedBackup) -> None:
        db.delete(row)
        db.flush()
