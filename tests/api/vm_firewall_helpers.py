"""Helpers for the VM firewall API tests: logged-in clients, a ready location and the fake Proxmox."""

from __future__ import annotations

import itertools

from app.api import vm_firewall_common
from app.models.user import User
from app.models.vm_firewall import ServiceFirewall
from tests.fakes.proxmox_firewall import FakeProxmoxFirewall
from tests.fakes.vm_firewall_world import World, make_world

SSH = {"direction": "in", "action": "ACCEPT", "protocol": "tcp", "ports": "22"}
WEB = {"direction": "in", "action": "ACCEPT", "protocol": "tcp", "ports": "80"}
FLAGGED_NET0 = "virtio=BC:24:11:00:00:01,bridge=vmbr0,firewall=1"

_seq = itertools.count(1)


def login(client, username: str, password: str) -> dict:
    response = client.post("/api/users/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def client_user(world: World, name: str, **fields) -> User:
    """A client with a real password, registered in the world under ``name``."""
    n = next(_seq)
    user = User(username=f"fw{name}{n}", email=f"fw{name}{n}@example.test", is_admin=False, **fields)
    user.set_password("secret123")
    world.db.add(user)
    world.db.flush()
    world.users[name] = user
    return user


def ready_world(db) -> World:
    """A location enabled for the firewall whose cluster passed readiness."""
    world = make_world(db)
    world.enable_location(readiness={"clusters": {str(world.cluster.id): {"ready": True, "reasons": []}}})
    return world


def fake_proxmox(monkeypatch, world: World, services, *, net0: str = "virtio=BC:24:11:00:00:01,bridge=vmbr0"):
    """Route the API's Proxmox access to one in-memory fake holding ``services``' VMs."""
    fake = FakeProxmoxFirewall()
    for service in services:
        fake.add_vm(service.vm.proxmox_vmid, net0=net0)

    async def client_for(db, service):
        return fake.client(service.vm.proxmox_vmid)

    monkeypatch.setattr(vm_firewall_common, "vm_firewall_client", client_for)
    monkeypatch.setattr(vm_firewall_common, "cluster_reader", lambda db, cluster: fake.reader())
    return fake


def revision(db, service_id: int) -> int:
    db.expire_all()
    return db.get(ServiceFirewall, service_id).revision
