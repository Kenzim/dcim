# Windows guest-agent VM provisioning

Rackflow provisions Windows VMs with the **`Windows - Guest agent`** install type
(`strategy_name`: `windows_guest_agent`). After cloning a Proxmox template, the
deployment worker powers the VM on, waits for the QEMU guest agent, then applies
**network** and **password** only through the agent (PowerShell / `set-user-password`).
There is no Cloudbase-Init or Proxmox cloud-init drive on this path.

## Catalog setup

1. Create (or select) a Proxmox template prepared as below.
2. In Rackflow **VM Templates**, set:
   - **OS type:** `Windows - Guest agent`
   - **Proxmox template name:** exact name/ID used for clone
   - **Strategy options** (defaults):
     - `guest_username` = `Administrator` (change for desktop images)
     - `network_mode` = `static` (uses the service’s VM IP allocation)
3. Link the template to a product. WHMCS / billing pass `admin_password` as usual;
   no module changes are required.

## Template prerequisites

Build the golden image before converting it to a Proxmox template:

1. Install Windows Server (or desktop) with VirtIO **disk**, **NIC**, and **balloon** drivers.
2. Install and enable the **QEMU Guest Agent** service (`qemu-ga`); confirm it starts on boot.
3. In Proxmox VM Options, set **QEMU Guest Agent** = enabled (`agent=1`).
4. Create/enable the local account that matches `guest_username` (default `Administrator`).
5. **Sysprep / generalize** the image so clones get unique SIDs/hostnames. Hostname/SID
   rewrite is not performed by Rackflow after clone.
6. Shut down and convert the VM to a template.

Verify on a test clone before catalog use:

```bash
qm agent <vmid> ping
qm guest exec <vmid> -- powershell.exe -NoProfile -Command "Get-Service QEMU-GA"
```

## What provisioning does

Pipeline: clone → size (CPU/RAM/disk) → power on → wait for guest agent → configure.

Inside the guest (via agent):

| Step | Mechanism |
|---|---|
| Network (static) | PowerShell `New-NetIPAddress` + DNS on first Up non-loopback adapter |
| Network (DHCP) | PowerShell enable DHCP on that adapter |
| Password | Prefer Proxmox `agent/set-user-password`; fallback `Set-LocalUser` |

Hypervisor `disk_gb` resize still runs in `configure_sizing`. Guest partition /
filesystem expand is left to the image’s own first-boot resize script.

Runtime actions reuse the same helpers: **Change Password** (sync or deferred
`apply_guest_password` job) and **Reset Network**.

## Out of scope

- Cloudbase-Init / Proxmox `ipconfig0` for Windows
- Bare-metal PXE Windows imaging (`os_templates/windows-server-2022/`)
- Automatic domain join or hostname assignment beyond what sysprep provides
