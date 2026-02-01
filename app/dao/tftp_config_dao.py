"""Data access for TFTP configuration (single row in tftp_config table)."""
from typing import Optional
from sqlalchemy.orm import Session
from app.models.tftp_config import TFTPConfigModel


class TFTPConfigDAO:
    CONFIG_ID = 1

    @staticmethod
    def get_config(db: Session) -> Optional[TFTPConfigModel]:
        return db.query(TFTPConfigModel).filter(TFTPConfigModel.id == TFTPConfigDAO.CONFIG_ID).first()

    @staticmethod
    def get_or_create(db: Session, default_root_directory: str) -> TFTPConfigModel:
        row = TFTPConfigDAO.get_config(db)
        if row is not None:
            return row
        row = TFTPConfigModel(
            id=TFTPConfigDAO.CONFIG_ID,
            enabled=True,
            root_directory=default_root_directory,
            bind_address="0.0.0.0",
            bind_port=69,
            allow_create=True,
            verbose=True,
            ipv4_only=True,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def update(db: Session, row: TFTPConfigModel) -> TFTPConfigModel:
        db.commit()
        db.refresh(row)
        return row
