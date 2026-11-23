"""Helpers for the VM firewall API tests: logged-in clients, a ready location and the fake Proxmox."""

from __future__ import annotations

import itertools
import json
from typing import Optional

from app.api import vm_firewall_common
from app.models.user import User
from app.models.vm_firewall import ServiceFirewall
from app.services.vm_firewall import service as svc
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


# Names and ranges a client must never see in any firewall response.
HIDDEN_TEXT = ("ops-hidden-group", "mgmt-hidden-set", "hidden-baseline", "10.77.0.0/16", "10.88.0.0/16", "10.99.0.0/16")


def seed_hidden_admin_objects(db, actor, service_id: Optional[int] = None) -> dict:
    """A global baseline using a hidden admin group, a hidden management IP set and an internal range.
    With ``service_id``, staff also attach the hidden group and a rule on the hidden IP set to that VM."""
    ipset = svc.create_ipset(db, actor, name="mgmt-hidden-set", entries=["10.99.0.0/16"], purpose="management")
    group = svc.create_group(db, actor, name="ops-hidden-group", rules=[{**SSH, "remote_cidrs": ["10.88.0.0/16"]}])
    svc.create_baseline(
        db, actor, name="hidden-baseline", scope_type="global", settings={"management_ipset_ids": [ipset.id]},
        rules=[
            {"kind": "group", "group_id": group.id},
            {"direction": "in", "action": "DROP", "remote_cidrs": ["10.77.0.0/16"], "comment": "internal"},
            {"direction": "in", "action": "ACCEPT", "remote_ipset_id": ipset.id},
            {"direction": "in", "action": "ACCEPT", "protocol": "icmp", "remote_cidrs": ["0.0.0.0/0"]},
        ],
    )
    if service_id is not None:
        row = svc.get_or_create(db, service_id)
        svc.attach_group(db, service_id, actor, group.id, revision=row.revision)
        svc.add_rule(db, service_id, actor, {**SSH, "remote_ipset_id": ipset.id}, revision=row.revision)
    db.commit()
    return {"group": group, "ipset": ipset}


def assert_nothing_hidden(payload) -> None:
    text = json.dumps(payload)
    for hidden in HIDDEN_TEXT:
        assert hidden not in text, hidden
