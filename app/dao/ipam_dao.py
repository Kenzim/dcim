from datetime import datetime
import ipaddress
from typing import List, Optional

from sqlalchemy.orm import Session

from app.models.ipam import IPSubnet, IPAddress, ServiceIPAssignment, ServiceIPAssignmentHistory
from app.services.ip_allocation import get_allocation_strategy_registry


class IPAMDAO:
    @staticmethod
    def create_subnet(
        db: Session,
        name: str,
        cidr: str,
        location_id: Optional[int] = None,
        range_start: Optional[str] = None,
        range_end: Optional[str] = None,
        tags: Optional[list] = None,
        allocation_strategy: str = "first_free",
    ) -> IPSubnet:
        subnet = IPSubnet(
            name=name,
            cidr=cidr,
            location_id=location_id,
            range_start=range_start,
            range_end=range_end,
            tags=tags or [],
            allocation_strategy=allocation_strategy,
        )
        db.add(subnet)
        db.commit()
        db.refresh(subnet)
        IPAMDAO._seed_ips(db, subnet)
        return subnet

    @staticmethod
    def _seed_ips(db: Session, subnet: IPSubnet) -> None:
        network = ipaddress.ip_network(subnet.cidr, strict=False)
        start_ip = ipaddress.ip_address(subnet.range_start) if subnet.range_start else None
        end_ip = ipaddress.ip_address(subnet.range_end) if subnet.range_end else None
        for host in network.hosts():
            if start_ip and host < start_ip:
                continue
            if end_ip and host > end_ip:
                continue
            existing = db.query(IPAddress).filter(IPAddress.ip_address == str(host)).first()
            if existing:
                continue
            db.add(IPAddress(subnet_id=subnet.id, ip_address=str(host), state="free"))
        db.commit()

    @staticmethod
    def list_subnets(db: Session) -> List[IPSubnet]:
        return db.query(IPSubnet).order_by(IPSubnet.id).all()

    @staticmethod
    def get_subnet(db: Session, subnet_id: int) -> Optional[IPSubnet]:
        return db.query(IPSubnet).filter(IPSubnet.id == subnet_id).first()

    @staticmethod
    def get_assignment_by_service(db: Session, service_id: int) -> List[ServiceIPAssignment]:
        return db.query(ServiceIPAssignment).filter(ServiceIPAssignment.service_id == service_id).all()

    @staticmethod
    def _pick_free_ip(db: Session, subnet: IPSubnet, strategy: str) -> Optional[IPAddress]:
        candidates = (
            db.query(IPAddress)
            .filter(IPAddress.subnet_id == subnet.id, IPAddress.state == "free")
            .all()
        )
        if not candidates:
            return None
        ordered = get_allocation_strategy_registry().resolve(strategy).order(candidates)
        return ordered[0] if ordered else None

    @staticmethod
    def assign_ip(
        db: Session,
        service_id: int,
        subnet_id: Optional[int] = None,
        strategy: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        assigned_by: Optional[str] = None,
    ) -> ServiceIPAssignment:
        subnets = [IPAMDAO.get_subnet(db, subnet_id)] if subnet_id else IPAMDAO.list_subnets(db)
        subnets = [s for s in subnets if s and s.enabled]
        if not subnets:
            raise ValueError("No enabled subnets available")

        for subnet in subnets:
            selected = IPAMDAO._pick_free_ip(db, subnet, strategy or subnet.allocation_strategy)
            if not selected:
                continue
            selected.state = "assigned"
            assignment = ServiceIPAssignment(
                service_id=service_id,
                ip_id=selected.id,
                username=username,
                password=password,
                assigned_by=assigned_by,
            )
            db.add(assignment)
            db.add(
                ServiceIPAssignmentHistory(
                    service_id=service_id,
                    ip_address=selected.ip_address,
                    subnet_cidr=subnet.cidr,
                    action="assigned",
                    username=username,
                    assigned_by=assigned_by,
                    details={"strategy": strategy or subnet.allocation_strategy},
                    created_at=datetime.utcnow(),
                )
            )
            db.commit()
            db.refresh(assignment)
            return assignment
        raise ValueError("No free IP available in selected subnets")

    @staticmethod
    def release_ip(db: Session, assignment_id: int, released_by: Optional[str] = None) -> bool:
        assignment = db.query(ServiceIPAssignment).filter(ServiceIPAssignment.id == assignment_id).first()
        if not assignment:
            return False
        ip_row = assignment.ip
        subnet = ip_row.subnet if ip_row else None
        ip_value = ip_row.ip_address if ip_row else ""
        if ip_row:
            ip_row.state = "free"
        db.add(
            ServiceIPAssignmentHistory(
                service_id=assignment.service_id,
                ip_address=ip_value,
                subnet_cidr=subnet.cidr if subnet else None,
                action="released",
                username=assignment.username,
                assigned_by=released_by,
                details={},
                created_at=datetime.utcnow(),
            )
        )
        db.delete(assignment)
        db.commit()
        return True

    @staticmethod
    def list_history(db: Session, service_id: Optional[int] = None) -> List[ServiceIPAssignmentHistory]:
        query = db.query(ServiceIPAssignmentHistory)
        if service_id is not None:
            query = query.filter(ServiceIPAssignmentHistory.service_id == service_id)
        return query.order_by(ServiceIPAssignmentHistory.created_at.desc()).limit(500).all()
