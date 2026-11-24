# NFS storage failover: three active nodes and a single SFTP master

Status: design, 2026-11-19. Applies from storage runner 0.3.0.

This document is the contract between the storage runner (`storage_runner/`) and Rackflow
(`app/services/storage_service.py`, `app/services/storage_sftp.py`, `app/api/storage*.py`) for
software failover of NFS storage. Read `docs/nfs-storage-sftp.md` and `storage_runner/README.md`
first for how a single node serves a tenant today.

## 1. Goals

1. Every storage node in a location is active. Each tenant (one `nfs_storage` service) is served
   by exactly one node at a time; Rackflow spreads tenants by choosing each tenant's preferred
   node.
2. If a node dies, the surviving nodes take over its tenants automatically, with no human and no
   Rackflow involvement. Clients use hard NFS 4.1 mounts and resume after a pause of roughly 60
   to 90 seconds without remounting.
3. Ownership is decided in Ceph (RADOS), never by Rackflow and never by talking to peers. A
   Rackflow outage never takes storage down and never causes a failover. Peers never exchange
   heartbeats with each other.
4. A node that may still be alive is fenced with a Ceph blocklist before anyone else serves its
   tenants, so a half-dead node cannot keep writing.
5. SFTP has one master per location behind one public virtual IP address (the VIP) and one
   shared host key, chosen by a Ceph lease in the same way.
6. Planned moves, rebalancing and draining a node are controlled from Rackflow and are faster
   than a failover. Tenants go back to their preferred node only in a controlled way.
7. A location with one node keeps working as it does today. Existing tenants migrate without
   data loss and with at most one short pause.

Not goals: Proxmox HA, active/active serving of one tenant (Ganesha's `rados_cluster`), moving
data between Ceph clusters, and failover between locations.

## 2. Terms

- **Node**: a storage node VM running the storage runner. Identified by its Rackflow node ID.
- **Tenant**: the storage side of one `nfs_storage` service (`storage_tenants` row). Has a
  private VLAN, a storage address (`endpoint_ip`, for example `10.0.0.254`) and shares.
- **Preferred node**: the node Rackflow placed the tenant on (`storage_tenants.node_id`, kept as
  the column name). It serves the tenant whenever it is healthy and no move is in progress.
- **Holder** or **current node**: the node that holds the tenant's lease and serves it now.
- **Lease**: a RADOS advisory lock (`cls_lock`) with a duration, renewed by the holder.
- **Fence**: cutting a node (or one Ganesha instance) off from Ceph with an OSD blocklist entry.
- **Takeover**: acquiring a lease whose previous holder did not release it; always preceded by a
  fence.
- **Hand-off**: a planned move where the holder stops cleanly and releases the lease; no fence.
- **SFTP master**: the one node per location that holds the location's SFTP lease, owns the VIP
  and runs the SFTP gateway and back ends.

## 3. Ceph objects

All failover state lives in one replicated RADOS pool, configured on every runner as
`CEPH_HA_POOL` (default `rackflow_ha`). The pool must be replicated (not erasure coded) and small;
a pilot or production cluster needs it created once (`ceph osd pool create rackflow_ha 32
replicated`, `ceph osd pool application enable rackflow_ha rackflow`). Runners never create pools.

Namespace `rackflow-ha` holds, per location:

| Object | Content |
|---|---|
| `tenant.<tenant_id>` | Lock `owner` (the tenant lease) and xattrs `rf.owner`, `rf.released`, `rf.fenced` |
| `sftp.<location_id>` | Lock `owner` (the SFTP master lease) and the same xattrs |
| `node.<node_id>` | JSON node record, rewritten on every renewal tick |

Namespace `rf-t<tenant_id>` (one per tenant) holds Ganesha's recovery database for that tenant
(`rados_ng` objects, see section 7). The tenant's own cephx user `client.rf-t<id>` gets
`allow rw pool=<CEPH_HA_POOL> namespace=rf-t<id>` added to its OSD caps, so a tenant's Ganesha can
read and write only its own recovery state. With a scoped node key (0.3.3, section 13) the mgr
writes tenant caps and cannot add that grant, so every tenant's Ganesha uses one shared recovery
login (`CEPH_RECOVERY_NAME`) limited to the tenant namespaces of the HA pool instead.

### 3.1 The lease lock

A lease is a **shared** `cls_lock` lock named `owner` with:

- **tag** `node.<node_id>`: the owning node. `cls_lock` refuses a shared lock whose tag differs
  from the tag of the current lockers (`EBUSY`), so only one node can hold it, exactly like an
  exclusive lock.
- **cookie** `node.<node_id>.<instance>`, where `<instance>` is random per lease client process.
- **duration** `lease_seconds` (20 s), evaluated by the OSD with its own clock. Expired lockers
  are dropped by the OSD before any lock or list call, so an expired lease is free.

Why shared and not exclusive: a lock is tied to the RADOS client instance that took it. After a
runner restart (an upgrade, a crash) the new process is a new instance and cannot renew the old
instance's lock. With a shared lock the new instance takes the same lock with the same tag while
the old entry is still valid, then breaks the old instance's entry. The lease never becomes free,
so a runner restart within `lease_seconds` causes no gap, no takeover and no fence.

Renewals call `lock_shared` with the same cookie and the `MUST_RENEW` flag, so a renewal fails
(rather than silently re-acquiring) once the entry has expired or been broken.

### 3.2 Lease xattrs

- `rf.owner` = `<node_id>:<token>`, written by a node right after it acquires the lease. `token`
  is random per acquisition.
- `rf.released` = `<token>`, written by the holder after it has fully stopped serving, just before
  it unlocks (hand-off and planned stops).
- `rf.fenced` = `<node_id>:<token>:<unix time>`, written by a node that fenced the previous
  holder, for audit only.

A new holder reads these after it has acquired the lock. If `rf.owner` is empty, names itself, or
its token equals `rf.released`, the previous holder stopped cleanly (or never existed) and no
fence is needed. Otherwise the previous holder is fenced before anything is served (section 5).

### 3.3 Node record

`node.<node_id>` holds `{"node_id", "location_id", "name", "hostname", "ceph_addr",
"runner_version", "seq"}`, rewritten with an increasing `seq` on every renewal tick. `ceph_addr` is
the IP address the node uses towards Ceph (the address of the runner's own RADOS client, which
is also the source address of every tenant's Ceph traffic because tenant namespaces are
masqueraded to the host address).

Other nodes read it to decide two things without comparing clocks between machines:
- **fresh**: `seq` changed within the last `lease_seconds`, as observed on the reader's own
  monotonic clock.
- **fence target**: the address to blocklist, cross-checked against the lock entry's address.

## 4. Timings

| Name | Value | Meaning |
|---|---|---|
| `renew_seconds` | 4 | Holder renews every lease and rewrites its node record this often |
| `lease_seconds` | 20 | Duration given to the OSD on every lock and renewal |
| `stop_margin_seconds` | 6 | The holder stops serving at `last_successful_renew_sent + lease - margin` (14 s) |
| `guard_margin_seconds` | 3 | The guard process stops serving at `sent + lease - 3` (17 s) as a backstop |
| `poll_seconds` | 2 | Non-holders look at free leases this often |
| `rank_step_seconds` | 6 | Extra wait per position in a tenant's takeover order |
| `fence_seconds` | 600 | Expiry of a node (whole address) blocklist entry |
| Ganesha `Lease_Lifetime` | 30 | NFSv4 client lease |
| Ganesha `Grace_Period` | 45 | Reclaim window after a start; lifted early once every known client has reclaimed |

All are sent by Rackflow in the `ha` block of the runner config (section 11.2) so they can be
tuned without a runner release; the runner enforces `stop_margin < lease`, `renew * 3 <= lease`
and refuses nonsense. The values above are the defaults.

Local deadlines use both `CLOCK_BOOTTIME` and the wall clock: a lease counts as expired locally
if either says so. This covers a wall clock step (boot time is unaffected). It does not reliably
cover a VM paused by the hypervisor: in the production test (14.2, T4) both guest clocks stood
still during a 60 s `qm suspend`, and NTP then slewed the wall clock back over about 10 minutes,
so neither deadline had passed on resume. What stops a resumed holder is the blocklist peers
set when they took over: its first RADOS call after the resume fails (errno 108), and the runner
stops Ganesha at once (0.1 s in the test).

### 4.1 Expected pauses

Crash of the holder (power loss, kernel panic, network cut to Ceph):

| Time (s) | Event |
|---|---|
| 0 | Last successful renewal; the node then dies |
| 20 | OSD considers the lease expired |
| 20 to 22 | Rank 0 of the takeover order sees the lease free and acquires it (rank 1 waits 6 s more, and so on) |
| 22 to 25 | Fence: blocklist the old node's address, evict its MDS sessions |
| 25 to 30 | Namespace, VLAN interface with the tenant's fixed MAC, address, gratuitous ARP, Ganesha start in grace |
| 30 to 55 | Clients' next TCP retransmission reaches the new node, gets a reset, reconnects and reclaims (Linux TCP backoff after 30 s of silence is up to about 25 s) |
| up to 75 | Grace ends, at the latest 45 s after the start; earlier once every client in the recovery database has reclaimed |

So a typical pause is 45 to 75 seconds and the worst case is about 90 seconds. Detection is
deliberately not faster: shorter leases make a slow Ceph PG or a VM migration look like a dead
node.

Planned hand-off (move, drain, return): the holder stops Ganesha and removes the address (about
2 s), releases; the target sees it within 2 s and starts (about 5 s). Clients pause for about 10
to 20 seconds (one TCP backoff step plus reclaim).

Runner restart (upgrade, crash and systemd restart): no pause; Ganesha keeps running while the
runner is down, the new process re-takes its leases by tag. If the runner stays down for more than
14 s, the guard stops serving and the tenants fail over as for a crash.

## 5. Lease protocol

### 5.1 The lease client

The runner is a static Go binary without cgo. RADOS access goes through a small Python helper
(`python3-rados`, which `ceph-common` already depends on on every node) started by the runner as a
long-lived child process, embedded in the binary and spoken to with JSON lines on stdin and
stdout. One helper process is one RADOS client instance. It sets `rados_osd_op_timeout` and
`rados_mon_op_timeout` to 8 s so no call hangs for ever; the runner also times out each call and
kills and restarts a helper that does not answer (section 3.1 makes that safe).

The helper provides: `lock_shared` (with optional `must_renew`), `unlock`, `break_lock`,
`list_lockers`, `get_xattrs`, `set_xattr`, `write_full`, `read`, `remove`, `purge` (removes a
tenant's recovery namespace after destruction), its own address, and `blocklist_add` /
`blocklist_ls`. The Reef (Ceph 18) Python binding has no `list_lockers` or `break_lock`, so for
those two the helper falls back to `rados lock info` and `rados lock break`; nodes therefore also
need the `rados` command (`ceph-common`). The helper authenticates with the node's key
(`CEPH_CONF`, `CEPH_KEYRING`, `CEPH_NAME`): `client.admin` in production, a scoped key where the
node shares a cluster it does not own (section 13, `storage_runner/README.md`). MDS commands
(`client ls`, `client evict`) and `ceph osd dump` style lookups use the existing `ceph` CLI wrapper.

Everything above the helper is behind a Go interface with an in-memory fake that models the OSD
clock, expiry, tags, `MUST_RENEW`, blocklists, partitions (calls fail or hang) and slow calls, so
the protocol is unit tested without Ceph.

### 5.2 Holding

On every tick (`renew_seconds`) the holder:
1. Rewrites its node record.
2. Renews every lease it holds (`lock_shared ... MUST_RENEW`). For each success it moves the
   local deadline to `send_time + lease - stop_margin` and writes the guard's deadline file.
3. For a failed renewal: `EBUSY`/`ENOENT` (the entry is gone, so someone may have taken over) or
   `EBLOCKLISTED` means the lease is **lost**: stop serving at once (section 5.6) and never
   renew it again. Any other error or timeout leaves the deadline where it was; serving stops
   when the deadline passes.

A separate timer checks deadlines every 500 ms, so a stalled renewal never delays the stop.

### 5.3 Acquiring a free lease

A node considers acquiring a lease when the tenant is in its config, the tenant should be served
(state `active` or `suspended`), the node is eligible (not draining, Ceph healthy, not fenced),
and `list_lockers` shows no lockers. It then waits its turn:

- If a planned move names this node as the target, it may acquire at once.
- If `rf.owner` names another node whose node record is **fresh**, wait `lease_seconds` more
  (that node is alive and connected to Ceph, so it is probably about to take its lease back, for
  example after a Ceph-wide outage).
- Otherwise wait `rank * rank_step_seconds` from the moment this node first saw the lease free,
  where `rank` is this node's position in the tenant's `takeover_order` from Rackflow (the
  preferred node is rank 0). A node not in the list never acquires.

Acquisition order:
1. `lock_shared(tag=node.<me>, duration=lease)`. `EBUSY` means someone else won; done.
2. Read `rf.owner` and `rf.released`. Decide the fence (5.4). If a fence is needed and fails,
   `unlock` and retry on a later tick. Nothing is served unfenced.
3. Write `rf.owner = <me>:<new token>`.
4. Bring the tenant up (section 7). Renewals continue during bring-up.

### 5.4 Fencing

Fencing happens only after this node holds the lock, so two nodes never fence each other over
the same tenant. The previous holder `P` comes from `rf.owner`.

- **Tenant fence**, when `P`'s node record is fresh (P is alive and talking to Ceph, so its own
  deadline made it stop serving before the OSD expired the lease): evict, on every MDS rank, the
  CephFS sessions with `auth_name=rf-t<id>` and `client_metadata.hostname=rf-t<id>-nfs` (the
  hostname of that tenant's Ganesha container). An MDS eviction blocklists the evicted client
  instance on the OSDs (`mds_session_blocklist_on_evict`, default on, for
  `mon_osd_blocklist_default_expire`, default one hour).
- **Node fence**, when `P`'s node record is stale (P is dead, partitioned from Ceph, or stuck):
  1. Find `P`'s Ceph address from its node record and check it against the address in the lock
     entry, if one is still visible. Refuse (and report) if it is this node's own address, or the
     address of any monitor, manager, MDS or OSD (from `mon dump`, `mgr dump`, `fs dump`,
     `osd dump`): blocklisting a whole address would also cut off a Ceph daemon running there.
     Storage nodes must not run Ceph daemons.
  2. `ceph osd blocklist add <addr>:0/0 <fence_seconds>`. An entry with port 0 and nonce 0 matches
     every client instance from that address (`OSDMap::is_blocklisted`), so the old node's
     Ganesha instances, SFTP kernel mounts and lease helper are all cut off from the OSDs.
  3. On every MDS rank, `client ls` and `client evict id=<n>` for every session from that
     address, so their capabilities are released at once instead of after `session_autoclose`.
  4. Write `rf.fenced`.

A node fence on one tenant fences the whole node, so all of its other leases will also lapse and
be taken over with the same fence (already in place). This is intended: a node that cannot renew
one lease is treated as unhealthy as a whole, because all renewals go through the same client.

As an extra layer, every tenant's Ganesha uses the per-tenant recovery node ID `rf-t<id>`
(section 7). Ganesha's FSAL_CEPH derives its CephFS client UUID from that ID and issues a
"reclaim reset" at start, which makes the MDS evict any older session with the same UUID. So even
without the runner's fence, a new Ganesha for a tenant evicts the previous one.

The new holder's Ganesha is a new Ceph client that fetches the current OSD map, which already
contains the blocklist entry; its operations carry that map epoch, so an OSD applies the entry
before serving them. This is the standard Ceph fencing argument (as used by RBD exclusive locks).

### 5.5 Releasing (hand-off)

A holder releases a lease when Rackflow asks it to (planned move, drain, the tenant is no longer
in its config, SFTP master step-down) or when the tenant is terminated:
1. Stop serving (section 5.6), and wait until the Ganesha container has exited.
2. Write `rf.released = <token>`.
3. `unlock`. Report the release in the next status.

If the holder crashes between 1 and 3, the lease simply expires; since `rf.released` may not have
been written, the next holder fences. That is safe, only slower.

A target refuses a hand-off target role (does not acquire) while it is draining, unhealthy or
fenced; the lease then follows the normal takeover order.

### 5.6 Stopping

In this order, so clients stop reaching the node before its Ceph client goes away:
1. Bring the tenant's VLAN interface down inside its namespace (clients stop reaching it at once).
2. Stop the Ganesha container (`podman stop -t 5`).
3. Remove the deadline file.

Stopping never deletes the namespace's Ceph user, subvolumes or data.

### 5.7 Startup and the guard

On start the runner, before anything else:
1. Starts the lease helper and loads the last config it saved (`<STATE_DIR>/config.json`, mode
   0600) if Rackflow cannot be reached.
2. For each lease whose lockers carry its own tag: takes the lock with a new cookie and breaks the
   old instance's entries. These tenants carry on with no interruption.
3. Stops anything local (Ganesha containers, VLAN interfaces, the VIP) it does not now hold.

`storage-runner guard` is a second, tiny mode of the same binary, run as its own systemd unit
(`rackflow-storage-guard.service`). Every second it reads the deadline files the runner writes
under `/run/rackflow-storage/leases/` and, for any that has passed, brings the tenant's VLAN
interface down and stops its container, or removes the SFTP VIP. It covers a runner that has
crashed, hung or been stopped, so a node never answers on a storage address it no longer holds
for longer than `lease - guard_margin` seconds. The runner also notifies systemd's watchdog
(`WatchdogSec=15`) from the lease loop only, so a hung lease loop gets the runner restarted.

## 6. Split-brain cases

| Case | What happens |
|---|---|
| Holder partitioned from its peers but not from Ceph | Nothing. Peers never talk to each other; the holder keeps renewing and keeps serving. |
| Holder partitioned from Ceph (still on the customer VLAN) | Renewals time out; at 14 s the holder brings the interface down and stops Ganesha (the guard at 17 s if the runner is stuck). At 20 s the lease expires; the next node fences the old node's address (record stale) and takes over. No two nodes answer on the address at the same time. |
| Two nodes racing for one free lease | The OSD applies one `lock_shared`; the other gets `EBUSY`. Rank delays make races rare; correctness does not depend on them. |
| Renewal stalls on the holder (slow helper, slow PG) | The holder stops at its deadline. If its node record is still fresh, peers wait an extra `lease_seconds`, then take over with a tenant fence. If the stall is node-wide, the record goes stale and peers node-fence. |
| Holder VM paused for a long time, then resumed | Peers took over and node-fenced it (blocklist) and the MDS evicted its sessions (instance blocklist, one hour). On resume its guest clocks have not moved, so the local deadline check does not fire; its first renewal fails because of the blocklist and the runner stops Ganesha at once (0.1 s on production), then removes the tenant network. Its stale Ganesha cannot write to Ceph. |
| Runner process dies, Ganesha keeps running | Restarted by systemd within seconds: re-takes leases by tag, no pause. If it does not come back, the guard stops serving at 17 s and peers take over and fence. |
| Ceph outage for everyone | Every holder stops at 14 s. When Ceph returns, each previous holder (fresh record) gets first claim and takes its own tenants back without a fence; others wait. |
| Rackflow outage | Nothing changes. Runners use the saved config; failovers still work; no moves start. |
| A node is fenced | Its helper gets `EBLOCKLISTED`, it stops everything, reports `fenced`, and retries Ceph until the entry expires (`fence_seconds`). It then holds nothing and takes tenants only through the takeover order or Rackflow moves. |

## 7. Serving a tenant on any node

Every node in a location already receives every private VLAN. To serve a tenant, a node:

1. Creates the namespace `rf-t<id>` and the VLAN interface as today, with a **fixed MAC address**
   `02:52:46:xx:xx:xx`, where `xx:xx:xx` is the tenant ID as 24 bits (locally administered, `R`
   `F`). Every node computes the same MAC, so clients' ARP entries stay valid after a move.
2. Adds the storage address and sends three gratuitous ARP replies from the namespace
   (`arping -U -c 3 -I <if> <addr>`, `iputils-arping` becomes a dependency), so switches and
   bridges learn the new port at once.
3. Writes the Ganesha configuration with
   ```
   NFSv4 {
       RecoveryBackend = rados_ng;
       Minor_Versions = 1;
       Lease_Lifetime = 30;
       Grace_Period = 45;
   }
   RADOS_KV {
       ceph_conf = "/etc/ceph/ceph.conf";
       userid = "rf-t<id>";
       pool = "<CEPH_HA_POOL>";
       namespace = "rf-t<id>";
       nodeid = "rf-t<id>";
   }
   ```
   `rados_ng` keeps the list of clients allowed to reclaim in RADOS under the tenant's own node
   ID, so whichever node starts the tenant's Ganesha sees the same list, and it commits the new
   list atomically when grace ends. `rados_cluster` is not used: it coordinates grace between
   several Ganesha servers serving the same data at once, which we never do.
4. Starts the container with `--hostname rf-t<id>-nfs` (used by the tenant fence filter) after
   evicting sessions matching that hostname and the tenant's cephx user. The current eviction by
   cephx user alone is replaced: it would also evict the SFTP master's kernel mounts of that
   tenant, which use the same cephx user on another node.

Work that changes Ceph for a tenant (subvolumes, quotas, cephx users and caps, share and tenant
destruction) is done only by the lease holder, so three nodes never race on the same Ceph
objects. A terminated tenant is never served: its holder releases it, and its preferred node then
takes the lease (without serving) to destroy it after the grace period and finally removes the
lease object and the `rf-t<id>` recovery namespace. Every node removes its own local leftovers
(namespace, container, files) for tenants it does not hold.

## 8. Placement, moves, drain and return

Rackflow owns placement; runners own failover.

- **Preferred node**: chosen at link time as today (least live tenants among enabled,
  non-draining nodes in the location).
- **Takeover order** (sent per tenant): the preferred node, then the other eligible nodes in the
  location ordered by `(live tenants preferred there, node id)` with a per-tenant rotation
  (`tenant_id mod n`), so a dead node's tenants spread over the survivors instead of all landing on
  one. Draining and disabled nodes are left out.
- **Planned move** (`move` on a tenant): Rackflow sets `move_to_node_id` and a move ID. The holder
  hands off (5.5) when it sees the move and the target's node record is fresh; the target acquires
  at once. The target is put first in the takeover order while the move is pending. Rackflow
  clears the move when the target reports serving the tenant, or after 10 minutes (reported as a
  failed move). A move can optionally make the target the new preferred node (default: yes).
- **Drain**: marks the node draining. Rackflow moves its tenants away one at a time (the next only
  after the previous target reports serving), to the least loaded other nodes, without changing
  their preferred node. A draining node is not in any takeover order, is never SFTP master (it
  hands the SFTP lease off), and acquires nothing. Undrain clears the flag; tenants then return
  through the return rule below.
- **Rebalance**: recomputes preferred nodes so each node has its fair share (by tenant count),
  changing as few tenants as possible, and returns them with moves one at a time.
- **Return to preferred node** (no flapping): a Rackflow job, at most one move per location at a
  time, moves a tenant back to its preferred node only when that node has been online, Ceph
  healthy, not fenced and not draining for at least `return_after_seconds` (default 900), and the
  tenant has been on its current node for at least as long. It can be switched off per location.
  Runners never move a tenant back on their own. A Rackflow outage therefore only delays returns.

## 9. SFTP master and the virtual IP

- One lease per location, object `sftp.<location_id>`, same protocol as a tenant lease. The
  takeover order is sent by Rackflow (`sftp.master_order`): eligible nodes (enabled, SFTP allowed
  on the node, not draining) by node ID. The master's fence follows 5.4; the tenant-level variant
  evicts the old master's SFTP kernel sessions (`client_metadata.hostname=<old node hostname>`
  for every tenant's cephx user).
- The master adds the VIP to the public interface (`SFTP_VIP_INTERFACE`, default the interface of
  the default route), sends three gratuitous ARP replies (`arping -U -c 3 -I <if> <vip>`), starts
  the gateway on `VIP:port` and the per-tenant back ends. On losing or releasing the lease it
  stops the gateway first, then removes the VIP, then stops the back ends and unmounts.
- Every node in the location receives every tenant's SFTP logins (bcrypt hashes and public keys,
  as today) so any node can become master. Tenant cephx keys need no distribution: runners read
  them from Ceph (`ceph auth get`) with their node key; only the lease holder creates or
  changes them. With an admin key the runner lists them with `auth ls`; a scoped key (0.3.3)
  reads each tenant of its config one by one, because `auth ls` prints every login's secret.
- One SSH host key per location: an ed25519 key generated by Rackflow (or imported, so the key of
  the node that serves SFTP today can be kept), stored sealed with the application encryption key
  like other secrets (`app/services/alerting/secrets.py` style `enc:` values), and sent to every
  node of the location in its config. Runners write it to `<STATE_DIR>/sftp_host_ed25519_key`
  (0600) and use it instead of their own. Without a location key (older Rackflow), a runner keeps
  its own key.
- Rackflow stores per location: SFTP enabled, public host name, port (default 2222), the VIP with
  its prefix length (for example `203.0.113.10/24`), whether failback is automatic, and the host
  key. Customers' `sftp` commands use the location's host name.

### 9.1 Network requirements for the VIP

- **Rackflow IPAM**: the VIP is a public address in the storage nodes' public subnet. When it
  falls inside an IPAM subnet (`ip_subnets`), Rackflow requires the address to be free and marks
  it `reserved` (`ip_addresses.state = "reserved"`, `details.reserved_for = "storage_sftp_vip"`),
  which removes it from allocation (allocation only takes `free` addresses). Changing or clearing
  the VIP releases the old reservation. A VIP outside every IPAM subnet is accepted with a warning.
- **Switches**: no Rackflow switch configuration is needed when all nodes' public interfaces are
  in the same layer 2 network (same VLAN), which is the case for VMs on the same public bridge.
  The upstream router must accept gratuitous ARP to update an existing entry (the default on
  common platforms). Dynamic ARP inspection, IP source guard or port security on the public
  ports would drop the VIP and must be off for these ports.
- **Proxmox**: if the VM firewall is enabled on a node's NICs, its IP filter must allow the VIP
  (or be off) on the public NIC, and the MAC filter must be off on the NIC that carries customer
  VLANs, because tenant interfaces use their own fixed MACs. The same applies to any hypervisor
  bridge with MAC or ARP filtering.

### 9.2 Public interface and return routing

When the nodes' default route is on a private management network (as in Ashburn Tier), each
node gets a separate public NIC that carries no address of its own; only the SFTP master holds
the VIP on it. The runner only adds and removes the address, so the host provides the rest:

- **NIC**: one extra NIC per node on the public bridge (Ashburn Tier: `vmbr0`, untagged,
  23.140.8.0/24, gateway 23.140.8.1, guest name `enp6s21`), Proxmox firewall off, no public
  address of its own, IPv6 disabled on it (`disable_ipv6=1`, so router advertisements give it no
  public address), and `SFTP_VIP_INTERFACE` set to it.
- **Anchor address**: the NIC keeps a permanent link-local `/32` (Ashburn Tier: 169.254.73.N
  for node N, scope link). When a device's last IPv4 address is deleted, the kernel flushes
  every route through it in all tables, so without the anchor a node that releases the VIP
  loses the policy routes below and the next time it takes the VIP its replies leave by the
  management default route (found in the production build, 2026-11-21). The anchor adds no
  routes and is never chosen as a source for public traffic.
- **Policy routing**: replies from the VIP must leave through the public NIC, not the
  management default route. A routing table holds `23.140.8.0/24 dev <pub> scope link` and
  `default via 23.140.8.1 dev <pub> onlink`, and a rule sends `from 23.140.8.30` to that table.
  `onlink` and the scope-link route let both exist on standby nodes while the address is
  absent, so taking over the VIP needs no routing change. When the master adds the VIP, the
  kernel also adds the connected route to the main table, so hosts on the public subnet are
  reached directly.
- **ARP and reverse path**: `arp_ignore=1` and `arp_announce=2` on the public NIC, so a node
  never answers ARP there for its management or Ceph addresses and announces the VIP only from
  the NIC that carries it; `rp_filter=2` (loose) on the public NIC.
- **Host filter**: the VIP must expose only the SFTP gateway. A separate nftables table (not
  `inet rf-storage`, which the runner owns, and loaded without `flush ruleset`) accepts, on the
  public NIC only, established and related traffic, ICMP, and TCP to the location's SFTP port,
  and drops everything else inbound. It also drops all forwarding into or out of the public NIC,
  because the nodes have `ip_forward=1` and a neighbour on the public subnet could otherwise
  route to tenant networks through the master. Other interfaces are left alone.

## 10. Mixed versions, single-node locations and rollout

- **HA active** for a location means: the location's `ha_enabled` is on (default) **and** every
  enabled node in it reports runner version 0.3.0 or later. Rackflow computes it on every config
  request.
- When HA is not active, a 0.3.0 runner receives only its preferred tenants (as today) with
  `ha.enabled = false`: it still takes and renews leases for them (so turning HA on later is
  seamless), never acquires a tenant not in its config, and releases any lease for a tenant that
  is no longer in its config. SFTP stays per node, as today, but with the location host key if one
  is set.
- Runners older than 0.3.0 get exactly today's config. Their tenants have no leases, which is safe
  because no other node gets those tenants until HA is active, and HA is active only once every
  node runs 0.3.0.
- A location with one node: HA may be active; the takeover order has one entry; the node holds
  all leases and the SFTP master lease. If Ceph is lost it stops serving (it could not serve
  anyway) and takes everything back without a fence when Ceph returns (it is the previous owner).
- **Migration of a tenant**: the first 0.3.0 runner pass restarts the tenant's Ganesha once to
  switch from the `fs` recovery backend (local disk) to `rados_ng`, with the fixed MAC. The new
  recovery database is empty, so clients cannot reclaim; the Linux client then re-opens its files
  and re-takes its locks with ordinary requests ("no grace" recovery). Data is not touched. Open
  files and non-conflicting locks survive; a lock another client took in the few seconds between
  would be lost, as after any server restart without recovery state. The pause is about 10 s.

Rollout order:
1. Create the `rackflow_ha` pool on the Ceph cluster.
2. Deploy the app (migration `storage_failover`). Nothing changes on nodes: all report < 0.3.0.
3. On each node in turn: install `iputils-arping` and `python3-rados` (`ceph-common` provides the
   `rados` command), set `CEPH_HA_POOL` (and `SFTP_VIP_INTERFACE` if needed), install the guard
   unit and the updated runner unit from `storage_runner/systemd/`, replace the runner binary with
   0.3.0, restart. Each tenant pauses once (about 10 s).
4. Set the location's SFTP host name, VIP and host key (import the current node's key to avoid a
   host key change), and point DNS at the VIP. Until then SFTP keeps working per node.
5. HA becomes active automatically once the last node in the location runs 0.3.0. Check the
   node list: every tenant has a current node, exactly one node is SFTP master.

Rollback: set the location's `ha_enabled` off (tenants return to preferred nodes by hand-off),
then downgrade runners. A 0.2.x runner ignores the leases and serves its own tenants with the
`fs` recovery backend (another one-off reclaim-less restart).

## 11. Rackflow

### 11.1 Data model (migration `storage_failover`)

- `storage_tenants`: `node_id` stays (the preferred node). New: `current_node_id` (FK, null),
  `current_since`, `move_to_node_id` (FK, null), `move_id` (string), `move_requested_at`,
  `move_set_preferred` (bool), `last_failover_at`, `last_failover_from_node_id`.
- `storage_nodes`: new `draining` (bool), `ha_state` (JSON reported by the runner:
  `lease_ok`, `fenced`, `fenced_until`, `sftp_master`, `ha_active`), `sftp_master` (bool),
  `healthy_since`.
- New `storage_locations` (one row per location that has storage settings): `location_id`
  (unique), `ha_enabled`, `auto_failback`, `sftp_enabled`, `sftp_host`, `sftp_port`,
  `sftp_virtual_ip`, `sftp_host_key` (sealed private key), `sftp_host_key_public`,
  `sftp_host_key_fingerprint`. The migration creates a row for each location that has a node with
  SFTP enabled, copying that node's host name and port.
- New `storage_events`: `id`, `at`, `tenant_id` (null for SFTP), `location_id`, `kind`
  (`takeover`, `handoff`, `lost`, `fenced`, `move_failed`, `sftp_master`), `node_id`,
  `from_node_id`, `detail`. Kept 90 days.

### 11.2 Runner config (GET /api/runner/storage/config)

Additions (all optional for older runners, which ignore unknown keys):

```json
{
  "node": {"id": 3, "name": "stor-03", "location_id": 1},
  "ha": {
    "enabled": true,
    "lease_seconds": 20, "renew_seconds": 4, "stop_margin_seconds": 6, "guard_margin_seconds": 3,
    "poll_seconds": 2, "rank_step_seconds": 6, "fence_seconds": 600,
    "nfs_lease_lifetime": 30, "nfs_grace_period": 45,
    "draining": false,
    "nodes": [{"id": 1, "name": "stor-01", "draining": false}, {"id": 3, "name": "stor-03", "draining": false}]
  },
  "sftp": {
    "enabled": true, "port": 2222, "max_auth_failures": 10, "failure_window_seconds": 600, "ban_seconds": 3600,
    "location_id": 1,
    "virtual_ip": "203.0.113.10/24",
    "host_key": "-----BEGIN OPENSSH PRIVATE KEY-----\n...",
    "master_eligible": true,
    "master_order": [1, 3]
  },
  "tenants": [
    {
      "id": 42, "state": "active", "...": "existing fields unchanged",
      "preferred_node_id": 1,
      "takeover_order": [1, 3],
      "move": {"id": "m-7f3a", "to_node_id": 3}
    }
  ]
}
```

With HA active, `tenants` lists every tenant of the location (including terminated ones awaiting
destruction), not only the node's own. `sftp.virtual_ip` null means SFTP is served per node as
today (no master election). The config now carries a secret (the host key); it is served only to
authenticated runners of that location and never shown in the admin API.

### 11.3 Runner status (POST /api/runner/storage/status)

Additions:

```json
{
  "ha": {"lease_ok": true, "fenced": false, "fenced_until": null, "sftp_master": true, "ha_active": true},
  "tenants": [
    {"id": 42, "state": "ready", "...": "existing fields",
     "serving": true, "since": "2026-11-19T10:00:00Z", "move_id": "m-7f3a"}
  ],
  "events": [
    {"at": "2026-11-19T10:00:00Z", "kind": "takeover", "tenant_id": 42, "from_node_id": 1, "fence": "node", "detail": "..."}
  ]
}
```

A runner reports a tenant entry only for tenants it serves or is acting on (destroying); the
existing per-tenant fields (state, shares, used bytes, SFTP state) are taken only from the node
that serves the tenant (or, for SFTP state, from the SFTP master). `destroyed_tenants` and
`destroyed_shares` are accepted from any node of the tenant's location. Rackflow sets
`current_node_id` from `serving: true` reports and clears it when the current node reports the
tenant not served or goes offline. A change of current node that is not the target of a pending
move is a failover (`last_failover_at`, an event and an alert).

### 11.4 Admin API, MCP and UI

- `POST /admin/storage/services/{id}/move` `{node_id, set_preferred=true}`.
- `POST /admin/storage/nodes/{id}/drain` `{draining}`.
- `POST /admin/storage/locations/{location_id}/rebalance`.
- `GET/PATCH /admin/storage/locations/{location_id}`: `ha_enabled`, `auto_failback`,
  `sftp_enabled`, `sftp_host`, `sftp_port`, `sftp_virtual_ip`; `POST .../host-key` with
  `{generate: true}` or `{private_key}` to import. The private key is never returned.
- Node output adds `draining`, `sftp_master`, `ha_state`, `ha_active`, `serving_tenants`
  (tenants it currently serves). Tenant output (staff) adds `preferred_node`, `current_node`,
  `current_since`, `move`, `last_failover_at`.
- MCP: `storage_node` gains actions `drain`, `undrain`, `location` (view or update the location
  settings above; `location_id` plus the existing `sftp_host`, `sftp_port`, `enabled` and a new
  `sftp_virtual_ip`), `rebalance`; `storage` gains `move` (`service_id`, `node_id`). No new tools.
- Admin UI (`StorageNodes.svelte` and the storage components): per node its served tenants,
  preferred-but-elsewhere tenants, SFTP master badge, HA state and drain/undrain; per tenant its
  preferred and current node with a move action; per location the SFTP and HA settings with
  rebalance.

### 11.5 Alerts

- `storage_tenant_failover` (warning): a tenant is served by a node other than its preferred node
  because of a failover (not a planned move). Resolves when it is back on its preferred node or
  staff make the current node preferred.
- `storage_location_degraded` (critical): a location with at least two enabled storage nodes has
  fewer than two healthy nodes (online, Ceph healthy, lease client healthy, not fenced, not
  draining).
- `storage_tenant_error` also fires for an active, linked tenant that no node has served for more
  than two minutes while HA is active.
- `storage_sftp_no_master` (critical): a location whose SFTP is behind a virtual IP, with HA
  active, has had no online node reporting to be the SFTP master for two minutes
  (`no_master_after_seconds`).

## 12. Testing

Go unit tests with fakes for the RADOS lease store (OSD clock, expiry, tags, `MUST_RENEW`,
blocklists, hangs), the `ceph` CLI, `ip` and the containers, including: holder partitioned from
peers but not from Ceph; holder partitioned from Ceph; two nodes racing for one lease; renewal
stalls with a fresh and with a stale node record; runner restart re-taking leases by tag; paused
VM; fence refusal for Ceph daemon addresses and the node's own address; hand-off and move
timeouts; SFTP master election and VIP add and remove; Ceph-wide outage and recovery without
fences. Python tests for placement, takeover order, moves, drain, rebalance, return, the config
sent to nodes (HA active and not), status handling and failover detection, alerts and the
migration.

What cannot be tested without real hardware: real `cls_lock` timing and OSD clock behaviour,
blocklist propagation, MDS eviction and Ganesha reclaim reset with `rados_ng`, client pauses with
real hard mounts, gratuitous ARP through Proxmox bridges and switches, the VIP on the public
network. A dev pilot needs three storage node VMs on the Ceph test cluster; section 14 lists what
it needs.

## 13. Details settled during the build

These refine the sections above; where they differ, this section is what the code does.

Runner (0.3.0):
- Timing validation also requires `guard_margin_seconds < lease_seconds`.
- Events carry `fence`: null when no fence was needed, otherwise `"tenant"` or `"node"`.
- The SFTP master reports an entry with `serving: false` for each tenant whose SFTP back end it
  runs while that tenant's NFS is on another node. Rackflow takes SFTP state from it and ignores it
  for the current node. A node reports a tenant it has just stopped serving, or is destroying, once
  with `serving: false`.
- If the `ha` block disappears from the config, the runner releases all leases and returns to
  legacy mode. An `ha` block without `node.id` makes the runner serve nothing and report a node error.
- The SFTP tenant-level fence evicts sessions of every tenant cephx user from the old master's
  hostname (taken from its node record).
- After a Ceph-wide outage the previous holder (named by `rf.owner`, not released) re-takes at once
  without a fence; other nodes wait their rank delay plus another `lease_seconds` while that node's
  record is fresh.
- Additions: a `version` subcommand, `LEASE_DEADLINE_DIR` for the guard, and unit files in
  `storage_runner/systemd/` (the runner unit is `Type=notify`, `WatchdogSec=15`, `RestartSec=2`).
- Every start of serving (namespace and interface, gratuitous ARP, Ganesha start or reload, VIP,
  gateway listen) and every Ceph change for a tenant (subvolume create, resize and removal, cephx
  user create, caps and removal) runs under one lock with a fresh check that the lease is held,
  and with a time limit of `stop_margin_seconds - 1` (5 s by default). Losing a lease waits for
  that lock, so the stop always happens before the OSD lets the lease go; a start or Ceph call
  that takes longer is abandoned and retried on the next pass.
- Guard deadline files are written only under the lease manager's lock for a lease that is still
  held, and removed only for the generation that is being stopped.

Rackflow:
- `storage_locations.rebalancing` keeps a rebalance going one move at a time across passes; it is
  cleared, with any pending moves, when HA stops being active in the location.
- A status entry without `serving` (runner older than 0.3.0) counts as serving only from the
  tenant's preferred node.
- Once a location has a settings row it is authoritative for the customer-facing SFTP host and
  port and for the runner's SFTP `enabled` and `port`. Location `sftp_port` refuses 2049.
- VIP checks: an address already used by another location is refused; an address inside a
  subnet's CIDR that is not one of its seeded IPAM addresses is accepted with a warning and no
  reservation.
- `storage_node_error` does not flag a non-master node for not listening when the location uses a
  VIP. `storage_sftp_no_master` (section 11.5) covers a location without a master.
- Extra admin output: `GET /admin/storage/locations`, the last 30 events in the location view,
  and per node `healthy`, `healthy_since` and `preferred_elsewhere`.

Runner (0.3.3), scoped node keys (`CEPH_SCOPED_KEY=true`; caps and reasons in
`storage_runner/README.md`, "Least-privilege node key"). Unset, nothing below changes:
- Tenant users are read with one `auth get` per tenant in the config or on the node, never
  `auth ls`. Access is granted and revoked with `ceph fs subvolume authorize --allow_existing_id`
  and `deauthorize`, never `auth get-or-create` or `auth caps`: those cannot be limited to safe
  caps (Ceph checks the last of a repeated cap type and stores the first). Removal is still
  `auth rm`, limited to the tenant prefix.
- Ganesha's `RADOS_KV` uses `CEPH_RECOVERY_NAME` (section 3). Any tenant's Ganesha can then reach
  every tenant's grace records, though not their data.
- No MDS commands: MDS caps cannot be limited to one file system's MDS (`allow * fsname=` still
  allows `tell` to all of them). The tenant fence and step 3 of the node fence are skipped; the
  blocklist and Ganesha's reclaim reset remain. A tenant fence on a fresh record therefore relies on
  the old holder having stopped at its deadline, as section 5.4 already assumes.
- The node fence uses `osd blocklist range add <addr>/32` (equivalent to `<addr>:0/0`). The key may
  add only `/32` ranges of the listed storage node addresses. `osd blocklist` has no narrower
  scope: those nodes can still be blocklisted cluster-wide by the key.
- The lease helper parses `osd blocklist ls` output as several JSON arrays (addresses, then
  ranges), which Ceph 20 prints; the self-fence check matches `/32` entries too.

## 14. Dev pilot

What the unit tests cannot show needs a pilot on the Ceph test cluster, with no production
tenants:
- **Three storage node VMs** in one location (Debian 13, Ceph 20 client tools, `python3-rados`,
  `iputils-arping`, podman, runner 0.3.0 and its two units), each on a different hypervisor if
  possible so a hypervisor failure can be tried. Each needs the Ceph admin keyring, a public NIC
  and a NIC carrying the customer VLANs (trunk), on the same bridges and VLANs on every
  hypervisor, with the Proxmox firewall MAC filter off on the trunk NIC.
- **One or two client VMs** on a test private VLAN, mounting NFS 4.1 with hard mounts, running a
  steady writer (for example `fio` or a loop writing and checksumming files) and holding a POSIX
  lock, to measure pauses and check data and lock survival.
- **Ceph**: a `rackflow_ha` pool (small, replicated), and a CephFS the nodes already use.
- **Public network**: one spare address in the nodes' public subnet for the SFTP VIP, reserved in
  IPAM, and a test DNS name pointing at it.
- **A dev Rackflow** (app with migration `storage_failover`) that the three runners report to.

Tests to run: stop the runner process; kill a node VM hard; cut a node from Ceph only (firewall
the Ceph public network); cut it from peers only; pause a VM for 60 s; planned move, drain and
return; SFTP master failover with a running transfer; Rackflow stopped for an hour; the upgrade
from 0.2.2 with a live client. For each: the measured client pause, that only one node ever
served, the blocklist entries, and that locks and data survived.

### 14.1 Production pilot results (Ashburn Tier, 21 November 2026)

The pilot ran on production Ashburn Tier before any storage was sold, not on the test cluster.

**Set-up:**
- Three nodes on runner 0.3.3 with the scoped Ceph logins (section 13), each on its own
  hypervisor: `rf-storage-01` (VM 900, hv-12), `rf-storage-02` (VM 901, hv-03) and
  `rf-storage-03` (VM 902, hv-08). Each has a trunk NIC on `vmbr3000`, a Ceph NIC on `vmbr230`
  and a management NIC, plus the public NIC of section 9.2. The hypervisors' Ceph address had
  to move from `bond1.230` onto a new `vmbr230` bridge on hv-03 and hv-08, as on hv-12.
- SFTP virtual IP 23.140.8.30, outside IPAM, with no DNS name yet.
- Two client VMs on the tenant's VLAN, hard NFS 4.1 mounts (`timeo=600,retrans=2`). Each ran a
  writer doing a 64 KiB write plus fsync every 0.5 s into rotating 10 MiB files, with a sha256
  per file. Client 1 held a POSIX lock and rewrote a heartbeat under it every second; client 2
  tried a conflicting non-blocking lock every second.
- Auto-failback was off during T1 to T5, T7 and T8.
- Pause is the longest gap between two successful writes, or two lock operations.

The 0.2.2 upgrade test was done earlier on node 1 and was not repeated. The Rackflow outage was
shortened to 15 minutes.

| Test | Serving node | Writer pause (client 1 / 2) | Lock pause, holder / prober | Notes |
|---|---|---|---|---|
| Baseline, 10 min | 2 | 0.7 / 1.0 s | 1.5 / 1.0 s | |
| T1 runner stopped (guard running) | 2 → 3 | 27.0 / 27.1 s | 26.8 / 30.4 s | node 3: "acquired tenant.3 after a node fence of node 2"; blocklist `10.16.1.52/32` |
| T2 node cut from peers and Rackflow (management NIC, 5 min) | 3 → 3 | 0.5 / 0.5 s | 1.0 / 1.0 s | no failover: leases live in Ceph |
| T3 node cut from Ceph only (5 min) | 3 → 2 | 31.8 / 31.9 s | 31.5 / 45.5 s | guard stopped the tenant 19 s after the cut; node 2 took over at 24 s with a node fence; after the cut ended node 3 found itself blocklisted (errno 108) and served nothing |
| T4 VM paused 60 s (`qm suspend`) | 2 → 3 | 39.0 / 39.2 s | 39.7 / 52.7 s | node 3 took over 32 s into the pause; on resume node 2 failed its first RADOS call (blocklisted) and stopped Ganesha within 0.1 s |
| T5 VM killed (`qm stop`) | 3 → 2 | 30.1 / 30.0 s | 30.4 / 43.2 s | takeover with a node fence 22 s after the kill; on boot node 3 came up fenced, then unfenced when the entry expired |
| T6 planned move, then drain, then return | 2 → 3 → 1 → 2 | move 9.8 / 9.8 s, drain 15.2 / 14.9 s, return 10.1 / 10.1 s | move 10.0 / 22.7 s, drain 14.8 / 27.9 s, return 10.1 / 23.1 s | move took 34 s from request to serving, drain 33 s; automatic return 15 min after the drain (`storage_return_after_seconds`); no fences |
| T7 SFTP master runner stopped during a 1 GiB upload | 2 → 2 (master 1 → 2) | 1.0 / 0.5 s | 1.0 / 1.0 s | VIP silent for 15 s, then answered only by node 2; the upload resumed with `reput`; final sha256 over NFS matched the source |
| T8 Rackflow app stopped 15 min | 2 → 2 | 1.1 / 1.0 s | 1.5 / 1.0 s | about 55 failed config and status calls per runner; normal again at once after the start |

**Results across all tests:**
- No client saw an I/O error in any test, and the T7 upload's sha256 matched over NFS. After
  the run every written file was re-read: 460 of 460 per client matched their sha256, 0
  mismatched. The only writer error came about 8 hours after T6, when the share reached its
  10 GiB quota (`EDQUOT`), outside every test window.
- The lock holder never lost its lock, and the prober never got the conflicting lock during
  the tests (0 `LOST`). One `LOST` was logged at clean-up, in the same second the holder was
  stopped on purpose.
- The tenant link (`rf-v3` up) was never up on two nodes at once.
- An ARP watcher on the public subnet saw at most one MAC for the VIP at any time.
- Ceph health did not change.

**Pause times:** crash-type failovers (T1, T3, T4, T5) paused writes for 27 to 39 s. That is
quicker than section 4.1's 45 to 75 s, because the clients reconnected on their first
retransmission. The lock prober waits 13 to 15 s longer: Ganesha refuses new locks
(`NFS4ERR_GRACE`) until the grace period ends, while the holder's reclaim succeeds at once.

**Blocklist entries:**
- In T1 and T7 the snapshot showed the expected `cidr:<old node Ceph address>/32` entry, expiring
  after 10 minutes.
- For T3, T4 and T5 the snapshot was taken after expiry. Two journal lines show the entry did its
  job: the new holder's "after a node fence of node N", and the old node's "this node is fenced
  (errno 108)" when it came back.

**Found and fixed during the pilot:**
- **Runner authentication** (app, commit ac619b4): every runner reaches the app through one
  tunnel address, and the per-address failure block was checked before the key. A disabled node
  retrying therefore locked every healthy node out with HTTP 429. Valid keys now always
  authenticate, and only unknown keys count towards the block.
- **VIP return routing** (host configuration, section 9.2): removing the VIP flushed the policy
  routes. Each node's public NIC now keeps an anchor address.
- **Pause handling** (section 4): a hypervisor pause freezes both guest clocks, so the local
  deadline check does not fire on resume. The blocklist is what stops the resumed holder; the
  text is corrected.

**Open items from the pilot:** ledger to-dos for the SFTP back-end showing its container
root, and for the guard leaving SFTP back-ends running on a fenced node (harmless).
