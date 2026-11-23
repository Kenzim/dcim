# VM firewall for Proxmox VMs

Status: design, 2026-11-23 (Phase A). Nothing is built yet. Kenzi asked for "a full firewall system
built for proxmox vms". This document is the contract for the build: data model, enforcement on
Proxmox, reconcile loop, surfaces, rollout and the cluster-level safety rules. Read
`docs/private-networks.md`, `docs/private-network-dhcp.md`, `docs/proxy-fast-path.md` and
`docs/nfs-storage-failover.md` first; this document says how the firewall keeps each of them working.

Measurements quoted here were taken on epyc (PVE 9.2.3, nftables backend) on 2026-11-23 with two
throwaway VMs, since destroyed. Raw logs: `/root/dcim/.forge/reviews/vm-firewall/epyc-measure-2026-11-23/`;
ledger fact 9eb1f440.

## 1. Summary

- Enforcement is the **Proxmox VE firewall** at VM level, driven through the Proxmox API. Rackflow is
  the **source of truth**: it renders each VM's complete firewall (options, ordered rules, IP sets)
  from its own database, writes it, and a reconcile worker detects and repairs drift.
- Rackflow writes **only per-VM firewall objects** (`/nodes/{node}/qemu/{vmid}/firewall/...`, stored by
  Proxmox in `/etc/pve/firewall/<vmid>.fw`) and the `firewall` flag of the VM's public NIC (`net0`).
  It **never writes** the datacenter firewall (`cluster.fw`) or any node firewall (`host.fw`). Client
  security groups and IP sets are expanded into each VM's own rules and IP sets, so no client action
  ever touches a cluster-wide file. The one cluster prerequisite (datacenter firewall enabled) is an
  operator step done once per cluster under a runbook (section 9), after a go-ahead; Rackflow only
  reads it and refuses to switch a location on until it is ready.
- Clients get per-VM rules (in and out, IPv4 and IPv6, protocol, ports, source and destination CIDRs
  or IP sets, order, enable, comment), default policies, presets (SSH, RDP, web, ping, allow all),
  reusable security groups and IP sets shared across their VMs.
- Admins get **baselines** that clients cannot override or remove: anti-spoofing with the VM's
  assigned addresses (`ipfilter`), MAC filter, optional outbound SMTP block, blocking management
  networks, and any admin rules, set globally, per location or per product. Baseline rules are
  evaluated before every client rule. Admins also define security groups clients may attach.
- Only the **public NIC** is filtered. Private network NICs, proxy fast path NICs and anything trunked
  keep `firewall=0` exactly as today, so private networks, DHCP on them, NFS storage and the fast path
  are untouched by construction.
- Everything is **off by default**: per location opt-in by an admin, then per VM opt-in by the client
  (or admin). New VMs in an opted-in location get `firewall=1` on `net0` at creation so a later opt-in
  causes no interruption; existing VMs get one short network blip when their NIC flag flips (measured
  on the nested test cluster: median 64 ms, worst 105 ms on nftables; median 169 ms, worst 363 ms on
  iptables; section 9.4). The UI says "under half a second".
- Surfaces: admin UI, client portal, WHMCS client area (native section, not the plugin panel),
  reseller panel (read only), client MCP, staff MCP (one compact tool inside the 38,000-byte budget),
  REST API, an audit row for every change, alerts for apply failures, drift and an unready cluster.
- Bare metal is out of scope; the model is written so a second enforcement backend can be added
  (section 15).
- Production today (read-only survey, 9.2a): cluster `pve-06` has the datacenter firewall off and no
  node firewall files, but 46 guests already carry `enable: 1` and 22 NICs `firewall=1`. The
  datacenter switch therefore needs a prepared, approved procedure (9.3), proven first on a nested
  test cluster on epyc, with the nftables backend.

## 2. What Proxmox gives us, and what we use

| Proxmox feature | Used | How |
|---|---|---|
| VM firewall options (`enable`, `policy_in`, `policy_out`, `ipfilter`, `macfilter`, `dhcp`, `ndp`, `radv`, `log_level_in`, `log_level_out`) | yes | Rendered per VM by Rackflow. Clients set only the policies (and log level when an admin allows it); the rest come from the baseline |
| VM rules (`type in/out`, `action ACCEPT/DROP/REJECT`, `proto`, `sport`, `dport`, `source`, `dest`, `icmp-type`, `enable`, `log`, `comment`, `pos`) | yes | One Proxmox rule per rendered Rackflow rule; no macros, no `iface` |
| VM IP sets, including `ipfilter-net0` | yes | Client and admin IP sets are copied into each VM that references them as `rf-s<id>`; `ipfilter-net0` holds the VM's assigned addresses |
| VM aliases | no | Rackflow expands addresses itself |
| Cluster security groups, cluster IP sets, cluster aliases | no | They live in `cluster.fw`. Writing client data there would let client actions modify the datacenter file. Expanded per VM instead (section 4.5) |
| Datacenter options (`enable`, policies, `ebtables`, `log_ratelimit`) | read only | Readiness check; changed only by the operator runbook |
| Node options (`host.fw`: `enable`, `nftables`, conntrack settings) | read only | Readiness check |
| NIC `firewall=1` flag | yes, `net0` only | Set at provisioning in opted-in locations, or flipped once at opt-in |
| Firewall log (`GET .../firewall/log`) | later | Phase B5 optional: client log view |

Measured behaviour (epyc, nftables backend):

- With the VM firewall on and `policy_in DROP`, unlisted ports are blocked. Packets of established
  and related connections are accepted **before** the rules and before `ipfilter`
  (`pre-vm-in`/`pre-vm-out` chains). Changing rules never cuts an established connection.
- Rule, IP set and option changes take effect within about 10 seconds, with no re-plug and no loss.
- `ipfilter=1` with no `ipfilter-net0` set leaves the IPv4 set **empty** (it is not filled from
  `ipconfig0`); only the IPv6 link-local address from the MAC is added. The VM then cannot start new
  IPv4 flows and ARP for its address is dropped. Rackflow must always write `ipfilter-net0`.
- With `ipfilter-net0` holding the VM's address, a spoofed source is dropped in both directions.
- `macfilter` is on by default (source MAC must equal the NIC's MAC). `dhcp` and `ndp` are allowed by
  default; router advertisements and redirects from the VM are dropped (`radv` off).

## 3. Scope: which VMs, which NICs

### 3.1 Managed VMs

A VM is eligible when all of these hold:

- it is a `vm` service with a `service_vm` row (cluster, node, VMID known);
- the service's location (through `ProxmoxCluster.location_id`) has the firewall switched on (8.1);
- the service is not infrastructure: not the VM of a storage node, a proxy runner, a dev or CI VM;
  on production the storage VMs are the ones on the Ceph bridge `vmbr230` (hv-03, hv-08, hv-12).
  Rackflow has no link from those to services today, so eligibility also refuses any VM with a NIC
  that has `trunks=`, a NIC on the private pool bridge without a `tag` (a trunk), a NIC on an
  admin-listed infrastructure bridge (setting `vm_firewall_infrastructure_bridges`, for example
  `vmbr230`), or a MAC in the storage presence ranges (`02:52:46`, `02:52:4e`). Admins can also mark a
  service `excluded`.

### 3.2 Filtered NICs

| NIC | `firewall` flag | Why |
|---|---|---|
| `net0` (public, from `VMIPAllocation`) | `1` once managed | The only filtered interface |
| Private network NICs (`net1`..`net31`, `tag=` on the pool bridge) | `0`, unchanged | A private network is the client's own isolated layer 2 segment. `attach_private_nic` keeps writing `firewall=0` (`app/plugins/proxmox.py:1237`); the firewall code never touches these NICs |
| Proxy fast path NIC (access network, `100.80.0.0/16`) | `0`, unchanged | One access network per client per location, isolated by VLAN |

Rules therefore apply to `net0` traffic only. The client UI says so ("Rules apply to your public
interface. Private networks are not filtered.").

### 3.3 Why each existing feature keeps working

- **Private networks**: NICs stay `firewall=0`, so no `ipfilter`, no `macfilter`, no rules. Clients may
  use any addresses on their private VLANs, as today.
- **DHCP on private networks** (storage presence, UDP 67/68 inside the VLAN): not filtered (NIC flag 0).
  On `net0`, the `dhcp` option stays on, so templates using `ipconfig0: ip=dhcp` keep working.
- **NFS storage**: client VMs reach the storage endpoint over their private NIC (unfiltered). Storage
  nodes are infrastructure and never managed, so their VIP and the fixed tenant MACs on their trunk
  NICs are untouched (`docs/nfs-storage-failover.md:427-429`, `:709`). The public SFTP virtual IP is
  reached by clients over `net0` as ordinary outbound traffic: allowed by the default `policy_out
  ACCEPT`, and the management-network baseline must never list storage SFTP addresses (the admin UI
  warns when a management IP set overlaps a storage location's SFTP host or virtual IP).
- **Proxy fast path**: the client's access-network NIC is unfiltered; proxy runners are not managed.
- **VNC console, guest agent, cloud-init**: go through the hypervisor, not the VM's network.
- **IP reassignment** (`app/services/vm_ip_reassign.py`): changes the desired `ipfilter-net0`; the
  reassign service bumps the firewall revision so the worker rewrites the set (section 6.6).

## 4. Data model

All tables are new; no existing column changes. String enums use `native_enum=False` as elsewhere.

### 4.1 `service_firewalls` (one per managed service)

Named for services, not VMs, so a later backend (bare metal) fits; `backend` is `proxmox_vm` for now.

| Column | Type | Notes |
|---|---|---|
| `service_id` | int PK, FK `services.id` cascade | |
| `backend` | str(32) | `proxmox_vm` |
| `client_enabled` | bool, default false | Client's (or staff's) switch for the client rules |
| `policy_in` | enum `DROP`/`REJECT`/`ACCEPT`, default `DROP` | |
| `policy_out` | enum, default `ACCEPT` | |
| `log_level_in`, `log_level_out` | enum `nolog`/`info`/`warning`/..., default `nolog` | Client may change only if `firewall_client_logging` is on |
| `managed` | bool, default true | Admin emergency switch: false means Rackflow does not touch this VM's firewall at all |
| `enforce_baseline` | bool, default false | Baseline enforced even while `client_enabled` is false. Set at provisioning in an enabled location (4.5a); staff can change it |
| `excluded` | bool, default false | Admin: never manage (infrastructure) |
| `admin_overrides` | JSON | Per-service admin overrides of baseline settings, e.g. `{"smtp_out": "allow", "macfilter": false, "extra_ipfilter": ["203.0.113.9"]}` |
| `revision` | int, default 1 | Bumped on every change of anything that renders into this VM |
| `applied_revision` | int, null | Revision last written successfully |
| `desired_hash`, `applied_hash` | str(64) | SHA-256 of the canonical rendered document |
| `applied_digest` | str(64) | Proxmox's digest of the VM `.fw` file after our last write |
| `nic_flag` | enum `unknown`/`off`/`on`/`pending_reboot` | Observed `net0` firewall flag |
| `state` | enum `off`/`pending`/`applied`/`error`/`drift`/`not_enforced`/`unmanaged`/`vm_missing` | |
| `last_error` | text | |
| `failing_since`, `last_applied_at`, `last_checked_at` | datetime | |
| `next_attempt_at` | datetime | Backoff |
| `claimed_by`, `claimed_until` | str, datetime | Per-row claim (section 6.2) |
| `drift_repairs_24h` | int | Rolling counter for the alert |
| `created_at`, `updated_at` | datetime | |

### 4.2 `firewall_rules`

One table for every ordered rule list: a VM's client rules, a security group's rules and a
baseline's rules. Exactly one of the three owner columns is set (checked in the service layer and by a
check constraint on MariaDB; SQLite relies on the service layer).

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `service_id` | FK `service_firewalls.service_id` cascade, null | VM rule |
| `group_id` | FK `firewall_groups.id` cascade, null | Group rule |
| `baseline_id` | FK `firewall_baselines.id` cascade, null | Baseline rule |
| `position` | int | Dense 0..n-1 within the owner |
| `kind` | enum `rule`/`group` | `group` entries reference `ref_group_id` and expand in place (VM lists and baselines only; groups cannot nest) |
| `ref_group_id` | FK `firewall_groups.id`, null | |
| `direction` | enum `in`/`out` | |
| `action` | enum `ACCEPT`/`DROP`/`REJECT` | |
| `ip_version` | enum `any`/`4`/`6` | |
| `protocol` | str(16), null | `tcp`, `udp`, `icmp`, `ipv6-icmp`, `sctp`, `gre`, `esp`, `ah`; null means any |
| `ports` | str(128), null | Destination ports: `22`, `80,443`, `1000:2000`; at most 10 items (Proxmox multiport takes 15) |
| `source_ports` | str(128), null | Same format |
| `icmp_type` | str(32), null | Named type, only with `icmp`/`ipv6-icmp` |
| `remote_cidrs` | JSON list, null | The far end: `source` for `in`, `dest` for `out`; at most 20 entries |
| `remote_ipset_id` | FK `firewall_ipsets.id`, null | Instead of `remote_cidrs` |
| `local_cidrs` | JSON list, null | Optional, the VM's side (one of its own addresses); admin baselines mostly |
| `enabled` | bool, default true | |
| `log` | bool, default false | Only if logging is allowed |
| `comment` | str(120), null | Kept in Rackflow only; never sent to Proxmox |
| `created_by_user_id` | FK users, null | |
| `created_at`, `updated_at` | datetime | |

Rules are deliberately "remote end plus ports", not raw source and destination, so clients cannot
write rules about other people's traffic: an `in` rule always has the VM as its destination.

### 4.3 `firewall_groups` and `firewall_ipsets`

| `firewall_groups` | Notes |
|---|---|
| `id`, `name` (str 48), `description` (str 255) | |
| `scope` | `client` or `admin` |
| `owner_user_id` | FK users, set for `client` groups; unique (`owner_user_id`, `name`) |
| `client_selectable` | bool, `admin` groups only: clients may attach it to their own VMs |
| `revision`, timestamps | |

| `firewall_ipsets` | Notes |
|---|---|
| `id`, `name`, `description`, `scope`, `owner_user_id`, `client_selectable` | As groups |
| `purpose` | `general` or `management` (admin only; the networks clients must not reach) |
| `revision`, timestamps | |

`firewall_ipset_entries`: `id`, `ipset_id` (cascade), `cidr` (str 49), `nomatch` (bool),
`comment` (str 120). Unique (`ipset_id`, `cidr`).

### 4.4 `firewall_baselines` and `firewall_location_settings`

| `firewall_baselines` | Notes |
|---|---|
| `id`, `name` | |
| `scope_type` | `global`, `location`, `product` |
| `scope_id` | null for global; location id or product id |
| `enabled` | bool |
| `settings` | JSON: `{"anti_spoof": true, "macfilter": true, "smtp_out": "block"|"allow"|null, "block_management": true, "management_ipset_ids": [..], "client_may_disable": false}`; null keys inherit |
| `revision`, timestamps | |

Merge order is global, then location, then product. For a setting the most specific non-null value
wins; rules are concatenated in that order (global rules first). Unique (`scope_type`, `scope_id`).

| `firewall_location_settings` | Notes |
|---|---|
| `location_id` | PK, FK locations |
| `enabled` | Location opt-in |
| `nic_flag_on_provision` | Set `net0` `firewall=1` on new VMs (default true once enabled) |
| `enforce_baseline_on_all` | Apply the baseline to every eligible VM, opted in or not (default false) |
| `smtp_block_new_vms` | Default true. New VMs provisioned here get the SMTP block and an enforced baseline from creation (4.5a) |
| `anti_spoof_new_vms` | Default false. Whether that enforced baseline on new VMs also includes `ipfilter` and `macfilter` (4.5a) |
| `ipv6_allowed_prefixes` | JSON list, null. Prefixes added to `ipfilter-net0` for VMs whose observed `net0` bridge (`service_firewalls.net0_bridge`) is listed in `vm_firewall_ipv6_ra_bridges`; while empty such VMs are not eligible (reason `ipv6_ra_bridge_needs_prefix`). Weaker protection, see 4.4a |
| `readiness` | JSON cache of the last readiness check per cluster (section 9.2) |
| `readiness_checked_at` | datetime |

### 4.4a IPv6 on router-advert bridges: what the prefix allowance does and does not stop

On a bridge with router adverts (production `vmbr750`), VMs pick their own IPv6 addresses by SLAAC,
so Rackflow does not know each VM's address. Putting the location's whole prefix into each VM's
`ipfilter-net0` means IPv6 anti-spoofing there **only stops addresses outside the prefix**. A VM can
still send from, and take over traffic for, a neighbour's address inside the same prefix. IPv4
anti-spoofing on the same VM is unaffected (its exact assigned address). The admin help text for
`ipv6_allowed_prefixes` says this in plain words. The later fix is per-VM IPv6 assignment
(Rackflow allocates a /128 or a /64 per VM, written by cloud-init instead of SLAAC), after which
`ipfilter-net0` holds the VM's own IPv6 addresses and the prefix allowance is no longer needed.
If `net0_bridge` has not been observed yet, render adds no prefixes and leaves `ipfilter` off while
any router-advert bridge is configured (no anti-spoofing rather than wrong anti-spoofing).

Global numbers are settings in a new `vm_firewall` group of `app/services/settings_store.py`:
`vm_firewall_reconcile_interval_seconds` (600), `vm_firewall_max_rules_per_vm` (100),
`vm_firewall_max_rendered_rules` (300), `vm_firewall_max_groups_per_client` (20),
`vm_firewall_max_rules_per_group` (50), `vm_firewall_max_groups_per_vm` (5),
`vm_firewall_max_ipsets_per_client` (20), `vm_firewall_max_ipset_entries` (64),
`vm_firewall_cluster_concurrency` (4), `vm_firewall_drift_mode` (`repair` or `report`),
`firewall_client_logging` (false), `vm_firewall_infrastructure_bridges` (comma-separated, empty), `vm_firewall_ipv6_ra_bridges`
(comma-separated, empty; bridges with router adverts, `vmbr750` on production). The
worker switch `run_vm_firewall_worker` (default true) and its
lease settings stay env-only in `config.py`, like the alert worker.

### 4.5 Rendering

`render(db, service_id) -> RenderedFirewall` is a pure function (reads only) that produces the
canonical document for one VM:

1. **Options**: `enable` = 1 if `client_enabled`, or if the merged baseline has anything enforced and
   (`client_enabled` or `enforce_baseline` or `enforce_baseline_on_all`); `policy_in`/`policy_out` from the client when
   `client_enabled`, otherwise `ACCEPT`/`ACCEPT` (baseline only); `ipfilter` = anti_spoof;
   `macfilter`; `dhcp: 1`; `ndp: 1`; `radv: 0`; log levels.
2. **IP sets**: `ipfilter-net0` = the VM's public address (`VMIPAllocation.ip_address`) plus any
   `extra_ipfilter` override, plus later IPv6 assignments; `rf-s<id>` for every IP set referenced by a
   rendered rule, entries copied.
3. **Rules**, in this order, each with Proxmox comment `rf:<marker>`:
   1. baseline rules (global, location, product), groups expanded (`rf:b<rule>` / `rf:b<rule>.<grouprule>`);
   2. generated baseline rules: SMTP block (`OUT DROP -p tcp -dport 25`, then 465 and 587 only if an
      admin asks), management block (`OUT DROP -dest +rf-s<mgmt>` and `IN DROP -source +rf-s<mgmt>`);
   3. client rules in position order, groups expanded in place (`rf:c<rule>` / `rf:g<group>.<rule>`);
   disabled rules are rendered with `enable 0` so positions stay stable.
4. A rule's family: an `ip_version` of `4` or `6` with no addresses renders as `source 0.0.0.0/0` or
   `::/0`; `icmp` implies 4 and `ipv6-icmp` implies 6; a rule mixing families is refused at validation.

Client text never reaches Proxmox: comments, names and descriptions stay in Rackflow; Proxmox sees
only generated markers and validated fields. The document is serialised canonically and hashed
(`desired_hash`).

Baseline rules are evaluated first and Proxmox stops at the first matching ACCEPT, DROP or REJECT, so
a client rule can never undo a baseline DROP. Baseline ACCEPT rules (for example the provider's
monitoring) likewise cannot be blocked by a client. When `client_enabled` is false, the client's rules
are not rendered and policies are ACCEPT, but the baseline still applies: turning the firewall "off"
as a client means "allow everything the baseline allows". Clients may disable baseline protections
only if `client_may_disable` is true (open question Q2).

### 4.5a SMTP block on new VMs (Kenzi, Q3)

Outbound TCP 25 is blocked by default on **new** VMs in firewall-enabled locations; 465 and 587 stay
open; existing VMs are unchanged, including when they later opt in. It is per service, not a global
baseline value, so existing VMs never pick it up by inheritance:

- the provisioning step (6.5), in a location with `smtp_block_new_vms`, creates the VM's row with
  `admin_overrides.smtp_out = "block"` and `enforce_baseline = true`. The VM's firewall is enabled
  before first boot with ACCEPT policies and a narrow baseline: the SMTP block and, if an admin has
  filled a management IP set, the management-network block. Anti-spoofing (`ipfilter`) and the MAC
  filter stay off on such a VM until the client enables the firewall, unless the location setting
  `anti_spoof_new_vms` (default off) is on (project chat decision, 2026-11-23). There is no blip and
  nothing else is filtered until the client adds rules;
- staff unblock per service by setting `admin_overrides.smtp_out = "allow"` (audited
  `firewall.admin_override_changed`); clients cannot change it (Q2);
- the global, location and product `smtp_out` baseline values stay available for a later wider
  policy, default null (allow).

### 4.6 Limits and validation

- Per VM: at most 100 client entries, 5 group references, 300 rendered rules in total.
- Per client: 20 groups of at most 50 rules, 20 IP sets of at most 64 entries.
- Ports 1 to 65535, ranges `a:b` with `a < b`, at most 10 items; CIDRs parsed with `ipaddress`, host
  bits must be zero, no `0.0.0.0/0` inside an IP set (use "any"), at most 20 per rule.
- A client may reference only its own groups and IP sets and admin ones marked `client_selectable`.
- Every write carries the `revision` the caller read; a stale one gets 409 `firewall_changed`.

## 5. Proxmox writer

New module `app/services/vm_firewall/proxmox_fw.py`, a thin async client using the existing
`ProxmoxPlugin` authentication and relocation (`_get_with_relocate`, `_post_with_relocate`, plus PUT
and DELETE helpers added beside them). Calls used:

| Call | Purpose |
|---|---|
| `GET /cluster/firewall/options` | Readiness (`enable`) |
| `GET /nodes/{n}/firewall/options`, `GET /nodes/{n}/services/{pve-firewall,proxmox-firewall}/state` | Readiness per node |
| `GET/PUT /nodes/{n}/qemu/{v}/firewall/options` | Options; the GET also returns the file `digest` |
| `GET/POST /nodes/{n}/qemu/{v}/firewall/rules`, `PUT/DELETE .../rules/{pos}` | Rules |
| `GET/POST/DELETE .../firewall/ipset`, `.../ipset/{name}`, `.../ipset/{name}/{cidr}` | IP sets |
| `GET /nodes/{n}/qemu/{v}/config`, `GET .../pending`, `PUT .../config` | `net0` flag, with `digest` |

Every write passes the `digest` from the preceding read, so a concurrent edit (staff on the Proxmox
GUI, another Rackflow process) fails the write instead of being overwritten blindly; the worker then
re-reads and re-plans.

Proxmox permissions needed by the cluster's API user: `VM.Audit` and `VM.Config.Network` on the VMs,
`Sys.Audit` on `/` and `/nodes`. Production uses a full-rights user today, so nothing changes, but the
live test uses a dedicated token with only these (section 13).

### 5.1 Applying a change without a permissive window

Proxmox has no "replace all rules" call: rules are inserted, updated, moved and deleted one position at
a time. The planner (`plan(current, desired) -> [ops]`, pure and unit tested) works from the markers:

- **One operation** (add, delete, update or move one rule, change one option, add or remove one IP set
  entry): applied directly. Any single step goes straight from the old set to the new one.
- **More than one rule operation**: the planner first inserts a **guard** rule right after the
  baseline rules: `IN DROP` (and `OUT DROP` if outbound client rules change), comment `rf:guard`. New
  connections in that direction are refused while the list is rewritten; established connections
  carry on, because they are accepted before any rule. Then the rest of the operations, then the guard
  is deleted last. A crash mid-apply leaves the guard in place: fail closed for new connections only,
  shown as `error`, repaired by the next attempt.
- **IP sets** are written before the rules that reference them, and deleted after.
- **Enable** is written last when turning on and first when turning off.

A typical rewrite of 50 rules takes a few seconds and the proxmox-firewall daemon may never even
compile an intermediate state; the guard makes that irrelevant.

### 5.2 The `net0` flag

The flag flip reads `net0`, replaces only the `firewall=` key (keeping model, MAC, bridge, tag,
`link_down`, rate) and writes it with the config digest. Order when opting in an existing VM: write
IP sets, rules and options with `enable: 0`; flip `net0` to `firewall=1`; then set `enable: 1`. If the
VM's hotplug setting excludes `network`, Proxmox records the change as pending: the state becomes
`pending_reboot` and the client sees "applies at next restart". Turning the firewall off never flips
the flag back; it only sets `enable: 0`.

What a running VM sees when its flag flips (measured, nftables backend): qemu-server unplugs the host
tap from the bridge and plugs it back (`tap_unplug` and `tap_plug`); the guest's virtual NIC is not
hot-unplugged and its carrier does not change. The tap was off the bridge for 115 to 132 ms; 6 to 7
pings at 20 ms intervals were lost; an open TCP stream stalled 226 ms and survived. Toggling the
VM-level `enable` caused no loss at all.

Measured again on the nested test cluster (section 9.4, 20 ms ping interval, so about plus or minus
20 ms): nftables median 64 ms, worst 105 ms over 20 flips, an open TCP stream stalled about 215 ms;
iptables median 169 ms, worst 363 ms over 19 flips (0 to 1 is the slower direction, worst 363 ms), a
TCP stream stalled typically 229 ms and up to 803 ms (a second retransmission), and the
`fwbr`/`fwpr`/`fwln` devices appear and disappear with the flag. The guest saw no carrier change on
either backend. The test cluster's qemu-server (9.2.20) is newer than production's and the flip runs
through qemu-server, so production may differ; re-measure on one production-like VM before quoting a
figure to clients. Every warning shown to admins and clients says "under half a second".

The flip must send the whole `net0` value read just before (model, MAC, bridge, tag, rate and the
changed `firewall` key) as form-encoded data: a malformed value in a hand-made test call was accepted
by Proxmox and left the guest with no bridge. `proxmox_fw.set_net0_firewall_flag` reads `net0`,
replaces only `firewall=` and writes it with the config digest; the end-to-end run (9.4) checks it
against the real API.

## 6. Reconcile worker

### 6.1 Process and lease

`app/workers/vm_firewall.py`, modelled on `app/workers/alerts.py`: started from the lifespan when
`run_vm_firewall_worker` is true, with a lease row `vm_firewall` in `recurring_billing_leases`
(`app/services/alerting/lease.py` pattern) so only one process sweeps. It does not run inside
`reconciliation_jobs.py`, which has no lease and runs in every API replica.

### 6.2 Two duties

1. **Apply queue** (every 5 s): rows where `revision != applied_revision`, or `state = error` and
   `next_attempt_at <= now`. Claimed per row (`claimed_by`, `claimed_until` 120 s, conditional update),
   at most `vm_firewall_cluster_concurrency` per cluster at once. Changes made within 2 s of each other
   are coalesced by reading the revision at claim time.
2. **Drift sweep** (every `vm_firewall_reconcile_interval_seconds`, spread over the interval): for each
   managed row, `GET .../firewall/options`. If the digest equals `applied_digest` and the options match,
   the VM is in sync (one API call per VM). Otherwise read rules, IP sets and `net0`, compare with the
   rendered document, and repair (`drift_mode = repair`) or record (`report`). Each repair writes a
   `firewall.drift_repaired` audit row (system actor) with what differed, and bumps
   `drift_repairs_24h`. Proxmox rules without an `rf:` marker count as drift and are removed: Rackflow
   owns the whole VM firewall of a managed VM. Staff who need hand-made rules set `managed = false`.
3. **Readiness** (each sweep, per cluster of an enabled location): section 9.2. If a cluster becomes
   not ready, every managed VM on it gets `state = not_enforced`, clients see "Firewall not currently
   enforced", and a critical staff alert opens.

### 6.3 States

| State | Meaning |
|---|---|
| `off` | Nothing rendered needs enforcing (`enable: 0` written or never managed) |
| `pending` | A revision is waiting to be written |
| `applied` | Proxmox matches the rendered document |
| `pending_reboot` | Applied, but the `net0` flag change waits for a restart |
| `error` | Last write failed; `last_error`, backoff 30 s doubling to 10 min |
| `drift` | Drift found in `report` mode, not repaired |
| `not_enforced` | Applied, but the cluster or node firewall is off |
| `unmanaged` | `managed = false` |
| `vm_missing` | Proxmox has no such VM (terminated, destroyed); no alert when the service is terminated |

### 6.4 Failures

Proxmox unreachable or a 5xx: `error`, backoff. A 4xx from a digest mismatch: re-read and re-plan at
once (at most 3 times per attempt). A validation error from Proxmox (for example an unknown
`icmp-type` on an old node): `error` with the message, no tight retry, alert after the threshold.

### 6.5 Provisioning hook

New idempotent deployment step `ApplyFirewallStep` in `app/services/deployment/steps.py`, added to
every VM strategy in `strategies.py` after the sizing and bridge step and **before** `PowerOnStep`:

- sets `net0` `firewall=1` when the location has `nic_flag_on_provision` (no blip: the VM is off);
- when the location has `smtp_block_new_vms`, creates the `service_firewalls` row with
  `enforce_baseline = true` and `admin_overrides.smtp_out = "block"` (section 4.5a);
- always writes the VM firewall options, so any `.fw` a clone inherited from its template (production
  has 760 such files) is replaced by what Rackflow renders, `enable: 0` included;
- if anything renders as enforced, writes the full firewall and waits for success, so the VM never
  boots unprotected. If the write fails the step fails and the job retries like any other step.

### 6.6 Lifecycle

| Event | Firewall action |
|---|---|
| Create, reinstall (VM deleted and rebuilt on the same VMID, which deletes the `.fw` file) | `ApplyFirewallStep` |
| Backup restore onto the same VM (vzdump restores the old `.fw`), retained backup restore | Bump revision at job completion; the sweep would also catch it as drift |
| IP reassignment | `reassign_vm_ip` bumps the revision (new `ipfilter-net0`) before the guest reconfigures |
| HA relocation | The plugin's relocator finds the new node; nothing else changes |
| Private network attach, detach | Nothing (private NICs are not filtered) |
| Suspend | Nothing (firewall stays) |
| Terminate | Rows kept until the VM is destroyed; then `vm_missing` and the row is deleted with the service |
| Ownership change (admin) | Group and IP set references owned by the old owner are removed from the VM, audited `firewall.references_removed`, revision bumped |
| Product change | Product baseline changes, revision bumped |
| Baseline, group or IP set change | Revision bumped on every VM that renders it (one `UPDATE ... WHERE service_id IN (...)`) |

## 7. Tenancy and permissions

- New client permission key `vm.firewall` ("Manage the VM firewall"), default **true** for VMs in
  `DEFAULT_PERMISSIONS_BY_SERVICE_TYPE` (`app/core/client_permissions.py`), so presets and reseller
  ceilings can turn it off. Viewing the firewall needs only `service.view`.
- Every client route resolves the service by id and `owner_user_id` (404 otherwise) and then
  `require_client_permission(db, service, "vm.firewall")` for writes, exactly like the power routes.
- Groups and IP sets are account objects (`owner_user_id`). Every reference check compares the
  referenced object's owner with the **service's** owner, never the caller's id alone, so a shared
  private network member, an impersonating admin or a reseller cannot cross-link objects between
  clients. Group and IP set routes need a live client session; attaching a group to a VM also needs
  `vm.firewall` on that VM.
- Billing API (WHMCS): `_assert_billing_owned_service` (the owner's `billing_integration_id` must be the
  caller's integration) plus the same permission check. Group lists over the billing API return only
  the service owner's groups.
- Reseller panel: read only, through `ServiceBilling.reseller_id`, like its other service views.
- Impersonation: allowed (support work); the audit row carries `impersonated_by`.
- Staff (admin session or staff MCP) may edit any VM's client rules and every admin object; admin-only
  fields (`managed`, `excluded`, `admin_overrides`, baselines) are not accepted on client or billing
  routes at all (separate request models, not a filtered shared one).

## 8. Rollout

### 8.1 Location opt-in

Admin page, per location: **Enable VM firewall here**. Refused unless the readiness check for every
cluster in the location passes (section 9.2), unless the admin ticks "staging, not enforced" (useful
before the cluster step, clients cannot see it then). Enabling a location:

- turns on `nic_flag_on_provision` (new VMs get `firewall=1` before first boot);
- makes the firewall section appear for that location's VMs in the portal, WHMCS and MCP;
- applies the baseline to opted-in VMs only, unless `enforce_baseline_on_all`.

### 8.2 Per VM opt-in

The client (or staff) opens Firewall and turns it on. The first time, the UI proposes rules from the
VM's template OS (Linux: SSH and ping; Windows: RDP and ping) with inbound DROP and outbound ACCEPT, and
shows them before saving. If the VM's `net0` flag is still 0 (VMs created before the location was
enabled), the UI says "Turning the firewall on briefly interrupts this server's network (under half a
second)" and the request must carry `confirm_interruption=true`. When Rackflow cannot read the VM's
live config, it treats the flag as unknown and requires the confirmation too.

### 8.3 Baseline for every VM in a location

`enforce_baseline_on_all` reaches existing VMs, so switching it on is a staged bulk rollout:

- the admin first sees a preview (`POST /api/admin/vm-firewall/locations/{id}/rollout` with
  `dry_run`, backed by `preview_enforce_baseline_on_all`): how many existing VMs change, how many are
  running without `firewall=1` on `net0` and will get the short blip (under half a second, measured
  in sections 5.2 and 9.4), and which;
- the switch is refused unless the request carries `confirm_existing_vms` equal to the previewed
  count (error `confirmation_required` with a fresh preview); turning it off needs no confirmation;
- the change is audited `firewall.location_updated` with the counts;
- the affected rows get a staggered `rollout_after` time so at most
  `vm_firewall_rollout_per_node_per_minute` (default 10) VMs per node become due each minute; render
  ignores the location switch for a row until its time, and the worker applies rows as they come due
  through its normal path, pausing the rollout after 3 consecutive failures.

SMTP blocking for existing VMs stays out of this (Q3: existing VMs unchanged).

### 8.4 Rollback

Per VM: client or staff turns it off, or staff sets `managed = false`; `enable: 0` is written, no blip.
Per location: disable, and the worker writes `enable: 0` on every managed VM in it. The `net0` flags
stay 1, which with the VM firewall disabled filters nothing on nftables. On iptables (measured in
9.4) a guest left at `firewall=1` with no VM firewall still passes traffic, but a connection from
another host to one of its closed ports times out instead of being refused (the guest's reset is
dropped as INVALID by the host's forward chain), so port checks and scanners see timeouts. On both
backends its traffic stays in the host's connection tracking while the datacenter firewall is on
(9.3).

## 9. Datacenter and node firewall: the safety rules

### 9.1 What Rackflow will and will not do

- Rackflow **never writes** `/cluster/firewall/*` or `/nodes/{node}/firewall/*`. There is no code path
  for it; the Proxmox writer module has no function for those endpoints and a unit test asserts that
  every URL it builds is under `/qemu/{vmid}/` (or a GET).
- The VM firewall only works when the datacenter firewall is enabled. That one switch is an operator
  action under the runbook below, per cluster, after reporting exactly what it will do and getting the
  project chat's go-ahead. On production hypervisors hv-01, hv-21 and hv-22 it also needs Kenzi's
  explicit approval, as they are production.

### 9.2 Readiness check (read only)

Per cluster: `GET /cluster/firewall/options` must have `enable: 1`. Per node: the firewall service is
running (`pve-firewall`, and `proxmox-firewall` when `host.fw` has `nftables: 1`). The result is cached
in `firewall_location_settings.readiness` and shown on the admin page with every reason, for example
"datacenter firewall disabled" or "node hv-21: proxmox-firewall not running". The check also reports
the datacenter `policy_in`, `policy_out`, `ebtables` and each node's `host.fw` `enable`, so the
operator can see whether the cluster was set up as the runbook says.

A second read-only admin action, **pre-enable audit** (`POST /api/admin/vm-firewall/clusters/{id}/audit`),
lists every guest on the cluster, managed or not, that would start filtering the moment the
datacenter firewall is enabled: a `.fw` with `enable: 1` and a NIC with `firewall=1`, with its
policies, rule count, and the dangerous cases (`ipfilter: 1` with no `ipfilter-net*` set, which
breaks outbound IPv4 as measured in section 2; `policy_in` DROP with no rules). It reads each VM's
config and firewall options (about two GETs per VM) and writes nothing.

### 9.2a Production today (survey 2026-11-23, ledger 6d5e90f4)

Production cluster `pve-06` (23 nodes, hv-01 to hv-23, PVE 9.2.3, the same versions as epyc):

- `cluster.fw` holds only `enable: 0`; no node has a `host.fw`, so the host firewall would default to
  on and the backend to iptables. Both `pve-firewall` and `proxmox-firewall` run on every node; nothing
  filters today (no nftables tables, no `PVEFW` iptables rules, empty ebtables, `br_netfilter` not
  loaded).
- 760 of 851 VMs already have a `.fw` file with options only (probably copied from templates at
  clone): `enable: 1` on 46, `macfilter: 1` on 759, `ipfilter: 1` on 1 (with no IP set), one guest with
  `policy_in DROP` and one rule. 22 VMs have `firewall=1` on a NIC. **The guests with both would start
  filtering as soon as the datacenter firewall is enabled**, with a VM default `policy_in` of DROP.
- Ceph is hyper-converged (public and cluster network on `bond1.230`), corosync rings on `bond0` VLAN
  220 and `bond1.230`: host interfaces, not bridges. A host firewall in DROP mode would cut both.
- IPv6 (read-only check on hv-05, 2026-11-23, ledger 46c899d9): VMs on the public bridge `vmbr0`
  (VLAN 733) have no IPv6 today. No router advertisements in about 130 s of capture, no global IPv6
  neighbours, only link-local guest traffic, and no VM config sets `ip6=`. hv-01 has a static host
  address there, hv-05 none. The Cogent bridge `vmbr750` (VLAN 750) does carry router adverts; any VM
  NIC on it needs the per-location IPv6 prefix before anti-spoofing is enabled for it.

### 9.3 Why enabling the datacenter firewall is dangerous, and the safe order

When the datacenter firewall is enabled, every node's **host** firewall starts too (host `enable`
defaults to on), and the datacenter `policy_in` defaults to **DROP**. With defaults, host traffic is
then allowed only from the auto-detected local management network to SSH, the API (8006), VNC and
corosync. Ceph between nodes, the Rackflow app reaching 8006 from another network, monitoring and
anything else would be cut: on a hyper-converged cluster that is a storage outage (the nested
contrast run in 9.4 confirmed this: every new connection from outside the node's own subnet, and all
ICMP, was blocked on both backends, while established flows survived). Connection tracking also
changes (measured in 9.4): on iptables, enabling the datacenter firewall turns on bridge netfilter
and **every bridged guest's traffic on the host is tracked from that moment**, including guests whose
NIC has `firewall=0`, even with `host.fw` `enable: 0`; on nftables nothing is tracked until the first
VM firewall on a node is enabled, and **then every bridged guest's flows on that node are tracked**,
again including `firewall=0` guests. A full conntrack table drops new connections for every VM on the
host, not only opted-in ones. epyc is already enabled (ACCEPT policies, a `fleet-tenant` group
from another project, `nftables: 1`), so epyc tells us nothing about this step.

Safe order, per cluster, executed by the operator (deploy agent) after go-ahead:

1. Read-only survey (done for production, 9.2a) and the pre-enable audit (9.2). The survey also
   reads, on every node, `net.netfilter.nf_conntrack_count`, `nf_conntrack_max` and the peak count
   over a busy period (`conntrack -S` or a few samples of the count), read only: no sysctl changes,
   and on hv-01, hv-21 and hv-22 nothing beyond reading. Each bridged flow costs one entry (a UDP
   entry lives about 30 to 120 s, an established TCP entry much longer). If a node's estimated peak
   with all its guests' flows tracked is above about half of `nf_conntrack_max`, the plan must say
   which comes first: raising `nf_conntrack_max` (and the hash size) on that node in a separate,
   approved change, or keeping that node's location off. The proposal to Kenzi states the numbers
   per node.
2. Neutralise pre-existing guest firewalls: for every guest the audit lists that Rackflow does not
   manage, write `enable: 0` in its VM firewall options (a no-op today, because the datacenter is off,
   so it changes nothing that runs). Guests whose owners really want those settings are opted in
   through Rackflow afterwards. This is a write to production VM configuration and needs Kenzi's
   approval like the rest of this list.
3. On **every** node first, write `host.fw` `[OPTIONS] enable: 0` (and `nftables: 1` if the nftables
   backend is chosen). With the host firewall off for the node, enabling the datacenter later should
   not filter host traffic; section 9.4 proves this on both backends before it is relied on.
4. In `cluster.fw` write `policy_in: ACCEPT`, `policy_out: ACCEPT`, `policy_forward: ACCEPT` and,
   on the iptables backend only, `ebtables: 0` unless the survey shows ebtables is wanted, **before**
   `enable`.
5. Set `enable: 1` in `cluster.fw`. Watch from a second session that SSH, 8006, corosync quorum,
   Ceph health and `nf_conntrack_count` stay green on every node for 10 minutes; the rollback is
   setting `enable: 0` again, which `pvesh` or the GUI can do from any node. On iptables the rollback
   leaves `net.bridge.bridge-nf-call-iptables` and `-ip6tables` at 1 (they were 0); new bridged flows
   are no longer tracked once the rules are gone, but to return the host fully to its earlier state
   also run `sysctl -w net.bridge.bridge-nf-call-iptables=0 net.bridge.bridge-nf-call-ip6tables=0` on
   each node (tested in 9.4; `br_netfilter` is built into the PVE kernel and cannot be unloaded).
   On nftables nothing is left behind.
6. Run Rackflow's readiness check and the pre-enable audit again (it must list nothing unmanaged);
   then enable the location in Rackflow.

**Backend: nftables (`proxmox-firewall`) recommended**, confirmed by the nested test (9.4): with
`host.fw` `enable: 0` the datacenter enable installs nothing and tracks nothing until a VM firewall is
switched on; the NIC flip is shorter (median 64 ms against 169 ms) and creates no `fwbr` devices; a
closed port still answers with a refusal; rollback leaves nothing behind. An earlier single-node
measurement on epyc suggested nftables tracks only the enabled VM's own flows; the controlled nested
test showed otherwise: once any VM firewall on a node is enabled, the `bridge proxmox-firewall-guests`
table tracks every bridged guest's flows on that node (300 of 300 for a `firewall=0` guest too). So
nftables delays the conntrack cost until the first opted-in VM on a node, but does not avoid it; the
conntrack check in step 1 applies to both backends. On production that is up to 851 VMs' traffic on
25G uplinks against a 262,144-entry conntrack table per node. Choosing nftables is the `nftables: 1` line in each node's `host.fw` in step 3, so the
production step writes `host.fw` with `enable: 0` and `nftables: 1` on all 23 nodes. Both daemons
running on every node is the normal PVE 9 install (epyc is the same): the `nftables` host option picks
which one generates rules, and the other stays idle, so they do not both manage rules. The option must
still be identical on every node of the cluster, and the readiness check (9.2) flags a node whose
`nftables` setting differs from the rest. The nested test
(9.4) still confirms the iptables behaviour so the choice is backed by a measurement both ways.

### 9.4 Proving the procedure first (nested test cluster on epyc)

Before any production cluster, Phase B builds a throwaway two-node nested PVE cluster on epyc (VMs in
an unused VMID range on the isolated `vmbr90`), and on it:

- runs steps 3 to 5 on the iptables backend and on the nftables backend and confirms host traffic,
  corosync and the API are unaffected (with a host interface carrying Ceph-like traffic);
- confirms VM firewalls work with `host.fw` `enable: 0` on both backends;
- measures the `net0` flag flip on the iptables backend (the `fwbr` path) like section 5.2;
- confirms whether `firewall=1` with the VM firewall disabled adds conntrack entries on the iptables
  backend (on nftables it does not, measured on epyc), and whether `br_netfilter` then tracks every
  bridged VM's traffic.

**Results (chat B0, 2026-11-23).** Full write-up, scripts and raw logs:
`/root/dcim/.forge/reviews/vm-firewall/b0-nested.md` and `b0-scripts/` (outside the repository).

The cluster: VMs 7910 (fwn1) and 7911 (fwn2) on epyc, cluster `b0nested`, PVE 9.2 with
`pve-firewall` 6.0.4 and `proxmox-firewall` 1.2.3 pinned to production's versions. qemu-server is
9.2.20, newer than production's (a full downgrade conflicted with the repository), which matters for
the NIC flip because it runs through qemu-server. Guests 101, 102, 103 on fwn1 and 201 on fwn2, all on
the plain bridge `vmbr0` (10.198.0.0/24); a separate host interface carried a steady Ceph-like TCP
stream between the nodes.

| Check | iptables | nftables |
|---|---|---|
| Steps 3 to 5 of 9.3 | Safe: 0 SSH, API or quorum failures, 0 lost pings, 0 stream stalls over 100 ms. Installs about 80 PVEFW rules and sets `bridge-nf-call-iptables` to 1 | Safe: same, and installs nothing at all. Two stalls of 103 and 108 ms during later NIC flips, judged noise, not ruled out |
| Contrast run (datacenter on with defaults) | Established flows and the node's local subnet survived; every new connection from other subnets (LAN SSH, API, Ceph-like network) and all ICMP blocked; revert from the guest-agent console path took 7 to 8 s | Same |
| VM firewall with `host.fw` `enable: 0` | Works: unlisted ports blocked, listed allowed, spoofed source dropped | Works |
| NIC flag flip on a running guest | Median 169 ms, worst 363 ms (19 flips); TCP stall up to 803 ms; `fwbr` devices come and go | Median 64 ms, worst 105 ms (20 flips); TCP stall about 215 ms; no extra devices |
| Conntrack (300 UDP flows, one entry per flow) | Every bridged guest tracked from the datacenter enable, including `firewall=0` guests | Nothing tracked until a VM firewall on the node is enabled, then every bridged guest on that node, including `firewall=0` guests |
| Closed port on a `firewall=1` guest with no VM firewall | Times out (reset dropped as INVALID) | Refused normally |
| Rollback (`cluster.fw` `enable: 0`) | Rules gone, no stall; `bridge-nf-call-iptables`/`ip6tables` stay 1; new flows not tracked; `sysctl -w ...=0` restores the earlier state | Rules gone, nothing left behind |

Consequences, already applied above: the flip figures (5.2, 8.2, 8.3), the conntrack check and the
iptables sysctl step (9.3), the closed-port behaviour (8.4), and nftables as the backend.

Further points from the test:

- The datacenter `management` IP set is the node's own local subnet. Rackflow's API access and any
  monitoring subnet must be inside it, or in an explicit allow rule, before any datacenter policy
  stricter than ACCEPT. With the 9.3 ACCEPT policies this does not arise.
- Privileges the worker needs on a cluster, tested with a dedicated token: role with `Sys.Audit`,
  `VM.Audit` and `VM.Config.Network` on `/`, plus `SDN.Use` on `/sdn/zones/localnetwork` (or the zone
  of the VM's bridge): a `net0` write, even of the firewall flag only, is refused without `SDN.Use`.
  That token can read everything readiness needs and write VM firewall options, rules and IP sets and
  the `net0` flag, and is refused for datacenter and node firewall writes, VM memory, stop and delete.
  Rackflow never writes `cluster.fw` or `host.fw` (9.1), so it never needs `Sys.Modify`; production
  should use a token with exactly these privileges.
- Not tested: production's older qemu-server, IPv6, guests on VLAN-aware bridges, real Ceph load.

**Kept for the end-to-end worker run.** Before the production procedure goes to Kenzi, Rackflow's real
worker runs end to end against this cluster: enable, a rule change, drift repair, rollback, an
ineligible VM and the paced rollout of 8.3. Conditions: a development Rackflow instance with its own
database, never production Rackflow or its database; the dedicated token `rackflow-fw@pve!worker` with
the privileges above, its secret only in `/root/rackflow-fw-token.json` on fwn1 (mode 600) and in the
development instance's settings, never in a chat, document or ledger. The cluster was left on the
nftables backend, firewall off (`cluster.fw` `enable: 0`, each `host.fw` `nftables: 1` and
`enable: 0`, every guest NIC `firewall=0`, no VM firewall files). API at `https://<fwn1 or fwn2>:8006`
with a self-signed certificate; the node addresses are DHCP leases on the LAN, listed in the B0
write-up with how to look them up again.

**Teardown.** The chat that runs the end-to-end test tears the cluster down right after its results
are recorded here:

1. On fwn1: `pveum user token remove rackflow-fw@pve worker; pveum user delete rackflow-fw@pve;
   pveum role delete RackflowFW; pveum role delete RackflowFWNet; shred -u /root/rackflow-fw-token.json`,
   and delete the secret from the development instance's settings.
2. On epyc: `qm stop 7910; qm stop 7911; qm destroy 7910 --purge 1; qm destroy 7911 --purge 1`
   (no `--destroy-unreferenced-disks`), then check that `ls /etc/pve/qemu-server/791*.conf` and
   `zfs list | grep vm-791` print nothing. The guests live inside these two VMs and go with them.
3. On the project host: remove the test SSH key copies listed in the B0 write-up.

## 10. Surfaces and API

All writes return the full firewall view (with the new `revision`) so callers never re-fetch. Lists
are bare JSON arrays. Domain errors share one body, `{code, field, message, detail}` (`detail` is the
message, for WHMCS): `firewall_changed` (409) adds `current_revision`, `not_eligible` (409) adds
`reasons[]`, `confirmation_required` (409) adds `preview`. Turning the firewall on while net0's
`firewall` flag is not 1 answers 409 `interruption_confirmation_required` (field
`confirm_interruption`) until `confirm_interruption=true` is sent; the route reads the VM's live
Proxmox config to decide (and passes it to `eligibility` so the NIC checks run). When the config
cannot be read the flag counts as unknown and the confirmation is required, even if the stored
`nic_flag` is `on`. Eligibility is checked first, so in a location whose firewall is off, turning it
on answers 409 `not_eligible` (`location_disabled`) for client, billing and staff alike, with or
without the confirmation; other edits there are stored as preferences and nothing is written to
Proxmox (staff `apply` is refused too, unless the VM was applied before and can only be switched
off). Reads never write: a VM or location with no row yet is shown with the defaults its first
write starts from. A missing id and an object the client or billing actor may not use (another
client's, or a non-selectable admin group or IP set) answer identically. Unknown request fields,
the admin-only ones among them, are refused with 422 on the client and billing routes. Every write
route leaves a `firewall.*` audit row (staff `apply` writes `firewall.apply_requested`); linking a
service to a new owner through billing `/link` removes rules referencing the old owner's objects.

### 10.1 Client API (`app/api/vm_firewall_client.py`)

| Route | Purpose |
|---|---|
| `GET /api/client/services/{id}/firewall` | `{service_id, available, reasons[], location_enabled, client_enabled, state, enforced, nic_flag, needs_interruption, policy_in, policy_out, log_level_in, log_level_out, rules[], groups[], ipsets[], baseline: {anti_spoof, mac_filter, smtp_out, management_blocked, client_may_disable, rules[] (read only)}, smtp_block_help, limits, suggested_presets[], revision, last_applied_at, message}` |
| `PUT /api/client/services/{id}/firewall` | `{revision, client_enabled?, policy_in?, policy_out?, log_level_in?, log_level_out?, confirm_interruption?}` |
| `PUT /api/client/services/{id}/firewall/rules` | Replace the whole ordered list `{revision, rules:[...]}` (group references included) |
| `POST .../firewall/rules` `{revision, rule, position?}`, `PUT .../rules/{rule_id}` `{revision, rule}`, `DELETE .../rules/{rule_id}?revision=` | Single rule edits, `revision` required |
| `POST .../firewall/rules/order` | `{revision, ids:[...]}` |
| `POST .../firewall/presets/{preset}` | `{revision}`; `ssh`, `rdp`, `web`, `ping`, `allow_all` (appends rules; `allow_all` sets `policy_in ACCEPT`) |
| `POST .../firewall/groups/{group_id}` `{revision, position?}`, `DELETE .../firewall/groups/{group_id}?revision=` | Attach or detach a group |
| `GET /api/client/firewall/presets` | `[{key, label, description, rules[], policy_in}]` |
| `GET/POST /api/client/firewall/groups`, `GET/PUT/DELETE /groups/{id}` | Groups with their rules (PUT replaces rules); delete refused while attached unless `?detach=true`. The list includes client-selectable admin groups; GET/PUT/DELETE of one reach only the client's own |
| `GET/POST /api/client/firewall/ipsets`, `GET/PUT/DELETE /ipsets/{id}` | IP sets with entries, same rules as groups |
| `GET /api/reseller-panel/services/{id}/firewall` | The client view, read only, for a service whose `ServiceBilling.reseller_id` is the reseller |

Rule items: `{id, position, kind: "rule"|"group", enabled, comment}` plus, for a group reference,
`group_id`, and otherwise `direction, action, ip_version, protocol, ports, source_ports, icmp_type,
remote_cidrs, remote_ipset_id, local_cidrs, log`. Groups: `{id, name, description, scope,
owner_user_id, client_selectable, revision, rules[]}`. IP sets: `{id, name, description, scope,
owner_user_id, client_selectable, purpose, revision, entries: [{id, cidr, nomatch, comment}]}`. The
view's `ipsets[]` holds `{id, name, scope, purpose, description}` of the sets a rule may reference.
Baseline rules are the admin rules (with `baseline_id`, `generated: false`) and then the generated
SMTP and management blocks (`generated: true`). `smtp_block_help` is `SMTP_BLOCK_HELP` from
`service.py` when the merged baseline blocks port 25, otherwise null. Groups and IP sets routes need
a client session (not an API key); writes on service routes need `vm.firewall`. The client service
detail (`GET /api/services/{id}`) carries `firewall_available`.

### 10.2 Admin API (`app/api/vm_firewall_admin.py`, `/api/admin/vm-firewall`)

| Route | Purpose |
|---|---|
| `GET /locations`, `GET /locations/{id}` | `{location_id, name, enabled, nic_flag_on_provision, enforce_baseline_on_all, smtp_block_new_vms, anti_spoof_new_vms, ipv6_allowed_prefixes[], readiness: {clusters: {<id>: {ready, reasons[], datacenter, nodes, name}}}, readiness_checked_at, clusters: [{id, name, readiness}], ready, ipv6_allowed_prefixes_help}` |
| `PUT /locations/{id}` | Any settings field, `staging?`, `confirm_existing_vms?`; 409 `confirmation_required` with `preview` when the change reaches existing VMs, 409 `not_ready` when enabling an unready location without `staging` |
| `POST /locations/{id}/readiness` | Re-run the read-only readiness check of every cluster; returns the location |
| `POST /locations/{id}/rollout` | `{dry_run (default true), confirm_existing_vms?}` returns `{dry_run, preview: {changed, service_ids[], services: [{id, name}], ...}, location}`; a real run switches `enforce_baseline_on_all` on |
| `POST /locations/{id}/rollout/resume` | Resume a rollout the worker paused; returns the location (items also carry `rollout_paused_at`, `rollout_pause_reason`, `rollout_failures`) |
| `GET /worker` | The worker's last status from `system_settings` `vm_firewall_worker` (`{}` before its first tick) |
| `GET/POST /baselines`, `GET/PUT/DELETE /baselines/{id}` | `{id, name, scope_type, scope_id, enabled, settings, revision, rules[]}` |
| `GET/POST /groups`, `GET/PUT/DELETE /groups/{id}` | Admin groups and any client's (`?scope=admin\|client&owner_user_id=`); create takes `owner_user_id?`, `client_selectable` |
| `GET/POST /ipsets`, `GET/PUT/DELETE /ipsets/{id}` | As groups, plus `purpose` (`general`, `management`; also a list filter) |
| `GET /services?state=&location_id=&q=&offset=&limit=` | `[{service_id, service_name, owner_user_id, owner_username, location_id, location_name, cluster_id, node, vmid, state, client_enabled, managed, excluded, nic_flag, revision, applied_revision, last_applied_at, last_error}]`; a VM never opened shows `off` |
| `GET/PUT /services/{id}` | The client view plus `managed, excluded, enforce_baseline, admin_overrides, location_id, cluster_id, cluster_ready, nics_checked, net0_bridge, rollout_after, applied_revision, desired_hash, applied_hash, in_sync, last_error, failing_since, last_checked_at, next_attempt_at, drift_repairs_24h, rendered: {options, rules[]}`; PUT takes the client fields and the four admin ones under one `revision`. Staff see the firewall of a VM in a staging location (`available` true) |
| `PUT/POST /services/{id}/rules`, `PUT/DELETE /services/{id}/rules/{rule_id}`, `POST /services/{id}/rules/order`, `POST /services/{id}/presets/{key}`, `POST/DELETE /services/{id}/groups/{group_id}` | As the client routes, answering the staff view |
| `POST /services/{id}/apply` | Apply now: `{result: {ok, state, ...}, firewall: <staff view>}`; 502 when Proxmox is unreachable |
| `POST /services/{id}/check` | Drift check now, writes nothing: `{in_sync, differences[], ...}`; 404 `firewall_not_found` when the VM has no firewall row |

### 10.3 Billing API for WHMCS (`app/api/billing_vm_firewall.py`)

`GET/PUT /api/billing/services/{id}/firewall`, `PUT .../firewall/rules`, `POST .../firewall/rules`,
`DELETE .../firewall/rules/{rule_id}?revision=`, `POST .../firewall/presets/{preset}`,
`GET .../firewall/groups` (the owner's groups and client-selectable admin groups, to attach). The
request and response shapes are the client ones. A service of another integration answers 404
`Service not found`; writes need the owner's `vm.firewall`. Every route is in
`tests/contract/whmcs_routes.py`, and `/status` carries `firewall_available`.

### 10.4 Admin and client UI

- `frontend/src/components/firewall/FirewallEditor.svelte`: one shared component with an `audience`
  prop (`staff`, `customer`), used by a panel in `VMServiceDetail.svelte` and a new Firewall tab in
  `client/VmServiceView.svelte` (tab id added in `ClientServiceDetail.svelte` `buildTabs`). Rule table
  with drag or up and down ordering, enable toggles, presets menu, policy selectors, the baseline shown
  read only above the client rules, state badge and the interruption confirmation.
- Admin page `/admin/vm-firewall` (`VmFirewall.svelte`, dynamic import): locations with readiness,
  baselines, admin groups and IP sets, status list with filters.
- Client page `/client/firewall`: the client's groups and IP sets.
- API helpers in `frontend/src/lib/vmFirewallApi.js`.

### 10.5 WHMCS

The plugin panel renderer (`rackflow_renderPluginPanels`) only draws fields, badges, text, dividers and
action buttons; a rule table with ordering does not fit it. So the module gets a native section:

- `rackflow_ClientArea` fetches `GET /api/billing/services/{id}/firewall` when `/status` reports
  `firewall_available` (a new field) and passes it to `clientarea.tpl`, which renders the rule table,
  policies, presets, enable switch and group attach list;
- edits go to a new AJAX endpoint `firewall_action.php` (same checks as `plugin_action.php`: POST only,
  AJAX header, owner or admin session, `rackflow` server type), which calls the billing routes;
- the admin service tab shows the state and a link to the Rackflow admin page;
- group and IP set editing stays in the Rackflow portal (SSO link), open question Q8.

### 10.6 Client MCP

No new tool: `services` gets `view="firewall"` (needs `service_id`) and `service_action` gets
`action="firewall"` with `op` in `enable`, `disable`, `policy`, `add_rule`, `delete_rule`,
`replace_rules`, `preset`, `attach_group`, `detach_group`, calling the client route handlers so
ownership, permissions and audit match the portal (`app/mcp/client/firewall.py`, dispatched like
`networks.py`). Scope `write`; `disable` and `replace_rules` need `confirm=true`.

### 10.7 Staff MCP

Today `tools/list` is 67 tools and 37,319 bytes against the 38,000-byte limit (measured 2026-11-23),
so there are about 680 bytes free. One new tool `firewall` with a description and schema of at most
550 bytes: `action` in `get`, `set`, `apply`, `check`, `status`, `readiness`; `service_id`,
`location_id`, `enabled`, `policy_in`, `policy_out`, `rules` (JSON list), `confirm`. Baselines and admin
groups are managed in the admin UI only. The budget tests (`tests/mcp/test_alerts_tool.py:62`,
`tests/mcp/test_private_network_dhcp_mcp.py:103`) move to 68 tools and must still pass under 38,000;
if the final schema is larger, the `storage` tool's description (2,152 bytes, the largest) is trimmed
to make room rather than raising the budget.

## 11. Audit, notifications and alerts

Every change writes an `AuditService.log` row in the same transaction, `subject_user_id` = the service
or object owner, `resource_type` `service`, `firewall_group`, `firewall_ipset`, `firewall_baseline` or
`location`:

`firewall.enabled`, `firewall.disabled`, `firewall.policy_changed`, `firewall.rule_created`,
`firewall.rule_updated`, `firewall.rule_deleted`, `firewall.rules_replaced`, `firewall.rules_reordered`,
`firewall.preset_applied`, `firewall.group_attached`, `firewall.group_detached`,
`firewall.group_created`/`_updated`/`_deleted`, `firewall.ipset_created`/`_updated`/`_deleted`,
`firewall.baseline_created`/`_updated`/`_deleted`, `firewall.location_updated`,
`firewall.rollout_started`, `firewall.admin_override_changed`, `firewall.references_removed`,
`firewall.drift_repaired` (system), `firewall.managed_changed`.

Metadata holds the before and after of what changed (rules as their validated fields). Client
notifications: a new category `firewall` (opt-in like the others) for changes made by staff, the
firewall being enabled or disabled, and a failure to apply (after the staff alert threshold).

Alert types (catalogue plus detectors reading `service_firewalls` and the readiness cache):

| Key | Severity | When |
|---|---|---|
| `vm_firewall_apply_failed` | warning | A VM has been `error` for more than 15 minutes (param) |
| `vm_firewall_drift_repeated` | warning | More than 3 drift repairs on one VM in 24 h (someone edits it on Proxmox) |
| `vm_firewall_cluster_not_ready` | critical | An enabled location's cluster or node fails readiness, so firewalls are not enforced |
| `vm_firewall_worker_stale` | warning | No completed drift sweep for 3 intervals |

## 12. Migration

One revision `vm_firewall` (file `alembic/versions/vm_firewall.py`), `down_revision` = the single head
of `main` when the branch is merged (today `billing_p2_email_money`; the billing programme's phase 3
adds revisions on its own branch, so whichever merges second re-points its `down_revision` so there is
one head). It only creates the tables of section 4 and their indexes; no existing table changes.
Plain `op.create_table`, `sa.JSON`, string enums, foreign keys left out on SQLite as the recent
revisions do (`private_network_dhcp.py`), a check constraint for "exactly one owner" on MariaDB only.
Downgrade drops the tables. The settings are code defaults, so no data migration. Tests: upgrade and
downgrade on SQLite in `tests/migrations/test_vm_firewall_migration.py`, schema parity with the models,
and the single-head assertion; MariaDB through the existing optional job.

## 13. Tests

- Unit: validation (ports, CIDRs, families, limits), renderer (merge order, baseline first, groups in
  place, disabled rules, client off means baseline only), planner (single op direct, guard placement
  and removal, IP sets before rules, enable last and first), the "never writes cluster or node" URL
  test.
- A fake Proxmox firewall (in-memory `.fw` model with positions, digests, IP sets, the `net0` config and
  pending changes) in `tests/fakes/proxmox_firewall.py`, used by the worker tests: apply queue, claims,
  coalescing, backoff, drift repair, unmarked rules removed, digest conflicts, `vm_missing`,
  readiness flips to `not_enforced`, crash mid-apply leaves the guard.
- Tenancy: client A cannot read or change client B's VM firewall, groups or IP sets (404); cannot
  attach B's group or reference B's IP set; billing integration X cannot reach integration Y's
  service; reseller panel sees only its own clients; `vm.firewall` false blocks writes and not reads;
  admin-only fields refused on client and billing routes; ownership change removes foreign references.
- API, MCP (both servers, budget test), alerts detectors, audit rows per action, deployment step
  ordering in every strategy, WHMCS contract tests and `php -l`, `npm run build`.
- Live, on epyc only: throwaway VMs created and deleted by the test, on `vmbr90`, through a dedicated
  Proxmox API token limited to a pool holding those VMs; the full path from the Rackflow service layer
  to enforcement (blocked and allowed connections, spoofing, presets, drift repair after a manual
  edit, reinstall re-apply). Never on hv-01, hv-21 or hv-22.

## 14. Build plan (Phase B)

| Step | Work | Files (owner) | Depends on |
|---|---|---|---|
| B0 | Production survey (requested), nested two-node PVE test cluster on epyc and the 9.4 experiments | ops, this doc | go-ahead |
| B1 | Models, migration, settings, validation, presets, renderer, planner, Proxmox writer, fake Proxmox | `app/models/vm_firewall.py`, `alembic/versions/vm_firewall.py`, `app/services/vm_firewall/`, `tests/fakes/proxmox_firewall.py`, `tests/services/test_vm_firewall_*` | |
| B2 | Worker, deployment step, lifecycle hooks, alert types and detectors, notifications | `app/workers/vm_firewall.py`, `app/services/deployment/{steps,strategies}.py`, `vm_ip_reassign.py`, `alerting/`, `client_notifications.py` | B1 |
| B3 | Client, admin, billing and reseller APIs, permission key, audit | `app/api/vm_firewall_*.py`, `app/api/billing_vm_firewall.py`, `client_permissions.py`, `main.py`, contract route list | B1 |
| B4a | Frontend (admin page, shared editor, client tab and page) | `frontend/src/components/firewall/`, `VmFirewall.svelte`, `client/`, `lib/vmFirewallApi.js` | API shapes in section 10 (can start with B3) |
| B4b | Client MCP, staff MCP, WHMCS section and `firewall_action.php` | `app/mcp/client/firewall.py`, `app/mcp/tools/firewall.py`, `whmcs/modules/servers/rackflow/` | B3 |
| B5 | Opus high-effort review of enforcement, tenancy and the worker; live tests on epyc; `CODEBASE.md` rows | | B2 to B4 |

B1 runs alone first (it fixes the interfaces). Then B2, B3 and B4a in parallel on separate files, then
B4b. Full checks after each merge: `pytest -n auto`, `pytest examples`, the WHMCS contract tests,
`alembic heads`, `npm run build`, `php -l`.

## 15. Bare metal later

Out of scope. The rule model (direction, protocol, ports, remote CIDRs or IP sets) is backend neutral
and `service_firewalls.backend` names the enforcer. A bare-metal backend could render the same rules
as switch ACLs on the server's access port (NX-OS port ACLs through the `nxos` plugin) or as an
nftables ruleset pushed to an agent; only the renderer, the writer and the drift read differ. The
anti-spoofing part maps to switch IP source guard.

## 16. Open questions

Recommended defaults are what will be built unless told otherwise.

**Kenzi's decisions**

| # | Question | Recommended default |
|---|---|---|
| Q1 | Price: free for every VM, or a paid add-on? | **Decided by Kenzi, 2026-11-23: free for every VM, no billing hook** |
| Q2 | May clients switch off baseline protections (anti-spoofing, SMTP block)? | No. Admins can relax them per service (`admin_overrides`) or per product |
| Q3 | Outbound SMTP (25) block: default for new VMs, and for existing ones? | **Decided by Kenzi, 2026-11-23: block outbound port 25 by default on new VMs in firewall-enabled locations; staff can unblock per service; 465 and 587 stay open; existing VMs unchanged.** Built as in 4.5a |
| Q4 | Enabling the datacenter firewall on production cluster `pve-06` after the nested test, and which backend | nftables backend (production runs PVE 9.2.3 with `proxmox-firewall` already installed and running), following 9.3 exactly: `host.fw` with `enable: 0` and `nftables: 1` on all 23 nodes, ACCEPT policies, then `enable: 1`; done by the deploy agent with your approval, in a quiet hour, with the rollback ready. Proven on the nested cluster (9.4). The proposal includes the read-only conntrack numbers per node (9.3 step 1) and, if a node's margin is small, whether `nf_conntrack_max` is raised first or that location stays off |
| Q4a | 46 production guests already have `enable: 1` in their VM firewall file and 22 have `firewall=1` on a NIC; those with both would start filtering (default inbound DROP) the moment the datacenter firewall is on. Switch their VM firewall off first (9.3 step 2)? | Yes: write `enable: 0` on every guest the pre-enable audit lists that Rackflow does not manage (a no-op today), and tell the owners they can opt in through Rackflow |
| Q5 | Anti-spoofing baseline on every VM in a location (each existing VM gets one blip, under half a second as measured in 9.4), or only VMs whose owners opt in? | Opt-in VMs only at first; a staged rollout to all VMs later, announced to clients |
| Q6 | Do production public networks carry IPv6 that VMs use today? | Answered 2026-11-23 (9.2a): no on `vmbr0` (no router adverts, no global IPv6 in use), so anti-spoofing with IPv4 plus the link-local address breaks nothing; `ndp` stays allowed. `vmbr750` does have router adverts: a VM NIC there is refused by eligibility until a per-location "allowed IPv6 prefix" is configured |
| Q7 | `vm.firewall` permission on by default for every VM client, including resellers' clients? | Yes; resellers can turn it off through their product permission ceiling |
| Q8 | WHMCS: full editor in the client area, or rules and presets in WHMCS with groups and IP sets in the Rackflow portal? | The latter (section 10.5) |

**Defaults chosen here (say if you disagree)**

- Client default policies inbound DROP, outbound ACCEPT; first-time rules proposed from the OS.
- Logging off (`nolog`), not client-adjustable until `firewall_client_logging` is turned on.
- Limits as in 4.6; drift repaired automatically every 10 minutes; unmarked rules on managed VMs removed.
- Private network and fast path NICs never filtered; a per-network option may come later.
- `macfilter` on with anti-spoofing; staff can turn it off per service for clients who run their own
  bridges or virtual MACs on the public NIC.
- The management-network IP set is empty until an admin fills it (the hypervisor, BMC, switch and
  Rackflow networks); the admin UI warns about overlaps with VM IP pools and storage SFTP addresses.
- Proxmox comments carry only `rf:` markers; client comments stay in Rackflow.
