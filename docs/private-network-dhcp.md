# Private networks: storage presence and DHCP

Status: design, 2026-11-20. Applies from storage runner 0.4.0 and migration `private_network_dhcp`.

Kenzi asked for two things: the storage runner is always present on every private network, whether
or not storage is linked, and that presence serves DHCP on the network. The network owner configures
DHCP per network: on or off, reservations, an optional dynamic range, and a small set of options.
Read `docs/private-networks.md`, `docs/private-network-sharing.md` and `docs/nfs-storage-failover.md`
first; this document only describes what changes.

## 1. Summary

- Every **standard** private network in a location with storage nodes gets a **presence**: the
  network's reserved storage address (`storage_endpoint_ip`, the last usable address) answered by
  exactly one storage node at a time, with a fixed MAC, under a RADOS lease like a storage tenant.
- When storage is linked, the storage tenant **is** the presence: same address, no renumbering.
  When no storage is linked, a lightweight **network presence** (no Ceph, no NFS) holds it.
- The serving node runs a **DHCP server** for the network inside the presence's namespace. By default
  it hands each attachment exactly the address Rackflow assigned, keyed by MAC, and nothing else:
  unknown MACs get no answer, no router and no DNS servers are sent.
- The **owner** can turn DHCP off, add MAC reservations for machines Rackflow does not manage, add a
  dynamic range for unknown MACs, and set a gateway, DNS servers, a search domain, the lease time,
  the MTU, static routes (option 121) and NTP servers. Members see the effective settings only.
- Cloud-init VMs keep their static `ipconfig<n>`; DHCP is for everything else (Windows, hot-added
  cards, templates without cloud-init, owner appliances, dedicated servers' tagged interfaces).

## 2. Presence model

### 2.1 Two kinds of presence

| Network state | Presence | Lease | Namespace and VLAN link | MAC | Ceph |
|---|---|---|---|---|---|
| Storage linked (live tenant) | the tenant | `tenant.<tenant_id>` (unchanged) | `rf-t<tenant>` / `rf-v<tenant>` (unchanged) | `02:52:46` + tenant id (unchanged) | yes |
| No storage linked | network presence | `net.<network_id>` | `rf-p<network>` / `rf-w<network>` | `02:52:4e` + network id as 24 bits | none |

A network presence has only the VLAN interface with the endpoint address: no veth, no routes, no
firewall entry, no container, no Ceph user. It cannot reach Ceph or the node.

**Decision: keep the tenant as its own unit and add a network unit beside it**, rather than making
the network the unit for everything. Making the network the unit would rename the namespace, the
lease object, the fixed MAC, the Ganesha hostname and the recovery namespace of every live tenant
(dev's shared NFS, production's tenants), which needs a coordinated cut-over with an NFS pause on
both installations, while production failover is being tested. The additive design leaves every
existing tenant exactly as it is; the cost is one hand-over between two lease keys when storage is
linked or terminated, which section 2.3 makes safe.

### 2.2 Which networks, which nodes

- Networks: `kind = standard`, `status = active`, in a location where presence is active (2.5).
  Proxy access (fast path) networks are **out of scope**: they are joined by every proxy runner, use
  a fixed range with a reserved proxy block, and their clients are configured from the proxy side;
  adding a DHCP server there is neither trivial nor needed.
- Nodes eligible to serve a network presence: enabled, not draining, `vlan_mode = preconfigured`.
  A `switch` mode node only carries the VLANs of storage it hosts (Rackflow trunks them per
  attachment), so it cannot serve presences for other networks; trunking every network to every
  switch mode node is out of scope. A location whose nodes are all in `switch` mode gets no network
  presences; its tenants still serve DHCP for their own networks. A `preconfigured` node must carry
  the pool's whole VLAN range (as a proxy runner does); failover between preconfigured nodes already
  relies on this for tenants.
- Preferred node of a network presence: the eligible nodes sorted by id, index `network_id mod n`.
  Takeover order: the preferred node, then the other eligible nodes sorted by id and rotated by
  `network_id mod (n - 1)`. Nothing is stored; adding a node can move a presence by hand-off.

### 2.3 Exactly one presence at a time

Inside one node the kernel refuses a second VLAN device with the same VLAN id on the same parent,
and the runner never ensures a network presence whose VLAN is used by a non-terminated tenant in its
config or by a tenant namespace still on the node. Across nodes:

- **Storage linked (network presence to tenant).** Rackflow stops listing the network presence as
  soon as storage is linked, so its holder releases `net.<N>` on its next pass (stop, mark
  released, unlock). The tenant's config entry carries `network_id`, and the runner **never acquires
  `tenant.<T>` while `net.<N>` has any locker** (a new `Defer` list on the lease want, checked with
  `list_lockers` just before acquiring). If the holder died, `net.<N>` expires after
  `lease_seconds` as usual. Storage therefore becomes ready one pass (about 30 s) later than today.
- **Storage terminated (tenant to network presence).** A terminated tenant's lease stays held by its
  destroyer for the grace period without serving, so a lease check cannot be used. Rackflow lists the
  network presence again only once no tenant of the location on that VLAN has a current node
  (`storage_tenants.current_node_id`, cleared when the old holder reports `serving: false` or goes
  offline). A terminated tenant is torn down at once by its holder, so this is one pass.
- **Rackflow unreachable.** Runners keep the last config (as today). A node holding `net.<N>` from a
  stale config keeps it; storage linked meanwhile waits in "provisioning" until that node sees the
  new config.

### 2.4 Leases, takeover and fencing for network presences

Same lock, timings, renewals, local deadline, guard file, hand-off, move-free takeover order and
draining rules as tenants (`docs/nfs-storage-failover.md` sections 3 to 6), with one difference:
**a network presence is never fenced.** It has no Ceph state, so blocklisting a node (which cuts
off every tenant it serves and can fail under a scoped key) to take over a DHCP responder would be
out of proportion. Exclusivity still holds: the holder stops at its local deadline (14 s) and the
guard sets the link down at 17 s, before the OSD frees the lease at 20 s; the next holder waits its
rank delay, plus `lease_seconds` more while the previous owner's record is fresh. A takeover event
for a network presence is reported with `network_id` instead of `tenant_id`.

### 2.5 When presence and DHCP are active

Presence is active for a location when it has at least one enabled storage node and **every enabled
node reports runner 0.4.0 or later** (`storage_ha.parse_version`, as the failover gate does). Then:

- each node's config carries `networks` (2.2) and each tenant entry carries `network_id` and `dhcp`;
- with failover active, every node gets every presence of the location with a takeover order; with
  failover not active (`ha_enabled` off), each presence goes only to its preferred node with
  `takeover_order: [that node]`, exactly like tenants.

Older runners get no new keys. A mixed location (some nodes older) gets no presences and no tenant
DHCP at all, so a failover never moves a network from a node that answers DHCP to one that does not.

## 3. DHCP behaviour

### 3.1 Server

An in-process DHCPv4 server in the runner (`internal/dhcp`), one UDP socket per served presence or
tenant, opened inside the namespace (a locked OS thread enters it with `setns`), bound to
`0.0.0.0:67` with `SO_BINDTODEVICE` on the VLAN interface and `SO_BROADCAST`. It is started under
the lease (`whileHolding`) after the interface is up, and stopped by `StopLease` together with
the link going down. The guard needs no change: a link that is down sends nothing.

**Decision: in-process rather than dnsmasq.** No new host package or process to adopt after a
runner restart, the packet logic is a pure function that is unit tested without a kernel, lease
state is reported straight to Rackflow (3.4), and a configuration change swaps the server's table
atomically with no socket restart. Kernel-level behaviour is covered by a `sandbox` tagged test
like the existing netns and firewall ones.

### 3.2 Who gets what

For each request, in this order:

1. `giaddr` not zero (relayed): ignored. These are flat layer 2 networks with no relays.
2. DHCP off for the network: ignored (the presence still answers ARP for its address).
3. MAC in the **reservation table** (attachments plus owner reservations, 4.3): that address.
4. Dynamic range configured: a dynamic lease (3.4).
5. Otherwise: ignored. An unknown MAC gets nothing, so default DHCP can never clash with a
   statically configured guest.

Messages: `DISCOVER` gets `OFFER`; `REQUEST` gets `ACK`, or `NAK` when a known client (reserved MAC,
or a dynamic client) asks for an address that is not the one it would get (RFC 2131 4.3.2: selecting
with another server's identifier is ignored; init-reboot and renewing requests are checked against
the table); `INFORM` from a reserved or leased address gets `ACK` with options and no address;
`RELEASE` frees a dynamic lease; `DECLINE` quarantines a dynamic address for 10 minutes.
Replies go unicast to `ciaddr` when it is set, else broadcast to `255.255.255.255:68` on the
interface. The server identifier is the endpoint address.

### 3.3 Options

Always: 1 (subnet mask), 51 (lease time), 58 and 59 (T1 = 50 %, T2 = 87.5 %), 54 (server id).
Only when the owner sets them: 3 (router), 6 (DNS servers), 15 (domain name), 26 (interface MTU),
42 (NTP servers), 121 (classless static routes). When both a router and static routes are set,
`0.0.0.0/0` via the router is added to option 121, because RFC 3442 clients ignore option 3 when 121
is present. No other option is ever sent, and there are no raw option numbers.

**Default lease time: 86 400 s (one day)**, setting `PRIVATE_NETWORK_DHCP_LEASE_SECONDS`, owner
range 300 s to 604 800 s. Reservations never change, so a long lease only means fewer renewals and,
more importantly, that a guest keeps its address for a day if every storage node of the location
is down; many clients drop the address when the lease runs out.

### 3.4 Dynamic leases and failover

Dynamic leases live in the serving node's memory and in `STATE_DIR/dhcp/<net|tenant>-<id>.json`,
are reported in every status post, stored by Rackflow (`private_network_dhcp_leases`) and sent
back to **every** node of the location in the config (`dhcp.leases`). A node that takes over seeds
its table from the config and its own file. Two more rules cover what Rackflow had not heard yet:

- before offering a dynamic address not in its table, the server probes it with duplicate address
  detection (`arping -D -c 2 -w 1` in the namespace); an address that answers is quarantined;
- a renewing or rebinding client asking for an address inside the range that no other MAC holds and
  no reservation uses is accepted and recorded, so clients keep their addresses across a failover.

**Decision: Rackflow plus probing rather than a RADOS object.** Writing lease state through the
lease helper would put DHCP traffic on the one Python client whose response time decides whether
storage stays up; Rackflow is already the place every node reads from, the owner can see the lease
list in the portal, and the probe and the renewal rule close the window of up to one status
interval during which a lease is only known to the node that gave it. Expired leases are pruned by
Rackflow and by the runner.

### 3.5 Changes take effect without restarting storage

A config change (attach, detach, owner settings) changes the DHCP table on the serving node on its
next pass (at most `POLL_INTERVAL`, 30 s); the table is swapped in place. Ganesha's configuration
and container hash do not include DHCP, so NFS is never restarted for a DHCP change. A detach drops
the reservation on the next pass; a client that still holds the lease keeps the address until it
renews, then gets a `NAK` (its MAC is no longer known) or nothing.

## 4. Rackflow

### 4.1 Data model (migration `private_network_dhcp`, single head)

`private_networks` gains:

| Column | Type | Meaning |
|---|---|---|
| `dhcp_enabled` | bool, default true | DHCP served on this network |
| `dhcp_range_start`, `dhcp_range_end` | string(15), null | Dynamic range, both or neither |
| `dhcp_lease_seconds` | int, null | null means the setting's default |
| `dhcp_options` | JSON, null | `{gateway, dns_servers, domain, mtu, routes: [{destination, gateway}], ntp_servers}` |
| `presence_node_id` | FK `storage_nodes` (SET NULL), null | Node that reports serving the network presence |
| `presence_since` | datetime, null | |
| `dhcp_error` | string(2000), null | Last DHCP error from the serving node |

New `private_network_dhcp_reservations`: `id`, `network_id` (cascade), `mac` (lower case,
colon-separated), `ip`, `label` (up to 64 characters, optional), `created_at`, `updated_at`; unique
(`network_id`, `mac`) and (`network_id`, `ip`).

New `private_network_dhcp_leases`: `id`, `network_id` (cascade), `mac`, `ip`, `expires_at`,
`hostname` (up to 64, control characters removed), `node_id` (SET NULL), `updated_at`; unique
(`network_id`, `mac`).

### 4.2 Validation (server side, on every change)

- **Range:** both ends inside the CIDR, usable (not network or broadcast), start not after end, not
  the endpoint, and it may not contain an attachment address, an owner reservation, the gateway, an
  address listed on a restricted share, or (for proxy reasons) anything on a proxy access network
  (refused outright). At most 4096 addresses.
- **Owner reservation:** MAC is six octets, unicast (first octet even), not all zero, not
  `02:52:46:*` or `02:52:4e:*` (presence MACs); unique in the network, and not the MAC of an
  attachment (Rackflow's own reservations cannot be overridden or given away). IP usable in the
  CIDR, not the endpoint, not an attachment address, not inside the dynamic range, not already
  reserved, and not an address listed on a restricted share (Kenzi's rule: a share's trusted address
  is never handed to a MAC by a reservation). Listing a reserved address on a share afterwards is
  allowed: the owner named that MAC. At most 256 reservations per network.
- **Gateway:** a usable address in the CIDR, not the endpoint, not inside the dynamic range. It may
  be an attachment or a reservation (a client router VM is the usual case).
- **DNS and NTP servers:** up to three IPv4 unicast addresses each (not 0/8, 127/8, multicast,
  broadcast); they may be outside the network.
- **Domain:** a DNS name up to 253 characters, labels of letters, digits and hyphens.
- **MTU:** 576 to 9000. **Lease time:** 300 to 604 800 s.
- **Static routes:** up to 16; destination a valid IPv4 network (not 0.0.0.0/0, which is what the
  gateway is for), gateway a usable address in the CIDR other than the endpoint.
- **Attachments:** `_pick_ip` skips owner reservations, the dynamic range and the gateway is *not*
  skipped (it may be the owner's own router VM, attached with that address on purpose); a requested
  address inside the range or reserved by the owner is refused with 409.
- **Shares:** a restricted share may not list an address inside the dynamic range.

### 4.3 The reservation table sent to runners

Built per network on every config request:

1. Attachments not in `detaching`: a VM's `mac_address` (from `attach_private_nic`); a dedicated
   server's PXE port MAC and the MACs of ports in the same LAG group (`network_ports.mac_address`),
   all mapped to the attachment's address. A Linux or Windows VLAN interface uses its parent's MAC
   (a bond's MAC is one of its members') unless set by hand, so the client's tagged interface asks
   with one of these. Storage node and proxy runner attachments get none.
2. Owner reservations.
3. A MAC that would map to two different addresses is dropped entirely (both entries) and shown to
   staff as a warning on the network; DHCP never guesses.

### 4.4 Runner config additions (0.4.0 and later only)

```json
{
  "networks": [
    {
      "id": 6, "vlan": 3921, "network_cidr": "10.0.0.0/24", "endpoint_ip": "10.0.0.254", "prefix_len": 24,
      "preferred_node_id": 2, "takeover_order": [2, 3],
      "dhcp": {
        "enabled": true,
        "lease_seconds": 86400,
        "reservations": [{"mac": "bc:24:11:00:00:01", "ip": "10.0.0.1"}],
        "range": {"start": "10.0.0.100", "end": "10.0.0.199"},
        "leases": [{"mac": "bc:24:11:00:00:09", "ip": "10.0.0.100", "expires_at": "2026-11-21T10:00:00Z"}],
        "options": {"router": null, "dns_servers": [], "domain": null, "mtu": null, "routes": [], "ntp_servers": []}
      }
    }
  ],
  "tenants": [{"id": 3, "...": "unchanged", "network_id": 6, "dhcp": {"...": "same block"}}]
}
```

`range` null means no dynamic range. `routes` entries are `{"destination": "192.168.50.0/24",
"gateway": "10.0.0.1"}`. A tenant without `network_id` (terminated, or an older Rackflow) serves no
DHCP and has no `Defer`.

A 0.4.0 or later runner always gets the `networks` key; it is empty while presence is not active in
its location (2.5), so a downgrade elsewhere in the location makes it drop its presences at once.
Tenant `network_id` and `dhcp` are sent only while presence is active.

### 4.5 Runner status additions

```json
{
  "networks": [{"id": 6, "serving": true, "since": "...", "error": null,
                "leases": [{"mac": "...", "ip": "...", "expires_at": "...", "hostname": "nas"}]}],
  "tenants": [{"id": 3, "...": "unchanged", "dhcp": {"error": null, "leases": []}}]
}
```

A node reports a network entry only for presences it serves or has just stopped serving
(`serving: false`, once). Rackflow sets `presence_node_id` and `presence_since` from `serving: true`,
clears them on `serving: false` from that node or when the node goes offline (the storage repair
loop), and replaces the network's dynamic leases with those reported by the node that serves the
network (the presence holder, or the tenant's current node).

### 4.6 Surfaces

Client API (owner only, owner routes; a network that is not yours is "not found"):

- `GET /api/client/private-networks/{id}/dhcp`: settings, effective options, owner reservations,
  the automatic (attachment) reservations, dynamic leases, `served` (a node currently serves the
  network) and `available` (presence active in the location).
- `PATCH /api/client/private-networks/{id}/dhcp`: any of `enabled`, `range_start`, `range_end`
  (both null to remove), `lease_seconds` (null for default), `gateway`, `dns_servers`, `domain`,
  `mtu`, `routes`, `ntp_servers`.
- `POST /api/client/private-networks/{id}/dhcp/reservations` `{mac, ip, label}`;
  `PATCH .../reservations/{reservation_id}`; `DELETE .../reservations/{reservation_id}`.

The GET response (and the PATCH and reservation responses, which return the same object):

```json
{
  "network_id": 6, "available": true, "served": true, "serving_node": "stor-a",
  "enabled": true, "lease_seconds": 86400, "lease_seconds_default": 86400, "lease_seconds_set": null,
  "range_start": "10.0.0.100", "range_end": "10.0.0.199",
  "gateway": null, "dns_servers": [], "domain": null, "mtu": null, "routes": [], "ntp_servers": [],
  "endpoint_ip": "10.0.0.254",
  "reservations": [{"id": 1, "mac": "aa:bb:cc:00:00:01", "ip": "10.0.0.20", "label": "router", "created_at": "..."}],
  "automatic": [{"attachment_id": 9, "ip": "10.0.0.1", "macs": ["bc:24:11:00:00:01"], "label": "vm-1 (service 12)"}],
  "leases": [{"mac": "bc:24:11:00:00:09", "ip": "10.0.0.100", "hostname": "nas", "expires_at": "..."}],
  "warnings": [],
  "error": null
}
```

`available` false means the location has no storage node or not every node is on 0.4.0; settings can
still be saved and apply once it is. `automatic` lists only the owner's own attachments plus, for
members' attachments, the address and `"label": "member attachment"` without MACs.

Members: `GET /shared/{id}` gains `dhcp`: `enabled`, `served`, `lease_seconds`, `gateway`,
`dns_servers`, `domain`, `mtu`, `routes`, `ntp_servers`. No range, reservations or leases; their
own attachments' addresses are already shown.

Staff API: the same `dhcp` routes under `/api/admin/private-networks/{id}/dhcp`, and the network
output gains `presence` (`node_id`, `node_name`, `since`, `dhcp_error`) and `dhcp_warnings`.

Response codes: the reservation `POST` answers 201, `PATCH` and `DELETE` 200, all with the GET
object. A proxy access network answers 409 on every `dhcp` route. The staff `presence` names the
node answering the network: the network presence holder, or the linked storage's current node
(`since` is then the tenant's `current_since`); it is null on proxy access networks. The staff view of
the GET object lists MACs and labels for every attachment, members' included.

Client MCP: `services view="networks"` includes each owned network's `dhcp` and each shared
network's member view; `service_action action="network"` gains sub-actions `dhcp_get`, `dhcp_set`
(params as the PATCH body plus `network_id`), `dhcp_reservation_add`, `dhcp_reservation_update`,
`dhcp_reservation_remove` (`reservation_id`). Scopes: `dhcp_get` read, the others write, remove
destructive. No new tool; `dhcp_get` needs no `confirm`. Staff MCP: `lookup kind="private_networks"`
shows presence and DHCP summary; no new tool.

Audit log: `private_network.dhcp_update` (changed fields), `private_network.dhcp_reservation_add`,
`.dhcp_reservation_update`, `.dhcp_reservation_remove`, with actor, network id and the MAC and IP.

Portal (`ClientNetworks.svelte`): a DHCP panel on each owned network (switch, range, options,
reservations table with add, edit and remove, dynamic leases), with the warning that a gateway
changes the default route of any guest that uses DHCP on that card; the member view shows the
effective settings. Admin (`PrivateNetworks.svelte`): presence node, DHCP state and the same panel.
The text that told clients to set addresses by hand now says when DHCP applies (5).

WHMCS: private networks are not shown in the WHMCS client area; only the storage text that tells
clients how to configure their servers changes.

## 5. What clients are told

- **Virtual machine:** the new network card gets its address by DHCP from the network (storage
  locations only). Windows and most desktop systems pick it up at once; on a Linux server, turn on
  DHCP for the new interface (for example `dhclient <if>` or a netplan or NetworkManager entry).
  Templates with cloud-init also get the address written on their next reboot.
- **Dedicated server:** a VLAN interface on the main network card (or the bond) asks with that
  card's MAC, which Rackflow knows, so DHCP works; an interface with its own MAC needs a reservation
  or a static address.
- **When DHCP does not apply:** the location has no storage node, DHCP is switched off for the
  network, or (rarely) every storage node of the location is down; then the address is set by hand.

**Decision on cloud-init:** `ipconfig<n>` stays static. A static address written by cloud-init
works even when no storage node is reachable, existing VMs need no change, and DHCP would hand out
the same address anyway.

## 6. Upgrade and rollout

1. Deploy the app (migration `private_network_dhcp`). Nothing changes on any node: no location has
   every node on 0.4.0.
2. Upgrade each storage node's runner to 0.4.0 in turn (binary only; `arping` is already a
   dependency). No tenant pauses: Ganesha's configuration and container hash do not change.
3. When the last enabled node of a location reports 0.4.0, presences and DHCP start on the next
   passes. Existing tenants (dev's shared NFS, VLAN 3921) start serving DHCP to their attachments
   at once; production networks without storage get network presences.
4. Rollback: downgrading one node turns presences off for the location; the 0.4.0 nodes then drop
   their network presences. A node downgraded to 0.3.x does not know `rf-p*` namespaces: stop its
   runner and remove them (`ip netns del rf-p<id>`) before starting the older binary.

## 7. Out of scope

Proxy access networks; network presences on `switch` mode nodes; DHCP relays, IPv6 and DHCPv6;
PXE options; a DNS server; raw option numbers; DHCP in locations without storage nodes (networks
there work as today, with addresses set by hand).
