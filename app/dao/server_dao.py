from sqlalchemy import or_
from sqlalchemy.orm import Session
from typing import Optional, List
from app.models.server import Server, BootMode
from app.models.boot_task import BootTask
from app.models.installation_task import InstallationTask
from app.models.hardware_detection_report import HardwareDetectionReport
from app.models.server_activity import ServerActivity
from app.models.server_capability import ServerCapability
from app.models.disk import Disk
from app.models.network_port import NetworkPort
from app.models.cable_run import CableRun
from app.models.service_bare_metal import ServiceBareMetal
from app.models.server_group import server_group_association


class ServerDAO:
    """Data Access Object for Server model"""

    @staticmethod
    def create(
        db: Session,
        name: str,
        server_ip: str,
        plugin_name: str,
        plugin_config: dict,
        description: Optional[str] = None,
        cpu_count: int = 1,
        cpu_model: Optional[str] = None,
        ram_gb: Optional[int] = None,
        port_speed_mbps: Optional[int] = None,
        location_id: Optional[int] = None,
        rack_id: Optional[int] = None,
        rack_unit: Optional[int] = None,
        rack_units: int = 1,
        enabled: bool = True,
        boot_mode: BootMode = BootMode.UEFI,
        pxe_boot_mode: Optional[BootMode] = None,
        os_boot_mode: Optional[BootMode] = None,
        pxe_kernel_args_general: Optional[str] = None,
        pxe_kernel_args_network: Optional[str] = None,
        ipmi_proxy_enabled: bool = False,
        ipmi_web_management_url: Optional[str] = None,
        ipmi_viewer_username: Optional[str] = None,
        ipmi_viewer_password: Optional[str] = None
    ) -> Server:
        """Create a new server"""
        # Use provided pxe_boot_mode/os_boot_mode, or fallback to boot_mode for backward compatibility
        if pxe_boot_mode is None:
            pxe_boot_mode = boot_mode
        if os_boot_mode is None:
            os_boot_mode = boot_mode
        
        server = Server(
            name=name,
            server_ip=server_ip,
            description=description,
            cpu_count=cpu_count,
            cpu_model=cpu_model,
            ram_gb=ram_gb,
            port_speed_mbps=port_speed_mbps,
            location_id=location_id,
            rack_id=rack_id,
            rack_unit=rack_unit,
            rack_units=rack_units,
            plugin_name=plugin_name,
            plugin_config=plugin_config,
            enabled=enabled,
            boot_mode=boot_mode,
            pxe_boot_mode=pxe_boot_mode,
            os_boot_mode=os_boot_mode,
            pxe_kernel_args_general=pxe_kernel_args_general,
            pxe_kernel_args_network=pxe_kernel_args_network,
            ipmi_proxy_enabled=ipmi_proxy_enabled,
            ipmi_web_management_url=ipmi_web_management_url,
            ipmi_viewer_username=ipmi_viewer_username,
            ipmi_viewer_password=ipmi_viewer_password
        )
        db.add(server)
        db.commit()
        db.refresh(server)
        return server

    @staticmethod
    def get_by_id(db: Session, server_id: int) -> Optional[Server]:
        """Get server by ID"""
        return db.query(Server).filter(Server.id == server_id).first()

    @staticmethod
    def get_by_name(db: Session, name: str) -> Optional[Server]:
        """Get server by name"""
        return db.query(Server).filter(Server.name == name).first()

    @staticmethod
    def get_by_ip(db: Session, server_ip: str) -> Optional[Server]:
        """Get server by primary IP address (exact match)."""
        if not server_ip or not server_ip.strip():
            return None
        return db.query(Server).filter(Server.server_ip == server_ip.strip()).first()

    @staticmethod
    def get_all(db: Session, skip: int = 0, limit: int = 100, enabled_only: bool = False) -> List[Server]:
        """Get all servers with pagination"""
        query = db.query(Server)
        if enabled_only:
            query = query.filter(Server.enabled == True)
        return query.order_by(Server.name).offset(skip).limit(limit).all()

    @staticmethod
    def get_by_plugin(db: Session, plugin_name: str) -> List[Server]:
        """Get all servers using a specific plugin"""
        return db.query(Server).filter(Server.plugin_name == plugin_name).all()

    @staticmethod
    def get_by_location(db: Session, location_id: int) -> List[Server]:
        """Get all servers in a location"""
        return db.query(Server).filter(Server.location_id == location_id).all()

    @staticmethod
    def update(db: Session, server: Server) -> Server:
        """Update a server"""
        db.commit()
        db.refresh(server)
        return server

    @staticmethod
    def delete(db: Session, server_id: int) -> bool:
        """Delete a server by ID.

        Explicitly removes dependent rows first. Several child FKs are NOT NULL
        without ON DELETE CASCADE, and SQLAlchemy's default relationship handling
        would otherwise try to SET server_id=NULL and raise IntegrityError.
        """
        server = db.query(Server).filter(Server.id == server_id).first()
        if not server:
            return False

        if db.query(ServiceBareMetal).filter(ServiceBareMetal.server_id == server_id).first():
            raise ValueError(
                "Server is assigned to one or more services and cannot be deleted"
            )

        # Order matters: reports/install tasks reference boot_tasks.
        db.query(HardwareDetectionReport).filter(
            HardwareDetectionReport.server_id == server_id
        ).delete(synchronize_session=False)
        db.query(InstallationTask).filter(
            InstallationTask.server_id == server_id
        ).delete(synchronize_session=False)
        db.query(BootTask).filter(BootTask.server_id == server_id).delete(
            synchronize_session=False
        )
        db.query(ServerActivity).filter(ServerActivity.server_id == server_id).delete(
            synchronize_session=False
        )
        db.query(ServerCapability).filter(ServerCapability.server_id == server_id).delete(
            synchronize_session=False
        )
        db.query(Disk).filter(Disk.server_id == server_id).delete(synchronize_session=False)

        port_ids = [
            row[0]
            for row in db.query(NetworkPort.id).filter(NetworkPort.server_id == server_id).all()
        ]
        if port_ids:
            db.query(CableRun).filter(
                or_(
                    CableRun.end_a_server_port_id.in_(port_ids),
                    CableRun.end_b_server_port_id.in_(port_ids),
                )
            ).delete(synchronize_session=False)
            db.query(NetworkPort).filter(NetworkPort.server_id == server_id).delete(
                synchronize_session=False
            )

        db.execute(
            server_group_association.delete().where(
                server_group_association.c.server_id == server_id
            )
        )

        # Bulk delete avoids ORM relationship nullification of remaining children.
        db.query(Server).filter(Server.id == server_id).delete(synchronize_session=False)
        db.commit()
        return True
