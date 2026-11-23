"""Database fixtures for VM firewall tests: a location, a cluster, clients and VM services."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.orm import Session

from app.models.location import Location
from app.models.product_catalog import Product, VMTemplate
from app.models.proxmox_inventory import ProxmoxCluster
from app.models.reseller import ServiceBilling
from app.models.service import ProvisioningSource, Service, ServiceStatus, ServiceType
from app.models.service_vm import ServiceVm
from app.models.user import User
from app.models.vm_ip_allocation import VMIPAllocation
from app.models.vm_firewall import FirewallLocationSettings
from app.services.vm_firewall.service import Actor

_seq = itertools.count(1)


@dataclass
class World:
    db: Session
    location: Location
    cluster: ProxmoxCluster
    staff: User
    users: dict[str, User] = field(default_factory=dict)

    @property
    def staff_actor(self) -> Actor:
        return Actor.staff(self.staff.id)

    def user(self, name: str) -> User:
        if name not in self.users:
            n = next(_seq)
            user = User(username=f"{name}{n}", email=f"{name}{n}@example.test", password="x")
            self.db.add(user)
            self.db.flush()
            self.users[name] = user
        return self.users[name]

    def actor(self, name: str) -> Actor:
        return Actor.client(self.user(name).id)

    def vm(
        self,
        owner: Optional[str] = "alice",
        *,
        ip: Optional[str] = "auto",
        vmid: Optional[int] = None,
        os_type: Optional[str] = None,
        product_code: Optional[str] = None,
        cluster: Optional[ProxmoxCluster] = None,
        node: str = "pve1",
    ) -> Service:
        n = next(_seq)
        if ip == "auto":
            ip = f"198.18.{n // 250}.{n % 250 + 1}"
        template_id = None
        if os_type is not None:
            template = VMTemplate(
                code=f"tpl{n}", name=f"Template {n}", os_type=os_type, proxmox_template_name=f"tpl-{n}"
            )
            self.db.add(template)
            self.db.flush()
            template_id = template.id
        allocation_id = None
        if ip is not None:
            allocation = VMIPAllocation(ip_address=ip, subnet_mask="255.255.255.0", gateway="203.0.113.1")
            self.db.add(allocation)
            self.db.flush()
            allocation_id = allocation.id
        service = Service(
            name=f"vm{n}",
            owner_user_id=self.user(owner).id if owner else None,
            service_type=ServiceType.VM,
            status=ServiceStatus.ACTIVE,
            provisioning_source=ProvisioningSource.INTERNAL,
            config={},
            product_snapshot={},
        )
        service.vm = ServiceVm(
            proxmox_cluster_id=(cluster or self.cluster).id,
            proxmox_node_name=node,
            proxmox_vmid=vmid if vmid is not None else 1000 + n,
            vm_template_id=template_id,
            vm_ip_allocation_id=allocation_id,
        )
        self.db.add(service)
        self.db.flush()
        if allocation_id is not None:
            self.db.get(VMIPAllocation, allocation_id).assigned_service_id = service.id
        if product_code is not None:
            product = self.product(product_code)
            self.db.add(ServiceBilling(service_id=service.id, product_id=product.id, monthly_price_cents=0))
        self.db.flush()
        return service

    def product(self, code: str) -> Product:
        product = self.db.query(Product).filter_by(code=code).one_or_none()
        if product is None:
            product = Product(name=code, code=code, overrides={})
            self.db.add(product)
            self.db.flush()
        return product

    def enable_location(self, **kw) -> FirewallLocationSettings:
        row = self.db.get(FirewallLocationSettings, self.location.id)
        if row is None:
            row = FirewallLocationSettings(location_id=self.location.id, nic_flag_on_provision=True,
                                           enforce_baseline_on_all=False)
            self.db.add(row)
        row.enabled = True
        for key, value in kw.items():
            setattr(row, key, value)
        self.db.flush()
        return row


def make_world(db: Session, *, location_enabled: bool = True) -> World:
    n = next(_seq)
    location = Location(name=f"London {n}")
    db.add(location)
    db.flush()
    cluster = ProxmoxCluster(
        name=f"c{n}", api_url="https://pve.example:8006", username="root@pam", password="x",
        verify_ssl=False, location_id=location.id,
    )
    staff = User(username=f"staff{n}", email=f"staff{n}@example.test", password="x", is_admin=True)
    db.add_all([cluster, staff])
    db.flush()
    world = World(db=db, location=location, cluster=cluster, staff=staff)
    if location_enabled:
        world.enable_location()
    return world
