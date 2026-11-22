"""Connection test, catalogue pickers, service creation, linking and lookup."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.dao.network_port_dao import NetworkPortDAO
from app.dao.product_catalog_dao import ProductDAO, ProductFamilyDAO, VMTemplateDAO
from app.dao.server_group_dao import ServerGroupDAO
from app.dao.vm_ip_allocation_dao import VMIPAllocationDAO

from tests.contract.shapes import (
    BOOL,
    DICT,
    INT,
    LIST,
    LOOKUP_ITEM,
    SERVICE_RESPONSE,
    STR,
    assert_error,
    assert_shape,
    optional,
)


def test_scripts_is_a_json_list_and_is_the_connection_test(whmcs):
    response = whmcs.call("scripts")
    assert response.status_code == 200
    assert response.json() == []


def test_server_groups_shape(whmcs, db_session):
    group = ServerGroupDAO.create(db_session, name="contract-pool")
    group.servers.append(whmcs.server())
    db_session.commit()
    response = whmcs.call("server_groups")
    assert response.status_code == 200
    row = next(r for r in response.json() if r["id"] == group.id)
    assert_shape(row, {"id": INT, "name": STR, "server_count": INT, "os_templates": LIST,
                       "enable_os_templates": BOOL, "permitted_os_templates": LIST})
    assert row["name"] == "contract-pool" and row["server_count"] == 1


def test_proxmox_clusters_shape(whmcs):
    cluster = whmcs.cluster()
    response = whmcs.call("proxmox_clusters")
    assert response.status_code == 200
    row = next(r for r in response.json() if r["id"] == cluster.id)
    assert_shape(row, {"id": INT, "name": STR, "nodes": LIST})


def _seed_vm_product(db):
    family = ProductFamilyDAO.create(
        db, name="Contract VM", description=None, code="contract-vm", service_type="vm",
        provisioning_backend="proxmox", defaults={"cpu_cores": 2}, constraints={},
    )
    product = ProductDAO.create(db, family_id=family.id, name="Contract VM 1", description=None,
                                code="contract-vm-1", overrides={})
    template = VMTemplateDAO.create(db, code="contract-tmpl", name="Contract Linux", os_type="Linux - Cloudinit",
                                    proxmox_template_name="tmpl", description=None)
    ProductDAO.set_vm_templates(db, product, [template.id])
    db.commit()
    return product, template


PRODUCT_ITEM = {
    "id": INT, "code": STR, "name": STR, "description": STR, "enabled": BOOL,
    "service_type": optional(STR), "family": optional(DICT), "effective_specs": DICT,
    "overrides": DICT, "vm_templates": LIST, "checkout_os_mode": STR,
}
VM_TEMPLATE_ITEM = {"id": INT, "code": STR, "name": STR, "os_type": optional(STR), "accepts_ssh_key": BOOL}


def test_products_list_and_filter(whmcs, db_session):
    product, template = _seed_vm_product(db_session)
    everything = whmcs.call("products")
    assert everything.status_code == 200
    filtered = whmcs.call("products", query={"service_type": "vm"})
    assert filtered.status_code == 200
    none = whmcs.call("products", query={"service_type": "bare_metal"})
    assert none.json() == []
    row = next(r for r in filtered.json() if r["code"] == product.code)
    assert_shape(row, PRODUCT_ITEM)
    assert row["service_type"] == "vm" and row["checkout_os_mode"] == "vm_template"
    assert_shape(row["vm_templates"][0], VM_TEMPLATE_ITEM)
    assert row["vm_templates"][0]["id"] == template.id


def test_product_by_code_and_unknown_code(whmcs, db_session):
    product, _ = _seed_vm_product(db_session)
    response = whmcs.call("product", product_code=product.code)
    assert response.status_code == 200
    assert_shape(response.json(), PRODUCT_ITEM)
    assert response.json()["code"] == "contract-vm-1"
    missing = whmcs.call("product", product_code="nope")
    assert_error(missing, 404)
    assert missing.json()["detail"] == "Unknown product_code 'nope'"


def test_isos_rows_carry_a_name(whmcs, monkeypatch):
    monkeypatch.setattr("app.api.billing.catalog_for_billing", lambda: [{"id": "a.iso", "name": "a.iso"}])
    response = whmcs.call("isos")
    assert response.status_code == 200
    assert_shape(response.json()[0], {"id": STR, "name": STR})


def test_server_by_ip_found_and_not_found(whmcs):
    server = whmcs.server(ip="203.0.113.20")
    found = whmcs.call("server_by_ip", query={"ip": "203.0.113.20"})
    assert found.status_code == 200
    assert_shape(found.json(), {"id": INT, "name": STR, "server_ip": STR, "description": optional(STR)})
    assert found.json()["id"] == server.id
    missing = whmcs.call("server_by_ip", query={"ip": "203.0.113.99"})
    assert_error(missing, 404)
    assert missing.json()["detail"] == "No server found with IP 203.0.113.99"


def _register_body(server, external="42"):
    return {
        "server_id": server.id,
        "external_service_id": external,
        "external_user_id": "7",
        "external_username": None,
        "external_email": None,
        "name": f"service-{external}",
    }


def test_register_service_is_201_and_idempotent(whmcs):
    server = whmcs.server()
    created = whmcs.call("register_service", body=_register_body(server))
    assert created.status_code == 201, created.text
    assert_shape(created.json(), SERVICE_RESPONSE)
    assert created.json()["service_type"] == "bare_metal" and created.json()["status"] == "active"
    assert created.json()["external_service_id"] == "42"
    again = whmcs.call("register_service", body=_register_body(server))
    assert again.status_code == 201 and again.json()["id"] == created.json()["id"]


def test_register_service_errors(whmcs):
    server = whmcs.server()
    assert_error(whmcs.call("register_service", body=_register_body(SimpleNamespace(id=99999))), 404)
    assert whmcs.call("register_service", body=_register_body(server, "1")).status_code == 201
    conflict = whmcs.call("register_service", body=_register_body(server, "2"))
    assert_error(conflict, 409)


def test_create_bare_metal_returns_service_with_ip(whmcs, db_session, monkeypatch):
    server = whmcs.server(ip="203.0.113.21")
    NetworkPortDAO.create(db_session, server_id=server.id, name="eth0", mac_address="02:00:00:00:09:01",
                          speed_mbps=1000, pxe_boot=True, pxe_ip="198.51.100.21")
    group = ServerGroupDAO.create(db_session, name="contract-bm", enable_os_templates=True,
                                  permitted_os_templates=["ubuntu-cloud-image"])
    group.servers.append(server)
    db_session.commit()
    monkeypatch.setattr(
        "app.api.billing._queue_template_install_for_service",
        lambda **kwargs: (SimpleNamespace(id=1), SimpleNamespace(id=2, boot_task_id=1)),
    )
    response = whmcs.call("create_bare_metal", body={
        "name": "service-5", "external_service_id": "5", "external_user_id": "7",
        "external_username": "u", "external_email": "u@example.com", "product_code": None,
        "service_type": "bare_metal", "description": None,
        "service_config": {"server_group_id": group.id, "template_id": "ubuntu-cloud-image",
                           "template_parameters": {"admin_password": "pw-12345678"}},
    })
    assert response.status_code == 201, response.text
    body = response.json()
    assert_shape(body, SERVICE_RESPONSE)
    assert body["server_ip"] == "203.0.113.21"
    assert body["external_service_id"] == "5" and body["service_type"] == "bare_metal"


def test_create_bare_metal_without_group_is_a_400_with_detail(whmcs):
    response = whmcs.call("create_bare_metal", body={
        "name": "service-6", "external_service_id": "6", "external_user_id": "7", "service_type": "bare_metal",
        "service_config": {},
    })
    assert_error(response, 400)


@pytest.mark.parametrize("route_key, extra", [
    ("create_bare_metal", {"service_type": "bare_metal"}),
    ("create_vm", {"product_code": "no-such-product"}),
], ids=["bare_metal", "vm"])
def test_create_accepts_an_empty_service_config_list_like_an_empty_object(whmcs, route_key, extra):
    """Older module copies send `"service_config": []` when no options are set."""
    bodies = [
        {"name": "service-10", "external_user_id": "7", **extra, "service_config": value} for value in ({}, [])
    ]
    responses = [whmcs.call(route_key, body=body) for body in bodies]
    assert responses[0].status_code == responses[1].status_code == 400
    assert responses[0].json() == responses[1].json()


def test_create_vm_returns_service(whmcs, db_session, monkeypatch):
    cluster = whmcs.cluster()
    VMIPAllocationDAO.create(
        db_session, ip_address_value="198.51.100.91", subnet_mask="255.255.255.0", gateway="198.51.100.1",
        bridge_name="vmbr0", cluster_ids=[cluster.id], enabled=True,
    )
    monkeypatch.setattr("app.services.provisioning.vm.schedule_vm_auto_provision", lambda *a, **k: None)
    response = whmcs.call("create_vm", body={
        "name": "service-8", "external_service_id": "8", "external_user_id": "7",
        "external_username": "u", "external_email": "u@example.com", "product_code": None,
        "description": None, "service_config": {}, "proxmox_cluster_id": cluster.id,
        "proxmox_node_name": "pve1", "auto_provision": False,
    })
    assert response.status_code == 201, response.text
    body = response.json()
    assert_shape(body, SERVICE_RESPONSE)
    assert body["service_type"] == "vm" and body["vm_ip_address"] == "198.51.100.91"
    assert body["proxmox_cluster_id"] == cluster.id


def test_create_vm_unknown_product_is_400_with_detail(whmcs):
    response = whmcs.call("create_vm", body={
        "name": "service-9", "external_user_id": "7", "product_code": "no-such-product", "service_config": {},
    })
    assert_error(response, 400)
    assert "no-such-product" in response.json()["detail"]


def test_lookup_rows_and_required_filter(whmcs):
    service = whmcs.vm()
    by_id = whmcs.call("lookup", query={"q": str(service.id), "service_id": service.id})
    assert by_id.status_code == 200
    assert_shape(by_id.json()[0], LOOKUP_ITEM)
    assert by_id.json()[0]["id"] == service.id and by_id.json()[0]["source"] == "rackflow"
    by_vmid = whmcs.call("lookup", query={"q": "101", "proxmox_vmid": 101})
    assert any(r["id"] == service.id for r in by_vmid.json())
    empty = whmcs.call("lookup")
    assert_error(empty, 400)


def test_lookup_by_server_ip(whmcs):
    service = whmcs.bare_metal(server=whmcs.server(ip="203.0.113.50"))
    response = whmcs.call("lookup", query={"q": "203.0.113.50", "server_ip": "203.0.113.50"})
    assert response.status_code == 200
    row = next(r for r in response.json() if r["id"] == service.id)
    assert row["server_ip"] == "203.0.113.50"


def test_link_then_unlink_round_trip(whmcs):
    service = whmcs.vm()
    link = whmcs.call("link", service.id, body={
        "external_service_id": "55", "external_user_id": "ext-contract-1",
        "external_username": None, "external_email": None,
    })
    assert link.status_code == 200, link.text
    assert_shape(link.json(), SERVICE_RESPONSE)
    assert link.json()["external_service_id"] == "55"
    unlink = whmcs.call("unlink", service.id, body=[])
    assert unlink.status_code == 200, unlink.text
    assert_shape(unlink.json(), SERVICE_RESPONSE)
    assert unlink.json()["external_service_id"] is None and unlink.json()["status"] == "active"


def test_link_conflict_is_409(whmcs):
    first = whmcs.vm()
    second = whmcs.vm()
    assert whmcs.call("link", first.id, body={"external_service_id": "77"}).status_code == 200
    assert_error(whmcs.call("link", second.id, body={"external_service_id": "77"}), 409)


def test_adopt_vm_is_201_and_returns_service(whmcs, monkeypatch):
    cluster = whmcs.cluster()

    async def _reserve(*_a, **kwargs):
        return int(kwargs["requested_vmid"])

    monkeypatch.setattr("app.api.billing._validate_adopt_vm_inputs", lambda *a, **k: None)
    monkeypatch.setattr("app.api.billing.reserve_vmid_aligned_with_proxmox", _reserve)
    response = whmcs.call("adopt_vm", body={
        "external_service_id": "61", "external_user_id": "7", "external_username": None, "external_email": None,
        "proxmox_cluster_id": cluster.id, "proxmox_node_name": "pve1", "proxmox_vmid": 4321,
        "name": "adopted", "product_code": None,
    })
    assert response.status_code == 201, response.text
    assert_shape(response.json(), SERVICE_RESPONSE)
    assert response.json()["proxmox_vmid"] == 4321 and response.json()["external_service_id"] == "61"


def test_vm_placement_update_returns_service(whmcs, monkeypatch):
    service = whmcs.vm()
    cluster_id = service.vm.proxmox_cluster_id

    async def _reserve(*_a, **kwargs):
        return int(kwargs.get("requested_vmid") or 200777)

    monkeypatch.setattr("app.api.billing.reserve_vmid_aligned_with_proxmox", _reserve)
    response = whmcs.call("vm_placement", service.id, body={
        "proxmox_cluster_id": cluster_id, "proxmox_node_name": "pve9", "proxmox_vmid": 200777,
        "adopt_existing": True,
    })
    assert response.status_code == 200, response.text
    assert_shape(response.json(), SERVICE_RESPONSE)
    assert response.json()["proxmox_vmid"] == 200777 and response.json()["proxmox_node_name"] == "pve9"
