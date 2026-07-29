"""
Catalog of client-facing permission keys and their built-in per-service-type
defaults.

These keys gate what an owning client (via the client portal / billing API,
e.g. WHMCS) may do with their own bare-metal, VM, and HTTP-proxy services.
They are unrelated to staff (``is_admin``) access, which is unaffected.

Keys are code-defined (not free-form DB strings) so the resolver, billing
API, and admin UI all agree on what a preset's ``permissions`` map may
contain. See ``app.services.client_permission_resolver`` for how a service's
effective permissions are computed from these defaults plus product/user/
service-level presets and overrides.
"""
from typing import Dict, List

from app.models.service import ServiceType


class PermissionKey:
    """Client permission keys. Values are stable identifiers used as JSON keys."""

    SERVICE_VIEW = "service.view"
    SERVICE_PORTAL = "service.portal"

    BMS_POWER = "bms.power"
    BMS_IPMI = "bms.ipmi"
    BMS_REINSTALL = "bms.reinstall"
    BMS_RUN_SCRIPT = "bms.run_script"

    VM_POWER = "vm.power"
    VM_CONSOLE = "vm.console"
    VM_REINSTALL = "vm.reinstall"
    VM_BACKUPS = "vm.backups"
    VM_CHANGE_PASSWORD = "vm.change_password"
    VM_RESET_NETWORK = "vm.reset_network"
    VM_MANAGE_SSH_KEYS = "vm.manage_ssh_keys"

    PROXY_VIEW_CREDENTIALS = "proxy.view_credentials"
    PROXY_ROTATE_CREDENTIALS = "proxy.rotate_credentials"


# Human-readable catalog entry: label + which service type(s) the key is
# meaningful for. Used by the admin UI to group checkboxes and by callers
# that want to validate/display permission maps.
PERMISSION_CATALOG: List[Dict] = [
    {
        "key": PermissionKey.SERVICE_VIEW,
        "label": "View service status",
        "service_types": ["bare_metal", "vm", "http_proxy"],
    },
    {
        "key": PermissionKey.SERVICE_PORTAL,
        "label": "Open client portal (SSO)",
        "service_types": ["bare_metal", "vm", "http_proxy"],
    },
    {
        "key": PermissionKey.BMS_POWER,
        "label": "Power on / off / reboot",
        "service_types": ["bare_metal", "http_proxy"],
    },
    {
        "key": PermissionKey.BMS_IPMI,
        "label": "Open IPMI / BMC console",
        "service_types": ["bare_metal", "http_proxy"],
    },
    {
        "key": PermissionKey.BMS_REINSTALL,
        "label": "Reinstall OS",
        "service_types": ["bare_metal", "http_proxy"],
    },
    {
        "key": PermissionKey.BMS_RUN_SCRIPT,
        "label": "Run permitted scripts",
        "service_types": ["bare_metal", "http_proxy"],
    },
    {
        "key": PermissionKey.VM_POWER,
        "label": "Power on / off / reboot",
        "service_types": ["vm"],
    },
    {
        "key": PermissionKey.VM_CONSOLE,
        "label": "Open VNC console",
        "service_types": ["vm"],
    },
    {
        "key": PermissionKey.VM_REINSTALL,
        "label": "Rebuild / reinstall guest",
        "service_types": ["vm"],
    },
    {
        "key": PermissionKey.VM_BACKUPS,
        "label": "List / create / restore VM backups",
        "service_types": ["vm"],
    },
    {
        "key": PermissionKey.VM_CHANGE_PASSWORD,
        "label": "Change guest password",
        "service_types": ["vm"],
    },
    {
        "key": PermissionKey.VM_RESET_NETWORK,
        "label": "Reset guest network",
        "service_types": ["vm"],
    },
    {
        "key": PermissionKey.VM_MANAGE_SSH_KEYS,
        "label": "Manage SSH public keys",
        "service_types": ["vm"],
    },
    {
        "key": PermissionKey.PROXY_VIEW_CREDENTIALS,
        "label": "View proxy credentials",
        "service_types": ["http_proxy"],
    },
    {
        "key": PermissionKey.PROXY_ROTATE_CREDENTIALS,
        "label": "Rotate proxy credentials",
        "service_types": ["http_proxy"],
    },
]

ALL_PERMISSION_KEYS = {entry["key"] for entry in PERMISSION_CATALOG}

# Built-in defaults per service type — the base layer of the resolution
# hierarchy, chosen to match current (pre-permission-system) behavior so
# existing clients see no regression when no presets are assigned:
# power + IPMI + portal + view were always available, reinstall/scripts
# were billing-API-ready but never surfaced client-side. vm.console is on
# by default (VNC console access, alongside vm.power) since clients already
# expect interactive access to their own VM. http_proxy defaults to power/
# IPMI OFF (unlike bare_metal): a proxy created from the IP pool has no
# linked server, so those controls are meaningless there by default; grant
# them via a preset for the (legacy) hardware-bound proxy case.
DEFAULT_PERMISSIONS_BY_SERVICE_TYPE: Dict[ServiceType, Dict[str, bool]] = {
    ServiceType.BARE_METAL: {
        PermissionKey.SERVICE_VIEW: True,
        PermissionKey.SERVICE_PORTAL: True,
        PermissionKey.BMS_POWER: True,
        PermissionKey.BMS_IPMI: True,
        PermissionKey.BMS_REINSTALL: False,
        PermissionKey.BMS_RUN_SCRIPT: False,
    },
    ServiceType.VM: {
        PermissionKey.SERVICE_VIEW: True,
        PermissionKey.SERVICE_PORTAL: True,
        PermissionKey.VM_POWER: True,
        PermissionKey.VM_CONSOLE: True,
        PermissionKey.VM_REINSTALL: False,
        PermissionKey.VM_BACKUPS: True,
        PermissionKey.VM_CHANGE_PASSWORD: True,
        PermissionKey.VM_RESET_NETWORK: False,
        PermissionKey.VM_MANAGE_SSH_KEYS: True,
    },
    ServiceType.HTTP_PROXY: {
        PermissionKey.SERVICE_VIEW: True,
        PermissionKey.SERVICE_PORTAL: True,
        # Power/IPMI default OFF: most http_proxy services are provisioned
        # straight from the IP pool (no linked rack server) so these controls
        # don't apply. Legacy/hardware-bound proxies (created via a server
        # group) can still be granted them explicitly via a preset.
        PermissionKey.BMS_POWER: False,
        PermissionKey.BMS_IPMI: False,
        PermissionKey.PROXY_VIEW_CREDENTIALS: True,
        PermissionKey.PROXY_ROTATE_CREDENTIALS: False,
    },
}


# Seed presets created by the initial migration / seed script. ``is_system``
# rows can't be deleted but their ``permissions`` map can still be edited by
# an admin.
SYSTEM_PRESETS: List[Dict] = [
    {
        "name": "Full access",
        "description": "Everything a client can be granted: power, IPMI, reinstall, scripts, proxy credentials.",
        "permissions": {
            PermissionKey.SERVICE_VIEW: True,
            PermissionKey.SERVICE_PORTAL: True,
            PermissionKey.BMS_POWER: True,
            PermissionKey.BMS_IPMI: True,
            PermissionKey.BMS_REINSTALL: True,
            PermissionKey.BMS_RUN_SCRIPT: True,
            PermissionKey.VM_POWER: True,
            PermissionKey.VM_CONSOLE: True,
            PermissionKey.VM_REINSTALL: True,
            PermissionKey.VM_BACKUPS: True,
            PermissionKey.VM_CHANGE_PASSWORD: True,
            PermissionKey.VM_RESET_NETWORK: True,
            PermissionKey.VM_MANAGE_SSH_KEYS: True,
            PermissionKey.PROXY_VIEW_CREDENTIALS: True,
            PermissionKey.PROXY_ROTATE_CREDENTIALS: True,
        },
    },
    {
        "name": "Power + IPMI",
        "description": "Default-equivalent preset: power control, IPMI/BMC console, VM console, backups, and guest password change; no reinstall or scripts.",
        "permissions": {
            PermissionKey.SERVICE_VIEW: True,
            PermissionKey.SERVICE_PORTAL: True,
            PermissionKey.BMS_POWER: True,
            PermissionKey.BMS_IPMI: True,
            PermissionKey.BMS_REINSTALL: False,
            PermissionKey.BMS_RUN_SCRIPT: False,
            PermissionKey.VM_POWER: True,
            PermissionKey.VM_CONSOLE: True,
            PermissionKey.VM_REINSTALL: False,
            PermissionKey.VM_BACKUPS: True,
            PermissionKey.VM_CHANGE_PASSWORD: True,
            PermissionKey.VM_RESET_NETWORK: False,
            PermissionKey.VM_MANAGE_SSH_KEYS: True,
        },
    },
    {
        "name": "Read-only",
        "description": "View status only. No power, IPMI, reinstall, script, or credential access.",
        "permissions": {
            PermissionKey.SERVICE_VIEW: True,
            PermissionKey.SERVICE_PORTAL: True,
            PermissionKey.BMS_POWER: False,
            PermissionKey.BMS_IPMI: False,
            PermissionKey.BMS_REINSTALL: False,
            PermissionKey.BMS_RUN_SCRIPT: False,
            PermissionKey.VM_POWER: False,
            PermissionKey.VM_CONSOLE: False,
            PermissionKey.VM_REINSTALL: False,
            PermissionKey.VM_BACKUPS: False,
            PermissionKey.VM_CHANGE_PASSWORD: False,
            PermissionKey.VM_RESET_NETWORK: False,
            PermissionKey.VM_MANAGE_SSH_KEYS: False,
            PermissionKey.PROXY_VIEW_CREDENTIALS: False,
            PermissionKey.PROXY_ROTATE_CREDENTIALS: False,
        },
    },
    {
        "name": "Proxy credentials only",
        "description": "For HTTP proxy services: view credentials, no power/IPMI/reinstall.",
        "permissions": {
            PermissionKey.SERVICE_VIEW: True,
            PermissionKey.SERVICE_PORTAL: True,
            PermissionKey.BMS_POWER: False,
            PermissionKey.BMS_IPMI: False,
            PermissionKey.PROXY_VIEW_CREDENTIALS: True,
            PermissionKey.PROXY_ROTATE_CREDENTIALS: False,
        },
    },
]
