# Rackflow storage runner

A Go agent for storage nodes. It polls Rackflow for the desired storage state
and makes the node match it: one isolated NFS server per customer (tenant),
backed by erasure-coded CephFS. Each tenant gets:

- a network namespace `rf-t<id>` holding a VLAN sub-interface `rf-v<id>` on
  the trunk interface, with the tenant's endpoint IP. Customer networks may
  overlap each other because every tenant has its own namespace;
- a veth pair `rf-h<id>` (host) / `rf-n<id>` (namespace) with a /30 from
  `LINK_CIDR`, and routes in the namespace to the Ceph public networks only
  (no default route);
- one CephFS subvolume `t<tenant>-s<share>` per share in the runner's
  subvolume group, sized to the share quota, and a cephx user
  `client.rf-t<id>` whose caps cover only that tenant's subvolume paths and
  RADOS namespaces;
- an NFS-Ganesha container `rf-t<id>-nfs` (podman) inside the namespace:
  NFS 4.1 only, bound to the endpoint IP, one export per share at `/<share name>`,
  access limited to the share's client IPs (RW or RO), with `root_squash` to
  uid/gid 65534 (`nobody`/`nogroup`). NFS 4.2 is not offered because
  Ganesha's READ_PLUS replies through the Ceph FSAL make Linux clients fail
  reads with an I/O error; clients that mount with plain `nfs4` negotiate 4.1,
  and an explicit `vers=4.2` mount is refused.
  Each share's top folder is created with mode `0777`, so any client user,
  including squashed root, can write without setting anything up. It has no
  sticky bit: with one, Linux clients' `fs.protected_regular` refuses root
  creating files there, because those files belong to `nobody`.

Every pass it posts a status report back to Rackflow.

## Contract with Rackflow

- `GET {RACKFLOW_URL}/api/runner/storage/config` with
  `Authorization: Bearer <RUNNER_KEY>` returns the desired state (version,
  node, settings, tenants and their shares).
- `POST {RACKFLOW_URL}/api/runner/storage/status` (expects 2xx, normally 204)
  reports `version_applied`, `runner_version`, node health (`ceph_ok`,
  `free_bytes` of the EC pool, capped by what is left under the pool's
  `max_bytes` quota when it has one, `error`), per-tenant and per-share state
  (`ready`, `pending`, `error`, `suspended`) with `used_bytes`, and the lists
  `destroyed_tenants`, `destroyed_shares` and `orphans`.

From 0.4.0 the config may also carry `networks` and per-tenant `network_id` and
`dhcp`, and the status then carries `networks` and per-tenant `dhcp`; see
"Private network presences and DHCP (0.4.0)" below. A config without those keys
is handled exactly as by 0.3.3.

`RACKFLOW_URL` must be `https://` (plain `http://` only for loopback). The
types live in `internal/api/types.go`.

## Configuration (environment, on the node only)

Nothing here ever comes from Rackflow.

| Variable | Default | Meaning |
|---|---|---|
| `RACKFLOW_URL` | required | Rackflow base URL |
| `RUNNER_KEY` | required | Bearer key for the runner API |
| `POLL_INTERVAL` | `30s` | Seconds (`30`) or a Go duration (`1m`) |
| `PARENT_IFACE` | required | Interface carrying the customer VLANs tagged: a bond, a plain NIC, or a VM NIC on a VLAN-aware bridge. Nothing assumes a bond |
| `CEPH_FS_NAME` | required | CephFS file system name |
| `CEPH_EC_POOL` | required | Erasure-coded data pool (must already exist, see below) |
| `CEPH_SUBVOLUME_GROUP` | `rackflow` | Subvolume group the runner owns; created on `CEPH_EC_POOL` if missing |
| `CEPH_PUBLIC_NETWORKS` | required | Comma-separated IPv4 CIDRs of the Ceph public network(s) (mons, OSDs, MDS) |
| `CEPH_CONF` | ceph default | `ceph.conf` path for the admin CLI |
| `CEPH_KEYRING` | ceph default | Admin keyring path on the node |
| `GANESHA_IMAGE` | required | Container image with `ganesha.nfsd` and the CephFS FSAL, e.g. `quay.io/ceph/ceph:v20.2.5` (nfs-ganesha 5.9, libcephfs2 20.2.5). Must be Ceph 20.2.4+ for clusters using aes256k keys |
| `LINK_CIDR` | `169.254.128.0/17` | Carved into /30s for tenant veth links; must not overlap Ceph or customer networks |
| `STATE_DIR` | `/var/lib/rackflow-storage-runner` | Link allocations, share deleting marks, per-tenant Ganesha config and keyrings (mode 0700) |
| `PODMAN_REMOTE_URL` | empty | Podman service URL (`unix:///run/podman/podman.sock`) when the runner itself runs in a container |
| `SFTP_LISTEN_ADDR` | `0.0.0.0` | Address the public SFTP gateway listens on; the port comes from Rackflow (node SFTP settings) |
| `SFTP_IMAGE` | empty | Image for the per-tenant SFTP back ends (see SFTP below). Needed only when Rackflow turns SFTP on; otherwise the node reports an SFTP error |
| `SFTP_SERVER_PATH` | `/usr/lib/ssh/sftp-server` | Path of OpenSSH's `sftp-server` inside `SFTP_IMAGE` |
| `CEPH_HA_POOL` | `rackflow_ha` | Replicated RADOS pool for failover leases, node records and Ganesha recovery databases (see Failover). Must exist; the runner never creates pools |
| `SFTP_VIP_INTERFACE` | default-route interface | Host interface that carries the location's SFTP virtual IP while this node is SFTP master |
| `CEPH_NAME` | (`client.admin`) | cephx entity of `CEPH_KEYRING`, passed to `ceph --name` and the lease helper. Set it when the node's key is not `client.admin`, e.g. `client.rackflow-dev-runner`. It must not start with `client.<CEPH_USER_PREFIX>`, because the runner treats those users as tenants' |
| `CEPH_USER_PREFIX` | `rf-t` | Start of each tenant's cephx user name (`client.<prefix><id>`). cephx users, and the objects in `CEPH_HA_POOL`, are cluster-wide, so a second Rackflow installation on the same Ceph cluster must set its own prefix (e.g. `rfdev-t`), its own `CEPH_HA_POOL` and its own file system. The runner refuses a non-default prefix with the default HA pool |
| `CEPH_SCOPED_KEY` | `false` | `true` when `CEPH_NAME` is a least-privilege key (see [Least-privilege node key](#least-privilege-node-key)) rather than `client.admin`. The runner then never runs `auth ls`, `auth get-or-create`, `auth caps` or MDS commands: it reads each tenant user with `auth get`, grants subvolumes with `ceph fs subvolume authorize`/`deauthorize`, skips MDS session evictions and fences with `/32` blocklist ranges. Needs `CEPH_NAME` |
| `CEPH_RECOVERY_NAME` | empty | With `CEPH_SCOPED_KEY` and HA: the cephx login every tenant's Ganesha uses for its recovery database in `CEPH_HA_POOL` (the mgr cannot grant tenant users that pool). Must differ from `CEPH_NAME` and not start with the tenant prefix |
| `CEPH_RECOVERY_KEYRING` | empty | Keyring holding `CEPH_RECOVERY_NAME`'s key; the runner copies that one key into each tenant's Ganesha keyring directory |

The guard (`storage-runner guard`) reads only `PODMAN_REMOTE_URL` and, for tests,
`LEASE_DEADLINE_DIR` (default `/run/rackflow-storage/leases`).

Customer networks that overlap a Ceph public network or `LINK_CIDR` are
rejected per tenant with a clear error.

## Host prerequisites

- **Ceph:** `ceph-common` (the `ceph` CLI) and an admin keyring
  (`CEPH_KEYRING`), or a least-privilege key with `CEPH_SCOPED_KEY=true`
  (see below). With an admin key the node manages subvolumes and
  `ceph auth` directly. **Version:** clusters on Ceph 20.2.4 or newer accept only
  aes256k cephx keys, which older clients cannot use, so both `ceph-common`
  on the node (or in the runner image) and the Ganesha image (its
  `libcephfs2`) must be Ceph 20.2.4+ for such clusters. The runner image
  installs ceph-common from download.ceph.com's `debian-tentacle` repository
  and its build fails if the client is older than 20.2.4.
- **EC data pool** added to the file system, with overwrites enabled. The
  runner checks but never creates or changes pools; if the pool is missing,
  is not a data pool of `CEPH_FS_NAME`, or lacks `allow_ec_overwrites`, it
  reports `ceph_ok=false` with the reason and creates nothing in Ceph:

  ```sh
  ceph osd pool create cephfs_ec erasure <profile>
  ceph osd pool set cephfs_ec allow_ec_overwrites true
  ceph fs add_data_pool cephfs cephfs_ec
  ```

- **Trunk interface** (`PARENT_IFACE`) receiving the customer VLANs tagged.
- **Kernel:** `8021q` (VLAN), `veth`, `nf_tables`, `nft_nat`/`nft_masq`,
  `nf_conntrack` modules; `net.ipv4.ip_forward=1`. Forwarding is only used
  for remote Ceph nodes, but the runner checks it on every pass regardless
  (it does not work out whether all Ceph addresses are local) and reports a
  node error while it is off; tenants are still reconciled.
- **Tools:** `iproute2` (`ip`, with JSON output), `nftables` (`nft`),
  `podman` (Docker cannot join a named network namespace by path, so podman
  is the only runtime), and for failover `python3-rados` (the lease client;
  `python3 -c 'import rados'` must work, same Ceph release as `ceph-common`)
  and `iputils-arping` (`arping -U`, gratuitous ARP). Both are in the image.
- **Host firewall:** the runner owns the table `inet rf-storage` only. nftables
  evaluates every table, so an accept in the runner's table does not override
  a drop elsewhere: if the host has its own `input` or `forward` policy of
  `drop`, allow `iifname "rf-h*"` to the Ceph networks on the Ceph ports there
  too, or tenants will not reach Ceph.

## Least-privilege node key

Production runs as `client.admin`. A node that shares a Ceph cluster it does
not own (dev Rackflow on production Ceph) uses a scoped key instead and sets
`CEPH_SCOPED_KEY=true`. Tested on Ceph 20.2.1 (tentacle). Replace `FS`,
`EC`, `HA`, `GROUP`, `PREFIX` (the `CEPH_USER_PREFIX`) and `NODES` (a regex
alternation of the Ceph public addresses of this installation's storage
nodes, dots escaped):

```sh
ceph auth get-or-create client.<node> \
  mon 'allow service mon r, allow service osd r, allow command "mgr dump", allow command "config generate-minimal-conf", allow command df with detail=detail, allow command "fs dump", allow command "fs ls", allow command "auth get" with entity regex "^client\.PREFIX[1-9][0-9]*$", allow command "auth rm" with entity regex "^client\.PREFIX[1-9][0-9]*$", allow command "osd blocklist" with range=range blocklistop=add addr regex "^(NODES)/32$"' \
  mgr 'allow command "fs subvolumegroup ls" with vol_name=FS, allow command "fs subvolumegroup info" with vol_name=FS group_name=GROUP, allow command "fs subvolumegroup create" with vol_name=FS group_name=GROUP pool_layout=EC, allow command "fs subvolume ls" with vol_name=FS group_name=GROUP, allow command "fs subvolume create" with vol_name=FS group_name=GROUP pool_layout=EC, allow command "fs subvolume resize" with vol_name=FS group_name=GROUP, allow command "fs subvolume info" with vol_name=FS group_name=GROUP, allow command "fs subvolume rm" with vol_name=FS group_name=GROUP, allow command "fs subvolume authorize" with vol_name=FS group_name=GROUP auth_id regex "^PREFIX[1-9][0-9]*$" access_level=rw, allow command "fs subvolume deauthorize" with vol_name=FS group_name=GROUP auth_id regex "^PREFIX[1-9][0-9]*$"' \
  osd 'allow rwx pool=HA namespace=rackflow-ha, allow rw pool=HA namespace=PREFIX*'
ceph auth get-or-create client.<recovery> \
  mon 'allow service mon r, allow service osd r' \
  osd 'allow rw pool=HA namespace=PREFIX*'
```

Why it looks like this:

- **No `auth ls`.** It prints every login's secret, `client.admin`'s too. The
  runner looks up only the tenants in its config or on its disk, one
  `auth get` each, limited by the regex to its own prefix.
- **No `auth get-or-create` or `auth caps`.** MonCap can constrain the caps
  argument (`with ... caps_mon="allow r"`), but Ceph checks the last value of
  a repeated cap type and stores the first, so
  `auth get-or-create client.X mon 'allow *' mon 'allow r'` passes the check
  and creates an `allow *` login. Any key allowed either command can make
  itself admin. Instead the mgr's `fs subvolume authorize` writes the caps
  (mds `allow rw path=<subvolume>`, osd on the subvolume's pool namespace,
  mon `allow r`), and the grant pins the file system, group, user regex and
  `access_level=rw`. `--allow_existing_id` lets it adopt users made by an
  admin-key runner; the runner authorizes a path before deauthorizing it so
  the mgr knows the grant.
- **Recovery login.** The mgr grants nothing on the HA pool, so tenants'
  Ganesha opens its recovery database as `CEPH_RECOVERY_NAME`, which can
  reach only the `PREFIX*` namespaces of the HA pool. Every tenant's
  container holds this key, so one tenant could read or damage another
  tenant's NFS grace records (not its data).
- **No MDS caps.** `mds 'allow * fsname=FS'` still lets `tell mds.*` reach
  every file system's MDS, including other installations', so the key has
  none: session evictions are skipped. A fenced node's clients are cut off by
  the blocklist instead, and a reclaiming Ganesha still evicts its old
  sessions itself.
- **Fencing.** `osd blocklist add` has no pool or file system scope, and the
  address form with nonce `0` blocks a whole IP that a regex cannot pin
  safely. The grant allows only the range form with a `/32` of a listed
  node, so the key can fence this installation's storage nodes and nothing
  else (not remove entries, not `/24`, not other addresses). Add a node's
  address to the regex when you add the node.
- **Reads it keeps.** `allow service mon r` (needed for the mgr map) also
  allows `config dump`, which shows the cluster's non-secret config
  database; `allow service osd r` (needed for the OSD map) allows
  `osd pool get` on every pool. Neither reveals keys or `config-key`
  values.

## Network and firewall

The runner replaces its own table `inet rf-storage` atomically each time the
set of allowed tenant links changes (`internal/firewall/firewall.go`). Only
active tenants are in the `tenant_links` set.

- **Ceph on this host** (single-node clusters, or a node that also runs mons
  or OSDs): traffic from a tenant veth to one of the host's own addresses is
  delivered locally through the `input` hook, not forwarded, so no NAT
  applies. The `input` chain accepts it only to `CEPH_PUBLIC_NETWORKS` on the
  Ceph ports (TCP 3300 and 6789 for mons, 6800-7568 for OSD/MDS/MGR) and drops
  every other packet from `rf-h*`: SSH, the runner host's other services, the
  veth's own host address, and anything else listening on the host.
- **Remote Ceph nodes:** the `forward` chain accepts tenant traffic to the
  Ceph networks on the same ports, replies back to tenants, and drops
  everything else from or to `rf-h*` (so tenants cannot reach each other or
  anything else). `postrouting` masquerades `LINK_CIDR` sources going to the
  Ceph networks so remote nodes need no route back.
- The namespace has routes to the Ceph networks only, so Ganesha's replies to
  customers leave through the VLAN interface and nothing else is reachable.

## Lifecycle

- **active:** network, subvolumes, cephx user, Ganesha. Ganesha's export
  section is reloaded with `SIGHUP` when only exports change; the container is
  recreated when anything else (endpoint, image, keyring, Ceph config) changes
  or when it is not running.
- **suspended:** Ganesha removed and the veth taken out of the firewall set;
  network, subvolumes and the cephx user are kept. Back to `active` restarts
  Ganesha.
- **terminated:** Ganesha, namespace, VLAN interface, veth, link allocation,
  firewall entry and the tenant's files (config, keyring) are removed at once.
  Subvolumes and the cephx user are destroyed only when `destroy_after` is set
  and in the past by the runner's clock **and** at least
  `destroy_grace_seconds` (default 7 days) have passed since `terminated_at`.
  The tenant is then reported in `destroyed_tenants` on every later pass while
  the config still lists it.
- **share `deleting`:** the export is removed at once and the cephx caps are
  narrowed. The subvolume is destroyed when `destroy_after` is past **and**
  `destroy_grace_seconds` have passed since the runner first saw the share
  deleting (recorded in `STATE_DIR`), then reported in `destroyed_shares`.
- **quota change:** `ceph fs subvolume resize`.
- **export ids:** each share gets a Ganesha `Export_Id` from a per-tenant
  counter in `STATE_DIR` (Rackflow share ids can exceed Ganesha's 16-bit
  limit). An id never changes for a share and is never reused within a
  tenant, because NFS file handles carry it.
- **limits** (`bandwidth_mbps`, `iops`) are parsed but not enforced; the hook
  is `applyLimits` in `internal/reconcile/reconcile.go`.

## SFTP

Design and trade-offs: `docs/nfs-storage-sftp.md`. In short:

- **Gateway (in the runner):** listens in the host namespace on `SFTP_LISTEN_ADDR` and the port
  Rackflow sends (default 2222). Open that TCP port to the node from the internet; the
  `inet rf-storage` table only filters tenant veths, so it does not block it. The ed25519 host key
  is created once at `STATE_DIR/sftp_host_ed25519_key` (mode 0600) and its fingerprint is reported
  to Rackflow. Logins are `<name>.<service id>`, checked against bcrypt hashes and public keys from
  Rackflow, after the user's source allow list. Only the `sftp` subsystem is offered. A source
  address is refused for `ban_seconds` after `max_auth_failures` failed connections within
  `failure_window_seconds`. Every login result and session is logged, never a password.
- **Back end per tenant:** only for active tenants with at least one SFTP share and one login.
  Each SFTP share is mounted with the kernel CephFS client as the tenant's own cephx user
  `client.rf-t<id>` at `/run/rackflow-storage/sftp/t<id>/<share>` (read-only for `ro` shares);
  the key is in `STATE_DIR/tenants/<id>/sftp/secret` (0600), never on a command line. Container
  `rf-sftp-t<id>` runs `sleep infinity` with `--network none --read-only --cap-drop all
  --security-opt no-new-privileges --user 65534:65534`, and only the tenant's shares bound at
  `/<share>`. Each session runs `podman exec -i rf-sftp-t<id> sftp-server`. The container is
  recreated when its shares or modes change (that tenant's open sessions drop).
- **Lifecycle:** suspend closes the tenant's sessions, removes the container and unmounts (data
  kept); terminate does the same and removes the tenant's SFTP state; a share turned off or being
  deleted is unmounted before its subvolume can be destroyed. SFTP errors only set the tenant's
  `sftp` status; NFS is never touched by the SFTP phase.
- **Safety:** `rf-sftp-t*` containers and mounts of tenants not in the config are reported as
  orphans and left alone; mount directories are only removed with a non-recursive remove.

Host prerequisites for SFTP: the kernel `ceph` module and `mount.ceph` (package `ceph-common`),
and the back end image. On clusters that use aes256k cephx keys (Ceph 20.2.4 and later),
`mount.ceph` must also be 20.2.4 or newer: older versions cannot parse the keys and the mount
fails. Debian 13's own `ceph-common` is 18.x; Proxmox's `ceph-tentacle` repository (suite
`trixie`, component `no-subscription`) has 20.2.x. Pin only the Ceph packages to it. Example back end image:

```Dockerfile
FROM alpine:3.20
RUN apk add --no-cache openssh-sftp-server
```

built as `podman build -t localhost/rf-sftp:1 .` and set as `SFTP_IMAGE=localhost/rf-sftp:1`.

Assumptions not yet checked on a real cluster: the kernel mount uses the tenant directory's
`ceph.conf` (monitor addresses) and `mds_namespace=<CEPH_FS_NAME>`; the tenant's cephx caps (which
Ganesha already uses) are enough for a kernel mount of its subvolume paths.

## Failover (0.3.0)

Design: `docs/nfs-storage-failover.md`. Failover is active only when the config from
Rackflow has an `ha` block; without one (an older Rackflow) the runner behaves exactly as
0.2.x: no leases, it serves the tenants in its own config.

- **Leases:** every tenant has a RADOS cls_lock lease `tenant.<id>` (and each location with an
  SFTP virtual IP one `sftp.<location_id>`) in pool `CEPH_HA_POOL`, namespace `rackflow-ha`.
  A node serves a tenant, and changes its Ceph state (subvolumes, quotas, caps, destruction),
  only while it holds the lease. With an `ha` block this holds even when `ha.enabled` is false;
  then the runner only takes leases for tenants in its own config and releases leases of tenants
  that leave it. Leases are renewed every `renew_seconds` (4); a node that cannot renew stops
  serving `lease_seconds - stop_margin_seconds` (14 s) after the last successful renewal send,
  measured on both `CLOCK_BOOTTIME` and the wall clock, and checked every 500 ms independently
  of renewals. A Rackflow outage never stops serving or causes a failover: the last config is
  kept in `STATE_DIR/config.json` (0600) and used while Rackflow is unreachable.
- **Lease client:** a small Python helper embedded in the binary (`internal/lease/rados_helper.py`)
  run as one long-lived `python3` child; it uses `CEPH_CONF` and `CEPH_KEYRING` (the admin key).
  It talks JSON lines, has 8 s RADOS op timeouts, and is killed and restarted when it does not
  answer within 10 s. Every node writes its record `node.<node_id>` (with an increasing `seq`)
  on each renewal; peers judge a node alive by that record changing.
- **Takeover:** a free lease is taken by the node at rank `r` of the tenant's `takeover_order`
  after `r * rank_step_seconds` (plus another `lease_seconds` if the previous owner's record is
  still fresh). Before serving, the new holder fences the previous one: a fresh record means a
  tenant fence (MDS eviction of `client.rf-t<id>` sessions from host `rf-t<id>-nfs`); a stale
  one a node fence (`ceph osd blocklist add <ip>:0/0 fence_seconds`, then eviction of every MDS
  session from that address). A node fence is refused for the node's own address and any
  mon, mgr, MDS or OSD address; a failed or refused fence means the lease is unlocked again and
  nothing is served. A blocklisted node stops everything, reports `fenced` and `fenced_until`,
  and takes nothing until the entry expires.
- **Planned moves and draining:** the holder hands off (stop, `rf.released`, unlock) when the
  config asks for a move to a node whose record is fresh; the target takes over at once with
  no fence. A draining node hands off every lease, including the SFTP master lease.
- **Tenant changes:** the VLAN interface gets the fixed MAC `02:52:46:xx:xx:xx` (tenant id as
  24 bits) and a gratuitous ARP (`arping -U -c 3`) from the namespace after each takeover.
  Ganesha runs with `--hostname rf-t<id>-nfs`, `RecoveryBackend = rados_ng` and a `RADOS_KV`
  block (pool `CEPH_HA_POOL`, namespace and nodeid `rf-t<id>`, user `rf-t<id>`), with
  `Lease_Lifetime`/`Grace_Period` from the config (30/45). The tenant's cephx user gains
  `allow rw pool=<CEPH_HA_POOL> namespace=rf-t<id>`. Losing a lease sets the VLAN interface
  down and stops the container (`podman stop -t 5`). Every node removes containers and
  networks of tenants it does not hold. A terminated tenant is destroyed by its preferred node
  after taking the lease (without serving), which then removes the lease object and the
  `rf-t<id>` recovery namespace.
- **SFTP:** with `sftp.virtual_ip` set, one node per location is SFTP master (lease
  `sftp.<location_id>`, order `sftp.master_order`, only `master_eligible` nodes): it adds the
  virtual IP to `SFTP_VIP_INTERFACE`, sends gratuitous ARP, and runs the gateway on VIP:port
  and the back ends of every tenant. On loss or release it stops the gateway, removes the
  virtual IP, stops the back ends and unmounts. Other nodes run no SFTP. With
  `virtual_ip` null SFTP stays per node, for the tenants the node holds. When the config
  carries `sftp.host_key`, it is written to `STATE_DIR/sftp_host_ed25519_key` (0600) and
  used instead of the node's own key.
- **Status:** an `ha` block (`lease_ok`, `fenced`, `fenced_until`, `sftp_master`,
  `ha_active`), per tenant `serving`, `since` and `move_id`, and `events` (`takeover`,
  `handoff`, `lost`, `fenced`, `sftp_master`), each sent until a status post carrying it
  succeeds. Tenants are reported only by the node that serves them (or destroys them, or has
  just stopped them, or serves their SFTP as master).
- **Guard:** `rackflow-storage-guard.service` runs `storage-runner guard`, which every second
  reads `/run/rackflow-storage/leases/*.json` (written by the runner on each renewal) and, once a
  file's deadline (`lease_seconds - guard_margin_seconds` after the renewal send) passes on
  either clock, sets the tenant's VLAN interface down and stops its container, or removes the
  SFTP virtual IP. It needs neither Ceph nor Rackflow.

### Upgrading from 0.2.x

1. Create the pool once: `ceph osd pool create rackflow_ha 32 32 replicated` and
   `ceph osd pool application enable rackflow_ha rados`.
2. Install `python3-rados` and `iputils-arping` (or use the 0.3.0 image), the new binary,
   the updated runner unit (`Type=notify`, `WatchdogSec=15`) and the guard unit.
3. The first pass with an `ha` block restarts each tenant's Ganesha once (new hostname and
   `rados_ng` recovery backend); clients see a short grace period. NFSv4 state from the old
   local recovery directory is not carried over.
4. Until Rackflow sends an `ha` block nothing changes.

## Private network presences and DHCP (0.4.0)

Design: `docs/private-network-dhcp.md` (sections 2, 3 and 4.4 to 4.5 are the runner's part).

- **Config:** `networks` lists the private networks whose presence this node may serve:
  `id`, `vlan`, `network_cidr`, `endpoint_ip`, `prefix_len`, `preferred_node_id`,
  `takeover_order` and `dhcp`. A tenant may carry `network_id` (the network it is the
  storage of) and the same `dhcp` block: `enabled`, `lease_seconds`, `reservations`
  (`mac`, `ip`), `range` (`start`, `end`, or null for none), `leases` (seed of dynamic leases:
  `mac`, `ip`, `expires_at`) and `options` (`router`, `dns_servers`, `domain`, `mtu`,
  `routes` of `destination` and `gateway`, `ntp_servers`; null or empty means not sent).
- **Presence:** namespace `rf-p<id>` with only a VLAN interface `rf-w<id>` on `PARENT_IFACE`,
  fixed MAC `02:52:4e:xx:xx:xx` (network id as 24 bits), the endpoint address with the
  prefix length, link up, and a gratuitous ARP after each takeover. No veth, routes,
  firewall entry, container or Ceph state. Presences are adopted after a restart and
  `rf-p*` namespaces not in the config are removed. A presence is never created on a VLAN
  used by a non-terminated tenant in the config or by a tenant namespace still on the node;
  that is reported as the network's error, and an existing presence on such a VLAN is
  removed before the tenant's network is set up.
- **Leases:** with an `ha` block a presence is served under lease `net.<id>` (rank from its
  `takeover_order`, like a tenant); without one by the node whose config lists it. A presence
  is never fenced: its takeover waits the rank delay (plus `lease_seconds` while the previous
  owner's record is fresh) and skips both fences. The guard sets `rf-w<id>` down at the
  deadline. A tenant with `network_id` is not acquired while `net.<network_id>` has any
  locker (`Want.Defer`). Draining releases presences like tenants. Events for a presence
  carry `network_id` instead of `tenant_id`.
- **DHCP (`internal/dhcp`):** an in-process DHCPv4 server, one UDP socket per served presence
  or tenant, opened inside the namespace and bound to `0.0.0.0:67` on the VLAN interface
  (`SO_BINDTODEVICE`, `SO_BROADCAST`). The server id is the presence's or tenant's address.
  Relayed requests and disabled DHCP are ignored; a reserved MAC gets its address; any other
  MAC gets a dynamic address only when a range is set, else no reply at all. REQUEST is
  answered with ACK or NAK per RFC 2131 4.3.2, INFORM with options only, RELEASE frees a
  dynamic lease and DECLINE quarantines the address for 10 minutes. Options 1, 51, 58, 59
  and 54 are always sent; 3, 6, 15, 26, 42 and 121 only when configured (121 then also
  carries `0.0.0.0/0` via the router when both are set). A dynamic address not already
  leased is probed first (`arping -D -c 2 -w 1` in the namespace) and quarantined if it
  answers. Dynamic leases live in memory and in `STATE_DIR/dhcp/<net|tenant>-<id>.json`,
  seeded from the config's `leases` and that file at start. Config changes swap the table
  without closing the socket. A server starts only while its lease is held and stops when
  the lease is lost or released, together with the link going down. DHCP never changes a
  tenant's Ganesha config or container.
- **Status:** `networks` has an entry (`id`, `serving`, `since`, `error`, `leases` of `mac`,
  `ip`, `expires_at`, `hostname`) for each presence the node serves, one `serving: false`
  entry after it stops, and a `serving: false` entry with the reason for a network this node
  could serve but cannot (invalid config or a VLAN conflict). A served tenant with
  `network_id` gets `dhcp` (`error`, `leases`).
- **Upgrade:** nothing to install beyond the 0.4.0 binary; `arping` is already required.
  Until Rackflow sends `networks` or `network_id` nothing changes.

## NFSv3 (0.4.1)

Ganesha serves NFSv3 next to NFSv4.1, mainly for the Windows "Client for NFS",
which only speaks v3. Everything else (exports, client lists, `root_squash`,
anonymous uid and gid, read-only or read-write) is the same export block, so
v3 and v4 clients get identical access.

- **Paths:** `Mount_Path_Pseudo = true`, so v3 clients mount the same
  `/<share>` path as v4. The CephFS `/volumes/...` path is refused.
- **Ports, all bound to the tenant's storage address in the tenant namespace:**
  NFS 2049/TCP, MOUNT 20048/TCP and UDP (Windows asks over UDP), and rpcbind
  111, started in the Ganesha container before `ganesha.nfsd` (its UDP sockets
  bound to the storage address and loopback; its TCP socket also listens on the
  namespace's link to the host, which the host firewall keeps unreachable from
  other tenants). Nothing is on a random port. NFS itself stays TCP only.
- **No locking over v3:** NLM and statd are off (`Enable_NLM = false`), so v3
  clients must mount with `nolock`. Locks are then local to each client: two
  clients do not see each other's locks. v4.1 clients keep real, failover-safe
  locks.
- **Mounting:**
  - Linux: `mount -t nfs -o vers=3,nolock,hard <storage ip>:/<share> /mnt`
  - Windows (Client for NFS): `mount -o nolock,mtype=hard \\<storage ip>\<share> Z:`.
    Windows sends uid and gid -2 (4294967294) unless `AnonymousUid` and
    `AnonymousGid` (DWORD) are set under
    `HKLM\SOFTWARE\Microsoft\ClientForNFS\CurrentVersion\Default` (then restart
    the NFS client service). Share roots are created mode 777, so either works;
    set them to the uid and gid your Linux clients use if both write to the
    share. File names with non-ASCII characters may need `-o lang=<code page>`.
- **Upgrade:** changing the core config restarts each tenant's Ganesha once,
  so v4 clients see one grace period (up to 45 s) and v3 clients a short pause.

## Safety rules

- Nothing is destroyed unless the config lists it explicitly with a destroy
  state and both checks above pass. A negative `destroy_grace_seconds`
  disables destruction.
- Tenants (namespaces, containers, cephx users) and subvolumes on the node
  but not in the config are reported in `orphans`, never removed. Anything in
  the subvolume group that does not follow the `t<id>-s<id>` naming is also
  reported as an orphan.
- If the config fetch fails, nothing changes and no status is posted (with
  failover, the last saved config is applied instead, see Failover). If the
  config has zero tenants while the node has any, nothing changes; the status
  keeps the previous `version_applied`, sets a node error and lists the
  orphans.
- A tenant whose config is invalid (bad VLAN, endpoint outside its network,
  network overlapping Ceph, duplicate VLAN, unknown state, ...) is reported as
  `error` and left exactly as it is.
- If Ceph is unavailable, running Ganesha containers are left alone and
  nothing is destroyed.
- If an existing share cannot be applied in a pass (its config is invalid or
  `ceph fs subvolume info` fails), the tenant's cephx caps are not narrowed
  and its Ganesha is left exactly as it is; the tenant reports `error` until
  the share can be applied again.
- A share's subvolume is only destroyed after Ganesha has confirmed it no
  longer exports it. The hash of the last config Ganesha loaded is kept in
  `STATE_DIR`, so a failed reload is retried on the next pass.
- A negative or overflowing `destroy_grace_seconds` disables destruction.
- The runner never follows HTTP redirects from Rackflow, so the bearer key
  cannot be sent anywhere but `RACKFLOW_URL`.
- The runner only touches objects it names: `rf-t*` namespaces, `rf-v*`,
  `rf-h*`, `rf-n*` interfaces, the `inet rf-storage` table, `rf-t*-nfs`
  containers labelled `rackflow.role=storage-nfs`, `client.rf-t*` users, and
  subvolumes in its own group.

## Running

Recommended: run the static binary on the host as a systemd service.

```sh
cd storage_runner
CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o storage-runner ./cmd/storage-runner
install -m 0755 storage-runner /usr/local/bin/
```

```ini
# /etc/systemd/system/rackflow-storage-runner.service
[Unit]
Description=Rackflow storage runner
After=network-online.target ceph.target
Wants=network-online.target rackflow-storage-guard.service

[Service]
Type=notify
EnvironmentFile=/etc/rackflow/storage-runner.env
ExecStart=/usr/local/bin/storage-runner
Restart=always
RestartSec=2
# The lease loop feeds the watchdog every renewal; a stuck runner is
# restarted, and the guard stops its tenants if renewals stop meanwhile.
WatchdogSec=15
# podman's conmon for each tenant's Ganesha lives in this unit's cgroup. The
# default KillMode would stop every tenant's NFS server whenever the runner
# restarts; with process only the runner stops and the new one adopts them.
KillMode=process

[Install]
WantedBy=multi-user.target
```

Both units are also in `systemd/`. The guard unit:

```ini
# /etc/systemd/system/rackflow-storage-guard.service
[Unit]
Description=Rackflow storage lease guard
After=local-fs.target

[Service]
EnvironmentFile=-/etc/rackflow/storage-runner.env
ExecStart=/usr/local/bin/storage-runner guard
Restart=always
RestartSec=1

[Install]
WantedBy=multi-user.target
```

The image (`Dockerfile`, built in CI as `storage-runner`) contains the binary,
`ceph-common`, `python3-rados`, `iputils-arping`, `iproute2`, `nftables` and
`podman`. With failover it also needs `/run/rackflow-storage` shared with the
host guard. In a container it needs
the host network, `--privileged`, `/run/netns` shared with the host
(`-v /run/netns:/run/netns:rshared`), `/etc/ceph` mounted, the host's podman
socket with `PODMAN_REMOTE_URL`, and `STATE_DIR` bind-mounted at the **same
path** as on the host (Ganesha's bind mounts are resolved by the host's
podman).

## Single-node test setup

For a pilot on one VM that runs the Ceph mons and OSDs as well as the runner.

1. Ceph (for example `cephadm bootstrap --single-host-defaults`) with at least
   three OSDs, a CephFS, and an EC pool whose failure domain is the OSD:

   ```sh
   ceph osd erasure-code-profile set rf-ec21 k=2 m=1 crush-failure-domain=osd
   ceph osd pool create cephfs_ec erasure rf-ec21
   ceph osd pool set cephfs_ec allow_ec_overwrites true
   ceph fs add_data_pool cephfs cephfs_ec
   ```

2. Network: the VM's second NIC (say `ens19`) attached untagged to a
   VLAN-aware Linux bridge on the hypervisor, so it receives the customer
   VLANs tagged. A test client VM gets a NIC on the same bridge tagged with the
   tenant's VLAN and an address in the tenant network.

3. Runner environment, e.g. `/etc/rackflow/storage-runner.env`:

   ```sh
   RACKFLOW_URL=https://rackflow.example.com
   RUNNER_KEY=...
   PARENT_IFACE=ens19
   CEPH_FS_NAME=cephfs
   CEPH_EC_POOL=cephfs_ec
   # from: ceph config get mon public_network
   CEPH_PUBLIC_NETWORKS=192.168.50.0/24
   CEPH_KEYRING=/etc/ceph/ceph.client.admin.keyring
   # Ceph 20.2.4+ (aes256k cephx keys); must not be older than the cluster's client requirement
   GANESHA_IMAGE=quay.io/ceph/ceph:v20.2.5
   ```

   The node's own `ceph` CLI must also be 20.2.4 or newer. Debian's own
   packages are too old (Debian 12 has 16.2.15, Debian 13 has 18.2.7), and
   download.ceph.com's Tentacle repository builds only for bookworm, jammy
   and noble. On a Debian 13 node, run the runner from its image (which has
   a 20.2.x client) or install a 20.2.4+ client some other way.

   Turn on forwarding even though every Ceph address is local here: the
   runner reports a node error on every pass while `net.ipv4.ip_forward` is
   0, whether or not anything is forwarded.

   ```sh
   echo net.ipv4.ip_forward=1 > /etc/sysctl.d/90-rackflow-storage.conf
   sysctl -p /etc/sysctl.d/90-rackflow-storage.conf
   ```

   The VM's own Ceph address is inside `CEPH_PUBLIC_NETWORKS`, so tenant
   traffic to Ceph takes the local `input` path described above.

4. Check from the test client VM:

   ```sh
   mount -t nfs4 <endpoint_ip>:/<share name> /mnt/x
   touch /mnt/x/hello && ls -l /mnt/x
   ```

   and from the client that the node's SSH is not reachable through the
   endpoint (`nc -zv <endpoint_ip> 22` should fail). On the node:
   `ip netns list`, `ip -n rf-t<id> addr`, `ip -n rf-t<id> route`,
   `nft list table inet rf-storage`, `podman logs rf-t<id>-nfs`,
   `ceph fs subvolume ls cephfs --group_name rackflow`.

## Development

```sh
cd storage_runner
go vet ./...
go test -race ./...
```

Every host interaction is behind an interface with a real driver and a fake
(`internal/netns`, `internal/firewall`, `internal/ceph`, `internal/ganesha`,
`internal/vip`, and `internal/lease`, whose `FakeCluster` models RADOS locks,
the OSD clock, blocklists and partitions). `internal/lease` tests run several
nodes against one fake pool with fake clocks (partitions, races, restarts,
paused VMs, clock steps, fences, moves); `internal/reconcile/ha_test.go` does
the same with whole reconcile passes. The Python helper can be checked with
`python3 -m py_compile internal/lease/rados_helper.py`.
`internal/reconcile` tests drive whole passes against the fakes and assert the
exact action list; the driver tests assert the exact `ip`, `nft`, `ceph` and
`podman` command lines.

Kernel-level checks are behind the `sandbox` build tag and must run inside a
throwaway user and network namespace, never directly on a host:

```sh
go test -c -tags sandbox -o /tmp/netns.test ./internal/netns/
go test -c -tags sandbox -o /tmp/fw.test ./internal/firewall/
go test -c -tags sandbox -o /tmp/dhcp.test ./internal/dhcp/
unshare -rnm sh -c 'mount -t tmpfs none /run && /tmp/netns.test -test.run Sandbox -test.v'
unshare -rnm sh -c 'mount -t tmpfs none /run && /tmp/fw.test -test.run Sandbox -test.v'
unshare -rnm sh -c 'mount -t tmpfs none /run && /tmp/dhcp.test -test.run Sandbox -test.v'
```

The DHCP sandbox test opens the real server socket inside a namespace and gets
an OFFER over a veth pair. `internal/dhcp` unit tests cover the packet rules
with a fake prober and clock; `internal/reconcile/network_test.go` covers
presences, tenant DHCP and presence failover against the fakes.

The firewall sandbox test builds two tenants with the real `ip` driver,
applies the real ruleset and sends TCP connections: local Ceph ports are
reachable, local SSH and the veth host address are not, remote Ceph is
forwarded and seen from the host's address (masqueraded), and a tenant
outside the allowed set reaches nothing. It needs `python3` and `nsenter`.

Not covered by any test: a real Ceph cluster (including the lease helper
against real librados, blocklisting and MDS eviction), real podman and
Ganesha (including `rados_ng` recovery), `arping`, real NFS clients and real
DHCP clients. Those wait for the pilot.
