# TODO: bare-metal reinstall from WHMCS

Status: planned, not started (2026-10-27).

## Problem

The WHMCS **Reinstall** module command is shown on every RackFlow product, but
`rackflow_Reinstall` (`whmcs/modules/servers/rackflow/rackflow.php`) only handles
VM services. On bare metal it returns "Reinstall is only available for VM products."

RackFlow already has a bare-metal endpoint,
`POST /api/billing/services/{id}/actions/reinstall-os` (`app/api/billing.py`,
`reinstall_os_on_service`). It checks the template against the server group, builds
the script, creates a boot task and calls `schedule_launch` (so it uses the
virtual-media boot). Nothing under `whmcs/` calls it.

That endpoint is a separate copy of the install logic and never creates an
`InstallationTask`, so `INSTALLATION_TASK_ID` is not set in the script. The Windows
`install.sh` only uploads logs, marks the install complete and reboots when that ID is
set. A reinstall through this endpoint would therefore install the OS but report no
progress or logs, never be marked completed, and not reboot at the end. Create does not
have this problem because it goes through `_queue_template_install_for_service`.

## Plan

### 1. RackFlow (`app/api/billing.py`)

- Make `reinstall_os_on_service` reuse `_queue_template_install_for_service`, keeping
  its existing permission and template/server-group checks.
- Before queueing, cancel any open boot or install tasks on the server (and release a
  staged boot ISO), so a reinstall replaces a stuck one instead of being refused.
- Return the same shape as bare-metal create (`server_ip`, `config.template_parameters`,
  `credentials.os_type`, `boot_method`) so the module can write back IP and username.

### 2. WHMCS module (`whmcs/modules/servers/rackflow/rackflow.php`)

- In `rackflow_Reinstall`, for bare-metal services call `reinstall-os`:
  - template = the service's chosen OS (resolve it the same way Create does);
  - send the WHMCS service password as `admin_password`.
- After success, write `dedicatedip` and `serviceusername` back with the same helper
  Create now uses (`rackflow_sync_bare_metal_login_fields`).
- Optional later: a bare-metal template picker in the admin panel's Reinstall section
  (currently VM-only).

### 3. Related gap

- Terminating a service does not cancel its open boot/install tasks, and the
  free-server allocator skips servers with open tasks, so a terminated test service can
  leave its server unallocatable. Cancel open tasks (and eject/remove the boot ISO) on
  terminate.

## Verify

- Unit tests: reinstall creates an `InstallationTask`, the script carries
  `INSTALLATION_TASK_ID`, open tasks are cancelled, `schedule_launch` is called.
- On a free test server (not one in customer use): Reinstall from WHMCS, confirm the
  virtual CD mounts, the install task reaches completed with logs, the server reboots,
  and WHMCS shows the right dedicated IP and username.
- Do not rsync the module to CT 165 without the owner's say-so.
