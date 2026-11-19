# Proxy fast path

A client's servers can reach Rackflow proxies over a private network in the same location instead of over the public internet. This saves the public routing hops (about 0.05 to 0.1 ms per new connection) and keeps proxy traffic off the uplinks. The proxy itself is unchanged: the same credentials work, and traffic leaves from the same public IP.

## Summary

- Every client's **proxy access network** uses the same fixed range, `100.80.0.0/16`.
- Each **proxy subnet** (an IPAM subnet that holds proxy IPs) that is pinned to a runner gets **one private IP** in that range. The IP belongs to the subnet, not to a runner: if the subnet moves to another runner, the IP moves with it.
- A client connects to `<subnet private IP>:<proxy port>` with the credentials of any of their proxy IPs in that subnet. The **credentials pick the outgoing IP**.
- On the runner, each client network lives in its own **network namespace** with its own listener, so clients with identical addresses never meet.

## Address plan

The range is the setting `PROXY_ACCESS_CIDR` (default `100.80.0.0/16`), part of `100.64.0.0/10` (shared address space). It is the same for every client and every location and must not change once networks exist.

| Block | Use |
|---|---|
| `100.80.0.0/20` (`100.80.0.1` to `100.80.15.254`) | Proxy subnet private IPs, 4,094 of them. Never given to a client server. |
| `100.80.16.1` to `100.80.255.254` | Client servers, handed out per network in order (first free). |

Normal client private networks already refuse `100.64.0.0/10` because it is not a private range; the access range is also added to the always-refused list so this stays true if that rule changes.

A subnet's private IP is allocated the first time the subnet is pinned (lowest free address in the `/20`) and stored on the subnet (`ip_subnets.fast_path_ip`, unique). Unpinning keeps it, so pinning again or moving the subnet never changes what clients have configured. An allocated IP is only freed when the subnet is deleted.

Client servers use the prefix `/16`, so every proxy subnet IP is on-link and no gateway or route is needed.

## Data model (all additive)

| Table | New columns |
|---|---|
| `proxy_runners` | `location_id` (nullable), `server_id` (nullable, the dedicated server the runner runs on), `vlan_mode` (`preconfigured` or `switch`, default `preconfigured`) |
| `ip_subnets` | `proxy_runner_id` (nullable, the runner that hosts the subnet), `fast_path_ip` (nullable, unique) |
| `private_network_pools` | `proxy_access_enabled` (default false) |
| `private_networks` | `kind` (`standard` or `proxy_access`, default `standard`) |
| `private_network_attachments` | `proxy_runner_id` (nullable): the attachment is a runner, not a client service |

A runner without a location and a subnet without a runner behave exactly as today. In particular the public part of the runner config (`assignments`) is unchanged: every enabled runner still receives every published assignment, so floating IPs and anycast keep working. Pinning only decides where the subnet's **private** IP lives.

A subnet may only be pinned to a runner that has a location. The fast path for that subnet exists in the runner's location.

## The proxy access network

A `PrivateNetwork` of kind `proxy_access`, at most one per client per location:

- **Turning it on:** the client turns it on per location in the portal (`/client/networks`), the WHMCS client area of a proxy service, or the client MCP. The location's pool must have `proxy_access_enabled`. It takes a VLAN from the pool like any private network but does not count towards the client's network limit. Its range is always `PROXY_ACCESS_CIDR`.
- **Who may turn it on:** it is not sold. A client may turn it on in a location when they own an active server (VM or dedicated) in that location and an active proxy with at least one IP whose subnet is pinned to a runner there. Otherwise the request is refused with what is missing, and the overview reports `eligible: false` with the same `reason`. Staff can turn it on for any client. (An earlier pool switch, `proxy_access_requires_product`, and the catalogue flag `proxy_fast_path` were removed by migration `drop_proxy_access_requires_product`.)
- **Runners join it:** every runner whose location is the network's location gets an attachment row (`proxy_runner_id` set, no service, no IP). In `switch` mode the VLAN is trunked on the runner server's uplink through the switch plugins, like a storage node. In `preconfigured` mode Rackflow makes no switch call, and the operator carries the pool's whole VLAN range to the runner once (for example a Proxmox NIC with `trunks=`, or a static trunk). The repair loop adds and removes runner attachments when runners or networks change.
- **Client servers:** join with the normal attach call. VMs get a tagged NIC, dedicated servers get the VLAN on their switch ports. Addresses come from the client block.
- **Turning it off:** refused while client servers are attached (detach them first, as with any network). Runner attachments are removed by Rackflow, then the VLAN is deleted.
- **Showing it:** the client sees, for each of their proxy IPs whose subnet is pinned, the fast-path address `private_ip:port` and whether it is usable (access network on in the runner's location). The port is the location's proxy port, the same as the public one.
- Storage can never be linked to an access network, and the network has no storage endpoint address.

## Runner config contract

`GET /api/runner/proxy/config` gains one key, `fast_path`, present only when the runner has a location whose pool has proxy access enabled:

```json
{
  "version": "a1b2c3d4e5f60718",
  "runner_id": 3,
  "assignments": [ { "service_id": 9, "bind_ip": "203.0.113.10", "username": "px1", "password": "..." } ],
  "fast_path": {
    "cidr": "100.80.0.0/16",
    "port": 11080,
    "tenants": [
      {
        "network_id": 41,
        "vlan": 2017,
        "addresses": ["100.80.0.1"],
        "assignments": [
          { "service_id": 9, "private_ip": "100.80.0.1", "bind_ip": "203.0.113.10", "username": "px1", "password": "..." }
        ]
      }
    ]
  }
}
```

- One tenant per active access network in the runner's location. Every runner in the location gets every tenant (it joins every network), so moving a subnet needs no VLAN change.
- A tenant's `addresses` are the private IPs of the subnets **this runner hosts** in which **that client** has at least one published assignment. Its `assignments` are only that client's assignments in those subnets (active service, enabled subnet, same rules as the public list). One client's tenant never contains another client's assignment.
- `version` is unchanged for a runner without `fast_path`; otherwise the fast-path content is folded into the hash.

## Runner networking: a namespace per client

Each tenant gets:

- a network namespace `pf-n<network id>`;
- a VLAN interface `pf-v<network id>` on `FASTPATH_PARENT_IFACE` with the tenant's VLAN ID, moved into the namespace, holding the tenant's `addresses` as `/16`;
- an nftables table `inet rf-proxy-fast` **inside the namespace**: input accepts only TCP to the proxy port (and established traffic of those connections), forward and everything else is dropped;
- one listener on `0.0.0.0:<port>` created inside the namespace by the runner process.

The runner accepts in the namespace and dials the destination from the host namespace, bound to the assignment's public IP, through the same dial path as the public proxy (destination policy, DNS cache).

### Why namespaces and not VRFs

Both keep identical addresses apart and both are kernel paths with no extra copy, so speed is the same. Namespaces were chosen because:

- **No host-wide change.** The VRF design needs `net.ipv4.tcp_l3mdev_accept=1`, which makes every listener on the host in the default VRF (sshd, the health port, anything else) reachable from every client VLAN, guarded only by firewall rules. In a namespace, the only socket a client can ever reach is the runner's listener there; the firewall is a second layer, not the only one.
- **No forwarding path exists.** A namespace has the VLAN interface and loopback, nothing else: no veth, no route to the host or the internet. Forwarding between clients or to the host is impossible, not just filtered.
- **Tenant identity is the listener.** With one listener per tenant the runner always knows which client a connection came from. With VRFs it would have to read the accepted socket's bound device, and the same private IP exists in every VRF.
- **Proven here.** The storage runner already runs one namespace per tenant with the same naming and cleanup rules, so operators know how to inspect it (`ip netns list`, `ip -n pf-n<id> addr`).

The cost is one listening socket and goroutine per client per runner, and the runner needs `CAP_NET_ADMIN` and `CAP_SYS_ADMIN` (to enter namespaces) when the fast path is on.

### Ownership and backwards compatibility

- The fast path is **off unless `FASTPATH_PARENT_IFACE` is set**. Without it the runner ignores `fast_path`, creates nothing and removes nothing, and runs exactly as today.
- The runner only creates, changes or removes objects it names: namespaces matching `^pf-n[0-9]+$`, interfaces `pf-v<id>`, and the `rf-proxy-fast` table inside its own namespaces. A namespace whose tenant is no longer in the config is removed (listener closed, VLAN interface deleted, namespace deleted).
- The public listener, the public credential store and the `X-Bind-IP` rule are untouched for public connections.

### Credentials and egress

- **Public path (unchanged):** credentials are checked against (the IP the client connected to, username); egress uses that IP. An `X-Bind-IP` header replaces that IP for both the check and the egress, so the header can only select an IP the credentials own.
- **Fast path:** credentials are checked against the tenant's own table, keyed by (private IP the client connected to, username), with a constant-time password compare. Egress uses the matching assignment's public IP. If the same username and password own several IPs in that subnet, `X-Bind-IP` chooses among them and anything else is refused (`403`); without the header the lowest IP is used. Fast-path credentials are never added to the public store, so a public connection cannot use a private IP, and a tenant's listener never sees another tenant's credentials.
- **Destinations:** shared address space (`100.64.0.0/10`, which holds the access range) is refused as a proxy destination on both paths, alongside the private and loopback ranges already refused, so no client can proxy into the access range or the runner host's own carrier-grade NAT or overlay addresses.

## Interfaces

| Where | What |
|---|---|
| Admin UI, Proxy Runners | Location, server and VLAN mode per runner; the subnets each runner hosts with their private IPs |
| Admin UI, Proxy IPAM | Pin a subnet to a runner (or unpin); shows the private IP |
| Admin UI, Private Networks | Pool switches for proxy access and "requires product"; access networks are labelled and show runner attachments |
| Client portal, `/client/networks` | Turn the fast path on or off per location, see the fast-path address of each proxy IP, attach servers as usual |
| WHMCS proxy client area | "Private proxy access" panel: fast-path address per IP, and on or off for the service's locations (`fastpath_action.php`, `rackflow_proxyFastPath*` functions) |
| Billing API | `GET` and `POST /api/billing/services/{id}/proxy/fast-path` (`{"enabled": true}`), gated by the `proxy.view_credentials` client permission; the status payload's `proxy_assignments[].fast_path` carries each IP's private address |
| Staff MCP | `proxy_runner` (update takes `location_id`, `server_id`, `vlan_mode` or `clear_placement`; new `fast_path` view with tenants and addresses, never credentials), `save_subnet` (`proxy_runner_id`, `clear_proxy_runner`) |
| Client MCP | `services` view `fast_path` (optionally narrowed by `service_id`); `service_action` actions `fast_path_on` and `fast_path_off` on a proxy service, applied to the locations of its IPs |

## Turning it on in a location

1. **Runner host:** a NIC (or bond) that can carry the pool's VLANs, named in `FASTPATH_PARENT_IFACE`. Run the runner with host networking, as root with `CAP_NET_ADMIN` and `CAP_SYS_ADMIN` (or `privileged`), `/run/netns` shared with the host (`-v /run/netns:/run/netns:rshared`), and the image target that includes `ip` and `nft`.
2. **Switches or hypervisor:** in `preconfigured` mode carry the pool's VLAN range to that NIC once. In `switch` mode set the runner's server; its PXE port (and bond) must be cabled to a switch with the `nxos` or `qnap_qss` plugin, as for private networks.
3. **Rackflow:** set the runner's location (and server and mode), pin each proxy subnet to the runner that hosts it, and turn on proxy access on the location's pool.
4. **Client firewalls:** a client whose server runs Tailscale with its default firewall mode drops traffic from `100.64.0.0/10` on other interfaces; they need to allow `100.80.0.0/16` on the access network interface.

## Known limits

- IPv4 only.
- One access network per client per location; all of a client's servers in a location share it.
- A subnet's fast path is only reachable in its runner's location.
- If a pinned runner is down, its subnets' fast-path IPs are down; there is no automatic failover. Moving the pin moves the IP (within one sync interval, 30 s by default).
