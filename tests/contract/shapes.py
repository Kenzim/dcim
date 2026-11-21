"""Small helpers to pin response shapes: required keys and their JSON types."""

from __future__ import annotations

from typing import Any

NONE = type(None)
INT = int
STR = str
BOOL = bool
FLOAT = (int, float)
LIST = list
DICT = dict


def optional(*types):
    return tuple(types) + (NONE,)


def assert_shape(data: Any, spec: dict[str, Any], *, where: str = "response") -> None:
    """Every key in `spec` must be present in `data` and hold one of the listed types.

    A key mapped to a type (or tuple of types) is checked with isinstance. `bool`
    is rejected where `int` is wanted, because JSON booleans and numbers differ
    for the WHMCS module (`(int)` casts of `true` would hide a type change).
    """
    assert isinstance(data, dict), f"{where} must be a JSON object, got {type(data).__name__}: {data!r}"
    for key, want in spec.items():
        assert key in data, f"{where} is missing required key {key!r}; keys present: {sorted(data)}"
        types = want if isinstance(want, tuple) else (want,)
        value = data[key]
        if isinstance(value, bool) and bool not in types:
            raise AssertionError(f"{where}[{key!r}] is a boolean, expected {types}")
        assert isinstance(value, types), f"{where}[{key!r}] is {type(value).__name__} ({value!r}), expected {types}"


def assert_error(response, status: int, *, detail_type=str) -> None:
    """The error shape the module relies on: the status code and a `detail` of the given type."""
    assert response.status_code == status, f"expected {status}, got {response.status_code}: {response.text}"
    body = response.json()
    assert isinstance(body, dict) and "detail" in body, f"error body has no 'detail': {body!r}"
    assert isinstance(body["detail"], detail_type), f"'detail' is {type(body['detail']).__name__}: {body['detail']!r}"


# BillingServiceResponse: returned by register, create, adopt, link, unlink and placement.
SERVICE_RESPONSE = {
    "id": INT,
    "name": STR,
    "external_service_id": optional(STR),
    "service_type": optional(STR),
    "product_code": optional(STR),
    "os_code": optional(STR),
    "vm_template_id": optional(INT),
    "server_id": optional(INT),
    "external_user_id": optional(INT),
    "provisioning_source": optional(STR),
    "proxmox_cluster_id": optional(INT),
    "proxmox_node_name": optional(STR),
    "proxmox_vmid": optional(INT),
    "vm_ip_allocation_id": optional(INT),
    "vm_ip_address": optional(STR),
    "vm_guest_state": optional(STR),
    "status": STR,
    "description": optional(STR),
    "config": optional(DICT),
    "server_ip": optional(STR),
    "credentials": optional(DICT),
    "proxy_assignments": optional(LIST),
    "created_at": STR,
    "updated_at": STR,
}

# BillingServiceLookupItem: rows of the admin link search.
LOOKUP_ITEM = {
    "id": optional(INT),
    "name": STR,
    "external_service_id": optional(STR),
    "service_type": optional(STR),
    "status": optional(STR),
    "proxmox_cluster_id": optional(INT),
    "proxmox_node_name": optional(STR),
    "proxmox_vmid": optional(INT),
    "server_ip": optional(STR),
    "server_name": optional(STR),
    "source": STR,
    "proxmox_status": optional(STR),
}
