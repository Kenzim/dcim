# VLAN management on the Cisco Nexus switches

How Rackflow configures private network VLANs on the production Nexus switches through the `nxos` plugin, what each switch needs before it can move from `snmpv3` to `nxos`, and the exact NX-OS configuration to paste. Nothing in this document has been applied to any switch; every switch-side change here is for Kenzi to make.

General private network setup (the VLAN pool, the bridge, reconcile) is in `docs/private-networks.md`.

## What Rackflow does

- **Server port, plain:** the VLAN is added tagged with `switchport trunk allowed vlan add`. An access port becomes a trunk whose native VLAN is the old access VLAN, so PXE and reinstalls keep working. The allowed list is written before `switchport mode trunk`, so the port is never briefly a trunk that allows every VLAN.
- **Port-channel on one switch:** configured once on `port-channelN`, however many member ports the server has in it.
- **vPC pair:** when a server's port lands on a vPC port-channel, Rackflow finds the same vPC number on the recorded peer switch and treats both as one uplink, even if only one of the server's cables is recorded (the preview and the attachment then carry a warning about the missing cable). One operation, in this order:
  1. create the VLAN on both peers with the same name (`pn-<network id>`);
  2. allow it on the vPC peer-link of both peers with `allowed vlan add`, only when the peer-link has an explicit allowed list (a peer-link that allows every VLAN is left alone);
  3. add it to the vPC port-channel on both peers.

  If any step fails, every earlier step that changed something is undone in reverse, so the two peers never disagree. Afterwards Rackflow reads `show vpc brief` on both peers and records any consistency problem, or the VLAN not being up on the vPC, on the attachment as a warning.
- **Detaching** removes the VLAN from the port-channels (both peers) in reverse order. The VLAN and the peer-link entry stay while other servers may use them; deleting the network removes the VLAN from the peer-links, then deletes it from every managed switch.
- **Bonds across two switches that are not vPC peers** (active-backup) are configured port by port.

What Rackflow refuses, before sending anything:

- VLAN 1 and the NX-OS reserved range 3968 to 4094 (and the pool rejects them too).
- A VLAN that already exists on a switch under another name: Rackflow never renames or deletes a VLAN it did not create.
- The vPC peer-link and its members, routed ports (where peer-keepalive runs), and anything that is not an `EthernetX/Y` or `port-channelN` interface (so never `mgmt0`).
- A port-channel with a member port whose recorded cable goes to another switch (an inter-switch link) or to another server.
- A trunk that allows every VLAN: it has to be given an explicit list first, so that `remove` can never turn into "remove from all".
- A vPC whose peer is not recorded in Rackflow, is disabled, or is not on a VLAN-capable plugin; a vPC number missing on the peer.
- Bonded server ports that reach both switches of a vPC pair but not one vPC port-channel (orphan or mismatched member ports).

`copy running-config startup-config` is sent at the end of each change only when the switch's "Save Configuration" setting is on (the default).

### Dry run

Staff can see the exact commands for a server without sending them:

- Admin API: `GET /api/admin/network-switches/vlan-dry-run/{server_id}?vlan_id=2001` (or `network_id=...`, add `remove=true` for detaching).
- Staff MCP: `switch_action` with `action="vlan_dry_run"`, `server_id`, and `vlan_id` or `network_id`.

The result lists the targets (switch, interface, member ports, vPC number), warnings (missing cables) and the configuration batches per switch. Read-only `show` commands do go to the switches, so NX-API must already be enabled.

### vPC pairing in Rackflow

Rackflow records which switches are vPC peers on the switch records (`vpc_domain_id`, `vpc_peer_switch_id`, `vpc_peer_link`, `vpc_role`). Detection reads CISCO-VPC-MIB over SNMP, so it works on `snmpv3` switches today and needs nothing on the switches:

- Admin → Switches → a switch → "Detect vPC pairs", or `POST /api/admin/network-switches/vpc/detect`, or staff MCP `switch_action` with `action="detect_vpc"`.
- Two switches are peers when each one's peer-keepalive destination is the other's keepalive source in the same domain. The domain ID alone is not used: all three production pairs use domain 1 and share the vPC system MAC.
- The switch page and the MCP switch rows show the domain, role, peer-link and peer. A switch with a vPC domain but no matching peer in Rackflow gets an error explaining which keepalive address is missing.

Run detection again after adding a switch or changing a vPC domain.

## Production survey (18 November 2026, read-only SNMP)

| Rackflow switch | Model | NX-OS | vPC | NX-API |
|---|---|---|---|---|
| 1 `uplink switch rack2` (VA-M96-N55-SW1) | N9K-C93108TC-EX | 10.3(5) | none; `port-channel1` (Eth1/53-54) to the 25G pair, allows 1-4094 | off (443 and 80 refused) |
| 5 `25G-UPLINK-SW1`, 6 `25G-UPLINK-SW2` | N9K-C93180YC-EX | 10.3(2) | domain 1, peer-link `port-channel45` (Eth1/45-46), allows 1-4094, keepalive 192.168.1.1 and .2 | off |
| 7 `CEPH-SW1`, 8 `CEPH-SW2` | N9K-C9236C | 9.3(12) | domain 1, peer-link `port-channel45` (Eth1/33-34), allows 1-4094, keepalive 192.168.3.1 and .2 | off |
| 3 `CLUSTER-SW1`, 4 `CLUSTER-SW2` | N3K-C3048TP-1GE | 9.3(12) | domain 1, peer-link `port-channel45` (Eth1/46-47), allows 1-4094, keepalive 192.168.2.1 and .2 (VRF management) | off |
| 2 `M96-N56-IPMI-SW1` | N3K-C3048TP-1GE | 9.3(12) | none | off |
| 9 ProCurve 2810-48G | J9022A | N.11.15 | n/a (BMCs only) | n/a |

All Nexus models and versions above support NX-API (`feature nxapi`); the N3K-C3048TP runs 9.3(12), not the old 6.x/7.x, so it is not a problem. All run rapid-PVST+.

vPC host links on the three pairs are single-member port-channels `port-channel101` upwards, one per hypervisor (hv-01 to hv-23), with the same vPC number on both peers but sometimes different member ports (for example hv-19 on the 25G pair: SW1 Eth1/24, SW2 Eth1/19, both vPC 120). Their allowed lists are explicit (25G: `210,291,733,750`, some also `220,230`; Ceph: `230` or the 25G list; cluster: `220`). The non-host vPCs (25G 44, 51, 53, 54; Ceph 36; cluster 54) allow every VLAN, so Rackflow refuses them anyway.

No dedicated (bare-metal) server is cabled to a vPC pair in Rackflow today. Dedicated servers are on switch 1 as single-homed access ports (Eth1/17 is the only trunk). 25G vPCs 122 and 123 have no recorded cable.

## Before a switch moves to `nxos`

| Switch | Needs VLAN management? | Before moving |
|---|---|---|
| 1 uplink switch rack2 | Yes: dedicated servers | NX-API config and account below. The uplink `port-channel1` already allows 1-4094, so pool VLANs reach the 25G pair. |
| 5 and 6, 25G pair | Yes: the dual-homed dedicated server, hypervisors, transit from switch 1 | NX-API on both, both moved together (Rackflow refuses a vPC whose peer is still on `snmpv3`). Record the dedicated server's two cables. Peer-links allow every VLAN, so nothing to pre-allow. |
| 7 and 8, Ceph pair | Only if an NFS storage node in `switch` mode is cabled here | Same as the 25G pair, both together. Otherwise stay on `snmpv3`. |
| 3 and 4, cluster pair | Only if a storage node in `switch` mode is cabled here | Same, both together. Otherwise stay on `snmpv3`. |
| 2 IPMI | No | Stay on `snmpv3`. |
| 9 ProCurve | No | Stay on `snmpv3`. |

Moving a switch keeps everything Rackflow has recorded: ports, port IDs and cable runs are not touched by a plugin change, the SNMP settings carry over (the `nxos` form contains the same SNMP fields), and monitoring, bandwidth graphs and LLDP collection keep running through SNMP. The admin API and staff MCP refuse the change if the resulting settings are incomplete (for example no NX-API login).

### vPC prerequisites

- Both peers must be in Rackflow, enabled, on `nxos`, and paired by vPC detection (the switch page shows "peer ...").
- Peer-link allowed list: all three production peer-links allow 1-4094, so Rackflow leaves them alone. If a peer-link is ever narrowed, Rackflow adds and removes pool VLANs on it with `add`/`remove`; it never replaces the list. Deleting a network removes its VLAN from an explicit peer-link list even if someone had pre-allowed it there, so do not narrow a peer-link to a list that relies on pre-allowed pool VLANs.
- VLANs on both peers: Rackflow creates them, with the same name on both. A pool VLAN that already exists on one peer under another name is refused.
- Hypervisor (VM) private networks are bridged on the hypervisor and do not touch the switches. For a VM and a dedicated server to share a network, the hypervisors' vPC host links must carry the pool range; that is a one-off on both peers (see "One-off for hypervisor links" below), not something Rackflow does per attachment.

How to verify pairing on the switches:

```
show vpc brief
show vpc consistency-parameters global
show vpc consistency-parameters vpc 120
show running-config interface port-channel45
```

`show vpc brief` must show `peer-ok`, `peer-alive`, `consistent`, and each host vPC `up` with consistency `SUCCESS` on both peers.

## Switch-side configuration (for Kenzi to paste)

Replace `<PASSWORD>` with a new strong password (store it in Rackflow's switch settings, nowhere else) and `<POOL>` with the private network pool range, for example `2000-2999`. Run on each switch in configuration mode.

### 1. NX-API over HTTPS only

```
feature nxapi
nxapi https port 443
no nxapi http
nxapi ssl protocols TLSv1.2
```

On NX-OS 10.x (switches 1, 5, 6) `nxapi ssl protocols TLSv1.2 TLSv1.3` is also accepted. If the address Rackflow uses for the switch is on `mgmt0`, add `nxapi use-vrf management`; check with `show ip interface brief vrf management`. Leave `verify_ssl` off in Rackflow unless the switch gets a certificate Rackflow trusts (`nxapi certificate httpscrt certfile bootflash:<crt>`, `nxapi certificate httpskey keyfile bootflash:<key>`, `nxapi certificate enable`). If the management network has an access list, allow TCP 443 from the Rackflow VM (10.16.251.240) only.

### 2. A least-privilege role and account

The role allows only the commands the plugin sends. NX-OS checks rules from the highest number down.

```
role name rackflow-vlan
  description Rackflow private network VLANs
  rule 200 permit command copy running-config startup-config
  rule 190 permit command configure terminal
  rule 185 permit command end
  rule 180 permit command vlan *
  rule 175 permit command no vlan *
  rule 170 permit command name *
  rule 165 permit command interface *
  rule 160 permit command switchport trunk allowed vlan *
  rule 155 permit command switchport trunk native vlan *
  rule 150 permit command switchport mode trunk
  rule 145 permit command spanning-tree port type edge trunk
  rule 120 permit command show running-config interface *
  rule 115 permit command show running-config vlan *
  rule 110 permit command show vlan brief
  rule 105 permit command show vpc brief
  rule 100 permit command show port-channel summary *
  rule 95 permit command show hostname
  vlan policy deny
    permit vlan <POOL>
  interface policy deny
    permit interface <SERVER PORTS>
```

`<SERVER PORTS>` per switch, from the survey (only server-facing ports, never uplinks, the peer-link or `mgmt0`):

| Switch | Interface policy |
|---|---|
| 1 uplink switch rack2 | `permit interface ethernet 1/1-44` (Eth1/45-54, including the `port-channel1` members, stay denied) |
| 5 25G-UPLINK-SW1 | `permit interface ethernet 1/1-25` and the host port-channels `port-channel101-126` |
| 6 25G-UPLINK-SW2 | `permit interface ethernet 1/1-25` and `port-channel101-126` |
| 7, 8 Ceph | `permit interface ethernet 1/1-24` and `port-channel101-125` |
| 3, 4 cluster | `permit interface ethernet 1/1-23` and `port-channel101-123` |

Notes on the role:

- The `vlan policy` limits VLAN creation and trunk changes to the pool. On switch 1, an access port that becomes a trunk gets `switchport trunk native vlan <its access VLAN>`, so add the access VLANs used by dedicated servers there (for example `permit vlan 100,<POOL>`), or leave the VLAN policy out on switch 1.
- On the vPC pairs the peer-link allows every VLAN, so Rackflow never sends commands to `port-channel45` and the interface policy can keep it denied. If a peer-link is ever narrowed, add it to the interface policy.
- If NX-API answers "permission denied" for a `show` command on a particular NX-OS release, replace rules 95 to 120 with `rule 90 permit read` (all `show` commands; this also lets the account read the running configuration).
- Not verified on these releases: whether the interface policy accepts port-channel ranges. If the switch rejects `permit interface port-channel101-126`, or the account cannot change a host port-channel, permit the port-channels one by one or drop the interface policy and rely on Rackflow's own checks (it only touches interfaces reached from a server's recorded cables).
- Check how the switch stored it with `show role name rackflow-vlan`, and test the account (step 3) with a dry run, then one real attachment, before relying on it.

Then the account:

```
username rackflow password 0 <PASSWORD> role rackflow-vlan
```

If the switches authenticate through TACACS+ or RADIUS, create the account and role there instead and map it to `rackflow-vlan`.

### 3. Verify

From the Rackflow VM (read-only):

```
curl -sk -u rackflow https://<switch>/ins -H 'content-type: application/json-rpc' \
  -d '[{"jsonrpc":"2.0","method":"cli","params":{"cmd":"show vpc brief","version":1},"id":1}]'
```

On the switch: `show nxapi` (HTTPS enabled, port 443, HTTP disabled), `show user-account rackflow`, `show role name rackflow-vlan`.

### 4. Move the switch in Rackflow

1. Admin → Switches → edit the switch → Plugin `nxos`. The hostname and SNMP settings stay filled in; add the NX-API username and password and set "Save Configuration" as wanted.
2. "Test connection": it checks NX-API (`show hostname`) and SNMP. Save.
3. For a vPC pair, move both switches, then click "Detect vPC pairs" and check both show each other as peer.
4. Before attaching anything, run the dry run for a server on that switch (see "Dry run" above) and read the commands.

### One-off for hypervisor links (only if VMs and dedicated servers share networks)

On both peers of the 25G pair, for each hypervisor vPC that should carry private networks (here all of `port-channel101-126`):

```
interface port-channel101-126
  switchport trunk allowed vlan add <POOL>
```

This is outside Rackflow's per-attachment changes and is owned by whoever runs the hypervisor trunking (the production deployment is handling manual trunking at the moment).

## Testing

- `tests/nxapi_fake.py` is a stateful NX-API stand-in (VLANs, interfaces, port-channels, vPC domain, `show` output in NX-OS 9.3/10.x shapes, error replies). `tests/plugins/test_nxos.py` covers the plugin; `tests/services/test_uplink_vlan_service.py` covers vPC pairs end to end (both peers, peer-link explicit and "all", several members configured once, one leg cabled, peer missing or not VLAN-capable, rollback when the second peer fails, removal order, dry run, orphan ports, inter-switch link refusal).
- Cisco's always-on NX-OS sandbox (`sbx-nxos-mgmt.cisco.com`) answered with HTTP 502 on 18 November 2026, so no live write test has been run against NX-OS yet. The first real write should be a dry run, then one attachment on a test server.
