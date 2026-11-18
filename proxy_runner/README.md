# Rackflow proxy runner

A Go forwarder for customers' HTTP proxy and SOCKS5 services. It has no dependencies outside the Go standard library.

- **One public listener** (`PROXY_LISTEN`). A first byte of `0x05` means SOCKS5; anything else is an HTTP proxy request (`CONNECT` or an absolute URI).
- **Config sync.** It pulls assignments from Rackflow (`GET /api/runner/proxy/config`) with `RUNNER_API_KEY`. Each poll also counts as the runner's phone-home.
- **Credentials.** Checked by `(bind IP, username, password)`. The bind IP is the local address of the accepted connection, so the proxy IPs must be on the host and the runner must use host networking.
- **`X-Bind-IP` (HTTP only)** replaces the bind IP for both the credential check and the egress. It can therefore only select an IP those credentials own; anything else gets `403`.
- **Egress** dials out from the bind IP. Loopback, link-local, multicast, private (RFC 1918, ULA) and shared address space (`100.64.0.0/10`) destinations are refused unless `PROXY_ALLOW_PRIVATE_DESTINATIONS=true`.
- **Health:** `GET /health` on `HEALTH_LISTEN`.
- **Proxy fast path** (optional): clients reach the runner over a private network in the same location. See below and `docs/proxy-fast-path.md`.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `RACKFLOW_BASE_URL` | (required) | App base URL, no trailing slash. Must be `https://` unless it is loopback |
| `RUNNER_API_KEY` | (required) | Bearer key from Admin, Proxy Runners (shown once) |
| `SYNC_INTERVAL_SECONDS` | `30` | Config poll and phone-home interval |
| `PROXY_LISTEN` | `:11080` | Public proxy listen address |
| `HEALTH_LISTEN` | `:8081` | Health HTTP listen address |
| `DIAL_TIMEOUT` | `15s` | Upstream dial timeout |
| `IDLE_TIMEOUT` | `5m` | Idle splice timeout |
| `PROXY_DNS_SERVER` | (system resolver) | DNS server for outbound lookups, e.g. `1.1.1.1` or `1.1.1.1:53` |
| `PROXY_DNS_CACHE_TTL` | `60s` | How long a resolved hostname is reused; `0` disables the cache |
| `PROXY_DNS_NEGATIVE_TTL` | `5s` | How long a failed lookup is remembered |
| `PROXY_DNS_CACHE_SIZE` | `10000` | Maximum cached hostnames |
| `PROXY_ALLOW_PRIVATE_DESTINATIONS` | `false` | Allow private and loopback destinations (tests only) |
| `FASTPATH_PARENT_IFACE` | (unset) | Host interface carrying the client VLANs of the proxy fast path. Unset turns the fast path off |

Outbound lookups follow the bind IP's family: an IPv4 proxy IP only queries A records.

**Port.** Customers are told the location's proxy port (11080 unless the location sets another). The binary defaults to `:11080`, but the compose files in this repository default `PROXY_LISTEN` to `:8080`, so set `PROXY_LISTEN=:11080` (or the location's port) when deploying with them.

## Deploy

Use `network_mode: host` so the customer bind IPs on the host are the local address of each connection.

1. In Rackflow Admin, **Proxy Runners**, create a runner and copy the generated API key (shown once).
2. Set `RACKFLOW_BASE_URL` and `RUNNER_API_KEY` on the runner.
3. The runner phones home on every poll; Rackflow shows it Online when `last_seen` is within 90 seconds.

The public part of the config holds every active proxy assignment, whatever the runner's location, and the runner only serves the IPs present on its host.

## Proxy fast path

A client's servers reach the proxy over a private VLAN instead of the internet. Every client's access network uses the same range (`100.80.0.0/16` by default). Each proxy subnet pinned to this runner has one private IP in it, and the credentials of the client's proxy IPs in that subnet decide which public IP the connection leaves from.

Turn it on by setting `FASTPATH_PARENT_IFACE`. For each client access network in the config (`fast_path.tenants`) the runner then keeps:

| Object | Name | Contents |
|---|---|---|
| Network namespace | `pf-n<network id>` | Only loopback and the VLAN interface; no route to the host or the internet |
| VLAN interface | `pf-v<network id>` | On `FASTPATH_PARENT_IFACE` with the client's VLAN ID, moved into the namespace, holding the subnet private IPs this runner hosts for that client |
| nftables table | `inet rf-proxy-fast`, inside the namespace | Input accepts only new TCP to the proxy port, loopback, and replies; forwarding is dropped |
| Listener | `0.0.0.0:<port>` inside the namespace | Serves only that client's credentials; egress is dialled from the host namespace |

The runner only creates, changes or removes namespaces matching `pf-n<number>`, the matching `pf-v<number>` interfaces and its own table inside them. A namespace whose network is no longer in the config is removed (listener closed, VLAN interface deleted, namespace deleted). Namespaces are kept across restarts and reused. A tenant that fails is retried on every poll and reported in `/health` under `fast_path.errors`; other tenants carry on.

Fast-path credentials never enter the public credential store: a public connection cannot use a private IP, and a client's listener never sees another client's credentials. `X-Bind-IP` on the fast path chooses among the public IPs those credentials own in that subnet; without it the lowest is used.

**Requirements on the runner host:**

- Host networking, and root with `CAP_NET_ADMIN` and `CAP_SYS_ADMIN` (or `privileged: true`).
- `/run/netns` shared with the host: `-v /run/netns:/run/netns:rshared`.
- The `fastpath` image target, which includes `ip` and `nft` (`docker build --target fastpath`). The default target stays the distroless non-root image.
- The parent interface must carry the location pool's VLANs: either preconfigured once (runner VLAN mode `preconfigured`) or trunked by Rackflow on the runner server's switch ports (mode `switch`).

**Inspecting:**

```sh
ip netns list                                   # pf-n<id> per client network
ip -n pf-n41 addr                               # VLAN interface and private IPs
ip netns exec pf-n41 nft list ruleset           # the rf-proxy-fast table
ip netns exec pf-n41 ss -ltn                    # the proxy listener
curl -s localhost:8081/health                   # fast_path: enabled, tenants, errors
```

## Tests

```sh
go vet ./... && go test -race ./...
```

The fast-path manager is tested against a fake `ip`/`nft`. The kernel-level check is behind the `sandbox` build tag and must run inside a throwaway network and mount namespace, never directly on a host (it refuses to run where the namespace has interfaces other than `lo`):

```sh
go test -c -race -tags sandbox -o /tmp/pf-sandbox.test ./internal/fastpath/
unshare -n -m --propagation private sh -c 'mount -t tmpfs none /run && PF_SANDBOX=1 /tmp/pf-sandbox.test -test.run Sandbox -test.v'
```

It builds a VLAN-filtering bridge as the switch, two client namespaces that both use `100.80.16.1` on VLANs 2001 and 2002, and two tenants that both hold `100.80.0.1`. It checks that each client reaches its own tenant and egresses from its own IP, that each client's credentials are refused in the other tenant, that one client cannot reach the other, that another port in the tenant namespace does not answer, and that removal deletes the namespaces. It needs root, `ip`, `bridge` and `nft`.
