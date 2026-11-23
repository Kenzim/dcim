"""The `/api/billing/*` routes the WHMCS module calls, as data.

Derived from `whmcs/modules/servers/rackflow/` (see README.md in this folder).
Every route is listed once. `path` is the FastAPI path template under `/api`;
placeholders use the FastAPI parameter names so the existence check can compare
them directly with the application's routes.

Fields:
  key      stable id used by the tests
  method   HTTP method the module sends
  path     path template under `/api`
  callers  PHP functions or files that make the call
  query    query parameters the module sends
  body     JSON body fields the module sends (empty tuple: no body or `[]`)
  reads    response fields the module reads (empty: only the status code)
  note     anything surprising about the call
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Route:
    key: str
    method: str
    path: str
    callers: tuple[str, ...]
    query: tuple[str, ...] = ()
    body: tuple[str, ...] = ()
    reads: tuple[str, ...] = ()
    note: str = ""


_SVC = "/billing/services/{service_id}"

ROUTES: tuple[Route, ...] = (
    # Connection test, catalogue and pickers
    Route("scripts", "GET", "/billing/scripts", ("rackflow_TestConnection",),
          note="Only the status code is used: 401 means a bad key, 404 a wrong URL, 2xx is success."),
    Route("server_groups", "GET", "/billing/server-groups",
          ("rackflow_getServerGroups", "rackflow_ServerGroupLoader"),
          reads=("id", "name", "server_count", "os_templates")),
    Route("proxmox_clusters", "GET", "/billing/proxmox/clusters", ("rackflow_ProxmoxClusterLoader",),
          reads=("id", "name", "nodes[].node_name")),
    Route("products", "GET", "/billing/products", ("rackflow_fetchCatalogProducts",),
          query=("service_type",), reads=("code", "name", "service_type")),
    Route("product", "GET", "/billing/products/{product_code}",
          ("rackflow_fetchCatalogProduct", "hooks.php ProductEdit and cart hooks"),
          reads=("code", "name", "service_type", "vm_templates", "effective_specs", "checkout_os_mode")),
    Route("isos", "GET", "/billing/isos", ("rackflow_fetchBillingIsos",), reads=("filename", "name", "id")),
    # Linking and creation
    Route("server_by_ip", "GET", "/billing/server-by-ip", ("rackflow_RegisterInRackflow",),
          query=("ip",), reads=("id",), note="A 404 is mapped to a friendly message."),
    Route("register_service", "POST", "/billing/register-service", ("rackflow_RegisterInRackflow",),
          body=("server_id", "external_service_id", "external_user_id", "external_username",
                "external_email", "name"),
          reads=("id",), note="Null username and email are sent as JSON null."),
    Route("create_bare_metal", "POST", "/billing/bare-metal/services", ("rackflow_CreateAccount",),
          body=("name", "external_service_id", "external_user_id", "external_username", "external_email",
                "product_code", "service_type", "description", "service_config"),
          reads=("id", "server_ip", "config.template_parameters.username", "credentials.os_type")),
    Route("create_vm", "POST", "/billing/vm/services", ("rackflow_CreateAccount",),
          body=("name", "external_service_id", "external_user_id", "external_username", "external_email",
                "product_code", "description", "service_config", "vm_template_id", "proxmox_cluster_id",
                "proxmox_node_name", "proxmox_vmid", "auto_provision"),
          reads=("id", "vm_ip_address")),
    Route("lookup", "GET", "/billing/services/lookup", ("admin_link.php action=search",),
          query=("q", "proxmox_vmid", "server_ip", "service_id"),
          reads=("id", "name", "external_service_id", "service_type", "status", "proxmox_cluster_id",
                 "proxmox_node_name", "proxmox_vmid", "server_ip", "server_name", "source")),
    Route("adopt_vm", "POST", "/billing/services/adopt-vm", ("admin_link.php action=adopt",),
          body=("external_service_id", "external_user_id", "external_username", "external_email",
                "proxmox_cluster_id", "proxmox_node_name", "proxmox_vmid", "name", "product_code"),
          reads=("id",)),
    Route("link", "POST", _SVC + "/link", ("admin_link.php action=link",),
          body=("external_service_id", "external_user_id", "external_username", "external_email"),
          reads=("id",)),
    Route("unlink", "POST", _SVC + "/unlink", ("admin_link.php action=unlink, link and adopt",),
          note="Sent with the PHP body `[]` (an empty PHP array); a 404 is tolerated."),
    Route("vm_placement", "PUT", _SVC + "/vm/placement", ("admin_link.php action=set_vmid",),
          body=("proxmox_cluster_id", "proxmox_node_name", "proxmox_vmid", "adopt_existing")),
    # Lifecycle and status
    Route("suspend", "POST", _SVC + "/suspend", ("rackflow_SuspendAccount",), body=("reason",)),
    Route("unsuspend", "POST", _SVC + "/unsuspend", ("rackflow_UnsuspendAccount",), body=("reason",)),
    Route("terminate", "DELETE", _SVC, ("rackflow_TerminateAccount",),
          note="Any 2xx is success; the module powers the machine off through the power route first."),
    Route("power", "POST", _SVC + "/power", ("rackflow_powerAction",), body=("action",),
          note="A plugin before hook can answer 409; the module shows `detail`."),
    Route("status", "GET", _SVC + "/status",
          ("rackflow_fetchServiceStatus", "rackflow_getClientPermissions", "client area and admin card"),
          reads=("service_id", "service_name", "service_status", "service_type", "server_name",
                 "server_enabled", "power_state", "status", "proxmox_cluster_id", "proxmox_node_name",
                 "proxmox_vmid", "vm_ip_address", "ipmi_proxy_available", "ipmi_viewer_username",
                 "ipmi_viewer_password", "kvm_console_available", "sol_console_available",
                 "windows_password_reset_available", "virtual_media_available", "vnc_console_available",
                 "backups_available", "proxy_credentials_available", "proxy_rotate_available",
                 "proxy_assignments", "installation", "client_permissions", "power_available",
                 "admin_actions", "client_actions")),
    Route("debug", "GET", _SVC + "/debug", ("rackflow_buildAdminStatusCard",),
          reads=("service", "server", "vm", "deployment_jobs", "installation_tasks", "boot_task", "activity")),
    # Storage and proxies
    Route("storage", "GET", _SVC + "/storage", ("rackflow_storageClientArea",),
          reads=("status", "size_gb", "used_bytes", "shares[]", "sftp")),
    Route("storage_resize", "POST", _SVC + "/storage/resize", ("rackflow_ChangePackage",), body=("size_gb",)),
    Route("proxy_rotate", "POST", _SVC + "/proxy/rotate",
          ("rackflow_RotateProxyCredentials", "admin_link.php action=rotate_proxy"),
          reads=("proxy_assignments",), note="Sent with the PHP body `[]`."),
    Route("proxy_fast_path", "GET", _SVC + "/proxy/fast-path", ("rackflow_proxyFastPathFetch",),
          reads=("cidr", "locations", "endpoints")),
    Route("proxy_fast_path_toggle", "POST", _SVC + "/proxy/fast-path", ("rackflow_proxyFastPathToggle",),
          body=("enabled",)),
    # Strategy actions and reinstall
    Route("run_action", "POST", _SVC + "/actions/{action_name}", ("rackflow_runStrategyAction",),
          query=("audience",), body=("params",),
          note="action_name is change_password, reset_network or randomize_smbios. reset_network and "
               "randomize_smbios send `params` as `[]` (PHP empty array). KNOWN MISMATCH: a PHP empty array encodes as `[]`, which the API answers with 422."),
    Route("reinstall_os", "POST", _SVC + "/actions/reinstall-os", ("rackflow_reinstallBareMetal",),
          query=("audience",), body=("template_id", "template_parameters"),
          reads=("server_ip", "config.template_parameters.username", "credentials.os_type")),
    Route("windows_password_reset", "POST", _SVC + "/windows-password-reset",
          ("rackflow_windowsPasswordReset",), query=("audience",), body=("password",),
          reads=("password",),
          note="Sent with the PHP body `[]` when no password is supplied. KNOWN MISMATCH: a PHP empty array encodes as `[]`, which the API answers with 422."),
    # VM operations
    Route("available_ips", "GET", _SVC + "/available-ips", ("rackflow_fetchAvailableIps",)),
    Route("reassign_ip", "POST", _SVC + "/reassign-ip", ("rackflow_reassignVmIp",),
          body=("allocation_id", "reset_network"), reads=("vm_ip_address", "network_error"),
          note="network_error is present only when the guest network reset failed."),
    Route("vm_reinstall_options", "GET", _SVC + "/vm/reinstall-options", ("rackflow_fetchReinstallOptions",),
          query=("audience",),
          reads=("reinstall_templates", "vm_template_id", "accepts_ssh_key", "ssh_public_keys_text")),
    Route("vm_reinstall", "POST", _SVC + "/vm/reinstall", ("rackflow_Reinstall",),
          query=("audience",), body=("vm_template_id", "ssh_public_keys"),
          note="Sent with the PHP body `[]` when neither is supplied. KNOWN MISMATCH: a PHP empty array encodes as `[]`, which the API answers with 422."),
    # VM backups
    Route("backups", "GET", _SVC + "/backups", ("rackflow_fetchBackups",), query=("audience",),
          reads=("backups", "jobs")),
    Route("backups_create", "POST", _SVC + "/backups", ("rackflow_CreateBackup",),
          query=("audience",), body=("wait", "mode", "notes")),
    Route("backups_delete", "POST", _SVC + "/backups/delete", ("rackflow_DeleteBackup",),
          query=("audience",), body=("volid", "storage")),
    Route("backups_restore", "POST", _SVC + "/backups/restore", ("rackflow_RestoreBackup",),
          query=("audience",), body=("volid", "wait", "start", "storage")),
    # Backup slots
    Route("backup_slot", "GET", _SVC + "/backup-slot", ("rackflow_slotClientAreaVars",),
          query=("audience",), reads=("status", "retained", "retained.notes")),
    Route("backup_slots", "GET", _SVC + "/backup-slots", ("rackflow_slotClientAreaVars",),
          query=("audience",), reads=("slots[].status",)),
    Route("backup_slot_resize", "POST", _SVC + "/backup-slot/resize", ("rackflow_slotChangePackage",),
          body=("size_gb",)),
    Route("backup_slot_retain", "POST", _SVC + "/backup-slot/retain", ("slot_action.php",),
          query=("audience",), body=("source_service_id", "volid", "replace", "name")),
    Route("backup_slot_release", "POST", _SVC + "/backup-slot/release", ("slot_action.php",),
          query=("audience",), note="Sent with the PHP body `[]`."),
    Route("backup_slot_restore", "POST", _SVC + "/backup-slot/restore", ("slot_action.php",),
          query=("audience",), body=("target_service_id", "confirm_data_loss", "start")),
    # Consoles, virtual media and portal sign-in
    Route("ipmi_ticket", "POST", _SVC + "/ipmi-ticket", ("rackflow_mintIpmiTicket", "ipmi_open.php"),
          reads=("launch_url",), note="Sent with the PHP body `[]`."),
    Route("kvm_ticket", "POST", _SVC + "/kvm-ticket", ("rackflow_mintKvmTicket", "kvm_open.php"),
          reads=("launch_url",), note="Sent with the PHP body `[]`."),
    Route("sol_ticket", "POST", _SVC + "/sol-ticket", ("rackflow_mintSolTicket", "sol_open.php"),
          reads=("launch_url",), note="Sent with the PHP body `[]`."),
    Route("vnc_ticket", "POST", _SVC + "/vnc-ticket", ("rackflow_mintVncTicket", "vnc_open.php"),
          reads=("launch_url",), note="Sent with the PHP body `[]`."),
    Route("portal_sso", "POST", _SVC + "/portal-sso", ("rackflow_mintPortalSsoTicket", "portal_open.php"),
          reads=("token", "redeem_path"), note="Sent with the PHP body `[]`."),
    Route("virtual_media", "GET", _SVC + "/virtual-media", ("rackflow_fetchVirtualMedia",), reads=("inserted", "image_name", "isos")),
    Route("virtual_media_insert", "POST", _SVC + "/virtual-media/insert", ("virtual_media_action.php",),
          body=("filename", "boot_once")),
    Route("virtual_media_eject", "POST", _SVC + "/virtual-media/eject", ("virtual_media_action.php",),
          note="Sent with no body."),
    # Plugin panels and add-on status
    Route("plugin_ui", "GET", _SVC + "/plugin-ui", ("rackflow_fetchPluginPanels",), query=("audience",),
          reads=("panels",)),
    Route("plugin_action", "POST", _SVC + "/plugin-actions/{plugin_id}/{action_id}",
          ("rackflow_runPluginAction", "plugin_action.php"), query=("audience",), body=("params", "confirm")),
    Route("addons", "POST", _SVC + "/addons", ("rackflow_reportAddonStatus (addon hooks in hooks.php)",),
          body=("addon_ref", "status")),
    # VM firewall (docs/vm-firewall.md 10.3 and 10.5). The module's native firewall section and
    # firewall_action.php are not written yet (B4b); the callers below are the planned ones.
    Route("firewall", "GET", _SVC + "/firewall", ("rackflow_ClientArea (planned)",),
          reads=("available", "client_enabled", "state", "needs_interruption", "policy_in", "policy_out",
                 "rules", "groups", "baseline", "smtp_block_help", "revision", "message"),
          note="Fetched only when /status reports firewall_available."),
    Route("firewall_update", "PUT", _SVC + "/firewall", ("firewall_action.php (planned)",),
          body=("revision", "client_enabled", "policy_in", "policy_out", "confirm_interruption"),
          reads=("revision",),
          note="409 interruption_confirmation_required until confirm_interruption is sent; 409 "
               "firewall_changed with current_revision when the revision is stale."),
    Route("firewall_rules_replace", "PUT", _SVC + "/firewall/rules", ("firewall_action.php (planned)",),
          body=("revision", "rules"), reads=("rules", "revision")),
    Route("firewall_rule_create", "POST", _SVC + "/firewall/rules", ("firewall_action.php (planned)",),
          body=("revision", "rule", "position"), reads=("rules", "revision")),
    Route("firewall_rule_delete", "DELETE", _SVC + "/firewall/rules/{rule_id}", ("firewall_action.php (planned)",),
          query=("revision",), reads=("rules", "revision")),
    Route("firewall_preset", "POST", _SVC + "/firewall/presets/{preset}", ("firewall_action.php (planned)",),
          body=("revision",), reads=("rules", "revision", "policy_in")),
    Route("firewall_groups", "GET", _SVC + "/firewall/groups", ("rackflow_ClientArea (planned)",),
          reads=("id", "name", "description", "scope")),
)

BY_KEY: dict[str, Route] = {route.key: route for route in ROUTES}

# Routes under /api/billing that the module does not call (the contract is not
# pinned for them here; they are listed so a reader knows they were considered).
SERVED_BUT_NOT_CALLED: tuple[tuple[str, str], ...] = (
    ("GET", "/billing/services"),
    ("GET", "/billing/services/{service_id}"),
    ("GET", "/billing/services/{service_id}/usage"),
    ("GET", "/billing/services/{service_id}/actions"),
    ("GET", "/billing/services/{service_id}/backup-jobs"),
    ("POST", "/billing/services/{service_id}/sol/send"),
    ("POST", "/billing/services/{service_id}/actions/run-script"),
    ("GET", "/billing/os-templates"),
    ("GET", "/billing/temp-os"),
)
