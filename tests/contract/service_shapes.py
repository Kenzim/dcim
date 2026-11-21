"""Response shapes for the service status and proxy routes."""

from __future__ import annotations

from tests.contract.shapes import BOOL, DICT, INT, LIST, STR, optional

# GET /services/{id}/status: the fields the module reads; every key is always present.
STATUS = {
    "service_id": INT,
    "service_name": STR,
    "service_status": STR,
    "service_type": STR,
    "server_id": optional(INT),
    "server_name": optional(STR),
    "server_enabled": optional(BOOL),
    "power_state": STR,
    "status": STR,
    "proxmox_cluster_id": optional(INT),
    "proxmox_node_name": optional(STR),
    "proxmox_vmid": optional(INT),
    "vm_ip_address": optional(STR),
    "vm_ip_allocation_id": optional(INT),
    "ipmi_proxy_available": BOOL,
    "ipmi_viewer_username": optional(STR),
    "ipmi_viewer_password": optional(STR),
    "kvm_console_available": BOOL,
    "sol_console_available": BOOL,
    "windows_password_reset_available": BOOL,
    "virtual_media_available": BOOL,
    "vnc_console_available": BOOL,
    "backups_available": BOOL,
    "proxy_credentials_available": BOOL,
    "proxy_rotate_available": BOOL,
    "proxy_assignments": optional(LIST),
    "installation": optional(DICT),
    "client_permissions": DICT,
    "power_available": BOOL,
    "admin_actions": LIST,
    "client_actions": LIST,
}

PROXY_ASSIGNMENT = {
    "id": INT,
    "ip_address": STR,
    "port": INT,
    "username": STR,
    "password": STR,
    "http_url": STR,
    "socks5_url": STR,
    "endpoint": STR,
    "fast_path": optional(DICT),
}
