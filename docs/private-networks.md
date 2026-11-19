# Private networks

A client can create an isolated layer 2 network in one location and connect their virtual machines and dedicated servers to it. Version 1 uses one 802.1Q VLAN per network, taken from a pool that is set per location. The feature is free and unmetered, with limits per client.

## How it works

- **Network:** a row in `private_networks` with an owner, a location, a VLAN ID (`segment_id`) and an address range. `segmentation_type` is always `vlan` today; it exists so that another driver (for example VXLAN) can be added without changing the API or the portal.
- **Virtual machine:** Rackflow adds an extra network card to the VM on the pool's bridge with `tag=<vlan>`, and sets `ipconfigN` so cloud-init can apply the address. The tag is set on the hypervisor, so the client cannot change it.
- **Dedicated server:** the server's uplink is its PXE port plus any ports in the same LAG group. Rackflow follows the cable runs to the switch ports and adds the VLAN there as tagged. An access port becomes a trunk whose untagged VLAN is the old access VLAN, with an explicit allowed list, so PXE and reinstalls keep working. A port in a port-channel is configured on the port-channel. A port on a Cisco vPC is configured on the vPC port-channel of both peers as one operation, with rollback (see `docs/nxos-vlan-management.md`).
- **VLAN database:** creating a network creates the VLAN (named `pn-<network id>`) on every enabled switch in the location whose plugin manages VLANs (`nxos` or `qnap_qss`). Deleting the network removes it.
- **QNAP QSW switches (`qnap_qss`):** QSS has no CLI, so the plugin uses the web interface's JSON API (`https://<switch>/api/v3`) with the switch's admin login. A QSS VLAN cannot exist without ports, so it is created with the first server port and removed with the last. The port keeps its untagged VLAN, and the private network is added tagged. VLAN IDs must be 2 to 4000, the management VLAN and LAG member ports are refused, and each operation logs in and out, so it fails with "busy" while someone is logged into the switch's web page.
- **Addresses:** Rackflow hands out static addresses inside the network's range. There is no gateway, DHCP or routing.

## One-off setup per location

1. **Pick a VLAN range** that nothing else uses, for example 2000 to 3899. Rackflow accepts 2 to 3967, because NX-OS keeps 3968 to 4094 for itself. Check with `show vlan internal usage` on a switch.
2. **Allow the range on the trunks** between the switches that carry client traffic, and on the hypervisor uplinks. Keep it off switches that should never carry client traffic (IPMI, Ceph).
3. **Make the Proxmox bridge VLAN-aware** on every node (`bridge-vlan-aware yes` and a `bridge-vids` range that covers the pool).
4. **Enable NX-API** on each switch that dedicated servers plug into (`feature nxapi`, HTTPS), and create a switch account that may configure VLANs and interfaces.
5. **Switch plugin:** in Admin → Switches, change those switches from `snmpv3` to `nxos` and fill in the NX-API account. The SNMP settings stay as they are, and monitoring and bandwidth graphs continue to work. Only switches set to `nxos` are configured by Rackflow, so leave the IPMI and Ceph switches on `snmpv3`.
6. **Cluster location:** in Admin → Proxmox Inventory, set the location of each cluster. A VM can only join networks of its cluster's location.
7. **Pool:** in Admin → Private Networks, add a pool for the location with the VLAN range, the bridge name and the limits.

Dedicated servers also need what Rackflow already records: a port marked as the PXE port, its LAG group if it is bonded, and a cable run from each uplink port to a switch port.

## Limits

| Setting | Default | Where |
|---|---|---|
| Networks per client per location | 5 | Pool |
| Networks per service | 4 | Pool |
| Address range | `10.0.0.0/24`; any private IPv4 range from /16 to /29 | Chosen per network |
| Networks per location | Size of the pool's VLAN range | Pool |

An admin can create a network for a client beyond the per-client limit.

Every network keeps its last usable address (for example `.254` on a /24) for an NFS storage endpoint; it is never given to a VM or dedicated server. Client ranges may not overlap 0.0.0.0/8, 127.0.0.0/8, 169.254.0.0/16, 224.0.0.0/4, the proxy access range (`100.80.0.0/16`, see below) or the pool's admin-only **reserved ranges** (`reserved_cidrs`); the client is only told the range is not available.

## Proxy fast path

A pool can offer the **proxy fast path** (`proxy_access_enabled`; not sold, any client with an active server and proxy in the location may turn it on). A client then turns on one proxy access network per location: a normal VLAN from the pool, but always `100.80.0.0/16`, outside the client's network limit, joined automatically by every proxy runner in the location and never linked to storage. Client servers attach as usual and get addresses from `100.80.16.1` up; `100.80.0.0/20` holds the private IPs of the proxy subnets. Full design and rollout: `docs/proxy-fast-path.md`.

## NFS storage

An `nfs_storage` service is linked to exactly one of the owner's networks (in the portal at `/client/storage`, or by staff). Linking without choosing a network creates one for the client in the storage's location, so a client with no servers and no network can still use storage; such a client also sees the storage location on `/client/networks`. Storage ordered for a location (see `docs/nfs-storage-billing.md`) only links to a network there. Linking places it on the least-loaded enabled storage node in the network's location and attaches that node's server on the reserved endpoint address. A storage node in `switch` VLAN mode gets the VLAN trunked on its `vlan_port_ids` (or its PXE uplink and bond when none are set); in `preconfigured` mode no switch is touched and the attachment counts as applied at once. A network with storage linked cannot be deleted. Terminating the storage detaches the node straight away and keeps the data for `storage_destroy_grace_seconds` (default seven days) before the storage runner destroys it.

Storage is kept while the client has no servers: terminating, suspending or letting every server expire only detaches those servers, never the storage node's attachment, and never deletes the network. Shares can also be reached over SFTP from outside our network; see `docs/nfs-storage-sftp.md`.

## What the client does

The portal page is `/client/networks`. After connecting a service, the client sets the address inside the operating system:

- **Virtual machine:** configure the new network card (the portal shows its MAC address). Templates that use cloud-init apply the address on the next reboot.
- **Dedicated server:** add a VLAN interface with the network's VLAN ID on the main network card and give it the address shown.

## Cleanup and repair

- **Terminate:** terminating a service through the billing API, the reseller API or admin delete detaches it from all its networks straight away. A service terminated by the commerce lifecycle worker (non-payment) is detached by the repair loop, within about five minutes.
- **Reuse gate:** a dedicated server is not offered for a new order while any attachment row still points at it, so a recycled server cannot land in the previous client's network. If a switch is unreachable, the attachment stays in `detaching` with the error shown on the admin page.
- **Repair loop:** the reconciliation loop (`app/services/reconciliation_jobs.py`) retries failed attaches and detaches, re-adds a VM network card lost in a rebuild, puts back a VLAN removed from a port by hand, and finishes network deletes.
- **VLAN reuse:** VLAN IDs are handed out in rotation, so a released ID is not reused until the pool wraps round.
- **Force delete:** deleting a network with "skip switches" drops the record without contacting the switches. Use it only for a switch that is gone for good.

## Known limits of version 1

- A port that is already a trunk allowing every VLAN is refused; give it an explicit allowed list first.
- Switch and hypervisor changes run one at a time inside the single app process (the Docker image starts one uvicorn process). Running several app processes would need a shared lock first.
- The NX-OS driver is covered by tests against a stateful NX-API fake (including vPC pairs); it has not been run against a real switch yet. Switch-side setup, the production survey and vPC handling are in `docs/nxos-vlan-management.md`. The QNAP driver was written from the QSS web interface's own code and is also tested against a mock only.
- Private traffic on a dedicated server is included in that server's port traffic graph, because the poller counts the whole port.
- The portal does not configure the operating system on dedicated servers.
- No WHMCS module changes: WHMCS clients reach the page through portal sign-in.

## Code map

| Layer | Path |
|---|---|
| Models | `app/models/private_network.py`, `proxmox_clusters.location_id` |
| Migration | `alembic/versions/add_private_networks.py` |
| DAO | `app/dao/private_network_dao.py` |
| Service | `app/services/private_network_service.py` |
| API | `app/api/private_networks.py` (`/api/admin/private-networks`, `/api/client/private-networks`) |
| Switch drivers | `app/plugins/nxos.py`, `app/plugins/qnap_qss.py` |
| Hypervisor | `attach_private_nic` / `detach_private_nic` in `app/plugins/proxmox.py` |
| Admin UI | `frontend/src/components/PrivateNetworks.svelte` |
| Client UI | `frontend/src/components/client/ClientNetworks.svelte` |
| Tests | `tests/services/test_private_network_service.py`, `tests/api/test_private_networks_api.py`, `tests/plugins/test_nxos.py`, `tests/plugins/test_qnap_qss.py`, `tests/plugins/test_proxmox_private_nic.py` |
