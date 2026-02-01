"""Data access for DHCP configuration (single row in dhcp_config table)."""
from typing import Optional
from sqlalchemy.orm import Session
from app.models.dhcp_config import DHCPConfigModel


class DHCPConfigDAO:
    CONFIG_ID = 1

    @staticmethod
    def get_config(db: Session) -> Optional[DHCPConfigModel]:
        return db.query(DHCPConfigModel).filter(DHCPConfigModel.id == DHCPConfigDAO.CONFIG_ID).first()

    @staticmethod
    def get_by_service_instance_id(db: Session, service_instance_id: int) -> Optional[DHCPConfigModel]:
        return (
            db.query(DHCPConfigModel)
            .filter(DHCPConfigModel.service_instance_id == service_instance_id)
            .first()
        )

    @staticmethod
    def get_or_create(db: Session, default_config_file_path: str, default_lease_file_path: str) -> DHCPConfigModel:
        row = DHCPConfigDAO.get_config(db)
        if row is not None:
            return row
        row = DHCPConfigModel(
            id=DHCPConfigDAO.CONFIG_ID,
            enabled=True,
            interfaces=[{"interface": "eth1", "ip": "192.168.12.74"}],
            dns_servers=None,
            hand_out_leases=True,
            default_lease_time=3600,
            max_lease_time=7200,
            config_file_path=default_config_file_path,
            lease_file_path=default_lease_file_path,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def get_or_create_for_service_instance(
        db: Session, service_instance_id: int, config_file_path: str, lease_file_path: str
    ) -> DHCPConfigModel:
        row = DHCPConfigDAO.get_by_service_instance_id(db, service_instance_id)
        if row is not None:
            return row
        row = DHCPConfigModel(
            service_instance_id=service_instance_id,
            enabled=True,
            interfaces=[{"interface": "eth1", "ip": "192.168.12.74"}],
            dns_servers=None,
            hand_out_leases=True,
            default_lease_time=3600,
            max_lease_time=7200,
            config_file_path=config_file_path,
            lease_file_path=lease_file_path,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def update(db: Session, row: DHCPConfigModel) -> DHCPConfigModel:
        db.commit()
        db.refresh(row)
        return row
