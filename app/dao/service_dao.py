from sqlalchemy.orm import Session
from typing import Optional, List
from app.models.service import Service, ServiceStatus, ServiceType, ProvisioningSource
from app.models.service_bare_metal import ServiceBareMetal
from app.models.service_vm import ServiceVm
from app.models.user import User
from app.models.reseller import ServiceBilling


class ServiceDAO:
    """Data Access Object for Service model"""

    @staticmethod
    def create_bare_metal(
        db: Session,
        *,
        name: str,
        server_id: Optional[int] = None,
        owner_user_id: Optional[int] = None,
        external_service_id: Optional[str] = None,
        service_type: ServiceType = ServiceType.BARE_METAL,
        status: ServiceStatus = ServiceStatus.PENDING,
        description: Optional[str] = None,
        config: Optional[dict] = None,
        product_code: Optional[str] = None,
        os_code: Optional[str] = None,
        product_snapshot: Optional[dict] = None,
        provisioning_source: ProvisioningSource = ProvisioningSource.BILLING,
    ) -> Service:
        if service_type == ServiceType.VM:
            raise ValueError("Use create_vm for VM services")
        if server_id is None and service_type != ServiceType.HTTP_PROXY:
            raise ValueError("server_id is required for bare_metal services")
        if provisioning_source == ProvisioningSource.BILLING and owner_user_id is None:
            raise ValueError("owner_user_id is required when provisioning_source is billing")

        service = Service(
            name=name,
            external_service_id=external_service_id,
            owner_user_id=owner_user_id,
            service_type=service_type,
            status=status,
            description=description,
            config=config or {},
            product_code=product_code,
            os_code=os_code,
            product_snapshot=product_snapshot or {},
            provisioning_source=provisioning_source,
            bare_metal=ServiceBareMetal(server_id=server_id),
        )
        db.add(service)
        db.commit()
        db.refresh(service)
        return service

    @staticmethod
    def create_vm(
        db: Session,
        *,
        name: str,
        owner_user_id: Optional[int] = None,
        external_service_id: Optional[str] = None,
        status: ServiceStatus = ServiceStatus.PENDING,
        description: Optional[str] = None,
        config: Optional[dict] = None,
        product_code: Optional[str] = None,
        os_code: Optional[str] = None,
        product_snapshot: Optional[dict] = None,
        provisioning_source: ProvisioningSource = ProvisioningSource.BILLING,
        proxmox_cluster_id: Optional[int] = None,
        proxmox_node_name: Optional[str] = None,
        proxmox_vmid: Optional[int] = None,
        vm_template_id: Optional[int] = None,
    ) -> Service:
        if provisioning_source == ProvisioningSource.BILLING and owner_user_id is None:
            raise ValueError("owner_user_id is required when provisioning_source is billing")

        service = Service(
            name=name,
            external_service_id=external_service_id,
            owner_user_id=owner_user_id,
            service_type=ServiceType.VM,
            status=status,
            description=description,
            config=config or {},
            product_code=product_code,
            os_code=os_code,
            product_snapshot=product_snapshot or {},
            provisioning_source=provisioning_source,
            vm=ServiceVm(
                proxmox_cluster_id=proxmox_cluster_id,
                proxmox_node_name=proxmox_node_name,
                proxmox_vmid=proxmox_vmid,
                vm_template_id=vm_template_id,
            ),
        )
        db.add(service)
        db.commit()
        db.refresh(service)
        return service

    @staticmethod
    def get_by_id(db: Session, service_id: int) -> Optional[Service]:
        return db.query(Service).filter(Service.id == service_id).first()

    @staticmethod
    def get_by_name(db: Session, name: str) -> Optional[Service]:
        return db.query(Service).filter(Service.name == name).first()

    @staticmethod
    def get_by_external_service_id(db: Session, external_service_id: str) -> Optional[Service]:
        return db.query(Service).filter(Service.external_service_id == external_service_id).first()

    @staticmethod
    def get_by_external_service_id_and_owner(
        db: Session, external_service_id: str, owner_user_id: int
    ) -> Optional[Service]:
        return (
            db.query(Service)
            .filter(
                Service.external_service_id == external_service_id,
                Service.owner_user_id == owner_user_id,
            )
            .first()
        )

    @staticmethod
    def get_by_external_service_id_and_integration(
        db: Session, external_service_id: str, integration_id: int
    ) -> Optional[Service]:
        return (
            db.query(Service)
            .join(User, Service.owner_user_id == User.id)
            .filter(
                Service.external_service_id == external_service_id,
                User.billing_integration_id == integration_id,
            )
            .first()
        )

    @staticmethod
    def get_by_id_and_reseller(
        db: Session, service_id: int, reseller_id: int
    ) -> Optional[Service]:
        """Return a service only through its reseller billing ownership."""
        return (
            db.query(Service)
            .join(ServiceBilling, ServiceBilling.service_id == Service.id)
            .filter(
                Service.id == service_id,
                ServiceBilling.reseller_id == reseller_id,
            )
            .first()
        )

    @staticmethod
    def get_by_external_service_id_and_reseller(
        db: Session, external_service_id: str, reseller_id: int
    ) -> Optional[Service]:
        """Tenant-scoped external service lookup for reseller APIs."""
        return (
            db.query(Service)
            .join(ServiceBilling, ServiceBilling.service_id == Service.id)
            .filter(
                Service.external_service_id == external_service_id,
                ServiceBilling.reseller_id == reseller_id,
            )
            .first()
        )

    @staticmethod
    def list_by_reseller(
        db: Session, reseller_id: int, *, skip: int = 0, limit: int = 100
    ) -> List[Service]:
        return (
            db.query(Service)
            .join(ServiceBilling, ServiceBilling.service_id == Service.id)
            .filter(ServiceBilling.reseller_id == reseller_id)
            .order_by(Service.id)
            .offset(skip)
            .limit(limit)
            .all()
        )

    @staticmethod
    def get_by_owner_user(db: Session, owner_user_id: int) -> List[Service]:
        return db.query(Service).filter(Service.owner_user_id == owner_user_id).all()

    @staticmethod
    def get_by_server(db: Session, server_id: int) -> List[Service]:
        return (
            db.query(Service)
            .join(ServiceBareMetal, ServiceBareMetal.service_id == Service.id)
            .filter(ServiceBareMetal.server_id == server_id)
            .all()
        )

    @staticmethod
    def get_all(
        db: Session,
        skip: int = 0,
        limit: int = 100,
        status: Optional[ServiceStatus] = None,
        provisioning_source: Optional[ProvisioningSource] = None,
    ) -> List[Service]:
        query = db.query(Service)
        if status:
            query = query.filter(Service.status == status)
        if provisioning_source is not None:
            query = query.filter(Service.provisioning_source == provisioning_source)
        return query.order_by(Service.name).offset(skip).limit(limit).all()

    @staticmethod
    def update(db: Session, service: Service) -> Service:
        db.commit()
        db.refresh(service)
        return service

    @staticmethod
    def delete(db: Session, service_id: int) -> bool:
        service = db.query(Service).filter(Service.id == service_id).first()
        if service:
            db.delete(service)
            db.commit()
            return True
        return False
