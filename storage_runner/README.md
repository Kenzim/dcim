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
  NFSv4 only, bound to the endpoint IP, one export per share at `/<share name>`,
  `root_squash`, access limited to the share's client IPs (RW or RO).

Every pass it posts a status report back to Rackflow.

## Contract with Rackflow

- `GET {RACKFLOW_URL}/api/runner/storage/config` with
  `Authorization: Bearer <RUNNER_KEY>` returns the desired state (version,
  node, settings, tenants and their shares).
- `POST {RACKFLOW_URL}/api/runner/storage/status` (expects 2xx, normally 204)
  reports `version_applied`, `runner_version`, node health (`ceph_ok`,
  `free_bytes` of the EC pool, `error`), per-tenant and per-share state
  (`ready`, `pending`, `error`, `suspended`) with `used_bytes`, and the lists
  `destroyed_tenants`, `destroyed_shares` and `orphans`.

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
| `GANESHA_IMAGE` | required | Container image with `ganesha.nfsd` and the CephFS FSAL, e.g. `quay.io/ceph/ceph:v18` |
| `LINK_CIDR` | `169.254.128.0/17` | Carved into /30s for tenant veth links; must not overlap Ceph or customer networks |
| `STATE_DIR` | `/var/lib/rackflow-storage-runner` | Link allocations, share deleting marks, per-tenant Ganesha config and keyrings (mode 0700) |
| `PODMAN_REMOTE_URL` | empty | Podman service URL (`unix:///run/podman/podman.sock`) when the runner itself runs in a container |

Customer networks that overlap a Ceph public network or `LINK_CIDR` are
rejected per tenant with a clear error.

## Host prerequisites

- **Ceph:** `ceph-common` (the `ceph` CLI) and an admin keyring
  (`CEPH_KEYRING`). The node needs `ceph` admin rights to manage subvolumes and
  `ceph auth`.
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
  `nf_conntrack` modules; `net.ipv4.ip_forward=1` (needed for remote Ceph
  nodes; the runner reports a node error while it is off).
- **Tools:** `iproute2` (`ip`, with JSON output), `nftables` (`nft`),
  `podman` (Docker cannot join a named network namespace by path, so podman
  is the only runtime).
- **Host firewall:** the runner owns the table `inet rf-storage` only. nftables
  evaluates every table, so an accept in the runner's table does not override
  a drop elsewhere: if the host has its own `input` or `forward` policy of
  `drop`, allow `iifname "rf-h*"` to the Ceph networks on the Ceph ports there
  too, or tenants will not reach Ceph.

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

## Safety rules

- Nothing is destroyed unless the config lists it explicitly with a destroy
  state and both checks above pass. A negative `destroy_grace_seconds`
  disables destruction.
- Tenants (namespaces, containers, cephx users) and subvolumes on the node
  but not in the config are reported in `orphans`, never removed. Anything in
  the subvolume group that does not follow the `t<id>-s<id>` naming is also
  reported as an orphan.
- If the config fetch fails, nothing changes and no status is posted. If the
  config has zero tenants while the node has any, nothing changes; the status
  keeps the previous `version_applied`, sets a node error and lists the
  orphans.
- A tenant whose config is invalid (bad VLAN, endpoint outside its network,
  network overlapping Ceph, duplicate VLAN, unknown state, ...) is reported as
  `error` and left exactly as it is.
- If Ceph is unavailable, running Ganesha containers are left alone and
  nothing is destroyed.
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
Wants=network-online.target

[Service]
EnvironmentFile=/etc/rackflow/storage-runner.env
ExecStart=/usr/local/bin/storage-runner
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

The image (`Dockerfile`, built in CI as `storage-runner`) contains the binary,
`ceph-common`, `iproute2`, `nftables` and `podman`. In a container it needs
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
   CEPH_PUBLIC_NETWORKS=192.168.50.0/24   # ceph config get mon public_network
   CEPH_KEYRING=/etc/ceph/ceph.client.admin.keyring
   GANESHA_IMAGE=quay.io/ceph/ceph:v18
   ```

   `sysctl -w net.ipv4.ip_forward=1`. The VM's own Ceph address is inside
   `CEPH_PUBLIC_NETWORKS`, so tenant traffic to Ceph takes the local `input`
   path described above.

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
(`internal/netns`, `internal/firewall`, `internal/ceph`, `internal/ganesha`).
`internal/reconcile` tests drive whole passes against the fakes and assert the
exact action list; the driver tests assert the exact `ip`, `nft`, `ceph` and
`podman` command lines.

Kernel-level checks are behind the `sandbox` build tag and must run inside a
throwaway user and network namespace, never directly on a host:

```sh
go test -c -tags sandbox -o /tmp/netns.test ./internal/netns/
go test -c -tags sandbox -o /tmp/fw.test ./internal/firewall/
unshare -rnm sh -c 'mount -t tmpfs none /run && /tmp/netns.test -test.run Sandbox -test.v'
unshare -rnm sh -c 'mount -t tmpfs none /run && /tmp/fw.test -test.run Sandbox -test.v'
```

The firewall sandbox test builds two tenants with the real `ip` driver,
applies the real ruleset and sends TCP connections: local Ceph ports are
reachable, local SSH and the veth host address are not, remote Ceph is
forwarded and seen from the host's address (masqueraded), and a tenant
outside the allowed set reaches nothing. It needs `python3` and `nsenter`.

Not covered by any test: a real Ceph cluster, real podman and Ganesha, and
real NFS clients. Those wait for the pilot.
