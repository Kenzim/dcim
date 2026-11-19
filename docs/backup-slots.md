# Backup slots

A backup slot lets a client keep a VM backup after the VM is gone. A client who rents servers for short periods can back a VM up, keep that backup in a slot they have bought, and restore it later onto a new VM. Slots belong to the client, not to a VM, so one backup can be restored onto the VM it came from or onto any other VM of the same client (restore, clone or move).

Version 1 covers VMs only. Bare metal has no image capture today. The tables and the `resource_type` column are shaped so bare metal can be added later.

## What a slot is

- **One backup per slot, with a size cap.** A slot is a `backup_slot` service with a size in GB. It accepts one backup whose size is at most that. This is the simplest thing to price: the product is a size, and a client who wants two backups buys two slots.
- **No copy.** Keeping a backup sets Proxmox's `protected` flag on the existing backup volume. PBS prune jobs and `purge_client_backups` leave protected volumes alone, so nothing is duplicated and keeping is instant.
- **Retained backups belong to the client.** They survive the source VM's termination and do not count towards `max_client_backups`. The row records the client, slot, volume id, storage, source service and VMID, backup size, size of the backed-up disk, template and OS details, and when the backup was taken.
- **Prices stay in the catalogue and WHMCS.** RackFlow only holds the size.

## Lifecycle

| Event | What happens |
| --- | --- |
| Create | The service is active at once. It has no server and no VM, only a `backup_slots` row with the size. |
| Keep a backup | Allowed on an active slot that is empty. `replace` swaps out the held backup after the new one is protected. The backup must be a client backup of one of the client's own VMs, not still being written, and no larger than the slot. |
| Release | Unprotects, then deletes the volume, then removes the row. If the storage cannot delete it, the row stays so it can be retried. |
| Suspend | No new keeps and no restores by the client. Data is kept. Staff can still restore. Releasing is allowed, because it only frees the client's own data. |
| Unsuspend | Back to normal. |
| Change size | Refused when the new size is below the held backup. |
| Terminate | The slot is marked terminated. If it holds a backup, `destroy_after` is set to `backup_slot_destroy_grace_seconds` ahead (default seven days, the same as NFS storage). The background reconciliation loop deletes the backup and the slot rows once that has passed. Staff who bring a terminated service back to active within the grace period cancel the deletion. |

## Restoring onto a VM

Restore runs as the durable `restore_retained_backup` deployment job, so it survives restarts and shows up in the VM's job list.

Checks before anything is queued (a refusal never leaves a VM half restored):

- The target is an active VM of the same client as the backup. Another client's VM looks like it does not exist.
- The slot is active (staff may restore from a suspended slot).
- The target is in the same Proxmox cluster as the backup, and the backup storage is active on the target's node.
- The target's primary disk is at least as large as the backed-up disk.
- The backup's guest type and the target's match, and are either cloud-init Linux or macOS (guest agent). Anything else (Windows, guest-agent Linux) can only be restored onto the VM it came from: a restored Windows guest would also carry the source's computer name and machine SID, which the guest-agent set-up does not change. A mismatch (for example a Linux backup onto a macOS VM) is refused with a message naming both types.
- No other deployment job is active on the target.
- The client confirmed that the target's data is replaced (`confirm_data_loss=true`). Without it the API answers 409 with a plain explanation.

Job steps: stop the guest, capture the target's own network interfaces and name, restore the archive into the target's VMID, wait for the task, put the target's interfaces back (so the MAC addresses, bridges and VLAN tags are the target's, and any interface only the source had is removed), set CPU and memory from the target's product, re-apply the target's static address (or DHCP), DNS and credentials through cloud-init, then power on and mark the VM ready.

### macOS targets

A macOS target runs the `restore_retained_backup_macos` job instead, because its address, password and Mac identity are not set through cloud-init. Its address, DNS and password live inside the macOS disk, and its Mac identity (SMBIOS serial and UUID) lives in the VM's Proxmox config and in OpenCore on the disk.

Job steps: stop the guest; capture the target's network interfaces and name plus `smbios1`, `args` and `vmgenid`; restore the archive; wait for the task; put the target's interfaces and those three settings back (settings that already match are not rewritten, because `args` can only be written by a root account), with the public interface (`net0`) set `link_down=1`; set CPU and memory; power on; wait for the guest agent; run the existing macOS guest configuration with the target's values; connect the public interface; finalise.

The guest configuration step reuses `ConfigureMacosGuestStep`. It differs in two ways: the OpenCore PlatformInfo is rewritten from the target's own `smbios1` (serial, UUID and model; the board serial and ROM are derived from them, so they are stable), then the guest reboots (a target whose `smbios1` has no serial and UUID is given a fresh identity before the restore, written to Proxmox with the other settings and into OpenCore, so two VMs never share one Mac identity); and the password change also tries the source VM's stored passwords (only when that VM belongs to the same client), because the restored guest still has the source's password. The address, gateway and DNS come from the target's IP allocation (or DHCP, as the template says). Private network interfaces keep the target's MAC addresses and are never held down. macOS has no SSH key or hostname set-up in Rackflow, so neither is re-applied after a cross-VM restore: the restored guest keeps the source's hostname, and any SSH keys on its disk.

Failure handling. The public interface stays disconnected from the restore until the guest has its own address, so the source VM's address is never live on the public network. If the guest agent does not answer within 15 minutes of the restore, or the guest configuration fails three times, the job fails, the VM is marked errored with the reason, and the interface stays disconnected. However the job ends in failure (a step error, an exhausted retry budget, too many worker crashes), the strategy's `on_job_failed` hook records the restore as failed with the error and marks the VM errored. Staff then fix the cause and run the VM action `retry_restore_configuration`, which queues `retry_restore_macos_configuration` (power on, wait for the agent, configure, connect the interface, finalise). That action is for staff only, and is accepted once the target's identity has been put back, the restore has not finished and no other job is active on the VM; it creates the job before touching the VM's state, so a failed enqueue changes nothing.

The window between the restore task and the target's identity being put back is the one time the VM's Proxmox config is the source's (its MAC addresses and SMBIOS identity). No step before the identity step boots the VM (the restore task is started with `start` false), the job's retries resume at the failing step, and the client, billing, reseller, staff and console power routes refuse on, reboot and reset with a 409 while the restore is in that window, including after the job has failed. Restoring onto the VM again after such a failure keeps the identity the first attempt recorded, because Proxmox no longer holds the target's own.

A macOS restore always starts the VM, whatever `start` says, because the guest has to boot to be configured. Apple account sign-ins inside macOS may need redoing on the target, because they are tied to the VM's serial number; the portal, WHMCS client area and MCP descriptions say so.

Restoring onto the same VM takes the ordinary restore path and needs no confirmation, as before. Restoring a volume id that sits in a slot through the VM's own restore route goes through the same checks.

## Setting it up

### In RackFlow

1. Add a catalogue family with service type `backup_slot` (Product catalogue, type "Backup slot"). Put the default size in `defaults.size_gb`, for example 50.
2. Add one product per size. Each can override `size_gb`; leave it blank to inherit the family's. Product codes are what WHMCS refers to, for example `slot-50`.
3. Optionally set a permission preset on the product. The permission is `backup_slot.manage` (keep, release, restore). Viewing needs `service.view`. Keeping a backup also needs `vm.backups` on the VM.
4. `backup_slot_destroy_grace_seconds` (environment variable, default 604800) sets the grace period after termination.

### In WHMCS

1. Create a product with the RackFlow module. Service Type `backup_slot`, Product Code the RackFlow product (for example `slot-50`). Size comes from the product's `size_gb`, or from a configurable option called `size_gb` or "Storage Size" on the order form.
2. Create, Suspend, Unsuspend and Terminate work as for other types. Terminate does not power anything off; RackFlow keeps the backup for the grace period.
3. Change Package resizes the slot to the new product's size, and fails with RackFlow's message if the new size is below the held backup.
4. Clients see a Backup slot card on the slot service page (held backup, restore onto a chosen VM, release). On a VM's service page, a "Keep a backup in a slot" block appears when the client has an active slot.
5. Slot actions go through `modules/servers/rackflow/slot_action.php`, which checks the WHMCS session and service ownership, and RackFlow checks the rest.

## Interfaces

- **Client API:** `GET /api/client/backup-slots`, `GET /api/client/backup-slots/{id}`, `POST .../{id}/retain`, `.../release`, `.../restore`. Clients see only their own slots.
- **Staff API:** `POST /api/admin/backup-slots`, `GET`, `POST .../{id}/resize`, `.../release`.
- **Billing API (WHMCS):** `GET /api/billing/services/{id}/backup-slot`, `GET .../backup-slots` (on a VM service), `POST .../backup-slot/resize|retain|release|restore`. All take `audience=client|admin`.
- **VM backups:** list items carry `retained`, `retained_backup_id`, `slot_service_id`, `protected`; a retained backup has `deletable=false` and is hidden from any other VM that later reuses the VMID.
- **MCP, staff (no new tools):** `service_info(view="backups")` on a slot shows the retained backup and restore targets, and on a VM adds `slots`; `restore_backup(action="retain"|"release"|"resize"|"restore")`; `provision(kind="backup_slot", owner_user_id, size_gb)`.
- **MCP, client (no new tools):** `services(view="backups")` on a slot or a VM; `service_action` with `backup_retain`, `backup_release` and `backup_restore` (a retained volume id restores onto any of the client's VMs of the same type; `confirm=true` is the data-loss confirmation).

## Not covered

- Bare metal (no image capture).
- Restoring another VM's backup onto a Windows or guest-agent Linux target, or onto a VM of a different type.
- Restoring across Proxmox clusters.
- Billing for slots beyond the catalogue price: WHMCS owns invoicing.
