from datetime import datetime, timezone
import ipaddress
from typing import List, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.models.ipam import IPSubnet, IPAddress, ServiceIPAssignment, ServiceIPAssignmentHistory
from app.models.service import Service
from app.services.ip_allocation import get_allocation_strategy_registry

# Hard cap on how many host addresses a single subnet create will seed as
# IPAddress rows. Without this, an operator fat-fingering e.g. a /16 would
# try to materialize 65k+ rows in one request.
MAX_SEED_HOSTS = 4096


class IPAMDAO:
    @staticmethod
    def _hosts_in_range(
        cidr: str,
        range_start: Optional[str],
        range_end: Optional[str],
    ) -> List[ipaddress._BaseAddress]:
        network = ipaddress.ip_network(cidr, strict=False)
        start_ip = ipaddress.ip_address(range_start) if range_start else None
        end_ip = ipaddress.ip_address(range_end) if range_end else None
        return [
            host
            for host in network.hosts()
            if (start_ip is None or host >= start_ip) and (end_ip is None or host <= end_ip)
        ]

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
        max_resale_count: int = 1,
    ) -> IPSubnet:
        # Validate size before writing anything so an oversized CIDR fails
        # cleanly instead of half-creating a subnet with a truncated pool.
        hosts = IPAMDAO._hosts_in_range(cidr, range_start, range_end)
        if len(hosts) > MAX_SEED_HOSTS:
            raise ValueError(
                f"Subnet would seed {len(hosts)} IPs, exceeding the {MAX_SEED_HOSTS} limit; "
                "narrow the CIDR or set range_start/range_end"
            )
        if max_resale_count < 1:
            raise ValueError("max_resale_count must be at least 1")

        subnet = IPSubnet(
            name=name,
            cidr=cidr,
            location_id=location_id,
            range_start=range_start,
            range_end=range_end,
            tags=tags or [],
            allocation_strategy=allocation_strategy,
            max_resale_count=max_resale_count,
        )
        db.add(subnet)
        db.commit()
        db.refresh(subnet)
        IPAMDAO._seed_ips(db, subnet, hosts)
        return subnet

    @staticmethod
    def _seed_ips(db: Session, subnet: IPSubnet, hosts: List[ipaddress._BaseAddress]) -> None:
        for host in hosts:
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
    def update_subnet(
        db: Session,
        subnet_id: int,
        *,
        name: Optional[str] = None,
        enabled: Optional[bool] = None,
        allocation_strategy: Optional[str] = None,
        location_id: Optional[int] = None,
        clear_location: bool = False,
        max_resale_count: Optional[int] = None,
    ) -> Optional[IPSubnet]:
        subnet = IPAMDAO.get_subnet(db, subnet_id)
        if not subnet:
            return None
        if name is not None:
            subnet.name = name
        if enabled is not None:
            subnet.enabled = enabled
        if allocation_strategy is not None:
            subnet.allocation_strategy = allocation_strategy
        if max_resale_count is not None:
            if max_resale_count < 1:
                raise ValueError("max_resale_count must be at least 1")
            subnet.max_resale_count = max_resale_count
        if clear_location:
            subnet.location_id = None
        elif location_id is not None:
            subnet.location_id = location_id
        db.commit()
        db.refresh(subnet)
        return subnet

    @staticmethod
    def delete_subnet(db: Session, subnet_id: int) -> None:
        """Delete subnet if no assigned IPs. Raises ValueError otherwise."""
        subnet = IPAMDAO.get_subnet(db, subnet_id)
        if not subnet:
            raise ValueError("Subnet not found")
        assigned = (
            db.query(IPAddress)
            .filter(IPAddress.subnet_id == subnet_id, IPAddress.state == "assigned")
            .count()
        )
        if assigned:
            raise ValueError(f"Subnet has {assigned} assigned IP(s); release them first")
        db.delete(subnet)
        db.commit()

    @staticmethod
    def get_assignment_by_service(db: Session, service_id: int) -> List[ServiceIPAssignment]:
        return db.query(ServiceIPAssignment).filter(ServiceIPAssignment.service_id == service_id).all()

    @staticmethod
    def list_assignments(db: Session) -> List[ServiceIPAssignment]:
        return (
            db.query(ServiceIPAssignment)
            .options(
                joinedload(ServiceIPAssignment.ip).joinedload(IPAddress.subnet),
                joinedload(ServiceIPAssignment.service),
            )
            .order_by(ServiceIPAssignment.assigned_at.desc())
            .all()
        )

    @staticmethod
    def _candidate_ips(db: Session, subnet: IPSubnet, owner_user_id: Optional[int]) -> List[IPAddress]:
        """Free IPs first; if none, IPs eligible for resale under this
        subnet's ``max_resale_count`` — excluding any IP already held by an
        assignment whose service shares ``owner_user_id`` (no double-selling
        the same IP to the same customer) and any IP already at its resale
        cap."""
        free = (
            db.query(IPAddress)
            .filter(IPAddress.subnet_id == subnet.id, IPAddress.state == "free")
            .all()
        )
        if free:
            return free

        max_resale = subnet.max_resale_count or 1
        if max_resale <= 1:
            return []

        assigned = (
            db.query(IPAddress)
            .options(joinedload(IPAddress.assignments).joinedload(ServiceIPAssignment.service))
            .filter(IPAddress.subnet_id == subnet.id, IPAddress.state == "assigned")
            .all()
        )
        candidates = []
        for ip_row in assigned:
            current_assignments = ip_row.assignments or []
            if len(current_assignments) >= max_resale:
                continue
            if owner_user_id is not None and any(
                a.service is not None and a.service.owner_user_id == owner_user_id
                for a in current_assignments
            ):
                continue
            candidates.append(ip_row)
        return candidates

    @staticmethod
    def _resolve_owner_user_id(db: Session, service_id: int) -> Optional[int]:
        service = db.query(Service).filter(Service.id == service_id).first()
        return service.owner_user_id if service else None

    @staticmethod
    def _order_subnets_for_assignment(subnets: List[IPSubnet], subnet_id: Optional[int], strategy: Optional[str]) -> List[IPSubnet]:
        if subnet_id or (strategy or "").lower() != "spread_subnets":
            return subnets
        # Order candidate subnets by current load (fewest assigned IPs first)
        # so allocation actually spreads across subnets instead of always
        # draining the first one before touching the next.
        return sorted(
            subnets,
            key=lambda s: len([ip for ip in (s.ip_addresses or []) if ip.state == "assigned"]),
        )

    @staticmethod
    def _enforce_resale_cap(
        db: Session, ip_row: IPAddress, max_resale: int, assignment: ServiceIPAssignment
    ) -> None:
        """Race guard: since ``ip_id`` is no longer unique, two concurrent
        requests can both pass the cap check before either commits. Re-count
        after commit; if oversold, only the earliest ``max_resale``
        assignments (by id) keep the slot — anyone else backs out and the
        caller retries."""
        current = (
            db.query(ServiceIPAssignment)
            .filter(ServiceIPAssignment.ip_id == ip_row.id)
            .order_by(ServiceIPAssignment.id)
            .all()
        )
        if len(current) <= max_resale:
            return
        surviving_ids = {a.id for a in current[:max_resale]}
        if assignment.id in surviving_ids:
            return

        db.delete(assignment)
        remaining = (
            db.query(ServiceIPAssignment)
            .filter(ServiceIPAssignment.ip_id == ip_row.id, ServiceIPAssignment.id != assignment.id)
            .count()
        )
        # Only flip the IP back to free if this was truly the last
        # assignment removed (defensive; normally a surviving assignment
        # keeps it "assigned").
        if remaining == 0:
            ip_row.state = "free"
        db.commit()
        raise ValueError("Selected IP was concurrently assigned at capacity; retry allocation")

    @staticmethod
    def _try_assign_in_subnet(
        db: Session,
        subnet: IPSubnet,
        owner_user_id: Optional[int],
        service_id: int,
        strategy: Optional[str],
        username: Optional[str],
        password: Optional[str],
        assigned_by: Optional[str],
    ) -> Optional[ServiceIPAssignment]:
        candidates = IPAMDAO._candidate_ips(db, subnet, owner_user_id)
        if not candidates:
            return None
        effective_strategy = strategy or subnet.allocation_strategy
        ordered = get_allocation_strategy_registry().resolve(effective_strategy).order(candidates)
        selected = ordered[0] if ordered else None
        if not selected:
            return None

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
                details={"strategy": effective_strategy},
                created_at=datetime.now(timezone.utc),
            )
        )
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise ValueError("Selected IP was concurrently assigned to another service; retry allocation")
        db.refresh(assignment)

        IPAMDAO._enforce_resale_cap(db, selected, subnet.max_resale_count or 1, assignment)
        return assignment

    @staticmethod
    def assign_ip(
        db: Session,
        service_id: int,
        subnet_id: Optional[int] = None,
        strategy: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        assigned_by: Optional[str] = None,
        subnet_ids: Optional[List[int]] = None,
    ) -> ServiceIPAssignment:
        """Assign one IP to ``service_id``.

        Selection scope (first match wins):
        - ``subnet_id`` — single subnet
        - else ``subnet_ids`` — only those subnets (e.g. a proxy subnet group)
        - else all enabled IPAM subnets
        """
        if subnet_id is not None:
            subnets = [IPAMDAO.get_subnet(db, subnet_id)]
        elif subnet_ids:
            wanted = {int(x) for x in subnet_ids}
            subnets = [s for s in IPAMDAO.list_subnets(db) if s.id in wanted]
        else:
            subnets = IPAMDAO.list_subnets(db)
        subnets = [s for s in subnets if s and s.enabled]
        if not subnets:
            raise ValueError("No enabled subnets available")

        owner_user_id = IPAMDAO._resolve_owner_user_id(db, service_id)
        subnets = IPAMDAO._order_subnets_for_assignment(subnets, subnet_id, strategy)

        for subnet in subnets:
            assignment = IPAMDAO._try_assign_in_subnet(
                db, subnet, owner_user_id, service_id, strategy, username, password, assigned_by
            )
            if assignment is not None:
                return assignment
        raise ValueError("No free IP available in selected subnets")

    @staticmethod
    def release_all_for_service(
        db: Session, service_id: int, released_by: Optional[str] = None
    ) -> int:
        """Release every IP assignment held by a service (e.g. on terminate)."""
        assignments = IPAMDAO.get_assignment_by_service(db, service_id=service_id)
        count = 0
        for assignment in assignments:
            if IPAMDAO.release_ip(db, assignment_id=assignment.id, released_by=released_by):
                count += 1
        return count

    @staticmethod
    def rotate_credentials(
        db: Session,
        assignment_id: int,
        username: Optional[str] = None,
        password: Optional[str] = None,
        rotated_by: Optional[str] = None,
    ) -> Optional[ServiceIPAssignment]:
        """Replace username/password on an existing assignment in place
        (same IP, new credentials) and record history."""
        assignment = db.query(ServiceIPAssignment).filter(ServiceIPAssignment.id == assignment_id).first()
        if not assignment:
            return None
        ip_row = assignment.ip
        subnet = ip_row.subnet if ip_row else None
        assignment.username = username
        assignment.password = password
        db.add(
            ServiceIPAssignmentHistory(
                service_id=assignment.service_id,
                ip_address=ip_row.ip_address if ip_row else "",
                subnet_cidr=subnet.cidr if subnet else None,
                action="rotated",
                username=username,
                assigned_by=rotated_by,
                details={},
                created_at=datetime.now(timezone.utc),
            )
        )
        db.commit()
        db.refresh(assignment)
        return assignment

    @staticmethod
    def release_ip(db: Session, assignment_id: int, released_by: Optional[str] = None) -> bool:
        assignment = db.query(ServiceIPAssignment).filter(ServiceIPAssignment.id == assignment_id).first()
        if not assignment:
            return False
        ip_row = assignment.ip
        subnet = ip_row.subnet if ip_row else None
        ip_value = ip_row.ip_address if ip_row else ""
        if ip_row:
            remaining_others = (
                db.query(ServiceIPAssignment)
                .filter(ServiceIPAssignment.ip_id == ip_row.id, ServiceIPAssignment.id != assignment.id)
                .count()
            )
            # Only free the IP's state once its last live assignment (slot)
            # is released — a resold IP stays "assigned" while other owners
            # still hold a slot on it.
            if remaining_others == 0:
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
                created_at=datetime.now(timezone.utc),
            )
        )
        db.delete(assignment)
        db.commit()
        return True

    @staticmethod
    def used_slots(db: Session, subnet_id: int) -> int:
        """Total live ServiceIPAssignment rows across all IPs in a subnet
        (i.e. how many resale "slots" are currently occupied)."""
        return (
            db.query(ServiceIPAssignment)
            .join(IPAddress, ServiceIPAssignment.ip_id == IPAddress.id)
            .filter(IPAddress.subnet_id == subnet_id)
            .count()
        )

    @staticmethod
    def list_history(db: Session, service_id: Optional[int] = None) -> List[ServiceIPAssignmentHistory]:
        query = db.query(ServiceIPAssignmentHistory)
        if service_id is not None:
            query = query.filter(ServiceIPAssignmentHistory.service_id == service_id)
        return query.order_by(ServiceIPAssignmentHistory.created_at.desc()).limit(500).all()
